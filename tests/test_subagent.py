"""Tests for the sub-agent executor module — unit tests only.

For blocking execution and SDK interaction tests, see test_subagent_blocking.py.
"""

import asyncio
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
from modastack.workflow.engine import WorkflowEngine


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


class TestDetectPhase:
    def _detect(self, text):
        return WorkflowEngine._detect_phase(None, text)

    def test_pickup(self):
        assert self._detect("/pickup Issue #AGD-12") == "pickup"

    def test_spec(self):
        assert self._detect("/spec AGD-12") == "spec"

    def test_implement(self):
        assert self._detect("/implement AGD-12") == "implement"

    def test_prepare_pr(self):
        assert self._detect("/prepare-pr") == "prepare-pr"

    def test_feedback(self):
        assert self._detect("/feedback address review comments") == "feedback"

    def test_fallback(self):
        assert self._detect("do some work") == "implement"

    def test_case_insensitive(self):
        assert self._detect("/PICKUP Issue #AGD-12") == "pickup"


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


class TestSpawnBackground:
    """Test that spawn_adhoc_background launches a detached subprocess."""

    @patch("modastack.subagent._launch_detached")
    def test_returns_session_name_with_issue_id(self, mock_launch):
        from modastack.subagent import spawn_adhoc_background
        name = spawn_adhoc_background(cwd="/tmp/test", task="Fix issue #42")
        assert name == "eng-42"
        mock_launch.assert_called_once()

    @patch("modastack.subagent._launch_detached")
    def test_returns_adhoc_hash_without_issue(self, mock_launch):
        from modastack.subagent import spawn_adhoc_background
        name = spawn_adhoc_background(cwd="/tmp/test", task="do something")
        assert name.startswith("eng-adhoc-")
        mock_launch.assert_called_once()

    @patch("modastack.subagent._launch_detached")
    def test_script_calls_spawn_adhoc(self, mock_launch):
        from modastack.subagent import spawn_adhoc_background
        spawn_adhoc_background(cwd="/tmp/repo", task="Fix #5", timeout=600)
        script = mock_launch.call_args[0][0]
        assert "spawn_adhoc" in script

    @patch("modastack.subagent._launch_detached")
    def test_passes_requested_by(self, mock_launch):
        from modastack.subagent import spawn_adhoc_background
        req = {"from": "Alice", "channel": "C1"}
        spawn_adhoc_background(cwd="/tmp/test", task="Fix #1", requested_by=req)
        args = mock_launch.call_args[0][1]
        import json
        parsed = json.loads(args[0])
        assert parsed["requested_by"] == req


class TestLaunchWorkflowBackground:
    """Test that launch_workflow_background launches a detached subprocess."""

    @patch("modastack.subagent._launch_detached")
    def test_returns_session_name(self, mock_launch):
        from modastack.subagent import launch_workflow_background
        name = launch_workflow_background("issue-lifecycle", {"data": {"issue_id": "42"}})
        assert name == "wf-issue-lifecycle-42"
        mock_launch.assert_called_once()

    @patch("modastack.subagent._launch_detached")
    def test_script_uses_dispatcher(self, mock_launch):
        from modastack.subagent import launch_workflow_background
        launch_workflow_background("build-failure", {"data": {"issue_id": "1"}})
        script = mock_launch.call_args[0][0]
        assert "WorkflowDispatcher" in script
        assert "run_by_name" in script

    @patch("modastack.subagent._launch_detached")
    def test_passes_name_and_event_as_args(self, mock_launch):
        from modastack.subagent import launch_workflow_background
        event = {"data": {"issue_id": "99", "repo": "moda-labs/jobtack"}}
        launch_workflow_background("issue-lifecycle", event)
        args = mock_launch.call_args[0][1]
        assert args[0] == "issue-lifecycle"
        import json
        assert json.loads(args[1]) == event


class TestEngineIntegration:
    """Test that the workflow engine correctly uses sub-agents."""

    def test_detect_phase_from_inject_text(self):
        detect = lambda text: WorkflowEngine._detect_phase(None, text)
        assert detect("/pickup Issue #AGD-12: Auth flow") == "pickup"
        assert detect("/implement AGD-12") == "implement"
        assert detect("/prepare-pr") == "prepare-pr"
