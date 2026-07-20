"""Context-rotation metric, manual compact, and the bounded/recoverable
rotation reconnect (regressions for the prod wedges).

The deployed eng-team manager grew to ~424K tokens of context without ever
rotating, because the rotation check measured ``input_tokens`` only. With
prompt caching on, ``input_tokens`` is just the *uncached* delta (≈2 on a
warm turn) — the real conversation lives in ``cache_read_input_tokens``.
So the 275K cap compared ~2 >= 275_000 and never fired. (#454)

The 2026-06-24 recurrence was a different mechanism: a fresh ``claude``
subprocess could hang while connecting. #456 bounds and recovers that work.
Issue #799 exposed a separate contract bug: ``connect(None)`` opens the
transport but starts no model turn, yet rotation tried to drain a terminal
``ResultMessage`` from that nonexistent turn. That wait blocked the inbox.

These tests pin the true metric (input + cache_read + cache_creation), the
manual ``compact`` trigger, and the bounded/recoverable reconnect.
"""

import asyncio
import json

import pytest

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from bobi.brain import AssistantText, BrainCost, TurnResult
from bobi.brain.claude import DEFAULT_INITIALIZE_TIMEOUT_MS
from bobi.inbox import Message
from bobi.session import (
    COMPACT_SENTINEL,
    ROTATION_RECONNECT_TIMEOUT,
    Session,
    _context_fill_tokens,
    _rotation_error_message,
)


class _FakeClient:
    """A brain-session stub whose ``receive_response`` replays canned messages.

    Assigned directly to ``session._client`` (bypassing the adapter), so it
    speaks the normalized brain contract: it yields :class:`AssistantText` /
    :class:`TurnResult` and exposes ``provider`` for cost attribution.
    """

    provider = "anthropic"

    def __init__(self, messages):
        self._messages = messages
        self.queries: list[str] = []

    async def query(self, text):
        self.queries.append(text)

    async def receive_response(self):
        for m in self._messages:
            yield m


class _ConnectOnlyClient:
    async def connect(self, prompt=None):
        pass

    async def disconnect(self):
        pass


def _assistant(usage: dict | None) -> AssistantMessage:
    """A real assistant message carrying one API call's per-call usage.

    This is the shape the SDK emits for each model call within a turn — NOT a
    MagicMock. Each call re-reads the cached prefix, so its ``cache_read`` is the
    full window fill for *that one call*, and a multi-call turn yields several.
    """
    return AssistantMessage(
        content=[TextBlock(text="step")],
        model="claude-opus-4-8",
        usage=usage,
    )


def _result(usage: dict | None, *, is_error: bool = False,
            api_error_status: int | None = None) -> ResultMessage:
    msg = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="sess-1",
        total_cost_usd=0.01,
        usage=usage,
        # Mirror the real warm-turn shape: model_usage carries only the tiny
        # uncached delta, exactly what the old check mistakenly trusted.
        model_usage={"claude-opus-4-8": {"input_tokens": 2, "output_tokens": 3}},
    )
    # api_error_status is an optional SDK attribute the drain reads via getattr.
    msg.api_error_status = api_error_status
    return msg


# Normalized brain messages for the direct-injection path (``s._client`` is a
# brain session post-#485, so its receive_response yields these — not SDK
# messages). The SDK ``_assistant``/``_result`` above are kept for the
# adapter-path fakes (``_ErrorTurnClient``), which the Claude adapter converts.
def _b_assistant(usage: dict | None) -> AssistantText:
    """One assistant turn carrying a single API call's per-call usage."""
    return AssistantText(text="step", usage=usage)


def _b_result(usage: dict | None = None, *, is_error: bool = False,
              api_error_status: int | None = None) -> TurnResult:
    """End-of-turn result. The rotation metric reads the last *assistant*
    message's usage, not the result's, so ``usage`` here is accepted for
    call-site symmetry but unused (mirrors the SDK shape)."""
    return TurnResult(
        session_id="sess-1",
        is_error=is_error,
        api_error_status=api_error_status,
        total_cost_usd=0.01,
        duration_ms=1,
        num_turns=1,
        costs=[BrainCost(model="claude-opus-4-8", input_tokens=2, output_tokens=3)],
    )


