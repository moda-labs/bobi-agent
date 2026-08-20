"""Tests for the sub-agent executor module — unit tests only.

For blocking execution and SDK interaction tests, see test_subagent_blocking.py.
"""

import json
import logging
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bobi import paths
from bobi.sdk import SessionEntry
from bobi.subagent import (
    AgentResult,
    _build_prompt,
    _resolve_project_name,
    cancel_agent,
    find_agent,
    list_agents,
)


@pytest.fixture
def tmp_cwd():
    d = tempfile.mkdtemp(prefix="subagent_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _write_agent_yaml(root: Path, body: str = "agent: t\nentry_point: x\n") -> None:
    paths.package_dir(root).mkdir(parents=True, exist_ok=True)
    paths.agent_yaml_path(root).write_text(body)


class TestBuildPrompt:
    @pytest.fixture(autouse=True)
    def bound_root(self, tmp_path, monkeypatch):
        """_build_prompt reads handoffs via the session registry, which
        requires a bound root — don't depend on one leaking from tests
        that ran earlier."""
        paths.bind_root(tmp_path)

    def test_includes_phase_and_issue(self):
        prompt = _build_prompt("pickup", "AGD-12")
        assert "pickup" in prompt
        assert "AGD-12" in prompt

    def test_includes_context(self):
        prompt = _build_prompt("implement", "AGD-12", context="Build the auth flow")
        assert "Build the auth flow" in prompt

    def test_includes_handoff_instruction(self):
        prompt = _build_prompt("spec", "AGD-12")
        assert "handoff" in prompt.lower()

    def test_nonexistent_phase_still_works(self):
        prompt = _build_prompt("nonexistent", "AGD-12")
        assert "AGD-12" in prompt



class TestResolveProjectName:
    def test_uses_dirname(self, tmp_path):
        assert _resolve_project_name(str(tmp_path)) == tmp_path.name


def _mock_registry(entries):
    registry = MagicMock()
    by_name = {e.name: e for e in entries}
    registry.get = MagicMock(side_effect=lambda name: by_name.get(name))
    registry.list_all = MagicMock(return_value=entries)
    registry.list_active = MagicMock(
        return_value=[e for e in entries
                      if e.status in ("starting", "running", "idle")])
    return registry


class TestAgentLifecycle:
    def test_cancel_no_agent(self):
        with patch("bobi.subagent.get_registry",
                   return_value=_mock_registry([])):
            assert not cancel_agent("AGD-99")

    def test_find_agent_none(self):
        with patch("bobi.subagent.get_registry",
                   return_value=_mock_registry([])):
            assert find_agent("AGD-99") is None

    def test_list_agents_empty(self):
        with patch("bobi.subagent.get_registry",
                   return_value=_mock_registry([])):
            assert list_agents() == []

    def test_find_agent_by_run_key(self):
        entry = SessionEntry(name="agent-agd-12-implement", run_key="AGD-12",
                             phase="implement", status="running", pid=0)
        with patch("bobi.subagent.get_registry",
                   return_value=_mock_registry([entry])):
            found = find_agent("AGD-12")
            assert found is not None
            assert found.name == "agent-agd-12-implement"

    def test_find_agent_by_session_name(self):
        entry = SessionEntry(name="agent-agd-12-implement", run_key="AGD-12",
                             phase="implement", status="running", pid=0)
        with patch("bobi.subagent.get_registry",
                   return_value=_mock_registry([entry])):
            assert find_agent("agent-agd-12-implement") is entry

    def test_find_agent_prefers_active(self):
        done = SessionEntry(name="agent-agd-12-spec", run_key="AGD-12",
                            phase="spec", status="done", pid=0)
        active = SessionEntry(name="agent-agd-12-implement", run_key="AGD-12",
                              phase="implement", status="running", pid=0)
        with patch("bobi.subagent.get_registry",
                   return_value=_mock_registry([done, active])):
            assert find_agent("AGD-12") is active

    def test_list_agents_excludes_managers(self):
        mgr = SessionEntry(name="moda-director-x", role="manager",
                           status="running", pid=0)
        eng = SessionEntry(name="agent-1-implement", run_key="1",
                           phase="implement", status="running", pid=0)
        with patch("bobi.subagent.get_registry",
                   return_value=_mock_registry([mgr, eng])):
            names = [a["name"] for a in list_agents()]
            assert names == ["agent-1-implement"]

    def test_cancel_running_agent_updates_registry(self):
        entry = SessionEntry(name="agent-agd-12-implement", run_key="AGD-12",
                             phase="implement", status="running", pid=0)
        registry = _mock_registry([entry])
        with patch("bobi.subagent.get_registry", return_value=registry):
            assert cancel_agent("AGD-12")
        registry.update.assert_called_once_with(
            "agent-agd-12-implement", status="cancelled", pid=0)

    def test_cancel_done_agent_returns_false(self):
        entry = SessionEntry(name="agent-agd-12-implement", run_key="AGD-12",
                             phase="implement", status="done", pid=0)
        with patch("bobi.subagent.get_registry",
                   return_value=_mock_registry([entry])):
            assert not cancel_agent("AGD-12")


class TestLaunchDetached:
    """Test the shared _launch_detached helper."""

    @patch("bobi.subagent.sp.Popen")
    def test_uses_start_new_session(self, mock_popen):
        from bobi.subagent import _launch_detached
        _launch_detached("print('hi')", [], Path("/tmp/test.log"))
        _, kwargs = mock_popen.call_args
        assert kwargs.get("start_new_session") is True

    @patch("bobi.subagent.sp.Popen")
    def test_creates_log_dir(self, mock_popen, tmp_path):
        from bobi.subagent import _launch_detached
        log_file = tmp_path / "nested" / "dir" / "test.log"
        _launch_detached("print('hi')", [], log_file)
        assert log_file.parent.exists()

    @patch("bobi.subagent.sp.Popen")
    def test_passes_args(self, mock_popen):
        from bobi.subagent import _launch_detached
        _launch_detached("import sys; print(sys.argv)", ["a", "b"], Path("/tmp/t.log"))
        cmd = mock_popen.call_args[0][0]
        assert cmd[-2:] == ["a", "b"]


class TestCheckRequires:
    """Test the dispatch-time requires check with TTL cache."""

    def test_returns_pass_for_healthy_deps(self, tmp_path):
        from bobi.subagent import check_requires, _requires_cache
        _requires_cache.clear()
        _write_agent_yaml(
            tmp_path,
            "entry_point: x\nrequires:\n  - name: ok\n    check: 'true'\n",
        )
        results = check_requires(tmp_path)
        assert len(results) == 1
        assert results[0][1] is True

    def test_returns_fail_for_broken_deps(self, tmp_path):
        from bobi.subagent import check_requires, _requires_cache
        _requires_cache.clear()
        _write_agent_yaml(
            tmp_path,
            "entry_point: x\nrequires:\n  - name: bad\n    check: 'false'\n",
        )
        results = check_requires(tmp_path)
        assert len(results) == 1
        assert results[0][1] is False

    def test_cache_hit_within_ttl(self, tmp_path):
        from bobi.subagent import check_requires, _requires_cache
        _requires_cache.clear()
        _write_agent_yaml(
            tmp_path,
            "entry_point: x\nrequires:\n  - name: ok\n    check: 'true'\n",
        )
        # First call populates cache
        check_requires(tmp_path)
        # Overwrite config to make check fail — cache should still return pass
        paths.agent_yaml_path(tmp_path).write_text(
            "entry_point: x\nrequires:\n  - name: ok\n    check: 'false'\n")
        results = check_requires(tmp_path)
        assert results[0][1] is True  # cached pass, not fresh fail

    def test_cache_expired(self, tmp_path):
        import time as _time
        from bobi.subagent import check_requires, _requires_cache
        _requires_cache.clear()
        _write_agent_yaml(
            tmp_path,
            "entry_point: x\nrequires:\n  - name: ok\n    check: 'true'\n",
        )
        check_requires(tmp_path)
        # Manually expire the cache entry
        key = str(tmp_path)
        ts, cached = _requires_cache[key]
        _requires_cache[key] = (ts - 300, cached)  # 5 min ago
        # Now change config
        paths.agent_yaml_path(tmp_path).write_text(
            "entry_point: x\nrequires:\n  - name: bad\n    check: 'false'\n")
        results = check_requires(tmp_path)
        assert results[0][1] is False  # re-ran, got fresh fail

    def test_empty_requires(self, tmp_path):
        from bobi.subagent import check_requires, _requires_cache
        _requires_cache.clear()
        _write_agent_yaml(tmp_path, "entry_point: x\n")
        results = check_requires(tmp_path)
        assert results == []

    def test_no_config(self, tmp_path):
        from bobi.subagent import check_requires, _requires_cache
        _requires_cache.clear()
        results = check_requires(tmp_path)
        assert results == []


class TestAlertRequiresFailure:
    """Test the Slack alerting helper for failed requires checks."""

    @patch("bobi.slack.post_slack_message")
    def test_posts_to_slack(self, mock_post, tmp_path):
        from bobi.config import RequiresEntry
        from bobi.subagent import _alert_requires_failure
        _write_agent_yaml(
            tmp_path,
            "entry_point: x\nservices:\n  - name: slack\n    channels: [C123]\n"
            "    credentials:\n      bot_token: xoxb-test\n",
        )
        failures = [(RequiresEntry(name="gstack", check="false",
                                   why="skills needed", fix="run setup"), "check failed")]
        _alert_requires_failure(tmp_path, failures)
        mock_post.assert_called_once()
        args = mock_post.call_args
        assert "gstack" in args[0][2] or "gstack" in str(args)

    @patch("bobi.slack.post_slack_message", side_effect=Exception("network error"))
    def test_slack_failure_does_not_crash(self, mock_post, tmp_path):
        from bobi.config import RequiresEntry
        from bobi.subagent import _alert_requires_failure
        _write_agent_yaml(
            tmp_path,
            "entry_point: x\nservices:\n  - name: slack\n    channels: [C123]\n"
            "    credentials:\n      bot_token: xoxb-test\n",
        )
        failures = [(RequiresEntry(name="gstack", check="false",
                                   why="skills", fix="setup"), "failed")]
        # Should not raise
        _alert_requires_failure(tmp_path, failures)

    def test_no_slack_service_does_not_crash(self, tmp_path):
        from bobi.config import RequiresEntry
        from bobi.subagent import _alert_requires_failure
        _write_agent_yaml(tmp_path, "entry_point: x\n")
        failures = [(RequiresEntry(name="gstack", check="false",
                                   why="skills", fix="setup"), "failed")]
        # Should not raise
        _alert_requires_failure(tmp_path, failures)

    @patch("bobi.slack.post_slack_message")
    def test_alert_carries_detail_alongside_why(self, mock_post, tmp_path):
        """`why:` is standing context, not a substitute for what went wrong.

        Rendering `why or detail` hid the real failure from every team that
        bothered to document its dependency (#771).
        """
        from bobi.config import RequiresEntry
        from bobi.subagent import _alert_requires_failure
        _write_agent_yaml(
            tmp_path,
            "entry_point: x\nservices:\n  - name: slack\n    channels: [C123]\n"
            "    credentials:\n      bot_token: xoxb-test\n",
        )
        failures = [(RequiresEntry(name="ga4-report", check="ga4 --version",
                                   why="GA4 reporting needs the CLI",
                                   fix="pipx install ga4"),
                     "check timed out (10s)")]
        _alert_requires_failure(tmp_path, failures)
        body = mock_post.call_args[0][2]
        assert "ga4-report" in body
        assert "check timed out (10s)" in body
        assert "GA4 reporting needs the CLI" in body
        assert "pipx install ga4" in body


class TestRequiresFailureAttribution:
    """A blocked dispatch has to say WHICH check failed and WHY (#771).

    `run_requires_checks` already distinguishes a timeout from a missing
    command from the check's own stderr; that detail must reach the operator
    through the raised error and the manager log, whether or not Slack
    alerting is configured.
    """

    @pytest.fixture(autouse=True)
    def bound_root(self, tmp_path):
        # No `services:` here, so the Slack alert cannot fire from this root,
        # which is exactly the configuration the detail has to survive.
        _write_agent_yaml(tmp_path)
        paths.bind_root(tmp_path)
        yield
        paths.bind_root(None)

    @staticmethod
    def _failures(*specs):
        """Build check_requires() output: (entry, passed, detail) tuples."""
        from bobi.config import RequiresEntry
        return [
            (RequiresEntry(name=name, check="ga4 --version", why=why, fix=fix),
             False, detail)
            for name, detail, why, fix in specs
        ]

    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_raise_carries_per_entry_detail(self, mock_launch, mock_reg, tmp_path):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        failures = self._failures(
            ("ga4-report", "check timed out (10s)", "", ""))
        with patch("bobi.subagent.check_requires", return_value=failures):
            with pytest.raises(RuntimeError) as exc:
                launch_agent(task="Fix #1", cwd=str(tmp_path),
                             workflow_name="adhoc")
        assert "ga4-report: check timed out (10s)" in str(exc.value)
        mock_launch.assert_not_called()

    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_raise_names_every_failing_check(self, mock_launch, mock_reg,
                                             tmp_path):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        failures = self._failures(
            ("ga4-report", "check timed out (10s)", "", ""),
            ("gstack", "check command failed: [Errno 2] no browse", "", ""),
        )
        with patch("bobi.subagent.check_requires", return_value=failures):
            with pytest.raises(RuntimeError) as exc:
                launch_agent(task="Fix #1", cwd=str(tmp_path),
                             workflow_name="adhoc")
        msg = str(exc.value)
        assert "ga4-report: check timed out (10s)" in msg
        assert "gstack: check command failed: [Errno 2] no browse" in msg

    @patch("bobi.slack.post_slack_message")
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_error_log_carries_detail_when_slack_cannot_alert(
            self, mock_launch, mock_reg, mock_post, tmp_path, caplog):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        failures = self._failures(
            ("ga4-report", "check timed out (10s)",
             "GA4 reporting needs the CLI", "pipx install ga4"))
        with caplog.at_level(logging.ERROR, logger="bobi.subagent"):
            with patch("bobi.subagent.check_requires", return_value=failures):
                with pytest.raises(RuntimeError):
                    launch_agent(task="Fix #1", cwd=str(tmp_path),
                                 workflow_name="adhoc")
        errors = [r.getMessage() for r in caplog.records
                  if r.levelno >= logging.ERROR]
        assert any(
            "ga4-report" in m and "check timed out (10s)" in m
            and "pipx install ga4" in m
            for m in errors
        ), errors
        # The alert path no-ops safely with no `channels:`: no post, no raise.
        mock_post.assert_not_called()

    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_raise_truncates_a_runaway_detail(self, mock_launch, mock_reg,
                                              tmp_path, caplog):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        long_detail = ("stderr line that will not stop " * 40).strip()
        failures = self._failures(("ga4-report", long_detail, "", ""))
        with caplog.at_level(logging.ERROR, logger="bobi.subagent"):
            with patch("bobi.subagent.check_requires", return_value=failures):
                with pytest.raises(RuntimeError) as exc:
                    launch_agent(task="Fix #1", cwd=str(tmp_path),
                                 workflow_name="adhoc")
        msg = str(exc.value)
        assert "ga4-report: stderr line that will not stop" in msg
        assert len(msg) < 400
        # Truncation is display-only: the log still records the whole detail.
        assert any(long_detail in r.getMessage() for r in caplog.records
                   if r.levelno >= logging.ERROR)

    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_detail_free_failure_still_reads_cleanly(self, mock_launch,
                                                     mock_reg, tmp_path):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        failures = self._failures(("gstack", "", "", ""))
        with patch("bobi.subagent.check_requires", return_value=failures):
            with pytest.raises(RuntimeError) as exc:
                launch_agent(task="Fix #1", cwd=str(tmp_path),
                             workflow_name="adhoc")
        msg = str(exc.value)
        assert "gstack: no detail" in msg


class TestLaunchAgent:
    """Test that launch_agent launches a detached subprocess."""

    @pytest.fixture(autouse=True)
    def bound_root(self, tmp_path, monkeypatch):
        """launch_agent reads the bound installation root; binding is the
        spawning process's job, so tests bind explicitly."""
        _write_agent_yaml(tmp_path)
        paths.bind_root(tmp_path)
        yield
        paths.bind_root(None)

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_returns_deterministic_name(self, mock_launch, mock_reg, mock_check):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        name = launch_agent(task="Fix issue #42", cwd="/tmp/test", workflow_name="adhoc", run_key="42")
        assert "adhoc" in name
        assert "42" in name
        mock_launch.assert_called_once()

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_subprocess_calls_entry(self, mock_launch, mock_reg, mock_check):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        launch_agent(task="Fix #1", cwd="/tmp/test", workflow_name="adhoc")
        script = mock_launch.call_args[0][0]
        assert "_run_agent_entry" in script

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_rejects_active_run(self, mock_launch, mock_reg, mock_check):
        active = MagicMock()
        active.status = "running"
        # A LIVE pid: admission crash-closes an active entry whose process is
        # gone, and a MagicMock pid would read as dead (#850).
        active.pid = os.getpid()
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=active))
        from bobi.subagent import launch_agent
        with pytest.raises(RuntimeError, match="already active"):
            launch_agent(task="Fix #1", cwd="/tmp/test", workflow_name="adhoc")

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_allows_after_done(self, mock_launch, mock_reg, mock_check):
        done = MagicMock()
        done.status = "done"
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=done))
        from bobi.subagent import launch_agent
        name = launch_agent(task="Fix #1", cwd="/tmp/test", workflow_name="adhoc")
        assert name  # no exception

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_passes_requested_by(self, mock_launch, mock_reg, mock_check):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        req = {"from": "Alice", "channel": "C1"}
        launch_agent(task="Fix #1", cwd="/tmp/test", workflow_name="adhoc", requested_by=req)
        args = mock_launch.call_args[0][1]
        import json
        parsed = json.loads(args[0])
        assert parsed["requested_by"] == req

    @patch("bobi.subagent._alert_requires_failure")
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_blocks_on_failed_requires(self, mock_launch, mock_reg, mock_alert, tmp_path):
        from bobi.config import RequiresEntry
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        failed = [(RequiresEntry(name="gstack", check="false"), False, "check failed")]
        with patch("bobi.subagent.check_requires", return_value=failed):
            from bobi.subagent import launch_agent
            with pytest.raises(RuntimeError, match="dependency check failed"):
                launch_agent(task="Fix #1", cwd=str(tmp_path), workflow_name="adhoc")
        mock_alert.assert_called_once()
        mock_launch.assert_not_called()

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_passes_installation_root_to_child(self, mock_launch, mock_reg,
                                               mock_check, tmp_path, monkeypatch):
        """The spawner's bound root travels in the args blob — the child
        inherits its identity instead of inferring it from cwd."""
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        _write_agent_yaml(tmp_path)
        paths.bind_root(tmp_path)
        repo = tmp_path / "repos" / "jobtack"
        repo.mkdir(parents=True)

        from bobi.subagent import launch_agent
        launch_agent(task="Fix #1", cwd=str(repo), workflow_name="adhoc")

        parsed = json.loads(mock_launch.call_args[0][1][0])
        assert parsed["root"] == str(tmp_path)
        assert parsed["cwd"] == str(repo)
        # Preflight also runs against the root, not the working dir
        mock_check.assert_called_once_with(tmp_path)

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_project_lead_launch_passes_configured_codex_brain_to_child(
        self, mock_launch, mock_reg, mock_check, tmp_path, monkeypatch,
    ):
        """A codex-backed project lead must not inherit the default Claude brain."""
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        monkeypatch.setenv("BOBI_BRAIN", "claude")
        _write_agent_yaml(
            tmp_path,
            "agent: eng-team\nbrain:\n  kind: codex\n  model: gpt-5-codex\n"
        )
        paths.bind_root(tmp_path)

        from bobi.subagent import launch_agent
        launch_agent(
            task="Lead #507",
            cwd=str(tmp_path),
            workflow_name="issue-lifecycle",
            role="project_lead",
        )

        child_env = mock_launch.call_args.kwargs["env"]
        assert child_env["BOBI_BRAIN"] == "codex"
        assert child_env["BOBI_BRAIN_MODEL"] == "gpt-5-codex"

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_raises_when_spawner_unbound(self, mock_launch, mock_reg,
                                         mock_check, tmp_path, monkeypatch):
        """An unbound spawning process is a bug — no resolution from cwd,
        no guessing. It raises before anything is registered or launched."""
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        paths.bind_root(None)
        repo = tmp_path / "repos" / "jobtack"
        repo.mkdir(parents=True)

        from bobi.subagent import launch_agent
        with pytest.raises(RuntimeError, match="not bound"):
            launch_agent(task="Fix #1", cwd=str(repo), workflow_name="adhoc")
        mock_launch.assert_not_called()

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent._launch_detached", return_value=1234)
    @patch("bobi.sdk.compute_manifest_hash", return_value="hash")
    @patch("bobi.subagent._check_spend_governor")
    def test_admission_rotation_and_register_happen_under_launch_lock(
        self, mock_spend, mock_hash, mock_launch, mock_check, tmp_path
    ):
        """The stale-count race is closed by one critical section around launch admission."""
        from bobi.subagent import launch_agent

        class TrackingLock:
            def __init__(self):
                self.held = False

            def __enter__(self):
                self.held = True

            def __exit__(self, exc_type, exc, tb):
                self.held = False

        class Registry:
            def __init__(self, lock):
                self.lock = lock

            def get(self, name):
                assert self.lock.held
                return None

            def register(self, entry):
                assert self.lock.held

            def update(self, name, **kwargs):
                assert not self.lock.held

        lock = TrackingLock()
        registry = Registry(lock)
        paths.agent_yaml_path(tmp_path).write_text(
            "entry_point: x\n"
            "max_concurrent_agents: 4\n"
            "launch_admission:\n"
            "  enabled: true\n"
            "  max_starting_agents: 1\n"
        )

        def assert_locked(*args, **kwargs):
            assert lock.held

        with patch("bobi.subagent.get_registry", return_value=registry), \
             patch("bobi.subagent._LAUNCH_ADMISSION_LOCK", lock), \
             patch("bobi.sdk.check_image_rotation",
                   side_effect=assert_locked) as mock_rotation, \
             patch("bobi.launch_admission.wait_for_launch_admission",
                   side_effect=assert_locked) as mock_admission:
            launch_agent(
                task="Fix #1",
                cwd=str(tmp_path),
                workflow_name="adhoc",
                run_key="a",
            )

        mock_admission.assert_called_once()
        mock_rotation.assert_called_once()
        assert mock_launch.call_count == 1

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent._launch_detached", side_effect=OSError("no python"))
    @patch("bobi.subagent._check_spend_governor")
    def test_marks_registered_session_crashed_when_detached_launch_fails(
        self, mock_spend, mock_launch, mock_check, tmp_path
    ):
        from bobi.sdk import TERMINAL_CRASHED
        from bobi.subagent import launch_agent

        registry = MagicMock(get=MagicMock(return_value=None))

        with patch("bobi.subagent.get_registry", return_value=registry):
            with pytest.raises(OSError, match="no python"):
                launch_agent(
                    task="Fix #1",
                    cwd=str(tmp_path),
                    workflow_name="adhoc",
                    run_key="spawn-fails",
                )

        registered = registry.register.call_args.args[0]
        registry.mark_terminal.assert_called_once()
        assert registry.mark_terminal.call_args.args[0] == registered.name
        assert registry.mark_terminal.call_args.args[1] == TERMINAL_CRASHED


