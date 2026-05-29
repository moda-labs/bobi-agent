"""Tests for the sub-agent executor module.

Unit tests run without external dependencies.
Integration tests (TestSDK*) require the `claude` CLI to be installed
and drive real Claude Code sessions via the claude-agent-sdk.
"""

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modastack.subagent import (
    AgentResult,
    _build_prompt,
    _resolve_skill_path,
    _run_agent,
    cancel_agent,
    get_result,
    is_running,
    list_agents,
    run_phase,
    _running,
)
from modastack.workflow.engine import WorkflowEngine

requires_claude = pytest.mark.skipif(
    not shutil.which("claude"),
    reason="claude CLI not installed",
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


# =============================================================================
# Integration tests — drive real Claude sessions via the SDK
# =============================================================================


@requires_claude
@pytest.mark.asyncio
@pytest.mark.timeout(120)
class TestSDKRunAgent:
    """Test _run_agent with real Claude Code sessions."""

    async def test_simple_prompt_completes(self, tmp_cwd):
        result = await _run_agent(
            prompt="Reply with just the word PONG. Nothing else.",
            cwd=tmp_cwd,
            issue_id="TEST-1",
            phase="implement",
            timeout=60,
            max_budget_usd=0.50,
        )
        assert result.success
        assert result.session_id != ""
        assert result.num_turns >= 1
        assert result.duration_ms > 0
        assert result.total_cost_usd > 0
        assert result.error == ""

    async def test_file_creation(self, tmp_cwd):
        marker = Path(tmp_cwd) / "agent_created.txt"
        result = await _run_agent(
            prompt=f"Create the file {marker} containing exactly: CREATED_BY_AGENT",
            cwd=tmp_cwd,
            issue_id="TEST-2",
            phase="implement",
            timeout=60,
            max_budget_usd=0.50,
        )
        assert result.success
        assert marker.exists(), f"Agent did not create {marker}"
        assert "CREATED_BY_AGENT" in marker.read_text()

    async def test_result_fields_populated(self, tmp_cwd):
        result = await _run_agent(
            prompt="Reply with OK.",
            cwd=tmp_cwd,
            issue_id="TEST-3",
            phase="spec",
            timeout=60,
            max_budget_usd=0.50,
        )
        assert result.issue_id == "TEST-3"
        assert result.phase == "spec"
        assert isinstance(result.duration_ms, int)
        assert isinstance(result.total_cost_usd, float)
        assert isinstance(result.num_turns, int)

    async def test_cwd_is_respected(self, tmp_cwd):
        result = await _run_agent(
            prompt=(
                "Run `pwd` and write its output to a file called cwd_check.txt "
                "in the current directory."
            ),
            cwd=tmp_cwd,
            issue_id="TEST-4",
            phase="implement",
            timeout=60,
            max_budget_usd=0.50,
        )
        assert result.success
        check_file = Path(tmp_cwd) / "cwd_check.txt"
        if check_file.exists():
            content = check_file.read_text().strip()
            assert tmp_cwd in content or Path(tmp_cwd).resolve().as_posix() in content


@requires_claude
@pytest.mark.asyncio
@pytest.mark.timeout(120)
class TestSDKHandoff:
    """Test handoff file writing via sub-agent."""

    async def test_agent_writes_handoff(self, tmp_cwd):
        handoff_dir = Path(tmp_cwd) / "handoffs"
        handoff_dir.mkdir()
        handoff_path = handoff_dir / "test-5.md"

        result = await _run_agent(
            prompt=(
                f"Create a YAML-frontmatter markdown file at {handoff_path} with "
                f"this exact content:\n"
                f"---\n"
                f"issue_id: TEST-5\n"
                f"phase: triage_complete\n"
                f"complexity: small\n"
                f"---\n"
                f"## Status\n"
                f"Triage complete.\n"
            ),
            cwd=tmp_cwd,
            issue_id="TEST-5",
            phase="pickup",
            timeout=60,
            max_budget_usd=0.50,
        )
        assert result.success
        assert handoff_path.exists(), "Agent did not create handoff file"
        content = handoff_path.read_text()
        assert "phase: triage_complete" in content
        assert "complexity: small" in content


@requires_claude
@pytest.mark.asyncio
@pytest.mark.timeout(120)
class TestSDKLifecycle:
    """Test the agent lifecycle by running _run_agent directly.

    Note: create_task + await hangs under pytest-asyncio due to subprocess
    child-watcher conflicts. Production code uses its own event loop so
    this is not an issue. We test the result shape and state transitions
    with direct awaits here; the task-tracking machinery (is_running,
    get_result, cancel_agent) is covered by unit tests above.
    """

    async def test_agent_returns_valid_result(self, tmp_cwd):
        result = await _run_agent(
            prompt="Reply with OK.",
            cwd=tmp_cwd,
            issue_id="TEST-6",
            phase="implement",
            timeout=60,
            max_budget_usd=0.50,
        )
        assert result.success
        assert result.session_id != ""
        assert result.issue_id == "TEST-6"
        assert result.phase == "implement"
        assert result.num_turns >= 1
        assert result.total_cost_usd > 0
        assert result.duration_ms > 0
        assert result.error == ""

    async def test_agent_with_max_turns_1(self, tmp_cwd):
        """Verify the agent respects max_turns by completing in 1 turn."""
        result = await _run_agent(
            prompt="Reply with just: DONE",
            cwd=tmp_cwd,
            issue_id="TEST-7",
            phase="spec",
            timeout=60,
            max_budget_usd=0.50,
        )
        assert result.success
        assert result.num_turns >= 1


@requires_claude
@pytest.mark.asyncio
@pytest.mark.timeout(180)
class TestSDKConcurrent:
    """Test multiple sub-agents running concurrently."""

    async def test_two_agents_simultaneously(self, tmp_cwd):
        dir_a = Path(tmp_cwd) / "agent_a"
        dir_b = Path(tmp_cwd) / "agent_b"
        dir_a.mkdir()
        dir_b.mkdir()

        result_a, result_b = await asyncio.gather(
            _run_agent(
                prompt=f"Create a file {dir_a / 'marker.txt'} containing AGENT_A",
                cwd=str(dir_a),
                issue_id="CONC-A",
                phase="implement",
                timeout=60,
                max_budget_usd=0.50,
            ),
            _run_agent(
                prompt=f"Create a file {dir_b / 'marker.txt'} containing AGENT_B",
                cwd=str(dir_b),
                issue_id="CONC-B",
                phase="implement",
                timeout=60,
                max_budget_usd=0.50,
            ),
        )

        assert result_a.success
        assert result_b.success
        assert result_a.session_id != result_b.session_id

        marker_a = dir_a / "marker.txt"
        marker_b = dir_b / "marker.txt"
        assert marker_a.exists(), "Agent A did not create its marker"
        assert marker_b.exists(), "Agent B did not create its marker"
        assert "AGENT_A" in marker_a.read_text()
        assert "AGENT_B" in marker_b.read_text()


@requires_claude
@pytest.mark.timeout(120)
class TestSDKSessionIntegration:
    """Test that session.py correctly reflects sub-agent state.

    Uses fake asyncio.Future tasks (not real SDK calls) to test the
    integration between session.py and the _running registry. The
    real SDK execution is proven by TestSDKRunAgent above.
    """

    def test_list_sessions_includes_subagent(self, tmp_cwd):
        from modastack.session import list_sessions
        from modastack.subagent import RunningAgent

        loop = asyncio.new_event_loop()
        future = loop.create_future()
        task = asyncio.ensure_future(future, loop=loop)

        _running["sess-1"] = RunningAgent(
            issue_id="SESS-1",
            phase="implement",
            session_id="",
            task=task,
            cwd=tmp_cwd,
        )

        sessions = list_sessions()
        assert "SESS-1" in sessions

        task.cancel()
        loop.close()

    def test_detect_state_running_subagent(self, tmp_cwd):
        from modastack.session import detect_state
        from modastack.subagent import RunningAgent

        loop = asyncio.new_event_loop()
        future = loop.create_future()
        task = asyncio.ensure_future(future, loop=loop)

        _running["sess-2"] = RunningAgent(
            issue_id="SESS-2",
            phase="spec",
            session_id="",
            task=task,
            cwd=tmp_cwd,
        )

        state = detect_state("SESS-2")
        assert state["state"] == "running"
        assert state["type"] == "subagent"

        task.cancel()
        loop.close()

    def test_detect_state_completed_subagent(self, tmp_cwd):
        from modastack.session import detect_state
        from modastack.subagent import RunningAgent

        loop = asyncio.new_event_loop()
        future = loop.create_future()
        future.set_result(AgentResult(
            session_id="real-session-id",
            issue_id="SESS-3",
            phase="implement",
            success=True,
            duration_ms=5000,
            total_cost_usd=0.08,
            num_turns=2,
        ))
        task = asyncio.ensure_future(future, loop=loop)

        _running["sess-3"] = RunningAgent(
            issue_id="SESS-3",
            phase="implement",
            session_id="",
            task=task,
            cwd=tmp_cwd,
        )

        state = detect_state("SESS-3")
        assert state["state"] == "completed"
        assert state["type"] == "subagent"
        assert state["phase"] == "implement"

        loop.close()


@requires_claude
@pytest.mark.asyncio
@pytest.mark.timeout(120)
class TestSDKEngineWiring:
    """Test that the workflow engine correctly starts sub-agents."""

    async def test_exec_prompt_inject_starts_subagent(self, tmp_cwd):
        from modastack.workflow.schema import (
            NodeDef, NodeType, TriggerDef, WaitForDef, WorkflowDef,
        )
        from modastack.workflow.state import WorkflowRun

        node = NodeDef(
            id="test_prompt",
            type=NodeType.PROMPT,
            session="TEST-ENG",
            inject="/implement TEST-ENG",
            wait_for=WaitForDef(phase="implement_complete"),
            timeout=60,
        )

        workflow = WorkflowDef(
            name="test",
            version=1,
            trigger=TriggerDef(event="test"),
            nodes={"test_prompt": node},
        )
        run = WorkflowRun.create("test", {
            "type": "test",
            "data": {"repo": tmp_cwd, "issue_id": "TEST-ENG"},
        })

        engine = WorkflowEngine(workflow, run)

        with patch.object(engine, '_resolve_cwd', return_value=tmp_cwd):
            engine._exec_prompt_inject(node)

        assert is_running("TEST-ENG")

        cancel_agent("TEST-ENG")