def test_context_fill_sums_cache_fields():
    """True context fill = fresh input + cache read + cache creation."""
    assert _context_fill_tokens(
        {
            "input_tokens": 2,
            "cache_read_input_tokens": 422_468,
            "cache_creation_input_tokens": 1_262,
            "output_tokens": 3_432,
        }
    ) == 423_732
    assert _context_fill_tokens(None) == 0
    assert _context_fill_tokens({}) == 0
    # Missing/None individual fields are treated as zero, not an error.
    assert _context_fill_tokens({"input_tokens": 10, "cache_read_input_tokens": None}) == 10


def test_rotation_error_message_never_empty_for_timeout():
    """asyncio.TimeoutError stringifies to "", but logs/events need a cause."""
    message = _rotation_error_message(asyncio.TimeoutError())

    assert message
    assert "TimeoutError" in message
    assert "timed out" in message


def test_rotation_error_message_handles_broken_exception_str():
    """Diagnostic formatting must not let broken SDK exceptions break recovery."""

    class BrokenStrError(Exception):
        def __str__(self):
            raise RuntimeError("stringification failed")

    message = _rotation_error_message(BrokenStrError())

    assert message
    assert "BrokenStrError" in message


@pytest.mark.asyncio
async def test_rotation_failure_records_nonempty_timeout_cause(
    bobi_install, monkeypatch
):
    """Exhausted reconnect retries must emit diagnosable failure details."""
    monkeypatch.setattr("bobi.session.ROTATION_RECONNECT_TIMEOUT", 0.01)
    monkeypatch.setattr("bobi.session.ROTATION_RECONNECT_BACKOFF", 0)
    events = []
    monkeypatch.setattr(
        "bobi.events.client._log_event",
        lambda event, session_id="": events.append(event),
    )

    s = Session(name="test-rot-timeout", cwd=str(bobi_install.repo_path))
    attempts = {"count": 0}

    async def timeout_reconnect():
        attempts["count"] += 1
        raise asyncio.TimeoutError()

    s._attempt_reconnect = timeout_reconnect
    s._make_brain_session = lambda resume=None: _ConnectOnlyClient()

    await s._rotate()

    assert attempts["count"] == 3
    failed_events = [
        event for event in events if event["type"] == "session.rotation_failed"
    ]
    assert failed_events
    payload = failed_events[0]["payload"]
    assert payload["attempts"] == 3
    assert payload["error"]
    assert "TimeoutError" in payload["error"]
    assert len(payload["attempt_errors"]) == 3
    assert [item["attempt"] for item in payload["attempt_errors"]] == [1, 2, 3]
    assert all(item["error"] for item in payload["attempt_errors"])

    log_path = bobi_install.sessions_dir / "test-rot-timeout" / "log.jsonl"
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    failure = next(record for record in records if record["event"] == "rotation_failed")
    assert failure["error"]
    assert failure["attempt_errors"] == payload["attempt_errors"]


def test_rotation_reconnect_timeout_exceeds_claude_initialize_default():
    assert ROTATION_RECONNECT_TIMEOUT * 1000 > DEFAULT_INITIALIZE_TIMEOUT_MS


@pytest.mark.asyncio
async def test_rotation_triggers_when_context_is_cached(bobi_install):
    """A warm turn (tiny input_tokens, huge cache_read) must trip the cap.

    This is the production failure: the old code read input_tokens=2 and
    never rotated while the real context sat at ~424K.
    """
    warm = {
        "input_tokens": 2,
        "cache_read_input_tokens": 422_468,
        "cache_creation_input_tokens": 1_262,
        "output_tokens": 3_432,
    }
    s = Session(name="test-rot", cwd=str(bobi_install.repo_path))
    s._input_ready = asyncio.Event()
    s._rotation_token_cap = 275_000
    # A single-call turn: the one assistant message carries the warm usage; the
    # ResultMessage aggregate equals it. Fill = 423_732 >= cap → rotate.
    s._client = _FakeClient([_b_assistant(warm), _b_result(warm)])

    await s._drain_turn()

    assert s._rotate_pending is True


