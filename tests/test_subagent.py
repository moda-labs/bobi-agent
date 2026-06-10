"""Tests for the sub-agent executor module — unit tests only.

For blocking execution and SDK interaction tests, see test_subagent_blocking.py.
"""

import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modastack.sdk import SessionEntry
from modastack.subagent import (
    AgentResult,
    _build_prompt,
    _parse_issue_number,
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

    def test_find_agent_by_issue_id(self):
        entry = SessionEntry(name="eng-agd-12-implement", issue_id="AGD-12",
                             phase="implement", status="running", pid=0)
        with patch("modastack.subagent.get_registry",
                   return_value=_mock_registry([entry])):
            found = find_agent("AGD-12")
            assert found is not None
            assert found.name == "eng-agd-12-implement"

    def test_find_agent_by_session_name(self):
        entry = SessionEntry(name="eng-agd-12-implement", issue_id="AGD-12",
                             phase="implement", status="running", pid=0)
        with patch("modastack.subagent.get_registry",
                   return_value=_mock_registry([entry])):
            assert find_agent("eng-agd-12-implement") is entry

    def test_find_agent_prefers_active(self):
        done = SessionEntry(name="eng-agd-12-spec", issue_id="AGD-12",
                            phase="spec", status="done", pid=0)
        active = SessionEntry(name="eng-agd-12-implement", issue_id="AGD-12",
                              phase="implement", status="running", pid=0)
        with patch("modastack.subagent.get_registry",
                   return_value=_mock_registry([done, active])):
            assert find_agent("AGD-12") is active

    def test_list_agents_excludes_managers(self):
        mgr = SessionEntry(name="moda-director-x", role="manager",
                           status="running", pid=0)
        eng = SessionEntry(name="eng-1-implement", issue_id="1",
                           phase="implement", status="running", pid=0)
        with patch("modastack.subagent.get_registry",
                   return_value=_mock_registry([mgr, eng])):
            names = [a["name"] for a in list_agents()]
            assert names == ["eng-1-implement"]

    def test_cancel_running_agent_updates_registry(self):
        entry = SessionEntry(name="eng-agd-12-implement", issue_id="AGD-12",
                             phase="implement", status="running", pid=0)
        registry = _mock_registry([entry])
        with patch("modastack.subagent.get_registry", return_value=registry):
            assert cancel_agent("AGD-12")
        registry.update.assert_called_once_with(
            "eng-agd-12-implement", status="cancelled", pid=0)

    def test_cancel_done_agent_returns_false(self):
        entry = SessionEntry(name="eng-agd-12-implement", issue_id="AGD-12",
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


class TestLaunchAgent:
    """Test that launch_agent launches a detached subprocess."""

    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_returns_deterministic_name(self, mock_launch, mock_reg):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from modastack.subagent import launch_agent
        name = launch_agent(task="Fix issue #42", cwd="/tmp/test", workflow_name="adhoc")
        assert "adhoc" in name
        assert "42" in name
        mock_launch.assert_called_once()

    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_subprocess_calls_entry(self, mock_launch, mock_reg):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from modastack.subagent import launch_agent
        launch_agent(task="Fix #1", cwd="/tmp/test", workflow_name="adhoc")
        script = mock_launch.call_args[0][0]
        assert "_run_agent_entry" in script

    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_rejects_active_run(self, mock_launch, mock_reg):
        active = MagicMock()
        active.status = "running"
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=active))
        from modastack.subagent import launch_agent
        with pytest.raises(RuntimeError, match="already active"):
            launch_agent(task="Fix #1", cwd="/tmp/test", workflow_name="adhoc")

    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_allows_after_done(self, mock_launch, mock_reg):
        done = MagicMock()
        done.status = "done"
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=done))
        from modastack.subagent import launch_agent
        name = launch_agent(task="Fix #1", cwd="/tmp/test", workflow_name="adhoc")
        assert name  # no exception

    @patch("modastack.subagent.get_registry")
    @patch("modastack.subagent._launch_detached")
    def test_passes_requested_by(self, mock_launch, mock_reg):
        mock_reg.return_value = MagicMock(get=MagicMock(return_value=None))
        from modastack.subagent import launch_agent
        req = {"from": "Alice", "channel": "C1"}
        launch_agent(task="Fix #1", cwd="/tmp/test", workflow_name="adhoc", requested_by=req)
        args = mock_launch.call_args[0][1]
        import json
        parsed = json.loads(args[0])
        assert parsed["requested_by"] == req


