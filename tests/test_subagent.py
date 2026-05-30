"""Tests for the sub-agent executor module — unit tests only.

For blocking execution and SDK interaction tests, see test_subagent_blocking.py.
"""

import asyncio
import tempfile
import shutil
from unittest.mock import MagicMock, patch

import pytest

from modastack.subagent import (
    AgentResult,
    _build_prompt,
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


class TestEngineIntegration:
    """Test that the workflow engine correctly uses sub-agents."""

    def test_detect_phase_from_inject_text(self):
        detect = lambda text: WorkflowEngine._detect_phase(None, text)
        assert detect("/pickup Issue #AGD-12: Auth flow") == "pickup"
        assert detect("/implement AGD-12") == "implement"
        assert detect("/prepare-pr") == "prepare-pr"