@pytest.mark.asyncio
async def test_no_rotation_below_cap(bobi_install):
    """A small turn well under the cap leaves rotation un-pending."""
    small = {
        "input_tokens": 1_000,
        "cache_read_input_tokens": 40_000,
        "cache_creation_input_tokens": 2_000,
        "output_tokens": 500,
    }
    s = Session(name="test-rot-small", cwd=str(bobi_install.repo_path))
    s._input_ready = asyncio.Event()
    s._rotation_token_cap = 275_000
    s._client = _FakeClient([_b_assistant(small), _b_result(small)])

    await s._drain_turn()

    assert s._rotate_pending is False


@pytest.mark.asyncio
async def test_compact_sentinel_requests_rotation_without_querying_model(bobi_install):
    """The compact command delivers a sentinel that flags rotation, not a prompt."""
    s = Session(name="test-compact", cwd=str(bobi_install.repo_path))
    s._input_ready = asyncio.Event()
    s._set_state("waiting_input")
    client = _FakeClient([])
    s._client = client

    await s._process_message(Message(id="m1", sender="cli", text=COMPACT_SENTINEL))

    assert s._rotate_pending is True
    assert s._rotate_reason == "manual"
    # The sentinel is a control signal — it must never reach the model.
    assert client.queries == []


@pytest.mark.asyncio
async def test_multi_call_turn_does_not_over_count_context(bobi_install):
    """A ≥3-model-call turn must measure ONE call's fill, not the turn aggregate.

    Failing-first reproduction of #454. In a multi-step turn (model → tool →
    model → tool → model) the cached prefix is re-read on every call, so the
    ResultMessage's *cumulative* usage sums ``cache_read`` across all N calls —
    reporting ``real_context × N``. Here real per-call fill is ~100k (well under
    the 275k cap) but the aggregate is ~300k. The old code read the aggregate
    and fired a FALSE "rotation pending"; the fix reads the last assistant
    message's single-call usage, so it correctly does NOT rotate.
    """
    per_call = {
        "input_tokens": 10,
        "cache_read_input_tokens": 100_000,
        "cache_creation_input_tokens": 0,
        "output_tokens": 50,
    }
    # ResultMessage usage = the SDK's per-turn aggregate (cache_read summed ×3).
    cumulative = {
        "input_tokens": 30,
        "cache_read_input_tokens": 300_000,
        "cache_creation_input_tokens": 0,
        "output_tokens": 150,
    }

    s = Session(name="test-multicall", cwd=str(bobi_install.repo_path))
    s._input_ready = asyncio.Event()
    s._rotation_token_cap = 275_000
    s._client = _FakeClient([
        _b_assistant(per_call),
        _b_assistant(per_call),
        _b_assistant(per_call),
        _b_result(cumulative),
    ])

    await s._drain_turn()

    # Real fill 100_010 < 275_000 — no rotation. (Old code: 300_030 >= cap → True.)
    assert s._rotate_pending is False


@pytest.mark.asyncio
async def test_multi_call_turn_rotates_when_single_call_is_over_cap(bobi_install):
    """When a single call's real fill exceeds the cap, rotation fires.

    Complements the over-count test: the metric must still trip on a genuine
    over-cap.
    """
    per_call = {
        "input_tokens": 10,
        "cache_read_input_tokens": 300_000,
        "cache_creation_input_tokens": 0,
        "output_tokens": 50,
    }
    s = Session(name="test-multicall-over", cwd=str(bobi_install.repo_path))
    s._input_ready = asyncio.Event()
    s._rotation_token_cap = 275_000
    s._client = _FakeClient([
        _b_assistant(per_call),
        _b_assistant(per_call),
        _b_assistant(per_call),
        _b_result({"input_tokens": 30, "cache_read_input_tokens": 900_000}),
    ])

    await s._drain_turn()

    assert s._rotate_pending is True


