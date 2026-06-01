"""Thorough unit tests for run_phase_blocking and _run_agent_supervised.

All tests mock the Claude SDK — no real Claude sessions.
Tests cover: normal completion, AskUserQuestion deferral + routing,
timeout, errors, resume from saved session, cost accumulation across
deferred rounds, connection loss, and the defer hook itself.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from modastack.subagent import (
    AgentResult,
    CheckResult,
    InputHandler,
    _build_check_prompt,
    _build_prompt,
    _make_defer_hook,
    _parse_check_output,
    _run_agent_supervised,
    _session_name,
    run_check_blocking,
    run_phase_blocking,
)


# ---------------------------------------------------------------------------
# Fake SDK types — mirror just enough structure for testing
# ---------------------------------------------------------------------------

@dataclass
class FakeTextBlock:
    text: str


@dataclass
class FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class FakeAssistantMessage:
    content: list
    model: str = "claude-test"


@dataclass
class FakeDeferredToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class FakeResultMessage:
    subtype: str = "success"
    duration_ms: int = 1000
    duration_api_ms: int = 800
    is_error: bool = False
    num_turns: int = 5
    session_id: str = "sess-abc"
    total_cost_usd: float | None = 0.10
    result: str | None = None
    deferred_tool_use: FakeDeferredToolUse | None = None
    stop_reason: str | None = None
    usage: dict | None = None


# ---------------------------------------------------------------------------
# Helper: builds a fake client whose receive_response yields given messages
# ---------------------------------------------------------------------------

class FakeClient:
    """Mimics ClaudeSDKClient with controllable message sequences."""

    def __init__(self, rounds: list[list]):
        """rounds: list of message-lists. Each round is one receive_response() call."""
        self._rounds = list(rounds)
        self._round_idx = 0
        self.connected = False
        self.queries: list[str] = []
        self.disconnected = False
        self._connect_prompt: str | None = None

    async def connect(self, prompt=None):
        self.connected = True
        self._connect_prompt = prompt

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

    async def receive_response(self):
        if self._round_idx >= len(self._rounds):
            return
        msgs = self._rounds[self._round_idx]
        self._round_idx += 1
        for msg in msgs:
            yield msg

    async def disconnect(self):
        self.disconnected = True


# ---------------------------------------------------------------------------
# Patch targets
# ---------------------------------------------------------------------------

SDK_PATCH = "modastack.subagent"


# ---------------------------------------------------------------------------
# Tests: _make_defer_hook
# ---------------------------------------------------------------------------

class TestMakeDeferHook:
    def test_returns_pre_tool_use_dict(self):
        hooks = _make_defer_hook()
        assert "PreToolUse" in hooks
        assert len(hooks["PreToolUse"]) == 1
        matcher = hooks["PreToolUse"][0]
        assert matcher.matcher == "AskUserQuestion"
        assert len(matcher.hooks) == 1

    @pytest.mark.asyncio
    async def test_hook_returns_defer_decision(self):
        hooks = _make_defer_hook()
        hook_fn = hooks["PreToolUse"][0].hooks[0]
        result = await hook_fn({}, "tool-123", {})
        assert result["hookSpecificOutput"]["permissionDecision"] == "defer"
        assert result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


# ---------------------------------------------------------------------------
# Tests: _run_agent_supervised — normal completion
# ---------------------------------------------------------------------------

class TestRunAgentSupervisedNormal:
    @pytest.mark.asyncio
    async def test_normal_completion(self):
        """Agent runs, produces messages, completes successfully."""
        messages = [
            FakeAssistantMessage(content=[FakeTextBlock("Working on it...")]),
            FakeResultMessage(session_id="sess-1", duration_ms=2000,
                              total_cost_usd=0.15, num_turns=3),
        ]
        client = FakeClient(rounds=[messages])

        mock_module = MagicMock()
        mock_module.AssistantMessage = FakeAssistantMessage
        mock_module.ClaudeAgentOptions = MagicMock()
        mock_module.ClaudeSDKClient = MagicMock(return_value=client)
        mock_module.ResultMessage = FakeResultMessage
        mock_module.TextBlock = FakeTextBlock

        with patch(f"{SDK_PATCH}.load_session_id", return_value=""), \
             patch(f"{SDK_PATCH}.save_session_id"), \
             patch(f"{SDK_PATCH}.log_activity"), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()), \
             patch(f"{SDK_PATCH}.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Do the thing",
                cwd="/tmp/test",
                issue_id="TEST-1",
                phase="implement",
                timeout=60,
            )

        assert result.success is True
        assert result.session_id == "sess-1"
        assert result.duration_ms == 2000
        assert result.total_cost_usd == 0.15
        assert result.num_turns == 3
        assert result.error == ""
        assert client.connected
        assert client.disconnected

    @pytest.mark.asyncio
    async def test_error_completion(self):
        """Agent runs and reports an error."""
        messages = [
            FakeResultMessage(session_id="sess-2", is_error=True,
                              result="file not found", duration_ms=500,
                              total_cost_usd=0.05, num_turns=1),
        ]
        client = FakeClient(rounds=[messages])

        mock_module = MagicMock()
        mock_module.AssistantMessage = FakeAssistantMessage
        mock_module.ClaudeAgentOptions = MagicMock()
        mock_module.ClaudeSDKClient = MagicMock(return_value=client)
        mock_module.ResultMessage = FakeResultMessage
        mock_module.TextBlock = FakeTextBlock

        with patch(f"{SDK_PATCH}.load_session_id", return_value=""), \
             patch(f"{SDK_PATCH}.save_session_id"), \
             patch(f"{SDK_PATCH}.log_activity"), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()), \
             patch(f"{SDK_PATCH}.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Do the thing",
                cwd="/tmp/test",
                issue_id="TEST-2",
                phase="spec",
                timeout=60,
            )

        assert result.success is False
        assert result.error == "file not found"
        assert result.session_id == "sess-2"

    @pytest.mark.asyncio
    async def test_connection_lost(self):
        """receive_response yields nothing (no ResultMessage)."""
        client = FakeClient(rounds=[[]])

        mock_module = MagicMock()
        mock_module.AssistantMessage = FakeAssistantMessage
        mock_module.ClaudeAgentOptions = MagicMock()
        mock_module.ClaudeSDKClient = MagicMock(return_value=client)
        mock_module.ResultMessage = FakeResultMessage
        mock_module.TextBlock = FakeTextBlock

        with patch(f"{SDK_PATCH}.load_session_id", return_value=""), \
             patch(f"{SDK_PATCH}.save_session_id"), \
             patch(f"{SDK_PATCH}.log_activity"), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()), \
             patch(f"{SDK_PATCH}.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Do the thing",
                cwd="/tmp/test",
                issue_id="TEST-3",
                phase="implement",
                timeout=60,
            )

        assert result.success is False
        assert "connection lost" in result.error


# ---------------------------------------------------------------------------
# Tests: _run_agent_supervised — AskUserQuestion deferral
# ---------------------------------------------------------------------------

class TestRunAgentSupervisedDeferral:
    @pytest.mark.asyncio
    async def test_single_deferral(self):
        """Agent asks one question, gets answer, completes."""
        round1 = [
            FakeAssistantMessage(content=[FakeTextBlock("Checking...")]),
            FakeResultMessage(
                session_id="sess-d1", duration_ms=1000,
                total_cost_usd=0.05, num_turns=2,
                deferred_tool_use=FakeDeferredToolUse(
                    id="tu-1", name="AskUserQuestion",
                    input={"question": "Which database?",
                           "options": [{"label": "Postgres"}, {"label": "SQLite"}]},
                ),
            ),
        ]
        round2 = [
            FakeAssistantMessage(content=[FakeTextBlock("Using Postgres...")]),
            FakeResultMessage(session_id="sess-d1", duration_ms=3000,
                              total_cost_usd=0.20, num_turns=5),
        ]
        client = FakeClient(rounds=[round1, round2])

        def handler(tool_name: str, tool_input: dict) -> str:
            assert tool_name == "AskUserQuestion"
            assert "database" in tool_input["question"].lower()
            return "Use Postgres"

        mock_module = MagicMock()
        mock_module.AssistantMessage = FakeAssistantMessage
        mock_module.ClaudeAgentOptions = MagicMock()
        mock_module.ClaudeSDKClient = MagicMock(return_value=client)
        mock_module.ResultMessage = FakeResultMessage
        mock_module.TextBlock = FakeTextBlock
        mock_module.HookMatcher = MagicMock()

        with patch(f"{SDK_PATCH}.load_session_id", return_value=""), \
             patch(f"{SDK_PATCH}.save_session_id"), \
             patch(f"{SDK_PATCH}.log_activity"), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()), \
             patch(f"{SDK_PATCH}.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Build the feature",
                cwd="/tmp/test",
                issue_id="TEST-D1",
                phase="implement",
                timeout=120,
                on_input_needed=handler,
            )

        assert result.success is True
        assert result.duration_ms == 4000  # 1000 + 3000
        assert result.total_cost_usd == pytest.approx(0.25)
        assert result.num_turns == 7  # 2 + 5
        assert client.queries == ["Use Postgres"]

    @pytest.mark.asyncio
    async def test_multiple_deferrals(self):
        """Agent asks two questions before completing."""
        round1 = [
            FakeResultMessage(
                session_id="sess-m1", duration_ms=500,
                total_cost_usd=0.02, num_turns=1,
                deferred_tool_use=FakeDeferredToolUse(
                    id="tu-1", name="AskUserQuestion",
                    input={"question": "Framework?"},
                ),
            ),
        ]
        round2 = [
            FakeResultMessage(
                session_id="sess-m1", duration_ms=500,
                total_cost_usd=0.02, num_turns=1,
                deferred_tool_use=FakeDeferredToolUse(
                    id="tu-2", name="AskUserQuestion",
                    input={"question": "ORM?"},
                ),
            ),
        ]
        round3 = [
            FakeResultMessage(session_id="sess-m1", duration_ms=2000,
                              total_cost_usd=0.10, num_turns=4),
        ]
        client = FakeClient(rounds=[round1, round2, round3])

        answers = []
        def handler(tool_name, tool_input):
            answers.append(tool_input["question"])
            if "Framework" in tool_input["question"]:
                return "FastAPI"
            return "SQLAlchemy"

        mock_module = MagicMock()
        mock_module.AssistantMessage = FakeAssistantMessage
        mock_module.ClaudeAgentOptions = MagicMock()
        mock_module.ClaudeSDKClient = MagicMock(return_value=client)
        mock_module.ResultMessage = FakeResultMessage
        mock_module.TextBlock = FakeTextBlock
        mock_module.HookMatcher = MagicMock()

        with patch(f"{SDK_PATCH}.load_session_id", return_value=""), \
             patch(f"{SDK_PATCH}.save_session_id"), \
             patch(f"{SDK_PATCH}.log_activity"), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()), \
             patch(f"{SDK_PATCH}.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Build it",
                cwd="/tmp/test",
                issue_id="TEST-M1",
                phase="implement",
                timeout=120,
                on_input_needed=handler,
            )

        assert result.success is True
        assert result.duration_ms == 3000
        assert result.total_cost_usd == pytest.approx(0.14)
        assert result.num_turns == 6
        assert answers == ["Framework?", "ORM?"]
        assert client.queries == ["FastAPI", "SQLAlchemy"]

    @pytest.mark.asyncio
    async def test_deferral_without_handler_ignored(self):
        """Deferred tool use without on_input_needed completes normally."""
        messages = [
            FakeResultMessage(
                session_id="sess-no-handler", duration_ms=1000,
                total_cost_usd=0.05, num_turns=2,
                deferred_tool_use=FakeDeferredToolUse(
                    id="tu-x", name="AskUserQuestion",
                    input={"question": "Which one?"},
                ),
            ),
        ]
        client = FakeClient(rounds=[messages])

        mock_module = MagicMock()
        mock_module.AssistantMessage = FakeAssistantMessage
        mock_module.ClaudeAgentOptions = MagicMock()
        mock_module.ClaudeSDKClient = MagicMock(return_value=client)
        mock_module.ResultMessage = FakeResultMessage
        mock_module.TextBlock = FakeTextBlock

        with patch(f"{SDK_PATCH}.load_session_id", return_value=""), \
             patch(f"{SDK_PATCH}.save_session_id"), \
             patch(f"{SDK_PATCH}.log_activity"), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()), \
             patch(f"{SDK_PATCH}.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Do it",
                cwd="/tmp/test",
                issue_id="TEST-NH",
                phase="implement",
                timeout=60,
                on_input_needed=None,
            )

        # Without handler, deferred tool use falls through to normal completion.
        # is_error defaults to False in FakeResultMessage, so success=True.
        assert result.success is True
        # The key thing: no crash, no infinite loop


# ---------------------------------------------------------------------------
# Tests: _run_agent_supervised — session resume
# ---------------------------------------------------------------------------

class TestRunAgentSupervisedResume:
    @pytest.mark.asyncio
    async def test_resumes_existing_session(self):
        """When a saved session ID exists, connect without prompt + send via query."""
        messages = [
            FakeResultMessage(session_id="sess-resumed", duration_ms=1000,
                              total_cost_usd=0.05, num_turns=2),
        ]
        client = FakeClient(rounds=[messages])

        mock_module = MagicMock()
        mock_module.AssistantMessage = FakeAssistantMessage
        mock_module.ClaudeAgentOptions = MagicMock()
        mock_module.ClaudeSDKClient = MagicMock(return_value=client)
        mock_module.ResultMessage = FakeResultMessage
        mock_module.TextBlock = FakeTextBlock

        with patch(f"{SDK_PATCH}.load_session_id", return_value="old-sess-id"), \
             patch(f"{SDK_PATCH}.save_session_id"), \
             patch(f"{SDK_PATCH}.log_activity"), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()), \
             patch(f"{SDK_PATCH}.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Continue working",
                cwd="/tmp/test",
                issue_id="TEST-R1",
                phase="implement",
                timeout=60,
            )

        assert result.success is True
        # With a saved session, connect is called with prompt=None
        assert client._connect_prompt is None
        # And the prompt is sent via query
        assert client.queries == ["Continue working"]


# ---------------------------------------------------------------------------
# Tests: _run_agent_supervised — exception handling
# ---------------------------------------------------------------------------

class TestRunAgentSupervisedExceptions:
    @pytest.mark.asyncio
    async def test_sdk_exception(self):
        """SDK raises an exception during connect."""
        mock_module = MagicMock()
        mock_module.AssistantMessage = FakeAssistantMessage
        mock_module.ClaudeAgentOptions = MagicMock()
        mock_module.ResultMessage = FakeResultMessage
        mock_module.TextBlock = FakeTextBlock

        bad_client = MagicMock()
        bad_client.connect = AsyncMock(side_effect=RuntimeError("CLI crashed"))
        bad_client.disconnect = AsyncMock()

        mock_module.ClaudeSDKClient = MagicMock(return_value=bad_client)

        with patch(f"{SDK_PATCH}.load_session_id", return_value=""), \
             patch(f"{SDK_PATCH}.save_session_id"), \
             patch(f"{SDK_PATCH}.log_activity"), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()), \
             patch(f"{SDK_PATCH}.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Do it",
                cwd="/tmp/test",
                issue_id="TEST-EX",
                phase="implement",
                timeout=60,
            )

        assert result.success is False
        assert "CLI crashed" in result.error
        bad_client.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_exception_swallowed(self):
        """Exception during disconnect is swallowed."""
        messages = [
            FakeResultMessage(session_id="sess-dc", duration_ms=100,
                              total_cost_usd=0.01, num_turns=1),
        ]

        class BadDisconnectClient(FakeClient):
            async def disconnect(self):
                raise RuntimeError("disconnect failed")

        client = BadDisconnectClient(rounds=[messages])

        mock_module = MagicMock()
        mock_module.AssistantMessage = FakeAssistantMessage
        mock_module.ClaudeAgentOptions = MagicMock()
        mock_module.ClaudeSDKClient = MagicMock(return_value=client)
        mock_module.ResultMessage = FakeResultMessage
        mock_module.TextBlock = FakeTextBlock

        with patch(f"{SDK_PATCH}.load_session_id", return_value=""), \
             patch(f"{SDK_PATCH}.save_session_id"), \
             patch(f"{SDK_PATCH}.log_activity"), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()), \
             patch(f"{SDK_PATCH}.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Do it",
                cwd="/tmp/test",
                issue_id="TEST-DC",
                phase="implement",
                timeout=60,
            )

        # Should succeed despite disconnect failure
        assert result.success is True


# ---------------------------------------------------------------------------
# Tests: _run_agent_supervised — registry/activity tracking
# ---------------------------------------------------------------------------

class TestRunAgentSupervisedTracking:
    @pytest.mark.asyncio
    async def test_registry_updated_on_completion(self):
        """Registry is updated to 'done' on successful completion."""
        messages = [
            FakeResultMessage(session_id="sess-track", duration_ms=100,
                              total_cost_usd=0.01, num_turns=1),
        ]
        client = FakeClient(rounds=[messages])
        mock_registry = MagicMock()

        mock_module = MagicMock()
        mock_module.AssistantMessage = FakeAssistantMessage
        mock_module.ClaudeAgentOptions = MagicMock()
        mock_module.ClaudeSDKClient = MagicMock(return_value=client)
        mock_module.ResultMessage = FakeResultMessage
        mock_module.TextBlock = FakeTextBlock

        with patch(f"{SDK_PATCH}.load_session_id", return_value=""), \
             patch(f"{SDK_PATCH}.save_session_id") as mock_save, \
             patch(f"{SDK_PATCH}.log_activity") as mock_log, \
             patch(f"{SDK_PATCH}.get_registry", return_value=mock_registry), \
             patch(f"{SDK_PATCH}.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Track me",
                cwd="/tmp/test",
                issue_id="TEST-T1",
                phase="spec",
                timeout=60,
            )

        # Registry updated to running, then done
        mock_registry.update.assert_any_call(
            "eng-test-t1-spec", status="running", phase="spec", session_id="",
        )
        mock_registry.update.assert_any_call(
            "eng-test-t1-spec", status="done", phase="spec",
            session_id="sess-track",
        )

        # Session ID saved
        mock_save.assert_called_with("eng-test-t1-spec", "sess-track")

        # Activity logged
        mock_log.assert_any_call("stop", {"session_id": "sess-track"},
                                 session="eng-test-t1-spec")

    @pytest.mark.asyncio
    async def test_registry_updated_on_error(self):
        """Registry is updated to 'error' on exception."""
        mock_module = MagicMock()
        mock_module.AssistantMessage = FakeAssistantMessage
        mock_module.ClaudeAgentOptions = MagicMock()
        mock_module.ResultMessage = FakeResultMessage
        mock_module.TextBlock = FakeTextBlock

        bad_client = MagicMock()
        bad_client.connect = AsyncMock(side_effect=RuntimeError("boom"))
        bad_client.disconnect = AsyncMock()
        mock_module.ClaudeSDKClient = MagicMock(return_value=bad_client)

        mock_registry = MagicMock()

        with patch(f"{SDK_PATCH}.load_session_id", return_value=""), \
             patch(f"{SDK_PATCH}.save_session_id"), \
             patch(f"{SDK_PATCH}.log_activity"), \
             patch(f"{SDK_PATCH}.get_registry", return_value=mock_registry), \
             patch(f"{SDK_PATCH}.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            await _run_agent_supervised(
                prompt="Fail", cwd="/tmp", issue_id="ERR-1",
                phase="implement", timeout=60,
            )

        mock_registry.update.assert_any_call("eng-err-1-implement", status="error")


# ---------------------------------------------------------------------------
# Tests: run_phase_blocking — the sync wrapper
# ---------------------------------------------------------------------------

class TestRunPhaseBlocking:
    def test_normal_completion(self):
        """run_phase_blocking blocks and returns AgentResult."""
        expected = AgentResult(
            session_id="sess-sync", issue_id="SYNC-1", phase="implement",
            success=True, duration_ms=2000, total_cost_usd=0.10, num_turns=3,
        )

        async def _mock(*a, **kw):
            return expected

        with patch(f"{SDK_PATCH}._run_agent_supervised", side_effect=_mock), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()):
            result = run_phase_blocking(
                issue_id="SYNC-1", phase="implement",
                cwd="/tmp/test", context="Build auth",
            )

        assert result.success is True
        assert result.session_id == "sess-sync"

    def test_timeout(self):
        """run_phase_blocking returns timeout error on asyncio.TimeoutError."""
        async def _slow(*args, **kwargs):
            await asyncio.sleep(999)

        with patch(f"{SDK_PATCH}._run_agent_supervised", side_effect=_slow), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()):
            # Override timeout to something tiny
            result = run_phase_blocking(
                issue_id="SLOW-1", phase="implement",
                cwd="/tmp/test", timeout=1,
            )

        assert result.success is False
        assert "timeout" in result.error

    def test_registers_session(self):
        """run_phase_blocking registers the session before starting."""
        expected = AgentResult(
            session_id="s", issue_id="REG-1", phase="pickup",
            success=True,
        )
        mock_registry = MagicMock()

        async def _mock(*a, **kw):
            return expected

        with patch(f"{SDK_PATCH}._run_agent_supervised", side_effect=_mock), \
             patch(f"{SDK_PATCH}.get_registry", return_value=mock_registry):
            run_phase_blocking(
                issue_id="REG-1", phase="pickup",
                cwd="/tmp/test", title="Fix auth", repo="moda-labs/app",
            )

        mock_registry.register.assert_called_once()
        entry = mock_registry.register.call_args[0][0]
        assert entry.name == "eng-reg-1-pickup"
        assert entry.issue_id == "REG-1"
        assert entry.title == "Fix auth"
        assert entry.repo == "moda-labs/app"
        assert entry.status == "starting"

    def test_passes_on_input_needed(self):
        """on_input_needed is passed through to _run_agent_supervised."""
        calls = []

        async def mock_supervised(*args, **kwargs):
            calls.append(kwargs)
            return AgentResult(
                session_id="s", issue_id="INP-1", phase="implement",
                success=True,
            )

        def my_handler(name, inp):
            return "answer"

        with patch(f"{SDK_PATCH}._run_agent_supervised", side_effect=mock_supervised), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()):
            run_phase_blocking(
                issue_id="INP-1", phase="implement",
                cwd="/tmp/test", on_input_needed=my_handler,
            )

        assert calls[0]["on_input_needed"] is my_handler

    def test_default_timeout_from_phase(self):
        """Timeout defaults to PHASE_TIMEOUT for the given phase."""
        calls = []

        async def mock_supervised(*args, **kwargs):
            calls.append(args)
            return AgentResult(session_id="s", issue_id="T-1", phase="pickup",
                               success=True)

        with patch(f"{SDK_PATCH}._run_agent_supervised", side_effect=mock_supervised), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()):
            run_phase_blocking(issue_id="T-1", phase="pickup", cwd="/tmp")

        # pickup timeout is 1800 (from PHASE_TIMEOUT)
        # The timeout arg is passed as the 5th positional arg
        assert calls[0][4] == 1800


# ---------------------------------------------------------------------------
# Tests: _session_name
# ---------------------------------------------------------------------------

class TestSessionName:
    def test_with_phase(self):
        assert _session_name("AGD-12", "spec") == "eng-agd-12-spec"

    def test_without_phase(self):
        assert _session_name("AGD-12") == "eng-agd-12"

    def test_lowercased(self):
        assert _session_name("BET-99", "implement") == "eng-bet-99-implement"


# ---------------------------------------------------------------------------
# Tests: _build_prompt
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_includes_issue_id(self):
        prompt = _build_prompt("implement", "AGD-12")
        assert "AGD-12" in prompt

    def test_includes_context(self):
        prompt = _build_prompt("implement", "AGD-12", context="Build auth flow")
        assert "Build auth flow" in prompt

    def test_includes_handoff_instruction(self):
        prompt = _build_prompt("spec", "AGD-12")
        assert "handoff" in prompt.lower()

    def test_includes_skill_reference_for_known_phase(self):
        prompt = _build_prompt("pickup", "AGD-12")
        assert "SKILL.md" in prompt


# ---------------------------------------------------------------------------
# Tests: _build_check_prompt
# ---------------------------------------------------------------------------

class TestBuildCheckPrompt:
    def test_includes_description_and_constraints(self):
        prompt = _build_check_prompt("Check prod URL returns 200")
        assert "Check prod URL returns 200" in prompt
        assert "non-interactive" in prompt.lower()
        # Read-only: must tell the agent not to make changes.
        assert "do not" in prompt.lower()
        assert '"finding": true' in prompt
        assert '"finding": false' in prompt

    def test_includes_extra_context(self):
        prompt = _build_check_prompt("Check it", extra={"url": "https://x.test"})
        assert "https://x.test" in prompt


# ---------------------------------------------------------------------------
# Tests: _parse_check_output
# ---------------------------------------------------------------------------

class TestParseCheckOutput:
    def test_finding_true_with_summary_and_details(self):
        text = 'Looks bad.\n{"finding": true, "summary": "down", "details": {"status": 503}}'
        finding, summary, details = _parse_check_output(text)
        assert finding is True
        assert summary == "down"
        assert details == {"status": 503}

    def test_finding_false(self):
        finding, summary, details = _parse_check_output('All good.\n{"finding": false}')
        assert finding is False
        assert summary == ""
        assert details == {}

    def test_picks_last_verdict_json(self):
        text = ('{"finding": false}\n'
                'reconsidering...\n'
                '{"finding": true, "summary": "actually down"}')
        finding, summary, _ = _parse_check_output(text)
        assert finding is True
        assert summary == "actually down"

    def test_ignores_non_verdict_json(self):
        text = '{"unrelated": 1}\nfinal\n{"finding": true, "summary": "x"}'
        finding, summary, _ = _parse_check_output(text)
        assert finding is True
        assert summary == "x"

    def test_no_json_defaults_to_no_finding(self):
        finding, summary, details = _parse_check_output("just prose, no json")
        assert finding is False
        assert summary == ""
        assert details == {}

    def test_empty_text(self):
        assert _parse_check_output("") == (False, "", {})

    def test_non_dict_details_coerced_to_empty(self):
        text = '{"finding": true, "summary": "x", "details": "oops"}'
        _, _, details = _parse_check_output(text)
        assert details == {}


# ---------------------------------------------------------------------------
# Tests: run_check_blocking
# ---------------------------------------------------------------------------

class TestRunCheckBlocking:
    def test_finding_parsed_from_agent_output(self):
        agent_result = AgentResult(
            session_id="s", issue_id="check-x", phase="check", success=True,
            duration_ms=1200, total_cost_usd=0.03,
            final_text='{"finding": true, "summary": "prod down", "details": {"code": 503}}',
        )

        async def _mock(*a, **kw):
            return agent_result

        with patch(f"{SDK_PATCH}._run_agent_supervised", side_effect=_mock), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()):
            result = run_check_blocking(description="Check prod", cwd="/tmp")

        assert isinstance(result, CheckResult)
        assert result.success is True
        assert result.finding is True
        assert result.summary == "prod down"
        assert result.details == {"code": 503}
        assert result.duration_ms == 1200

    def test_no_finding(self):
        agent_result = AgentResult(
            session_id="s", issue_id="check-x", phase="check", success=True,
            final_text='Everything healthy.\n{"finding": false}',
        )

        async def _mock(*a, **kw):
            return agent_result

        with patch(f"{SDK_PATCH}._run_agent_supervised", side_effect=_mock), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()):
            result = run_check_blocking(description="Check prod", cwd="/tmp")

        assert result.success is True
        assert result.finding is False

    def test_agent_failure_propagates(self):
        agent_result = AgentResult(
            session_id="", issue_id="check-x", phase="check", success=False,
            error="CLI crashed",
        )

        async def _mock(*a, **kw):
            return agent_result

        with patch(f"{SDK_PATCH}._run_agent_supervised", side_effect=_mock), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()):
            result = run_check_blocking(description="Check prod", cwd="/tmp")

        assert result.success is False
        assert result.finding is False
        assert "CLI crashed" in result.error

    def test_timeout_returns_failed_check(self):
        async def _slow(*a, **kw):
            await asyncio.sleep(999)

        with patch(f"{SDK_PATCH}._run_agent_supervised", side_effect=_slow), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()):
            result = run_check_blocking(description="Check", cwd="/tmp", timeout=1)

        assert result.success is False
        assert "timeout" in result.error

    def test_registers_monitor_session(self):
        agent_result = AgentResult(
            session_id="s", issue_id="check-x", phase="check", success=True,
            final_text='{"finding": false}',
        )
        mock_registry = MagicMock()

        async def _mock(*a, **kw):
            return agent_result

        with patch(f"{SDK_PATCH}._run_agent_supervised", side_effect=_mock), \
             patch(f"{SDK_PATCH}.get_registry", return_value=mock_registry):
            run_check_blocking(description="Check prod", cwd="/tmp", name="check-deploy")

        mock_registry.register.assert_called_once()
        entry = mock_registry.register.call_args[0][0]
        assert entry.role == "monitor"
        assert entry.phase == "check"
        assert entry.name == "eng-check-deploy-check"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