class TestDeriveRunKey:
    """The default run key is derived, so identical launches collide (#850)."""

    def test_same_workflow_and_task_gives_same_key(self):
        from bobi.subagent import derive_run_key
        assert (derive_run_key("adhoc", "Investigate the backlog")
                == derive_run_key("adhoc", "Investigate the backlog"))

    def test_different_tasks_give_different_keys(self):
        from bobi.subagent import derive_run_key
        assert (derive_run_key("adhoc", "Investigate the backlog")
                != derive_run_key("adhoc", "Investigate the other backlog"))

    def test_workflow_participates(self):
        from bobi.subagent import derive_run_key
        assert (derive_run_key("adhoc", "Do the thing")
                != derive_run_key("issue-lifecycle", "Do the thing"))

    def test_whitespace_is_normalized(self):
        """A task re-emitted with different wrapping is the same task."""
        from bobi.subagent import derive_run_key
        assert (derive_run_key("adhoc", "  Fix   the\n  login bug ")
                == derive_run_key("adhoc", "Fix the login bug"))

    def test_case_is_not_folded(self):
        """Normalization stays conservative - a false collision refuses work."""
        from bobi.subagent import derive_run_key
        assert derive_run_key("adhoc", "Deploy") != derive_run_key("adhoc", "deploy")

    def test_keeps_adhoc_prefix_so_unkeyed_runs_stay_visible(self):
        from bobi.subagent import derive_run_key
        assert derive_run_key("adhoc", "x").startswith("adhoc-")

    @pytest.mark.parametrize("dial", ["project", "role", "model", "effort"])
    def test_every_launch_dial_participates(self, dial):
        """One task at two settings is two runs, not a duplicate.

        Deriving from task text alone refuses the second - and skills/bobi.md
        documents varying exactly these per delegation.
        """
        from bobi.subagent import derive_run_key
        assert (derive_run_key("adhoc", "Do the thing", **{dial: "a"})
                != derive_run_key("adhoc", "Do the thing", **{dial: "b"}))

    def test_an_explicit_dial_equal_to_the_default_is_not_the_default(self):
        """The documented conservative edge: dials are the values as PASSED.

        Two launches that both omit --model agree on "" and collide - the
        incident's shape. One that passes the role's default explicitly reads
        as different, which errs toward launching rather than toward a false
        refusal.
        """
        from bobi.subagent import derive_run_key
        assert (derive_run_key("adhoc", "t")
                != derive_run_key("adhoc", "t", model="opus"))

    def test_key_is_twelve_hex_chars(self):
        """48 bits is what makes an accidental collision negligible; a shorter
        digest would silently start refusing unrelated tasks."""
        from bobi.subagent import derive_run_key
        digest = derive_run_key("adhoc", "t").removeprefix("adhoc-")
        assert len(digest) == 12
        assert all(c in "0123456789abcdef" for c in digest)


