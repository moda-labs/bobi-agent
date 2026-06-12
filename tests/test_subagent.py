"""Tests for the sub-agent executor module — unit tests only.

For blocking execution and SDK interaction tests, see test_subagent_blocking.py.
"""

import json
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modastack.sdk import SessionEntry
from modastack.subagent import (
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


class TestBuildPrompt:
    @pytest.fixture(autouse=True)
    def bound_root(self, tmp_path, monkeypatch):
        """_build_prompt reads handoffs via the session registry, which
        requires a bound root — don't depend on one leaking from tests
        that ran earlier."""
        monkeypatch.setattr("modastack.paths._root", tmp_path)

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
        with patch("modastack.subagent.get_registry",
                   return_value=_mock_registry([])):
            assert not cancel_agent("AGD-99")

    def test_find_agent_none(self):
        with patch("modastack.subagent.get_registry",
                   return_value=_mock_registry([])):
            assert find_agent("AGD-99") is None

    def test_list_agents_empty(self):
        with patch("modastack.subagent.get_registry",
                   return_value=_mock_registry([])):
            assert list_agents() == []

    def test_find_agent_by_run_key(self):
        entry = SessionEntry(name="agent-agd-12-implement", run_key="AGD-12",
                             phase="implement", status="running", pid=0)
        with patch("modastack.subagent.get_registry",
                   return_value=_mock_registry([entry])):
            found = find_agent("AGD-12")
            assert found is not None
            assert found.name == "agent-agd-12-implement"

    def test_find_agent_by_session_name(self):
        entry = SessionEntry(name="agent-agd-12-implement", run_key="AGD-12",
                             phase="implement", status="running", pid=0)
        with patch("modastack.subagent.get_registry",
                   return_value=_mock_registry([entry])):
            assert find_agent("agent-agd-12-implement") is entry

    def test_find_agent_prefers_active(self):
        done = SessionEntry(name="agent-agd-12-spec", run_key="AGD-12",
                            phase="spec", status="done", pid=0)
        active = SessionEntry(name="agent-agd-12-implement", run_key="AGD-12",
                              phase="implement", status="running", pid=0)
        with patch("modastack.subagent.get_registry",
                   return_value=_mock_registry([done, active])):
            assert find_agent("AGD-12") is active

    def test_list_agents_excludes_managers(self):
        mgr = SessionEntry(name="moda-director-x", role="manager",
                           status="running", pid=0)
        eng = SessionEntry(name="agent-1-implement", run_key="1",
                           phase="implement", status="running", pid=0)
        with patch("modastack.subagent.get_registry",
                   return_value=_mock_registry([mgr, eng])):
            names = [a["name"] for a in list_agents()]
            assert names == ["agent-1-implement"]

    def test_cancel_running_agent_updates_registry(self):
        entry = SessionEntry(name="agent-agd-12-implement", run_key="AGD-12",
                             phase="implement", status="running", pid=0)
        registry = _mock_registry([entry])
        with patch("modastack.subagent.get_registry", return_value=registry):
            assert cancel_agent("AGD-12")
        registry.update.assert_called_once_with(
            "agent-agd-12-implement", status="cancelled", pid=0)

    def test_cancel_done_agent_returns_false(self):
        entry = SessionEntry(name="agent-agd-12-implement", run_key="AGD-12",
                             phase="implement", status="done", pid=0)
        with patch("modastack.subagent.get_registry",
                   return_value=_mock_registry([entry])):
            assert not cancel_agent("AGD-12")


class TestLaunchDetached:
    """Test the shared _launch_detached helper."""

    @patch("modastack.subagent.sp.Popen")
    def test_uses_start_new_session(self, mock_popen):
        from modastack.subagent import _launch_detached
        _launch_detached("print('hi')", [], Path("/tmp/test.log"))
        _, kwargs = mock_popen.call_args
        assert kwargs.get("start_new_session") is True

    @patch("modastack.subagent.sp.Popen")
    def test_creates_log_dir(self, mock_popen, tmp_path):
        from modastack.subagent import _launch_detached
        log_file = tmp_path / "nested" / "dir" / "test.log"
        _launch_detached("print('hi')", [], log_file)
        assert log_file.parent.exists()

    @patch("modastack.subagent.sp.Popen")
    def test_passes_args(self, mock_popen):
        from modastack.subagent import _launch_detached
        _launch_detached("import sys; print(sys.argv)", ["a", "b"], Path("/tmp/t.log"))
        cmd = mock_popen.call_args[0][0]
        assert cmd[-2:] == ["a", "b"]


class TestCheckRequires:
    """Test the dispatch-time requires check with TTL cache."""

    def test_returns_pass_for_healthy_deps(self, tmp_path):
        from modastack.subagent import check_requires, _requires_cache
        _requires_cache.clear()
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text(
            "entry_point: x\nrequires:\n  - name: ok\n    check: 'true'\n")
        results = check_requires(tmp_path)
        assert len(results) == 1
        assert results[0][1] is True

    def test_returns_fail_for_broken_deps(self, tmp_path):
        from modastack.subagent import check_requires, _requires_cache
        _requires_cache.clear()
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text(
            "entry_point: x\nrequires:\n  - name: bad\n    check: 'false'\n")
        results = check_requires(tmp_path)
        assert len(results) == 1
        assert results[0][1] is False

    def test_cache_hit_within_ttl(self, tmp_path):
        from modastack.subagent import check_requires, _requires_cache
        _requires_cache.clear()
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text(
            "entry_point: x\nrequires:\n  - name: ok\n    check: 'true'\n")
        # First call populates cache
        check_requires(tmp_path)
        # Overwrite config to make check fail — cache should still return pass
        (config_dir / "agent.yaml").write_text(
            "entry_point: x\nrequires:\n  - name: ok\n    check: 'false'\n")
        results = check_requires(tmp_path)
        assert results[0][1] is True  # cached pass, not fresh fail

    def test_cache_expired(self, tmp_path):
        import time as _time
        from modastack.subagent import check_requires, _requires_cache
        _requires_cache.clear()
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text(
            "entry_point: x\nrequires:\n  - name: ok\n    check: 'true'\n")
        check_requires(tmp_path)
        # Manually expire the cache entry
        key = str(tmp_path)
        ts, cached = _requires_cache[key]
        _requires_cache[key] = (ts - 300, cached)  # 5 min ago
        # Now change config
        (config_dir / "agent.yaml").write_text(
            "entry_point: x\nrequires:\n  - name: bad\n    check: 'false'\n")
        results = check_requires(tmp_path)
        assert results[0][1] is False  # re-ran, got fresh fail

    def test_empty_requires(self, tmp_path):
        from modastack.subagent import check_requires, _requires_cache
        _requires_cache.clear()
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text("entry_point: x\n")
        results = check_requires(tmp_path)
        assert results == []

    def test_no_config(self, tmp_path):
        from modastack.subagent import check_requires, _requires_cache
        _requires_cache.clear()
        results = check_requires(tmp_path)
        assert results == []


class TestAlertRequiresFailure:
    """Test the Slack alerting helper for failed requires checks."""

    @patch("modastack.slack.post_slack_message")
    def test_posts_to_slack(self, mock_post, tmp_path):
        from modastack.config import RequiresEntry
        from modastack.subagent import _alert_requires_failure
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text(
            "entry_point: x\nservices:\n  - name: slack\n    channels: [C123]\n"
            "    credentials:\n      bot_token: xoxb-test\n")
        failures = [(RequiresEntry(name="gstack", check="false",
                                   why="skills needed", fix="run setup"), "check failed")]
        _alert_requires_failure(tmp_path, failures)
        mock_post.assert_called_once()
        args = mock_post.call_args
        assert "gstack" in args[0][2] or "gstack" in str(args)

    @patch("modastack.slack.post_slack_message", side_effect=Exception("network error"))
    def test_slack_failure_does_not_crash(self, mock_post, tmp_path):
        from modastack.config import RequiresEntry
        from modastack.subagent import _alert_requires_failure
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text(
            "entry_point: x\nservices:\n  - name: slack\n    channels: [C123]\n"
            "    credentials:\n      bot_token: xoxb-test\n")
        failures = [(RequiresEntry(name="gstack", check="false",
                                   why="skills", fix="setup"), "failed")]
        # Should not raise
        _alert_requires_failure(tmp_path, failures)

    def test_no_slack_service_does_not_crash(self, tmp_path):
        from modastack.config import RequiresEntry
        from modastack.subagent import _alert_requires_failure
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir(parents=True)
        (config_dir / "agent.yaml").write_text("entry_point: x\n")
        failures = [(RequiresEntry(name="gstack", check="false",
                                   why="skills", fix="setup"), "failed")]
        # Should not raise
        _alert_requires_failure(tmp_path, failures)


class TestLaunchAgent:
    """Test that launch_agent launches a detached subprocess."""

    @pytest.fixture(autouse=True)
    def bound_root(self, tmp_path, monkeypatch):
        """launch_agent reads the bound installation root; binding is the
        spawning process's job, so tests bind explicitly."""
        monkeypatch.setattr("modastack.paths._root", tmp_path)

    @patch("modastack.subagent.check_requires", return_value=[])
    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_returns_deterministic_name(self, mock_launch, mock_reg, mock_check):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from modastack.subagent import launch_agent
        name = launch_agent(task="Fix issue #42", cwd="/tmp/test", workflow_name="adhoc", run_key="42")
        assert "adhoc" in name
        assert "42" in name
        mock_launch.assert_called_once()

    @patch("modastack.subagent.check_requires", return_value=[])
    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_subprocess_calls_entry(self, mock_launch, mock_reg, mock_check):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from modastack.subagent import launch_agent
        launch_agent(task="Fix #1", cwd="/tmp/test", workflow_name="adhoc")
        script = mock_launch.call_args[0][0]
        assert "_run_agent_entry" in script

    @patch("modastack.subagent.check_requires", return_value=[])
    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_rejects_active_run(self, mock_launch, mock_reg, mock_check):
        active = MagicMock()
        active.status = "running"
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=active))
        from modastack.subagent import launch_agent
        with pytest.raises(RuntimeError, match="already active"):
            launch_agent(task="Fix #1", cwd="/tmp/test", workflow_name="adhoc")

    @patch("modastack.subagent.check_requires", return_value=[])
    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_allows_after_done(self, mock_launch, mock_reg, mock_check):
        done = MagicMock()
        done.status = "done"
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=done))
        from modastack.subagent import launch_agent
        name = launch_agent(task="Fix #1", cwd="/tmp/test", workflow_name="adhoc")
        assert name  # no exception

    @patch("modastack.subagent.check_requires", return_value=[])
    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_passes_requested_by(self, mock_launch, mock_reg, mock_check):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from modastack.subagent import launch_agent
        req = {"from": "Alice", "channel": "C1"}
        launch_agent(task="Fix #1", cwd="/tmp/test", workflow_name="adhoc", requested_by=req)
        args = mock_launch.call_args[0][1]
        import json
        parsed = json.loads(args[0])
        assert parsed["requested_by"] == req

    @patch("modastack.subagent._alert_requires_failure")
    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_blocks_on_failed_requires(self, mock_launch, mock_reg, mock_alert, tmp_path):
        from modastack.config import RequiresEntry
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        failed = [(RequiresEntry(name="gstack", check="false"), False, "check failed")]
        with patch("modastack.subagent.check_requires", return_value=failed):
            from modastack.subagent import launch_agent
            with pytest.raises(RuntimeError, match="dependency check failed"):
                launch_agent(task="Fix #1", cwd=str(tmp_path), workflow_name="adhoc")
        mock_alert.assert_called_once()
        mock_launch.assert_not_called()

    @patch("modastack.subagent.check_requires", return_value=[])
    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_passes_installation_root_to_child(self, mock_launch, mock_reg,
                                               mock_check, tmp_path, monkeypatch):
        """The spawner's bound root travels in the args blob — the child
        inherits its identity instead of inferring it from cwd."""
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        monkeypatch.setattr("modastack.paths._root", tmp_path)
        repo = tmp_path / "repos" / "jobtack"
        repo.mkdir(parents=True)

        from modastack.subagent import launch_agent
        launch_agent(task="Fix #1", cwd=str(repo), workflow_name="adhoc")

        parsed = json.loads(mock_launch.call_args[0][1][0])
        assert parsed["root"] == str(tmp_path)
        assert parsed["cwd"] == str(repo)
        # Preflight also runs against the root, not the working dir
        mock_check.assert_called_once_with(tmp_path)

    @patch("modastack.subagent.check_requires", return_value=[])
    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_raises_when_spawner_unbound(self, mock_launch, mock_reg,
                                         mock_check, tmp_path, monkeypatch):
        """An unbound spawning process is a bug — no resolution from cwd,
        no guessing. It raises before anything is registered or launched."""
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        monkeypatch.setattr("modastack.paths._root", None)
        repo = tmp_path / "repos" / "jobtack"
        repo.mkdir(parents=True)

        from modastack.subagent import launch_agent
        with pytest.raises(RuntimeError, match="not bound"):
            launch_agent(task="Fix #1", cwd=str(repo), workflow_name="adhoc")
        mock_launch.assert_not_called()