class _HangingClient:
    """A Claude-shaped promptless client with no turn to receive."""

    provider = "anthropic"
    instances = 0
    receive_calls = 0

    def __init__(self, options=None):
        type(self).instances += 1
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def query(self, text):
        pass

    async def disconnect(self):
        self.disconnected = True

    async def receive_response(self):
        type(self).receive_calls += 1
        # Claude emits nothing after connect(None). Any caller trying to drain
        # here waits forever for a turn that was never created.
        await asyncio.Event().wait()
        yield  # pragma: no cover - unreachable


@pytest.mark.asyncio
async def test_rotation_promptless_connect_does_not_drain_nonexistent_turn(
    bobi_install, monkeypatch
):
    """A successful promptless connect is ready without receiving a turn."""
    from bobi import session as session_mod

    _HangingClient.instances = 0
    _HangingClient.receive_calls = 0
    # Tiny bounds so the test runs in a fraction of a second.
    monkeypatch.setattr(session_mod, "ROTATION_RECONNECT_TIMEOUT", 0.05)
    monkeypatch.setattr(session_mod, "ROTATION_RECONNECT_BACKOFF", 0.0)
    monkeypatch.setattr(session_mod, "ROTATION_MAX_RECONNECT_ATTEMPTS", 2)

    s = Session(name="test-reconnect-hang", cwd=str(bobi_install.repo_path))
    s._input_ready = asyncio.Event()
    s._client = None  # nothing to disconnect
    s._make_brain_session = lambda resume=None: _HangingClient()

    # Must return immediately rather than waiting for a phantom ResultMessage.
    await asyncio.wait_for(s._rotate(), timeout=5.0)

    assert s._client is not None
    assert s.detect_state() == "waiting_input"
    assert s.detect_state() != "error"
    assert s._rotation_count == 1
    assert _HangingClient.instances == 1
    assert _HangingClient.receive_calls == 0


class _ConnectHangsClient:
    """A client whose ``connect()`` itself blocks forever — the *literal* #472
    shape: "a hung fresh Claude Code SDK ``connect()``". Distinct from
    ``_HangingClient`` (connect() returns fast, the connect-turn drain hangs).

    Every client this test builds hangs in connect(), so even the final
    fresh-connect recovery in ``_recover_rotation_failure`` cannot complete —
    the session must surface *terminally* (loud raise + "error" state) within
    the timeout budget rather than hang forever."""

    provider = "anthropic"
    instances = 0

    def __init__(self, options=None):
        type(self).instances += 1
        self.disconnected = False
        self.connect_cancellations = 0

    async def connect(self):
        # Never returns and suppresses the first cancellation. A timeout helper
        # that waits for cooperative cancellation would still wedge here.
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.connect_cancellations += 1
                if self.connect_cancellations >= 2:
                    raise

    async def query(self, text):
        pass

    async def disconnect(self):
        self.disconnected = True

    async def receive_response(self):
        yield  # pragma: no cover - connect() never returns, drain never runs


class _ConnectNeedsDisconnectClient:
    """A connect task that ignores cancellation until adapter cleanup runs."""

    provider = "anthropic"

    def __init__(self):
        self.release = asyncio.Event()
        self.is_disconnected = False
        self.is_aborted = False

    async def connect(self):
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                continue

    async def disconnect(self):
        self.is_disconnected = True

    def abort(self):
        self.is_aborted = True
        self.release.set()