class TestLaunchAgentUnkeyedDedup:
    """Omitting --id must not silently remove duplicate-run protection (#850).

    The incident: 50 launches of one task, none carrying --id, every one of them
    minted a random key, so the "already active" guard never fired and the chain
    ran to the spend cap.
    """

    TASK = "Investigate the sales-calls backlog and report"

    @pytest.fixture(autouse=True)
    def bound_root(self, tmp_path):
        _write_agent_yaml(tmp_path)
        paths.bind_root(tmp_path)
        yield
        paths.bind_root(None)

    @pytest.fixture
    def registry(self, tmp_path):
        """A REAL registry on an isolated root.

        The duplicate guard reads back what a previous launch wrote, so the
        collision has to be genuine. A mock answers whatever the test told it
        to and passes even when the two launches never collided - which is the
        bug. Real dead-pid reaping matters here too.
        """
        from bobi.sdk import SessionRegistry
        return SessionRegistry(tmp_path)

    # A pid that cannot be alive. The spawned pid is written to the registry,
    # and these tests need the predecessor to read as DEAD so the relaunch is
    # admitted; a small literal like 123 exists on plenty of container hosts
    # and would fail the collision tests for a reason unrelated to dedup.
    DEAD_PID = 2 ** 22

    @staticmethod
    def _mocks():
        return (patch("bobi.subagent.check_requires", return_value=[]),
                patch("bobi.subagent._launch_detached",
                      return_value=TestLaunchAgentUnkeyedDedup.DEAD_PID))

    def _launch(self, registry, **kwargs):
        from bobi.subagent import launch_agent
        checks, detached = self._mocks()
        with checks, detached, patch("bobi.subagent.get_registry",
                                     return_value=registry):
            return launch_agent(task=kwargs.pop("task", self.TASK),
                                cwd="/tmp/test", workflow_name="adhoc",
                                **kwargs)

    @staticmethod
    def _mark_running(registry, name, pid):
        """Put a live-looking entry back, the way a crash or a peer leaves one."""
        from dataclasses import replace
        registry.register(replace(registry.get(name), status="running", pid=pid))

    def test_identical_unkeyed_launches_share_one_session_name(self, registry):
        names = {self._launch(registry) for _ in range(5)}
        assert len(names) == 1, f"un-keyed launches did not collide: {names}"

    def test_second_identical_launch_is_refused_while_the_first_runs(self, registry):
        """The incident, in miniature: the guard only fires if the names agree."""
        name = self._launch(registry)
        self._mark_running(registry, name, os.getpid())
        with pytest.raises(RuntimeError, match="already active"):
            self._launch(registry)

    def test_a_different_task_still_launches_alongside(self, registry):
        """Suppression must not become a global lock on one agent at a time."""
        first = self._launch(registry)
        self._mark_running(registry, first, os.getpid())
        second = self._launch(registry, task="Something else entirely")
        assert first != second

    def test_refusal_names_the_opt_out(self, registry):
        """An agent reading the error needs to know how to run both on purpose."""
        self._mark_running(registry, self._launch(registry), os.getpid())
        with pytest.raises(RuntimeError, match="--id-random"):
            self._launch(registry)

    def test_explicit_key_refusal_does_not_mention_the_opt_out(self, registry):
        """--id 42 twice is a deliberate correlation, not an accidental one."""
        self._mark_running(registry, self._launch(registry, run_key="42"),
                           os.getpid())
        with pytest.raises(RuntimeError) as exc:
            self._launch(registry, run_key="42")
        assert "--id-random" not in str(exc.value)

    def test_random_key_restores_distinct_names_for_parallel_fan_out(self, registry):
        names = {self._launch(registry, random_key=True) for _ in range(5)}
        assert len(names) == 5

    def test_random_key_is_greppable_in_the_session_list(self, registry):
        """`rand-`, not `adhoc-`: a screen of dedup-disabled runs should be
        readable at a glance, not a hash-length comparison."""
        assert "rand-" in self._launch(registry, random_key=True)

    @pytest.mark.parametrize("dial,a,b", [
        ("role", "engineer", "reviewer"),
        ("model", "opus", "haiku"),
        ("effort", "high", "low"),
    ])
    def test_one_task_at_two_settings_is_two_runs(self, registry, dial, a, b):
        """The widened derivation, end to end through the launcher.

        Testing derive_run_key alone is not enough: launch_agent has to FORWARD
        each dial. Dropping one there is invisible until two delegations that
        differ only by --model refuse each other - the false refusal the
        derivation's docstring calls the worse failure.
        """
        first = self._launch(registry, **{dial: a})
        self._mark_running(registry, first, os.getpid())
        assert self._launch(registry, **{dial: b}) != first

    def test_derived_key_starts_a_clean_transcript(self, registry):
        """A derived key names a slot for collision, not a conversation to resume."""
        from bobi.subagent import launch_agent
        checks, detached = self._mocks()
        with checks, detached as mock_detached, \
                patch("bobi.subagent.get_registry", return_value=registry):
            launch_agent(task=self.TASK, cwd="/tmp/test", workflow_name="adhoc")
        assert json.loads(mock_detached.call_args[0][1][0])["fresh"] is True

    def test_explicit_key_keeps_the_resume_contract(self, registry):
        """--id is the workflow engine's retry handle: it resumes by default."""
        from bobi.subagent import launch_agent
        checks, detached = self._mocks()
        with checks, detached as mock_detached, \
                patch("bobi.subagent.get_registry", return_value=registry):
            launch_agent(task=self.TASK, cwd="/tmp/test",
                         workflow_name="adhoc", run_key="42")
        assert json.loads(mock_detached.call_args[0][1][0])["fresh"] is False

    def test_a_crashed_run_does_not_block_its_own_relaunch(self, registry):
        """The corpse problem: a stable name means a dead run is read back.

        A run killed by OOM/SIGKILL leaves status="running" with a dead pid.
        Un-keyed launches never read a stale entry back before (new name every
        time), so nothing had to reap; now they land on the same one and an
        unreaped corpse would refuse the relaunch forever. reconcile_sessions
        only runs at manager startup, so nothing else clears it in time.
        """
        name = self._launch(registry)
        self._mark_running(registry, name, self.DEAD_PID)
        assert self._launch(registry) == name  # no exception

    def test_a_live_run_still_blocks_its_relaunch(self, registry):
        """The reap must not swallow the guard for a process that is alive."""
        self._mark_running(registry, self._launch(registry), os.getpid())
        with pytest.raises(RuntimeError, match="already active"):
            self._launch(registry)

    def test_a_crashed_predecessor_is_reported_before_it_is_replaced(self, registry):
        """Reaping the corpse silently loses the only record that it died.

        Admission registers a brand-new entry over the dead one moments later,
        taking the crash status and the un-emitted flag the reconciler's sweep
        keys off with it. Whoever launched the dead run would never learn it
        died, so the close emits here, not in a later sweep.
        """
        emitted = []
        name = self._launch(registry)
        self._mark_running(registry, name, self.DEAD_PID)
        with patch("bobi.reconcile._default_emit",
                   side_effect=lambda t, d: emitted.append((t, d)) or True):
            assert self._launch(registry) == name

        assert [t for t, _ in emitted] == ["agent/session.failed"]
        assert "died without reporting a terminal status" in emitted[0][1]["error"]
        # And the relaunch's own entry is what survives, not the crash record.
        assert registry.get(name).status == "starting"

    def test_a_suspended_run_is_not_taken_over_by_a_derived_key(self, registry):
        """`waiting` is dormant, not free - its process exited on purpose.

        A launch that matched only by task text cannot mean "resume that", and
        admitting it would hand the new run the suspended one's session name,
        worktree branch and registry entry.
        """
        from dataclasses import replace
        name = self._launch(registry)
        registry.register(replace(registry.get(name), status="waiting", pid=0))
        with pytest.raises(RuntimeError, match="suspended run already holds"):
            self._launch(registry)

    def test_an_explicit_key_may_still_redispatch_onto_a_suspended_run(self, registry):
        """--id 42 means "this is run 42" - resuming it is the retry contract."""
        from dataclasses import replace
        name = self._launch(registry, run_key="42")
        registry.register(replace(registry.get(name), status="waiting", pid=0))
        assert self._launch(registry, run_key="42") == name  # no exception

    def test_the_suspended_refusal_names_a_remedy_that_works(self, registry):
        """`subagents cancel` refuses a waiting run outright (cancel_agent only
        touches ACTIVE_STATUSES), so telling the caller to cancel or to wait
        would leave --id-random - duplicating parked work - as the only move
        with an effect. That is the storm this guard exists to stop.
        """
        from dataclasses import replace
        from bobi.subagent import DuplicateRunError
        name = self._launch(registry)
        entry = replace(registry.get(name), status="waiting", pid=0)
        registry.register(entry)
        with pytest.raises(DuplicateRunError) as exc:
            self._launch(registry)
        message = str(exc.value)
        assert "cannot be cancelled" in message, message
        assert f"--id {entry.run_key!r}" in message, message
        # And it still quotes the parked run's task, so the reader can tell
        # whether it is really the same work.
        assert entry.title in message, message

    def test_refusal_is_typed_so_callers_can_tell_it_from_a_failure(self, registry):
        """A dependency preflight failure is not "this work already happening"."""
        from bobi.subagent import DuplicateRunError
        self._mark_running(registry, self._launch(registry), os.getpid())
        with pytest.raises(DuplicateRunError) as exc:
            self._launch(registry)
        assert exc.value.derived_key is True
        assert exc.value.status == "running"

    def test_random_key_is_rejected_alongside_an_explicit_one(self, registry):
        from bobi.subagent import launch_agent
        checks, detached = self._mocks()
        with checks, detached, patch("bobi.subagent.get_registry",
                                     return_value=registry):
            with pytest.raises(ValueError, match="random_key"):
                launch_agent(task=self.TASK, cwd="/tmp/test",
                             workflow_name="adhoc", run_key="42",
                             random_key=True)

    def test_a_persistent_agent_blocks_its_twin_but_not_its_restart(self, registry):
        """`idle` is an active status and a persistent agent parks there.

        Two identical persistent launches are a duplicate and must be refused;
        a crashed one must still be restartable, or `--subscribe` (which
        implies --persistent) becomes a one-shot.
        """
        name = self._launch(registry, persistent=True)
        self._mark_running(registry, name, os.getpid())
        registry.update(name, status="idle")
        with pytest.raises(RuntimeError, match="already active"):
            self._launch(registry, persistent=True)

        registry.update(name, pid=self.DEAD_PID)
        assert self._launch(registry, persistent=True) == name


