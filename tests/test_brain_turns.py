"""#1048 - the one turn primitive.

``drain_turn`` is the single copy of the spawn-style drain: text capture,
session-id save, the full-fact stop record, and normalization of the three
ways a stream can die. These tests pin that contract directly; the
orchestrator and subagent suites pin their adapters on top of it.
"""

import asyncio
from unittest.mock import call, patch

import pytest

from bobi.brain.base import AssistantText, TurnResult
from bobi.brain.turns import (
    drain_turn,
    network_drop_error,
    timeout_error,
    tool_crash_error,
)


class StreamClient:
    """Client whose receive_response yields a fixed message list."""

    def __init__(self, messages):
        self._messages = messages
        self.stream_closed = False

    async def receive_response(self):
        try:
            for m in self._messages:
                yield m
        finally:
            self.stream_closed = True


class RaisingClient:
    def __init__(self, exc, before=()):
        self._exc = exc
        self._before = before

    async def receive_response(self):
        for m in self._before:
            yield m
        raise self._exc


class HangingClient:
    async def receive_response(self):
        await asyncio.sleep(3600)
        yield  # pragma: no cover


@pytest.mark.asyncio
async def test_success_captures_text_saves_session_and_logs_stop():
    client = StreamClient([
        AssistantText(text="thinking..."),
        AssistantText(text="the answer"),
        TurnResult(session_id="s1", num_turns=2, duration_ms=1234),
    ])
    with patch("bobi.brain.turns.save_session_id") as save, \
         patch("bobi.brain.turns.log_activity") as log:
        outcome = await drain_turn(client, "sess", model="haiku")

    assert outcome.result is not None
    assert outcome.result.session_id == "s1"
    assert outcome.final_text == "the answer"
    assert outcome.failure == "" and outcome.failure_kind == ""
    save.assert_called_once_with("sess", "s1", model="haiku")
    # Both assistant messages logged as responses.
    assert call("response", {"text": "thinking..."}, session="sess") \
        in log.call_args_list
    # The stop record carries every terminal fact the brain reported (#845).
    stop = next(c for c in log.call_args_list if c.args[0] == "stop")
    assert stop.args[1] == {
        "session_id": "s1", "is_error": False, "error_kind": "",
        "error_message": "", "api_error_status": None,
        "num_turns": 2, "duration_ms": 1234,
    }
    # Returning at the terminal result must still finalize the stream's
    # generator NOW (adapter teardown, e.g. subprocess reaping, lives in its
    # finally) - not at the loop's asyncgen-shutdown hook.
    assert client.stream_closed


@pytest.mark.asyncio
async def test_empty_text_never_clobbers_and_long_text_is_truncated_in_log():
    client = StreamClient([
        AssistantText(text="x" * 600),
        AssistantText(text=""),  # tool-use-only message: no text signal
        TurnResult(session_id="s5"),
    ])
    with patch("bobi.brain.turns.save_session_id"), \
         patch("bobi.brain.turns.log_activity") as log:
        outcome = await drain_turn(client, "sess", model="")

    assert outcome.final_text == "x" * 600
    responses = [c for c in log.call_args_list if c.args[0] == "response"]
    assert len(responses) == 1
    assert responses[0].args[1]["text"] == "x" * 500


@pytest.mark.asyncio
async def test_brain_error_still_returns_the_result_for_caller_policy():
    client = StreamClient([
        TurnResult(session_id="s2", is_error=True,
                   error_kind="max_turns_reached", error_message="cap hit"),
    ])
    with patch("bobi.brain.turns.save_session_id") as save, \
         patch("bobi.brain.turns.log_activity") as log:
        outcome = await drain_turn(client, "sess", model="")

    # A brain-reported failure is NOT a drain failure: the caller decides.
    assert outcome.result is not None
    assert outcome.result.is_error is True
    assert outcome.failure == ""
    save.assert_called_once_with("sess", "s2", model="")
    # The stop record names the failure - the #845 facts exist FOR the
    # error case; the success shape alone would be vacuous.
    stop = next(c for c in log.call_args_list if c.args[0] == "stop")
    assert stop.args[1]["is_error"] is True
    assert stop.args[1]["error_kind"] == "max_turns_reached"
    assert stop.args[1]["error_message"] == "cap hit"


@pytest.mark.asyncio
async def test_empty_stream_is_a_network_drop():
    with patch("bobi.brain.turns.save_session_id") as save, \
         patch("bobi.brain.turns.log_activity"):
        outcome = await drain_turn(StreamClient([]), "sess", model="")
    assert outcome.result is None
    assert outcome.failure == network_drop_error()
    assert outcome.failure_kind == "network_drop"
    # A broken stream must never write a session id.
    save.assert_not_called()


@pytest.mark.asyncio
async def test_mid_stream_crash_is_a_tool_crash_and_keeps_the_text():
    client = RaisingClient(RuntimeError("boom"),
                           before=[AssistantText(text="partial")])
    with patch("bobi.brain.turns.save_session_id") as save, \
         patch("bobi.brain.turns.log_activity"):
        outcome = await drain_turn(client, "sess", model="")
    assert outcome.result is None
    assert outcome.failure == tool_crash_error("boom") == "tool crash: boom"
    assert outcome.failure_kind == "tool_crash"
    # Text seen before the crash survives for diagnostics.
    assert outcome.final_text == "partial"
    save.assert_not_called()


@pytest.mark.asyncio
async def test_sdk_timeout_is_normalized():
    client = RaisingClient(asyncio.TimeoutError())
    with patch("bobi.brain.turns.save_session_id") as save, \
         patch("bobi.brain.turns.log_activity"):
        outcome = await drain_turn(client, "sess", model="")
    assert outcome.result is None
    assert outcome.failure == timeout_error()
    assert outcome.failure_kind == "timeout"
    save.assert_not_called()


@pytest.mark.asyncio
async def test_cancellation_passes_through_to_the_callers_boundary():
    """D067: an outer asyncio.timeout must still land in the CALLER's
    handler - the primitive must not swallow the cancellation into a
    TurnOutcome, or wall-clock enforcement silently vanishes."""
    with patch("bobi.brain.turns.save_session_id") as save, \
         patch("bobi.brain.turns.log_activity"):
        with pytest.raises(asyncio.TimeoutError):
            async with asyncio.timeout(0.05):
                await drain_turn(HangingClient(), "sess", model="")
    save.assert_not_called()


def test_tool_crash_error_is_idempotent():
    assert tool_crash_error("tool crash: x") == "tool crash: x"
    assert tool_crash_error(ValueError("")) == "tool crash: ValueError"


def test_timeout_error_names_the_budget_when_known():
    assert timeout_error(60) == "subprocess timeout after 60s"
    assert timeout_error() == "subprocess timeout while draining response"
