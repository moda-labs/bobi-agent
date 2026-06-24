"""MDS-65 — close the sub-agent completion-delivery loop.

Reproduction + regression tests for the four root causes:
  RC#2 — honest terminal status (no `done` on an error result)
  RC#3 — durable, reconcilable terminal status (survives a swallowed bus POST)
The lifecycle-subscription (RC#1), requested_by threading (RC#4), and the
reconciler are covered in test_subscriptions / test_subagent_blocking /
test_reconcile respectively.
"""

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from modastack.sdk import (
    SessionEntry, get_registry,
    TERMINAL_COMPLETED, TERMINAL_FAILED, TERMINAL_CRASHED,
)
from modastack.subagent import _run_agent_supervised, _session_name


# --- minimal SDK fakes (mirror test_subagent_blocking) ---------------------

@dataclass
class FakeTextBlock:
    text: str


@dataclass
class FakeAssistantMessage:
    content: list
    model: str = "claude-test"


@dataclass
class FakeResultMessage:
    subtype: str = "success"
    duration_ms: int = 1000
    is_error: bool = False
    num_turns: int = 1
    session_id: str = "sess-x"
    total_cost_usd: float | None = 0.05
    result: str | None = None
    api_error_status: int | None = None
    deferred_tool_use: Any = None


class FakeClient:
    def __init__(self, rounds):
        self._rounds = list(rounds)
        self._i = 0
        self.connected = self.disconnected = False

    async def connect(self, prompt=None):
        self.connected = True

    async def query(self, prompt, session_id="default"):
        pass

    async def receive_response(self):
        if self._i >= len(self._rounds):
            return
        msgs = self._rounds[self._i]
        self._i += 1
        for m in msgs:
            yield m

    async def disconnect(self):
        self.disconnected = True


SDK_PATCH = "modastack.subagent"


def _sdk_module(client):
    mod = MagicMock()
    mod.AssistantMessage = FakeAssistantMessage
    mod.ClaudeAgentOptions = MagicMock()
    mod.ClaudeSDKClient = MagicMock(return_value=client)
    mod.ResultMessage = FakeResultMessage
    mod.TextBlock = FakeTextBlock
    return mod


@pytest.fixture(autouse=True)
def bound_root(tmp_path, monkeypatch):
    monkeypatch.setattr("modastack.paths._root", tmp_path)


def _register(run_key, phase, role=""):
    name = _session_name(run_key, role=role, phase=phase)
    get_registry().register(SessionEntry(
        name=name, run_key=run_key, phase=phase, role=role, status="starting",
    ))
    return name


async def _run(client, run_key, phase):
    with patch(f"{SDK_PATCH}.load_session_id", return_value=""), \
         patch(f"{SDK_PATCH}.save_session_id"), \
         patch(f"{SDK_PATCH}.log_activity"), \
         patch("modastack.sdk.get_cli_path", return_value="/usr/bin/claude"), \
         patch.dict("sys.modules", {"claude_agent_sdk": _sdk_module(client)}):
        return await _run_agent_supervised(
            prompt="do it", cwd="/tmp", run_key=run_key, phase=phase, timeout=60,
        )


# --- RC#2: honest terminal status ------------------------------------------

class TestHonestTerminalStatus:
    @pytest.mark.asyncio
    async def test_success_records_completed_not_done(self):
        name = _register("OK-1", "implement")
        client = FakeClient([[FakeResultMessage(session_id="s1")]])
        result = await _run(client, "OK-1", "implement")
        assert result.success is True
        assert get_registry().get(name).status == TERMINAL_COMPLETED

    @pytest.mark.asyncio
    async def test_error_result_records_failed_not_done(self):
        """The bug: an error ResultMessage (not an exception) was written `done`.
        It must be `failed`, with the error persisted."""
        name = _register("ERR-1", "implement")
        client = FakeClient([[FakeResultMessage(
            session_id="s2", is_error=True, result="boom")]])
        result = await _run(client, "ERR-1", "implement")
        assert result.success is False
        entry = get_registry().get(name)
        assert entry.status == TERMINAL_FAILED
        assert entry.status != "done"
        assert entry.error == "boom"
        assert entry.terminal_at > 0

    @pytest.mark.asyncio
    async def test_transient_529_is_failed_and_tagged_transient(self):
        """A 529 surfaces as an error ResultMessage, not an exception. It is
        recorded honestly as `failed` (never `done`, never retried in the spawn
        path) and tagged transient via the shared classifier (§4.3)."""
        name = _register("OVL-1", "implement")
        client = FakeClient([[FakeResultMessage(
            session_id="s3", is_error=True,
            result="API Error: 529 Overloaded", api_error_status=529)]])
        result = await _run(client, "OVL-1", "implement")
        assert result.success is False
        assert result.transient is True
        # exactly one round consumed — no spawn-side retry
        assert client._i == 1
        assert get_registry().get(name).status == TERMINAL_FAILED

    @pytest.mark.asyncio
    async def test_hard_400_is_failed_not_transient(self):
        name = _register("BAD-1", "implement")
        client = FakeClient([[FakeResultMessage(
            session_id="s4", is_error=True,
            result="API Error: 400 Bad Request", api_error_status=400)]])
        result = await _run(client, "BAD-1", "implement")
        assert result.transient is False
        assert get_registry().get(name).status == TERMINAL_FAILED

    @pytest.mark.asyncio
    async def test_connection_lost_records_failed(self):
        name = _register("LOST-1", "implement")
        client = FakeClient([[]])  # no ResultMessage
        result = await _run(client, "LOST-1", "implement")
        assert result.success is False
        assert get_registry().get(name).status == TERMINAL_FAILED


# --- RC#3: durable terminal status survives a swallowed emit ----------------

class TestDurableTerminalStatus:
    @pytest.mark.asyncio
    async def test_terminal_persisted_even_if_emit_would_be_swallowed(self):
        """The terminal status is written to state.json synchronously, before and
        independent of the best-effort bus POST. Here the registry write is the
        source of truth; a later swallowed emit can't lose it."""
        name = _register("DUR-1", "implement")
        client = FakeClient([[FakeResultMessage(
            session_id="s5", is_error=True, result="boom")]])
        await _run(client, "DUR-1", "implement")
        entry = get_registry().get(name)
        assert entry.status == TERMINAL_FAILED
        # emit not yet confirmed → the reconciler is the backstop (Phase 3).
        assert entry.emit_confirmed is False

    def test_mark_terminal_is_idempotent_and_durable(self):
        name = _register("DUR-2", "implement")
        reg = get_registry()
        reg.mark_terminal(name, TERMINAL_CRASHED, error="died")
        first = reg.get(name)
        assert first.status == TERMINAL_CRASHED
        assert first.error == "died"
        assert first.pid == 0
        assert first.terminal_at > 0