class TestWaitAndPersistentShareOneNamespace:
    """#1057: one derivation namespace, two distinct name SHAPES.

    The retired `adhoc:spawn` namespace existed to keep an un-keyed --wait
    run and an un-keyed persistent agent on one task from colliding on one
    session name (one inbox, one registry pid, one transcript, two live
    processes). Both now derive in the `adhoc` namespace: the wait run
    registers the workflow shape (make_session_name), the persistent agent
    the bare key - distinct by construction. A mutant that registers the
    wait run under the bare derived key recreates the collision and fails
    here.
    """

    def test_wait_session_name_differs_from_a_persistent_launch(self):
        from bobi.subagent import derive_run_key
        from bobi.workflow.orchestrator import make_session_name
        key = derive_run_key("adhoc", "Investigate CI")
        persistent_name = key  # launch_agent: session_name = run_key
        wait_name = make_session_name("adhoc", "proj", key)
        assert wait_name != persistent_name
        assert wait_name == f"wf-adhoc-proj-{key}"


class TestLaunchAgentWaitMode:
    """wait=True runs the admitted workflow in-process (#1057)."""

    @pytest.fixture(autouse=True)
    def bound_root(self, tmp_path):
        _write_agent_yaml(tmp_path)
        paths.bind_root(tmp_path)
        yield
        paths.bind_root(None)

    def test_wait_refuses_persistent(self):
        from bobi.subagent import launch_agent
        with pytest.raises(ValueError, match="persistent"):
            launch_agent(task="t", cwd="/tmp", workflow_name="adhoc",
                         wait=True, persistent=True)

    @staticmethod
    def _launch_wait(**kwargs):
        from bobi.subagent import launch_agent

        collected = {}

        def fake_run_workflow(*, collect=None, **kw):
            collected.update(kw)
            if collect is not None:
                collect["final_text"] = "the answer"
            return True

        with patch("bobi.workflow.orchestrator.run_workflow",
                   side_effect=fake_run_workflow), \
             patch("bobi.subagent.check_requires", return_value=[]), \
             patch("bobi.subagent._check_spend_governor"), \
             patch("bobi.subagent._check_concurrency_semaphore"), \
             patch("bobi.sdk.check_image_rotation"):
            result = launch_agent(task="Investigate CI", cwd="/tmp/test",
                                  workflow_name="adhoc", wait=True, **kwargs)
        return result, collected

    def test_wait_returns_the_final_text_from_the_run(self):
        result, kw = self._launch_wait()
        assert result.success is True
        assert result.final_text == "the answer"
        # The executor is run_workflow, under the admitted identity.
        assert kw["workflow"].name == "adhoc"
        assert kw["run_key"]

    def test_wait_registers_the_workflow_shaped_session_name(self):
        from bobi.sdk import get_registry
        result, kw = self._launch_wait()
        from bobi.workflow.orchestrator import make_session_name
        expected = make_session_name(
            "adhoc", "test", kw["run_key"])
        assert result.run_key == expected
        assert get_registry().get(expected) is not None

    def test_wait_stamps_the_chain_into_its_own_environment(self, monkeypatch):
        from bobi.launch_lineage import LINEAGE_ENV_VAR
        monkeypatch.delenv(LINEAGE_ENV_VAR, raising=False)
        result, _ = self._launch_wait()
        assert result.run_key in os.environ[LINEAGE_ENV_VAR]


