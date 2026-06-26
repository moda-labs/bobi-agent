"""Thorough unit tests for run_phase_blocking and _run_agent_supervised.

All tests mock the Claude SDK — no real Claude sessions.
Tests cover: normal completion, AskUserQuestion deferral + routing,
timeout, errors, resume from saved session, cost accumulation across
deferred rounds, connection loss, and the defer hook itself.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


@pytest.fixture(autouse=True)
def bound_root(tmp_path, monkeypatch):
    """spawn_adhoc and prompt building read the bound installation root
    (roles, registry, memory) — bind explicitly instead of relying on a
    root leaked from earlier test files."""
    (tmp_path / ".bobi").mkdir()
    monkeypatch.setattr("bobi.paths._root", tmp_path)


from bobi.subagent import (
    AgentResult,
    CheckResult,
    InputHandler,
    _build_check_prompt,
    _build_prompt,
    _emit_lifecycle_event,
    _emit_session_finished,
    _emit_session_started,
    _make_defer_hook,
    _parse_check_output,
    _parse_check_verdict,
    _run_agent_supervised,
    _session_name,
    _summarize_output,
    run_check_blocking,
    run_phase_blocking,
    spawn_adhoc,
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
# Fake Session for run_phase_blocking / spawn_adhoc tests
# ---------------------------------------------------------------------------

class FakeSession:
    """Mimics bobi.session.Session for unit tests."""

    def __init__(self, success=True, response="done", session_id="sess-fake",
                 cost=0.10, duration=2000, turns=3, start_ok=True,
                 is_error=False):
        self._success = success
        self._response = response
        self._start_ok = start_ok
        self.name = ""
        self.cwd = ""
        self._last_response = response
        self._last_is_error = is_error or (not success)
        self._total_cost_usd = cost
        self._total_duration_ms = duration
        self._total_turns = turns
        self._session_id = session_id
        self.inbox = MagicMock()
        self.inbox.port = 0

    def start(self, startup_prompt=None, timeout=120):
        return self._start_ok

    def stop(self):
        pass

    def get_session_id(self):
        return self._session_id


def _make_fake_session_class(**kwargs):
    """Return a Session class constructor that produces a FakeSession."""
    def _cls(*args, **init_kwargs):
        fs = FakeSession(**kwargs)
        fs.name = init_kwargs.get("name", args[0] if args else "")
        fs.cwd = init_kwargs.get("cwd", args[1] if len(args) > 1 else "")
        return fs
    return _cls


# ---------------------------------------------------------------------------
# Patch targets
# ---------------------------------------------------------------------------

SDK_PATCH = "bobi.subagent"
SESSION_PATCH = "bobi.session.Session"


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
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Do the thing",
                cwd="/tmp/test",
                run_key="TEST-1",
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
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Do the thing",
                cwd="/tmp/test",
                run_key="TEST-2",
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
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Do the thing",
                cwd="/tmp/test",
                run_key="TEST-3",
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
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Build the feature",
                cwd="/tmp/test",
                run_key="TEST-D1",
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
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Build it",
                cwd="/tmp/test",
                run_key="TEST-M1",
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
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Do it",
                cwd="/tmp/test",
                run_key="TEST-NH",
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
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Continue working",
                cwd="/tmp/test",
                run_key="TEST-R1",
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
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Do it",
                cwd="/tmp/test",
                run_key="TEST-EX",
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
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Do it",
                cwd="/tmp/test",
                run_key="TEST-DC",
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
        """Registry records the honest terminal status 'completed' on success
        (MDS-65 RC#2 — never the old unconditional 'done')."""
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
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            result = await _run_agent_supervised(
                prompt="Track me",
                cwd="/tmp/test",
                run_key="TEST-T1",
                phase="spec",
                timeout=60,
            )

        # Registry updated to running, then the honest terminal status.
        mock_registry.update.assert_any_call(
            "agent-test-t1-spec", status="running", phase="spec", session_id="",
        )
        mock_registry.mark_terminal.assert_any_call(
            "agent-test-t1-spec", "completed", error="",
            session_id="sess-track", phase="spec",
        )

        # Session ID saved
        mock_save.assert_called_with("agent-test-t1-spec", "sess-track")

        # Activity logged (now carries the terminal status)
        mock_log.assert_any_call(
            "stop", {"session_id": "sess-track", "status": "completed"},
            session="agent-test-t1-spec",
        )

    @pytest.mark.asyncio
    async def test_registry_updated_on_error(self):
        """An unhandled executor exception records the honest terminal status
        'crashed' (MDS-65 RC#2 — was the old 'error'), with the error persisted."""
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
             patch("bobi.sdk.get_cli_path", return_value="/usr/bin/claude"), \
             patch.dict("sys.modules", {"claude_agent_sdk": mock_module}):

            await _run_agent_supervised(
                prompt="Fail", cwd="/tmp", run_key="ERR-1",
                phase="implement", timeout=60,
            )

        mock_registry.mark_terminal.assert_any_call(
            "agent-err-1-implement", "crashed", error="boom",
            session_id=None, phase="implement",
        )


# ---------------------------------------------------------------------------
# Tests: run_phase_blocking — the sync wrapper
# ---------------------------------------------------------------------------

class TestRunPhaseBlocking:
    def test_normal_completion(self):
        """run_phase_blocking blocks and returns AgentResult."""
        fake_cls = _make_fake_session_class(
            success=True, session_id="sess-sync",
            cost=0.10, duration=2000, turns=3,
        )

        with patch(SESSION_PATCH, side_effect=fake_cls):
            result = run_phase_blocking(
                run_key="SYNC-1", phase="implement",
                cwd="/tmp/test", context="Build auth",
            )

        assert result.success is True
        assert result.session_id == "sess-sync"
        assert result.total_cost_usd == 0.10

    def test_start_failure(self):
        """run_phase_blocking returns error when session fails to start."""
        fake_cls = _make_fake_session_class(start_ok=False)

        with patch(SESSION_PATCH, side_effect=fake_cls):
            result = run_phase_blocking(
                run_key="FAIL-1", phase="implement",
                cwd="/tmp/test", timeout=1,
            )

        assert result.success is False
        assert "failed to start" in result.error

    def test_threads_requested_by_into_lifecycle_events(self):
        """RC#4: run_phase_blocking must carry requested_by onto BOTH the
        started and finished lifecycle emits — the phase path used to drop it,
        so completions couldn't be routed back to the requester's thread."""
        fake_cls = _make_fake_session_class(success=True, session_id="sess-rb")
        rb = {"slack_user": "U9", "thread_ts": "777.0"}

        with patch(SESSION_PATCH, side_effect=fake_cls), \
             patch(f"{SDK_PATCH}._emit_session_started") as started, \
             patch(f"{SDK_PATCH}._emit_session_finished") as finished:
            run_phase_blocking(
                run_key="RB-1", phase="implement", cwd="/tmp/test",
                context="ctx", project="p", requested_by=rb,
            )

        assert started.call_args.kwargs["requested_by"] == rb
        assert finished.call_args.kwargs["requested_by"] == rb


# ---------------------------------------------------------------------------
# Tests: lifecycle events
# ---------------------------------------------------------------------------

class TestSummarizeOutput:
    def test_takes_last_lines(self):
        text = "\n".join(f"line {i}" for i in range(10))
        out = _summarize_output(text, max_lines=3)
        assert out == "line 7\nline 8\nline 9"

    def test_skips_blank_lines(self):
        out = _summarize_output("a\n\n\nb\n  \nc", max_lines=2)
        assert out == "b\nc"

    def test_truncates_chars(self):
        out = _summarize_output("x" * 1000, max_chars=50)
        assert len(out) == 50

    def test_handles_empty(self):
        assert _summarize_output("") == ""
        assert _summarize_output(None) == ""


class TestEmitLifecycleEvent:
    def test_posts_via_cli_post_event(self):
        """_emit_lifecycle_event posts (issue_id, repo, ...) via events.publish.post_event."""
        with patch("bobi.events.publish.post_event") as post:
            _emit_lifecycle_event("agent/session.started",
                                  {"run_key": "X-1", "project": "r", "task": ""})
            # Runs on a daemon thread — wait for it to drain.
            for t in threading.enumerate():
                if t.name == "lifecycle-event":
                    t.join(timeout=2)

        post.assert_called_once()
        event_type, data = post.call_args[0]
        assert event_type == "agent/session.started"
        assert data["run_key"] == "X-1"
        # Empty values are stripped from the payload.
        assert "task" not in data

    def test_never_raises_on_post_failure(self):
        with patch("bobi.events.publish.post_event", side_effect=RuntimeError("boom")):
            _emit_lifecycle_event("agent/session.failed", {"run_key": "X-2"})
            for t in threading.enumerate():
                if t.name == "lifecycle-event":
                    t.join(timeout=2)
        # No exception escapes — test reaching here is the assertion.

    def test_blocking_waits_for_post_to_land(self):
        """blocking=True returns only after the POST completes — no daemon race.

        The terminal emit fires as the last action before the spawn process
        exits; without the join the daemon thread would be killed mid-POST.
        """
        landed = threading.Event()

        def _slow_post(event_type, payload):
            time.sleep(0.1)
            landed.set()

        with patch("bobi.events.publish.post_event", side_effect=_slow_post):
            _emit_lifecycle_event(
                "agent/session.completed", {"issue_id": "X-3"}, blocking=True,
            )
            # The POST has already landed by the time the call returns.
            assert landed.is_set()

    def test_blocking_join_is_bounded_by_timeout(self):
        """A hung POST can't block the process forever — the join is bounded."""
        release = threading.Event()

        def _hang(event_type, payload):
            release.wait(5)

        with patch("bobi.events.publish.post_event", side_effect=_hang):
            start = time.time()
            _emit_lifecycle_event(
                "agent/session.completed", {"issue_id": "X-4"},
                blocking=True, timeout=0.1,
            )
            elapsed = time.time() - start
            # Returned promptly despite the POST still hanging.
            assert elapsed < 1.0
        release.set()  # let the daemon thread unwind


class TestSessionFinishedEvents:
    def test_completed_event_on_success(self):
        calls = []
        result = AgentResult(
            session_id="sdk-1", run_key="DONE-1", phase="adhoc",
            success=True, final_text="all\ndone\nPR up at #42",
        )
        with patch(f"{SDK_PATCH}._emit_lifecycle_event",
                   side_effect=lambda *a, **kw: calls.append((a, kw))):
            _emit_session_finished(result, "moda-labs/app", "adhoc-x", 0.0)

        assert len(calls) == 1
        (event_type, data), kwargs = calls[0]
        assert event_type == "agent/session.completed"
        assert data["run_key"] == "DONE-1"
        assert data["project"] == "moda-labs/app"
        assert data["session_id"] == "adhoc-x"
        assert "PR up at #42" in data["summary"]
        assert "duration" in data
        # Terminal emit blocks so the POST lands before the process exits.
        assert kwargs.get("blocking") is True

    def test_failed_event_on_error(self):
        calls = []
        result = AgentResult(
            session_id="", run_key="FAIL-1", phase="implement",
            success=False, error="timeout after 60s",
        )
        with patch(f"{SDK_PATCH}._emit_lifecycle_event",
                   side_effect=lambda *a, **kw: calls.append((a, kw))):
            _emit_session_finished(result, "r", "agent-fail-1-implement", 0.0)

        (event_type, data), kwargs = calls[0]
        assert event_type == "agent/session.failed"
        assert data["error"] == "timeout after 60s"
        assert kwargs.get("blocking") is True


class TestSpawnAdhocLifecycle:
    def test_emits_started_and_completed(self):
        events = []
        fake_cls = _make_fake_session_class(success=True, response="done")

        with patch(SESSION_PATCH, side_effect=fake_cls), \
             patch(f"{SDK_PATCH}._emit_lifecycle_event",
                   side_effect=lambda et, d, **kw: events.append(et)):
            spawn_adhoc(cwd="/tmp/test", task="Fix the login bug", name="adhoc-x")

        assert events == ["agent/session.started", "agent/session.completed"]

    def test_started_carries_task_and_repo(self):
        captured = []
        fake_cls = _make_fake_session_class(success=True)

        with patch(SESSION_PATCH, side_effect=fake_cls), \
             patch(f"{SDK_PATCH}._resolve_project_name", return_value="moda-labs/jobtack"), \
             patch(f"{SDK_PATCH}._emit_lifecycle_event",
                   side_effect=lambda et, d, **kw: captured.append((et, d))):
            spawn_adhoc(cwd="/repo/path", task="Investigate CI", name="adhoc-y")

        et, data = captured[0]
        assert et == "agent/session.started"
        assert data["task"] == "Investigate CI"
        assert data["project"] == "moda-labs/jobtack"
        assert data["session_id"] == "adhoc-y"

    def test_requested_by_echoed_on_lifecycle_events(self):
        captured = []
        fake_cls = _make_fake_session_class(success=True, response="done")
        requester = {"from": "Alice", "user_id": "U1", "channel": "C1",
                     "thread_ts": "171.42"}

        with patch(SESSION_PATCH, side_effect=fake_cls), \
             patch(f"{SDK_PATCH}._emit_lifecycle_event",
                   side_effect=lambda et, d, **kw: captured.append((et, d))):
            spawn_adhoc(cwd="/repo", task="Fix it", name="adhoc-z",
                        requested_by=requester)

        started = next(d for et, d in captured if et.endswith("started"))
        finished = next(d for et, d in captured if et.endswith("completed"))
        assert started["requested_by"] == requester
        assert finished["requested_by"] == requester

    def test_requested_by_absent_when_not_provided(self):
        captured = []
        fake_cls = _make_fake_session_class(success=True, response="done")

        with patch(SESSION_PATCH, side_effect=fake_cls), \
             patch(f"{SDK_PATCH}._emit_lifecycle_event",
                   side_effect=lambda et, d, **kw: captured.append((et, d))):
            spawn_adhoc(cwd="/repo", task="Fix it", name="adhoc-w")

        started = next(d for et, d in captured if et.endswith("started"))
        assert started["requested_by"] is None

    def test_started_uses_explicit_name_as_run_key(self):
        captured = []
        fake_cls = _make_fake_session_class(success=True)

        with patch(SESSION_PATCH, side_effect=fake_cls), \
             patch(f"{SDK_PATCH}._resolve_project_name", return_value="moda-labs/jobtack"), \
             patch(f"{SDK_PATCH}._emit_lifecycle_event",
                   side_effect=lambda et, d, **kw: captured.append((et, d))):
            spawn_adhoc(cwd="/repo/path",
                        task="Write a spec for issue #5: AI Extraction Pipeline",
                        name="5")

        et, data = captured[0]
        assert et == "agent/session.started"
        assert data["run_key"] == "5"

    def test_started_generates_adhoc_id_without_explicit_name(self):
        captured = []
        fake_cls = _make_fake_session_class(success=True)

        with patch(SESSION_PATCH, side_effect=fake_cls), \
             patch(f"{SDK_PATCH}._resolve_project_name", return_value="moda-labs/jobtack"), \
             patch(f"{SDK_PATCH}._emit_lifecycle_event",
                   side_effect=lambda et, d, **kw: captured.append((et, d))):
            spawn_adhoc(cwd="/repo/path", task="Fix the login bug")

        et, data = captured[0]
        assert et == "agent/session.started"
        assert data["run_key"].startswith("adhoc-")


class TestRunPhaseBlockingLifecycle:
    def test_emits_failed_on_start_failure(self):
        events = []
        fake_cls = _make_fake_session_class(start_ok=False)

        with patch(SESSION_PATCH, side_effect=fake_cls), \
             patch(f"{SDK_PATCH}._emit_lifecycle_event",
                   side_effect=lambda et, d, **kw: events.append(et)):
            run_phase_blocking(run_key="FAIL-1", phase="implement",
                               cwd="/tmp", timeout=1)

        assert events == ["agent/session.started", "agent/session.failed"]


# ---------------------------------------------------------------------------
# Tests: _session_name
# ---------------------------------------------------------------------------

class TestSessionName:
    def test_with_phase(self):
        assert _session_name("AGD-12", phase="spec") == "agent-agd-12-spec"

    def test_without_phase(self):
        assert _session_name("AGD-12") == "agent-agd-12"

    def test_lowercased(self):
        assert _session_name("BET-99", phase="implement") == "agent-bet-99-implement"

    def test_with_role(self):
        assert _session_name("42", role="engineer", phase="spec") == "engineer-42-spec"

    def test_with_role_no_phase(self):
        assert _session_name("42", role="engineer") == "engineer-42"


# ---------------------------------------------------------------------------
# Tests: _build_prompt
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_includes_run_key(self):
        prompt = _build_prompt("implement", "AGD-12")
        assert "AGD-12" in prompt

    def test_includes_context(self):
        prompt = _build_prompt("implement", "AGD-12", context="Build auth flow")
        assert "Build auth flow" in prompt

    def test_includes_handoff_instruction(self):
        prompt = _build_prompt("spec", "AGD-12")
        assert "handoff" in prompt.lower()

    def test_includes_phase_name(self):
        prompt = _build_prompt("pickup", "AGD-12")
        assert "pickup" in prompt


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


class TestParseCheckVerdict:
    """A missing verdict must be distinguishable from an explicit finding=false.

    Regression: a check agent that hit a tool-use glitch (emitting a tool call
    as literal text, then stopping) produced no verdict, which the old code
    collapsed to finding=false — silently dropping a real support email.
    """

    def test_explicit_finding_false_returns_verdict(self):
        assert _parse_check_verdict('{"finding": false}') == {"finding": False}

    def test_finding_true_returns_full_verdict(self):
        v = _parse_check_verdict('{"finding": true, "summary": "down"}')
        assert v == {"finding": True, "summary": "down"}

    def test_no_json_returns_none(self):
        assert _parse_check_verdict("just prose, no verdict") is None

    def test_empty_returns_none(self):
        assert _parse_check_verdict("") is None

    def test_malformed_tool_call_text_returns_none(self):
        # The exact failure seen in production: the model emitted a ToolSearch
        # call as text instead of executing it, then stopped — no verdict.
        text = ('court\n<invoke name="ToolSearch">\n'
                '<parameter name="query">select:mcp__Venn__execute_tool</parameter>\n'
                '</invoke>')
        assert _parse_check_verdict(text) is None

    def test_non_verdict_json_returns_none(self):
        assert _parse_check_verdict('{"unrelated": 1}') is None


# ---------------------------------------------------------------------------
# Tests: run_check_blocking
# ---------------------------------------------------------------------------

class TestRunCheckBlocking:
    def test_finding_parsed_from_agent_output(self):
        agent_result = AgentResult(
            session_id="s", run_key="check-x", phase="check", success=True,
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
            session_id="s", run_key="check-x", phase="check", success=True,
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
            session_id="", run_key="check-x", phase="check", success=False,
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

    def test_no_verdict_is_failure_not_silent_no_finding(self):
        """Regression: an agent that produces no parseable verdict (malformed
        tool call, truncated output) must NOT be reported as a healthy
        finding=false — that silently drops real signals. After exhausting
        retries it is a failed check."""
        agent_result = AgentResult(
            session_id="s", run_key="check-x", phase="check", success=True,
            final_text='court\n<invoke name="ToolSearch"></invoke>',  # no verdict
        )

        async def _mock(*a, **kw):
            return agent_result

        with patch(f"{SDK_PATCH}._run_agent_supervised", side_effect=_mock), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()):
            result = run_check_blocking(description="Read inbox", cwd="/tmp", attempts=2)

        assert result.success is False
        assert result.finding is False
        assert "verdict" in result.error

    def test_retries_then_succeeds_on_clean_verdict(self):
        """A transient no-verdict glitch on the first attempt is retried, and a
        clean verdict on the second attempt is returned."""
        results = iter([
            AgentResult(session_id="s", run_key="check-x", phase="check",
                        success=True, final_text="garbage, no verdict"),
            AgentResult(session_id="s", run_key="check-x", phase="check",
                        success=True,
                        final_text='{"finding": true, "summary": "new email"}'),
        ])

        async def _mock(*a, **kw):
            return next(results)

        with patch(f"{SDK_PATCH}._run_agent_supervised", side_effect=_mock), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()):
            result = run_check_blocking(description="Read inbox", cwd="/tmp", attempts=2)

        assert result.success is True
        assert result.finding is True
        assert result.summary == "new email"

    def test_explicit_finding_false_does_not_retry(self):
        """A clean finding=false ends the loop immediately — no wasted retry."""
        calls = {"n": 0}

        async def _mock(*a, **kw):
            calls["n"] += 1
            return AgentResult(session_id="s", run_key="check-x", phase="check",
                               success=True, final_text='{"finding": false}')

        with patch(f"{SDK_PATCH}._run_agent_supervised", side_effect=_mock), \
             patch(f"{SDK_PATCH}.get_registry", return_value=MagicMock()):
            result = run_check_blocking(description="Read inbox", cwd="/tmp", attempts=3)

        assert result.success is True
        assert result.finding is False
        assert calls["n"] == 1

    def test_registers_monitor_session(self):
        agent_result = AgentResult(
            session_id="s", run_key="check-x", phase="check", success=True,
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
        assert entry.name == "monitor-check-deploy-check"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