@pytest.mark.asyncio
async def test_timed_out_connect_is_tracked_until_adapter_cleanup(
    bobi_install, monkeypatch
):
    """Hard timeout must retain and reap cancellation-resistant connect work."""
    from bobi import session as session_mod

    monkeypatch.setattr(session_mod, "ROTATION_RECONNECT_TIMEOUT", 0.01)
    client = _ConnectNeedsDisconnectClient()
    session = Session(name="test-connect-reap", cwd=str(bobi_install.repo_path))
    session._make_brain_session = lambda resume=None: client

    try:
        with pytest.raises(asyncio.TimeoutError):
            await session._attempt_reconnect()

        for _ in range(20):
            if not session._hard_timeout_tasks:
                break
            await asyncio.sleep(0.01)

        assert client.is_disconnected is True
        assert client.is_aborted is True
        assert session._hard_timeout_tasks == set()
    finally:
        # Keep a failed pre-fix run from leaving an intentionally
        # cancellation-resistant task alive in pytest's event loop.
        client.abort()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_rotation_reconnect_bounds_hung_connect_and_surfaces_terminally(
    bobi_install, monkeypatch
):
    """#472 (the literal connect()-hang shape): a fresh ``connect()`` that hangs
    forever must NOT wedge ``_rotate()``. Every bounded reconnect attempt times
    out, and because the final fresh-connect recovery's ``connect()`` hangs too,
    the session surfaces *terminally* — a loud raise + the "error" state, never
    an infinite hang with ``rotation_count`` pinned at 0.

    This pins the variant the other reconnect tests don't: ``_HangingClient``
    lets connect() return and hangs the drain (so connect-only recovery
    succeeds, which is graceful); here connect() itself is the hang (so recovery
    can't succeed, which is terminal-but-bounded). Both are bounded by the hard
    timeout helper; removing it hangs this test forever.
    """
    from bobi import session as session_mod

    _ConnectHangsClient.instances = 0
    events = []
    monkeypatch.setattr(
        "bobi.events.client._log_event",
        lambda event, session_id="": events.append(event),
    )
    # Tiny bounds so the whole attempt → backoff → recovery budget is a fraction
    # of a second, well inside the 5s outer guard below.
    monkeypatch.setattr(session_mod, "ROTATION_RECONNECT_TIMEOUT", 0.05)
    monkeypatch.setattr(session_mod, "ROTATION_RECONNECT_BACKOFF", 0.0)
    monkeypatch.setattr(session_mod, "ROTATION_MAX_RECONNECT_ATTEMPTS", 2)

    s = Session(name="test-connect-hang", cwd=str(bobi_install.repo_path))
    s._input_ready = asyncio.Event()
    s._client = None  # nothing to disconnect
    s._make_brain_session = lambda resume=None: _ConnectHangsClient()

    # Bounded: _rotate() must RAISE (terminal) well within the outer guard
    # rather than block forever. The outer wait_for(5.0) is the regression
    # tripwire — if the bound were removed, this would trip at 5s instead of
    # the inner ~0.15s terminal raise.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(s._rotate(), timeout=5.0)

    # Terminal-but-loud: the inline rotation wrapper sets error state, no
    # addressable client left behind, rotation never falsely counted.
    assert s.detect_state() == "error"
    assert s._client is None
    assert s._rotation_count == 0
    # 2 bounded reconnect attempts + 1 final recovery client = 3 built; proves
    # the loop didn't hang on the first connect().
    assert _ConnectHangsClient.instances == 3

    failed_events = [
        event for event in events if event["type"] == "session.rotation_failed"
    ]
    assert failed_events
    terminal_payload = failed_events[-1]["payload"]
    assert terminal_payload["final_recovery_error"]
    assert "TimeoutError" in terminal_payload["final_recovery_error"]

    log_path = bobi_install.sessions_dir / "test-connect-hang" / "log.jsonl"
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    terminal_failure = [
        record for record in records
        if record["event"] == "rotation_failed" and record.get("final_recovery_error")
    ]
    assert terminal_failure
    assert "TimeoutError" in terminal_failure[-1]["final_recovery_error"]