class TestRunAgentEntryRootBinding:
    """_run_agent_entry binds the root its spawner passed, never cwd."""

    @patch("bobi.subagent.run_persistent_agent")
    def test_binds_passed_root(self, mock_spawn, tmp_path, monkeypatch):
        import bobi.sdk as sdk
        monkeypatch.setattr("bobi.paths._root", None)
        root = tmp_path / "dev"
        repo = root / "jobtack"
        _write_agent_yaml(root, "name: t\n")
        repo.mkdir()

        from bobi.subagent import _run_agent_entry
        _run_agent_entry({
            "task": "t", "cwd": str(repo), "root": str(root),
            "workflow_name": "adhoc", "persistent": True, "subscribe": [],
        })

        assert sdk.get_project_root() == root
        # cwd stays the working dir for the spawned session
        assert mock_spawn.call_args.kwargs["cwd"] == str(repo)

    @patch("bobi.subagent.run_persistent_agent")
    def test_sets_process_brain_from_passed_root(self, mock_spawn, tmp_path,
                                                monkeypatch):
        monkeypatch.setattr("bobi.paths._root", None)
        monkeypatch.setenv("BOBI_BRAIN", "claude")
        root = tmp_path / "dev"
        repo = root / "jobtack"
        _write_agent_yaml(
            root,
            "name: t\nbrain:\n  kind: codex\n  model: gpt-5-codex\n"
        )
        repo.mkdir()

        from bobi.brain import BRAIN_ENV, get_process_brain_model
        from bobi.subagent import _run_agent_entry
        _run_agent_entry({
            "task": "t", "cwd": str(repo), "root": str(root),
            "workflow_name": "adhoc", "persistent": True, "subscribe": [],
        })

        assert os.environ[BRAIN_ENV] == "codex"
        assert get_process_brain_model() == "gpt-5-codex"

    @patch("bobi.subagent.run_persistent_agent")
    def test_clears_stale_process_brain_when_passed_root_has_default_brain(
        self, mock_spawn, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr("bobi.paths._root", None)
        monkeypatch.setenv("BOBI_BRAIN", "codex")
        root = tmp_path / "dev"
        repo = root / "jobtack"
        _write_agent_yaml(root, "name: t\n")
        repo.mkdir()

        from bobi.brain import BRAIN_ENV, get_process_brain_model
        from bobi.subagent import _run_agent_entry
        monkeypatch.setenv("BOBI_BRAIN_MODEL", "gpt-5-codex")
        _run_agent_entry({
            "task": "t", "cwd": str(repo), "root": str(root),
            "workflow_name": "adhoc", "persistent": True, "subscribe": [],
        })

        assert BRAIN_ENV not in os.environ
        assert get_process_brain_model() == ""

    @patch("bobi.subagent.run_persistent_agent")
    def test_missing_root_is_a_spawner_bug(self, mock_spawn, tmp_path,
                                           monkeypatch):
        """An args blob without a root fails loudly — the child never
        guesses its identity from cwd."""
        monkeypatch.setattr("bobi.paths._root", None)
        repo = tmp_path / "dev" / "jobtack"
        repo.mkdir(parents=True)

        from bobi.subagent import _run_agent_entry
        with pytest.raises(RuntimeError, match="missing 'root'|no 'root'"):
            _run_agent_entry({
                "task": "t", "cwd": str(repo),
                "workflow_name": "adhoc", "persistent": True, "subscribe": [],
            })
        mock_spawn.assert_not_called()

    @patch("bobi.subagent.run_persistent_agent")
    def test_rejects_root_without_marker(self, mock_spawn, tmp_path,
                                         monkeypatch):
        """A root that is not a real installation must be refused — binding
        it would mkdir a fresh scattered .bobi at a bogus path."""
        monkeypatch.setattr("bobi.paths._root", None)
        bogus = tmp_path / "not-an-install"
        bogus.mkdir()

        from bobi.subagent import _run_agent_entry
        with pytest.raises(RuntimeError, match="not a Bobi installation"):
            _run_agent_entry({
                "task": "t", "cwd": str(bogus), "root": str(bogus),
                "workflow_name": "adhoc", "persistent": True, "subscribe": [],
            })
        mock_spawn.assert_not_called()


class TestResolveSelfGitHubLogin:
    """The bot's own GitHub login backs the reactor self-author guard (#411)."""

    def _reset_cache(self):
        import bobi.subagent as sub
        sub._self_github_login = None
        sub._self_github_login_resolved = False

    @patch("bobi.subagent.sp.run")
    def test_resolves_login_from_gh(self, mock_run):
        self._reset_cache()
        mock_run.return_value = MagicMock(returncode=0, stdout="bobi\n")
        from bobi.subagent import _resolve_self_github_login
        assert _resolve_self_github_login() == "bobi"

    @patch("bobi.subagent.sp.run")
    def test_caches_result_across_calls(self, mock_run):
        self._reset_cache()
        mock_run.return_value = MagicMock(returncode=0, stdout="bobi\n")
        from bobi.subagent import _resolve_self_github_login
        _resolve_self_github_login()
        _resolve_self_github_login()
        mock_run.assert_called_once()  # cached, not re-shelled

    @patch("bobi.subagent.sp.run")
    def test_fail_open_on_gh_error(self, mock_run):
        """gh missing/unauthenticated → None (guard stays off, fail open)."""
        self._reset_cache()
        mock_run.side_effect = OSError("gh not found")
        from bobi.subagent import _resolve_self_github_login
        assert _resolve_self_github_login() is None

    @patch("bobi.subagent.sp.run")
    def test_fail_open_on_nonzero_exit(self, mock_run):
        self._reset_cache()
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        from bobi.subagent import _resolve_self_github_login
        assert _resolve_self_github_login() is None


class TestAutoDispatchIdentityRequirement:
    def test_self_match_requires_identity_even_when_self_authored_is_allowed(self):
        from bobi.subagent import _auto_dispatch_needs_self_login

        rules = [{
            "event": "github.issues",
            "match": {"action": "assigned", "assignee": "$self"},
            "allow_self_authored": True,
        }]

        assert _auto_dispatch_needs_self_login(rules) is True

    def test_literal_match_with_self_authored_allowed_needs_no_identity(self):
        from bobi.subagent import _auto_dispatch_needs_self_login

        rules = [{
            "event": "github.issues",
            "match": {"action": "assigned"},
            "allow_self_authored": True,
        }]

        assert _auto_dispatch_needs_self_login(rules) is False

    def test_default_self_author_guard_still_requires_identity(self):
        from bobi.subagent import _auto_dispatch_needs_self_login

        assert _auto_dispatch_needs_self_login([
            {"event": "github.pull_request_review"},
        ]) is True


class TestPeriodAdmission:
    """A periodic workflow admits one run per period (#1048)."""

    @pytest.fixture(autouse=True)
    def bound_root(self, tmp_path, monkeypatch):
        _write_agent_yaml(tmp_path)
        wf_dir = paths.workflows_dir(tmp_path)
        wf_dir.mkdir(parents=True, exist_ok=True)
        (wf_dir / "standup.yaml").write_text(
            "name: standup\n"
            "period: daily\n"
            "steps:\n"
            "  - name: post\n"
            "    prompt: post it\n"
        )
        paths.bind_root(tmp_path)
        yield
        paths.bind_root(None)

    @staticmethod
    def _period_key():
        import time
        return "standup-" + time.strftime("%Y-%m-%d")

    @staticmethod
    def _ledger_entry(status, run_key, session_name="", checkpoint=-1,
                      repo="test"):
        # repo defaults to what _resolve_project_name("/tmp/test") yields:
        # the ledger is repo-scoped, so an entry for another repo must not
        # block this one (see test_other_repos_period_does_not_block).
        from bobi.workflow.state import WorkflowRun
        run = WorkflowRun.create("standup", {"data": {"run_key": run_key}})
        run.run_key = run_key
        run.status = status
        run.session_name = session_name
        run.checkpoint_step = checkpoint
        run.repo = repo
        if status == "completed":
            run.completed_at = "2026-08-18T09:00:00+00:00"
        if status in ("waiting",):
            run.await_event = "review.approved"
        run.save()
        return run

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_period_key_overrides_caller_key(self, mock_launch, mock_reg,
                                             mock_check):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        name = launch_agent(task="post it", cwd="/tmp/test",
                            workflow_name="standup", run_key="manual-oops")
        assert self._period_key() in name
        assert "manual-oops" not in name
        blob = json.loads(mock_launch.call_args[0][1][0])
        assert blob["run_key"] == self._period_key()

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_two_dispatch_paths_one_period_key(self, mock_launch, mock_reg,
                                               mock_check):
        # The #1016 shape: a scheduled tick and a manual catch-up, different
        # caller keys, land on ONE identity.
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        a = launch_agent(task="scheduled tick", cwd="/tmp/test",
                         workflow_name="standup", run_key="sched-1")
        b = launch_agent(task="manual catch-up", cwd="/tmp/test",
                         workflow_name="standup", run_key="manual-2")
        assert a == b

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_completed_period_refuses_relaunch(self, mock_launch, mock_reg,
                                               mock_check):
        from bobi.subagent import DuplicateRunError, launch_agent
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        self._ledger_entry("completed", self._period_key())
        with pytest.raises(DuplicateRunError, match="already ran"):
            launch_agent(task="post it", cwd="/tmp/test",
                         workflow_name="standup")
        mock_launch.assert_not_called()

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_suspended_period_refuses_relaunch(self, mock_launch, mock_reg,
                                               mock_check):
        from bobi.subagent import DuplicateRunError, launch_agent
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        self._ledger_entry("waiting", self._period_key())
        with pytest.raises(DuplicateRunError, match="suspended"):
            launch_agent(task="post it", cwd="/tmp/test",
                         workflow_name="standup")
        mock_launch.assert_not_called()

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_stale_running_entry_does_not_block_the_period(
            self, mock_launch, mock_reg, mock_check):
        # The liveness check the naive period-key design lacked: the ledger
        # says running, but no live process holds the session. Admission
        # closes the entry and admits the relaunch.
        from bobi.subagent import launch_agent
        from bobi.workflow.state import WorkflowRun
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        stale = self._ledger_entry("running", self._period_key())
        name = launch_agent(task="post it", cwd="/tmp/test",
                            workflow_name="standup")
        assert name
        mock_launch.assert_called_once()
        assert WorkflowRun.load(stale.run_id).status == "failed"

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_live_running_period_still_refused(self, mock_launch, mock_reg,
                                               mock_check):
        from bobi.subagent import DuplicateRunError, launch_agent
        from bobi.workflow.state import WorkflowRun
        live = MagicMock()
        live.status = "running"
        live.pid = os.getpid()
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=live))
        stale = self._ledger_entry("running", self._period_key())
        with pytest.raises(DuplicateRunError, match="already active"):
            launch_agent(task="post it", cwd="/tmp/test",
                         workflow_name="standup")
        # A LIVE run's ledger entry is not clobbered.
        assert WorkflowRun.load(stale.run_id).status == "running"

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_random_key_overridden_for_periodic_workflow(
            self, mock_launch, mock_reg, mock_check):
        # Overridden, never refused: the reactor passes random_key for every
        # id-less event, so a refusal would kill periodic auto-dispatch.
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        name = launch_agent(task="post it", cwd="/tmp/test",
                            workflow_name="standup", random_key=True)
        assert self._period_key() in name
        assert "rand-" not in name
        mock_launch.assert_called_once()

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_non_periodic_workflow_keeps_caller_key(self, mock_launch,
                                                    mock_reg, mock_check):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        name = launch_agent(task="fix it", cwd="/tmp/test",
                            workflow_name="adhoc", run_key="42")
        assert "42" in name

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_other_repos_period_does_not_block(self, mock_launch, mock_reg,
                                               mock_check):
        # The period is scoped per repo, like the session name: repo A's
        # completed standup must not deny repo B its run.
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        self._ledger_entry("completed", self._period_key(), repo="other-repo")
        name = launch_agent(task="post it", cwd="/tmp/test",
                            workflow_name="standup")
        assert name
        mock_launch.assert_called_once()

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_fresh_reruns_a_completed_period(self, mock_launch, mock_reg,
                                             mock_check):
        # --fresh is the deliberate operator escape hatch: automatic
        # dispatchers never pass it, so the dedupe still holds for them.
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        self._ledger_entry("completed", self._period_key())
        name = launch_agent(task="post it", cwd="/tmp/test",
                            workflow_name="standup", fresh=True)
        assert name
        mock_launch.assert_called_once()

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_stale_resuming_entry_does_not_block_the_period(
            self, mock_launch, mock_reg, mock_check):
        # A resume process killed mid-claim leaves "resuming" behind (D071);
        # with no live session it must not hold the period hostage.
        from bobi.subagent import launch_agent
        from bobi.workflow.state import WorkflowRun
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        stale = self._ledger_entry("resuming", self._period_key())
        name = launch_agent(task="post it", cwd="/tmp/test",
                            workflow_name="standup")
        assert name
        mock_launch.assert_called_once()
        assert WorkflowRun.load(stale.run_id).status == "failed"

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_registry_waiting_backstop_refuses_with_period_remedy(
            self, mock_launch, mock_reg, mock_check):
        # Ledger empty but the registry remembers a suspended holder of this
        # session name: the backstop must refuse, and must NOT suggest --id,
        # which a period key overrides right back onto this refusal.
        from bobi.subagent import DuplicateRunError, launch_agent
        waiting = MagicMock()
        waiting.status = "waiting"
        waiting.pid = 0
        waiting.title = "post it"
        waiting.run_key = self._period_key()
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=waiting))
        with pytest.raises(DuplicateRunError, match="parked at an await"):
            launch_agent(task="post it", cwd="/tmp/test",
                         workflow_name="standup")
        mock_launch.assert_not_called()

    @patch("bobi.subagent.check_requires", return_value=[])
    @patch("bobi.subagent.get_registry")
    @patch("bobi.subagent._launch_detached")
    def test_workflow_without_period_keeps_caller_key(self, mock_launch,
                                                      mock_reg, mock_check,
                                                      tmp_path):
        # A REAL workflow file with no period: the caller's key must survive
        # (the not-found path is covered separately by the adhoc test).
        from bobi import paths
        (paths.workflows_dir() / "oneshot.yaml").write_text(
            "name: oneshot\n"
            "steps:\n"
            "  - name: go\n"
            "    prompt: go\n"
        )
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from bobi.subagent import launch_agent
        name = launch_agent(task="fix it", cwd="/tmp/test",
                            workflow_name="oneshot", run_key="42")
        assert "42" in name