class TestRunAgentEntryRootBinding:
    """_run_agent_entry binds the root its spawner passed, never cwd."""

    @patch("modastack.subagent.spawn_adhoc")
    def test_binds_passed_root(self, mock_spawn, tmp_path, monkeypatch):
        import modastack.sdk as sdk
        monkeypatch.setattr("modastack.paths._root", None)
        root = tmp_path / "dev"
        repo = root / "jobtack"
        (root / ".modastack").mkdir(parents=True)
        (root / ".modastack" / "agent.yaml").write_text("name: t\n")
        repo.mkdir()

        from modastack.subagent import _run_agent_entry
        _run_agent_entry({
            "task": "t", "cwd": str(repo), "root": str(root),
            "workflow_name": "adhoc", "persistent": True, "subscribe": [],
        })

        assert sdk.get_project_root() == root
        # cwd stays the working dir for the spawned session
        assert mock_spawn.call_args.kwargs["cwd"] == str(repo)

    @patch("modastack.subagent.spawn_adhoc")
    def test_missing_root_is_a_spawner_bug(self, mock_spawn, tmp_path,
                                           monkeypatch):
        """An args blob without a root fails loudly — the child never
        guesses its identity from cwd."""
        monkeypatch.setattr("modastack.paths._root", None)
        repo = tmp_path / "dev" / "jobtack"
        repo.mkdir(parents=True)

        from modastack.subagent import _run_agent_entry
        with pytest.raises(RuntimeError, match="missing 'root'|no 'root'"):
            _run_agent_entry({
                "task": "t", "cwd": str(repo),
                "workflow_name": "adhoc", "persistent": True, "subscribe": [],
            })
        mock_spawn.assert_not_called()

    @patch("modastack.subagent.spawn_adhoc")
    def test_rejects_root_without_marker(self, mock_spawn, tmp_path,
                                         monkeypatch):
        """A root that is not a real installation must be refused — binding
        it would mkdir a fresh scattered .modastack at a bogus path."""
        monkeypatch.setattr("modastack.paths._root", None)
        bogus = tmp_path / "not-an-install"
        bogus.mkdir()

        from modastack.subagent import _run_agent_entry
        with pytest.raises(RuntimeError, match="not a Modastack installation"):
            _run_agent_entry({
                "task": "t", "cwd": str(bogus), "root": str(bogus),
                "workflow_name": "adhoc", "persistent": True, "subscribe": [],
            })
        mock_spawn.assert_not_called()


