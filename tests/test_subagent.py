"""Tests for the sub-agent executor module — unit tests only.

For blocking execution and SDK interaction tests, see test_subagent_blocking.py.
"""

import asyncio
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modastack.subagent import (
    AgentResult,
    _build_prompt,
    _parse_issue_number,
    _resolve_repo_name,
    _resolve_skill_path,
    cancel_agent,
    get_result,
    is_running,
    list_agents,
    _running,
)


@pytest.fixture(autouse=True)
def clear_running():
    _running.clear()
    yield
    _running.clear()


@pytest.fixture
def tmp_cwd():
    d = tempfile.mkdtemp(prefix="subagent_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


class TestResolveSkillPath:
    def test_existing_skill(self):
        path = _resolve_skill_path("pickup")
        assert path is not None
        assert path.name == "SKILL.md"
        assert "pickup" in str(path)

    def test_nonexistent_skill(self):
        path = _resolve_skill_path("nonexistent-phase")
        assert path is None


class TestBuildPrompt:
    def test_includes_skill_reference(self):
        prompt = _build_prompt("pickup", "AGD-12")
        assert "SKILL.md" in prompt
        assert "AGD-12" in prompt

    def test_includes_context(self):
        prompt = _build_prompt("implement", "AGD-12", context="Build the auth flow")
        assert "Build the auth flow" in prompt

    def test_includes_handoff_instruction(self):
        prompt = _build_prompt("spec", "AGD-12")
        assert "handoff" in prompt.lower()

    def test_nonexistent_skill_still_works(self):
        prompt = _build_prompt("nonexistent", "AGD-12")
        assert "AGD-12" in prompt


class TestParseIssueNumber:
    def test_issue_hash(self):
        assert _parse_issue_number("Write a spec for issue #5") == "5"

    def test_issue_hash_no_space(self):
        assert _parse_issue_number("fix issue#42 please") == "42"

    def test_issue_hash_extra_space(self):
        assert _parse_issue_number("issue # 7 is broken") == "7"

    def test_issue_word_then_number(self):
        assert _parse_issue_number("Issue 12: AI Extraction Pipeline") == "12"

    def test_issues_plural(self):
        assert _parse_issue_number("address issues #99 and others") == "99"

    def test_bare_hash(self):
        assert _parse_issue_number("Investigate #314 regression") == "314"

    def test_case_insensitive(self):
        assert _parse_issue_number("ISSUE #8 needs attention") == "8"

    def test_prefers_issue_keyword_over_bare_hash(self):
        # A bare "#3" earlier should not beat the explicit "issue #5".
        assert _parse_issue_number("see section #3, fix issue #5") == "5"

    def test_no_reference_returns_none(self):
        assert _parse_issue_number("Fix the login bug") is None

    def test_empty_returns_none(self):
        assert _parse_issue_number("") is None

    def test_does_not_match_numbers_without_marker(self):
        assert _parse_issue_number("bump version to 5 today") is None


class TestResolveRepoName:
    def test_explicit_repo_field(self, tmp_path):
        (tmp_path / ".modastack.yaml").write_text("repo: moda-labs/jobtack\n")
        assert _resolve_repo_name(str(tmp_path)) == "moda-labs/jobtack"

    def test_git_remote_ssh(self, tmp_path):
        with patch("modastack.subagent._git_remote_name", return_value="moda-labs/jobtack"):
            assert _resolve_repo_name(str(tmp_path)) == "moda-labs/jobtack"

    def test_falls_back_to_dirname(self, tmp_path):
        with patch("modastack.subagent._git_remote_name", return_value=""):
            assert _resolve_repo_name(str(tmp_path)) == tmp_path.name

    def test_explicit_field_wins_over_remote(self, tmp_path):
        (tmp_path / ".modastack.yaml").write_text("repo: owner/explicit\n")
        with patch("modastack.subagent._git_remote_name", return_value="owner/remote"):
            assert _resolve_repo_name(str(tmp_path)) == "owner/explicit"


class TestAgentLifecycle:
    def test_is_running_no_agent(self):
        assert not is_running("AGD-99")

    def test_get_result_no_agent(self):
        assert get_result("AGD-99") is None

    def test_cancel_no_agent(self):
        assert not cancel_agent("AGD-99")

    def test_list_agents_empty(self):
        assert list_agents() == []

    def test_is_running_with_pending_task(self):
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        task = asyncio.ensure_future(future, loop=loop)

        from modastack.subagent import RunningAgent
        _running["agd-12"] = RunningAgent(
            issue_id="AGD-12",
            phase="implement",
            session_id="test-session",
            task=task,
            cwd="/tmp/test",
        )

        assert is_running("AGD-12")
        assert list_agents()[0]["running"]

        task.cancel()
        loop.close()

    def test_get_result_completed_task(self):
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        expected = AgentResult(
            session_id="sess-1",
            issue_id="AGD-12",
            phase="spec",
            success=True,
            duration_ms=5000,
            total_cost_usd=0.42,
            num_turns=15,
        )
        future.set_result(expected)
        task = asyncio.ensure_future(future, loop=loop)

        from modastack.subagent import RunningAgent
        _running["agd-12"] = RunningAgent(
            issue_id="AGD-12",
            phase="spec",
            session_id="sess-1",
            task=task,
            cwd="/tmp/test",
        )

        result = get_result("AGD-12")
        assert result is not None
        assert result.success
        assert result.total_cost_usd == 0.42
        assert "agd-12" not in _running

        loop.close()

    def test_cancel_running_agent(self):
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        task = asyncio.ensure_future(future, loop=loop)

        from modastack.subagent import RunningAgent
        _running["agd-12"] = RunningAgent(
            issue_id="AGD-12",
            phase="implement",
            session_id="test-session",
            task=task,
            cwd="/tmp/test",
        )

        assert cancel_agent("AGD-12")
        assert "agd-12" not in _running
        assert task.cancelled()

        loop.close()

    def test_get_result_failed_task(self):
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        future.set_exception(RuntimeError("SDK connection lost"))
        task = asyncio.ensure_future(future, loop=loop)

        from modastack.subagent import RunningAgent
        _running["agd-12"] = RunningAgent(
            issue_id="AGD-12",
            phase="implement",
            session_id="",
            task=task,
            cwd="/tmp/test",
        )

        result = get_result("AGD-12")
        assert result is not None
        assert not result.success
        assert "SDK connection lost" in result.error

        loop.close()


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


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    """Point the session registry at a throwaway path and reset its singleton.

    launch_agent now resolves the repo and registers the run synchronously, so
    these tests need an empty, isolated registry rather than the real one.
    """
    monkeypatch.setattr("modastack.sdk.REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr("modastack.sdk.SESSION_DIR", tmp_path)
    monkeypatch.setattr("modastack.sdk._registry", None)
    yield
    monkeypatch.setattr("modastack.sdk._registry", None)


@pytest.mark.usefixtures("isolated_registry")
class TestLaunchAgent:
    """Test that launch_agent launches a detached subprocess."""

    @patch("modastack.subagent._launch_detached", return_value=4242)
    def test_adhoc_returns_eng_prefix(self, mock_launch):
        from modastack.subagent import launch_agent
        name = launch_agent(task="Fix issue #42", cwd="/tmp/test")
        assert name == "eng-42"
        mock_launch.assert_called_once()

    @patch("modastack.subagent._launch_detached", return_value=4242)
    def test_adhoc_hash_without_issue(self, mock_launch):
        from modastack.subagent import launch_agent
        name = launch_agent(task="do something", cwd="/tmp/test")
        assert name.startswith("eng-adhoc-")

    @patch("modastack.subagent._launch_detached", return_value=4242)
    def test_workflow_returns_wf_prefix(self, mock_launch):
        from modastack.subagent import launch_agent
        name = launch_agent(task="Work on #42", cwd="/tmp/test", workflow_name="issue-lifecycle")
        assert name == "wf-issue-lifecycle-42"

    @patch("modastack.subagent._launch_detached", return_value=4242)
    def test_explicit_issue_binds_run_id(self, mock_launch):
        """--issue wins over a number parsed from the task text."""
        from modastack.subagent import launch_agent
        name = launch_agent(task="Run workflow issue-lifecycle", cwd="/tmp/test",
                            workflow_name="issue-lifecycle", issue="34")
        assert name == "wf-issue-lifecycle-34"
        import json
        parsed = json.loads(mock_launch.call_args[0][1][0])
        assert parsed["issue_id"] == "34"

    @patch("modastack.subagent._launch_detached", return_value=4242)
    def test_adhoc_ids_are_unique_per_invocation(self, mock_launch):
        """Two runs of the *same* task text must not collide on one run id.

        This is the root-cause regression: a task-hash run id aliased the
        second invocation onto the first run. UUID-based ids stay distinct.
        """
        from modastack.subagent import launch_agent
        a = launch_agent(task="Run workflow issue-lifecycle", cwd="/tmp/test",
                         workflow_name="issue-lifecycle")
        b = launch_agent(task="Run workflow issue-lifecycle", cwd="/tmp/test2",
                         workflow_name="issue-lifecycle")
        assert a != b

    @patch("modastack.subagent._launch_detached", return_value=4242)
    def test_subprocess_is_detached(self, mock_launch):
        from modastack.subagent import launch_agent
        launch_agent(task="Fix #1", cwd="/tmp/test")
        script = mock_launch.call_args[0][0]
        assert "_run_agent_entry" in script

    @patch("modastack.subagent._launch_detached", return_value=4242)
    def test_passes_requested_by(self, mock_launch):
        from modastack.subagent import launch_agent
        req = {"from": "Alice", "channel": "C1"}
        launch_agent(task="Fix #1", cwd="/tmp/test", requested_by=req)
        args = mock_launch.call_args[0][1]
        import json
        parsed = json.loads(args[0])
        assert parsed["requested_by"] == req

    @patch("modastack.subagent._launch_detached", return_value=4242)
    def test_registers_run_with_pid(self, mock_launch):
        """The launched run is registered synchronously with its pid so a
        second invocation for the same (repo, issue) can be rejected."""
        from modastack.subagent import launch_agent
        from modastack.sdk import get_registry
        launch_agent(task="Fix #7", cwd="/tmp/test")
        entry = get_registry().get("wf-adhoc-7")
        assert entry is not None
        assert entry.issue_id == "7"
        assert entry.pid == 4242

    # A live pid (this test process) so the duplicate guard's dead-process
    # reaping doesn't immediately retire the run we just registered.
    @patch("modastack.subagent._launch_detached", return_value=os.getpid())
    def test_rejects_duplicate_active_run(self, mock_launch):
        """A second run for an already-active (repo, issue) raises."""
        from modastack.subagent import launch_agent, RunCollision
        launch_agent(task="Fix #7", cwd="/tmp/test")
        with pytest.raises(RunCollision):
            launch_agent(task="Fix #7", cwd="/tmp/test")

    @patch("modastack.subagent._launch_detached", return_value=os.getpid())
    def test_allows_same_issue_different_repo(self, mock_launch):
        """The guard is keyed by (repo, issue) — same issue number in a
        different repo is a distinct run and must be allowed."""
        from modastack.subagent import launch_agent
        launch_agent(task="Fix #7", cwd="/tmp/repo-a")
        # Different cwd → different resolved repo → no collision.
        launch_agent(task="Fix #7", cwd="/tmp/repo-b")
        assert mock_launch.call_count == 2


@pytest.mark.usefixtures("isolated_registry")
class TestCancelRun:
    """cancel_run resolves the same ids `status`/the registry print and kills
    the detached process — the working kill switch for an in-flight run."""

    def _register(self, **kw):
        from modastack.sdk import get_registry, SessionEntry
        get_registry().register(SessionEntry(**kw))

    @patch("modastack.subagent.os.killpg")
    @patch("modastack.subagent.os.getpgid", return_value=4242)
    def test_cancel_by_session_name(self, mock_getpgid, mock_killpg):
        from modastack.subagent import cancel_run
        self._register(name="wf-issue-lifecycle-36", issue_id="36",
                       role="engineer", status="running", pid=os.getpid())
        cancelled = cancel_run("wf-issue-lifecycle-36")
        assert cancelled == ["wf-issue-lifecycle-36"]
        mock_killpg.assert_called_once()

    @patch("modastack.subagent.os.killpg")
    @patch("modastack.subagent.os.getpgid", return_value=4242)
    def test_cancel_by_issue_id(self, mock_getpgid, mock_killpg):
        from modastack.subagent import cancel_run
        from modastack.sdk import get_registry
        self._register(name="wf-issue-lifecycle-36", issue_id="36",
                       role="engineer", status="running", pid=os.getpid())
        # The id `status` prints is the issue id — that must resolve too.
        cancelled = cancel_run("36")
        assert cancelled == ["wf-issue-lifecycle-36"]
        assert get_registry().get("wf-issue-lifecycle-36").status == "cancelled"

    def test_cancel_unknown_returns_empty(self):
        from modastack.subagent import cancel_run
        assert cancel_run("nope-123") == []

    @patch("modastack.subagent.os.killpg")
    @patch("modastack.subagent.os.getpgid", return_value=4242)
    def test_cancel_skips_terminal_runs(self, mock_getpgid, mock_killpg):
        from modastack.subagent import cancel_run
        self._register(name="wf-x-1", issue_id="1", role="engineer",
                       status="done", pid=os.getpid())
        assert cancel_run("1") == []
        mock_killpg.assert_not_called()

    @patch("modastack.subagent.os.killpg")
    @patch("modastack.subagent.os.getpgid", return_value=4242)
    def test_cancel_waiting_run(self, mock_getpgid, mock_killpg):
        """A run suspended at an approval gate is still cancellable."""
        from modastack.subagent import cancel_run
        self._register(name="wf-x-2", issue_id="2", role="engineer",
                       status="waiting", pid=os.getpid())
        assert cancel_run("2") == ["wf-x-2"]


