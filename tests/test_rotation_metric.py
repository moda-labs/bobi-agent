"""Context-rotation metric, manual compact, and the bounded/recoverable
rotation reconnect (regressions for the prod wedges).

The deployed eng-team manager grew to ~424K tokens of context without ever
rotating, because the rotation check measured ``input_tokens`` only. With
prompt caching on, ``input_tokens`` is just the *uncached* delta (≈2 on a
warm turn) — the real conversation lives in ``cache_read_input_tokens``.
So the 275K cap compared ~2 >= 275_000 and never fired. (#454)

The 2026-06-24 recurrence was a *different* mechanism: the rotation reconnect
(``connect()`` + the connect-turn drain) was unbounded, so a fresh ``claude``
subprocess that never yielded a ``ResultMessage`` hung ``_rotate()`` forever —
``_rotation_count`` stuck, the run loop off ``inbox.recv``. #456 bounds and
recovers that reconnect.

These tests pin the true metric (input + cache_read + cache_creation), the
manual ``compact`` trigger, and the bounded/recoverable reconnect.
"""

import asyncio

import pytest

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from bobi.brain import AssistantText, BrainCost, BrainSession, TurnResult
from bobi.inbox import Message
from bobi.session import (
    COMPACT_SENTINEL,
    Session,
    _context_fill_tokens,
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
    """`bobi compact` delivers a sentinel that flags rotation, not a prompt."""
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
    """A client whose connect() returns fast but whose connect-turn drain
    never yields a ResultMessage — the exact 2026-06-24 wedge shape. The fresh
    ``claude`` subprocess connects but the first turn blocks forever."""

    instances = 0

    def __init__(self, options=None):
        type(self).instances += 1
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def receive_response(self):
        # Block forever — the connect-turn drain never completes. asyncio.wait_for
        # in _attempt_reconnect must bound this; without the bound _rotate() hangs.
        await asyncio.Event().wait()
        yield  # pragma: no cover - unreachable


class _ErrorTurnClient:
    """A client that connects fine but whose connect-turn ResultMessage arrives
    carrying an API error (e.g. 529 Overloaded) — the #443 *arrives-with-error*
    shape, distinct from the never-yields hang."""

    def __init__(self, options=None):
        self.connected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        pass

    async def receive_response(self):
        yield _result({"input_tokens": 1, "cache_read_input_tokens": 10},
                      is_error=True, api_error_status=529)


@pytest.mark.asyncio
async def test_rotation_reconnect_bounds_and_recovers_on_never_yields(
    bobi_install, monkeypatch
):
    """Mechanism #3 (the 2026-06-24 wedge): a connect turn that never yields a
    ResultMessage must NOT hang _rotate() forever. The reconnect is bounded by
    ROTATION_RECONNECT_TIMEOUT, retried ROTATION_MAX_RECONNECT_ATTEMPTS times,
    then RECOVERS into an addressable connected client — never a silent park,
    never the terminal "error" state that would deafen the session (#443).
    """
    import claude_agent_sdk
    from bobi import session as session_mod

    _HangingClient.instances = 0
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _HangingClient)
    # Tiny bounds so the test runs in a fraction of a second.
    monkeypatch.setattr(session_mod, "ROTATION_RECONNECT_TIMEOUT", 0.05)
    monkeypatch.setattr(session_mod, "ROTATION_RECONNECT_BACKOFF", 0.0)
    monkeypatch.setattr(session_mod, "ROTATION_MAX_RECONNECT_ATTEMPTS", 2)

    s = Session(name="test-reconnect-hang", cwd=str(bobi_install.repo_path))
    s._input_ready = asyncio.Event()
    s._client = None  # nothing to disconnect

    # Must return (bounded) rather than block forever.
    await asyncio.wait_for(s._rotate(), timeout=5.0)

    # Every connect-turn attempt hung and timed out; final fresh-connect
    # recovery (connect-only, no drain) succeeded → session is addressable.
    assert s._client is not None
    assert s.detect_state() == "waiting_input"   # NOT terminal "error"
    assert s.detect_state() != "error"
    # Reconnect attempts (2) + the final recovery client (1) were all built.
    assert _HangingClient.instances == 3


@pytest.mark.asyncio
async def test_rotation_reconnect_clears_error_on_arrives_with_529(
    bobi_install, monkeypatch
):
    """A connect-turn ResultMessage that arrives with is_error/529 must clear
    via the #443 path and return the session to ready — NOT the terminal
    "error" state. This is the *arrives-with-error* shape; step 1's timeout
    covers the *never-arrives* shape (the test above)."""
    import claude_agent_sdk
    from bobi import session as session_mod

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _ErrorTurnClient)
    monkeypatch.setattr(session_mod, "ROTATION_RECONNECT_TIMEOUT", 2.0)
    monkeypatch.setattr(session_mod, "ROTATION_MAX_RECONNECT_ATTEMPTS", 2)

    s = Session(name="test-reconnect-529", cwd=str(bobi_install.repo_path))
    s._input_ready = asyncio.Event()
    s._client = None

    await asyncio.wait_for(s._rotate(), timeout=5.0)

    # The error ResultMessage was handled (no exception, no timeout): the
    # reconnect succeeded on the first attempt and the session is ready. The
    # live client is now a brain session wrapping _ErrorTurnClient (the adapter
    # converted its SDK ResultMessage into a normalized TurnResult).
    assert s._client is not None
    assert isinstance(s._client, BrainSession)
    assert s.detect_state() == "waiting_input"
    assert s.detect_state() != "error"
    assert s._rotation_count == 1  # rotation completed, not wedged at 0


class _ConnectHangsClient:
    """A client whose ``connect()`` itself blocks forever — the *literal* #472
    shape: "a hung fresh Claude Code SDK ``connect()``". Distinct from
    ``_HangingClient`` (connect() returns fast, the connect-turn drain hangs).

    Every client this test builds hangs in connect(), so even the final
    fresh-connect recovery in ``_recover_rotation_failure`` cannot complete —
    the session must surface *terminally* (loud raise + "error" state) within
    the timeout budget rather than hang forever."""

    instances = 0

    def __init__(self, options=None):
        type(self).instances += 1
        self.disconnected = False

    async def connect(self):
        # Never returns — the connect() hang the wedge was made of. The
        # asyncio.wait_for in _attempt_reconnect / _recover_rotation_failure
        # is the only thing that can unstick this; without it _rotate() blocks
        # forever and the run loop never returns to inbox.recv.
        await asyncio.Event().wait()

    async def disconnect(self):
        self.disconnected = True

    async def receive_response(self):
        yield  # pragma: no cover - connect() never returns, drain never runs


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
    succeeds → graceful); here connect() itself is the hang (so recovery can't
    succeed → terminal-but-bounded). Both are bounded by the same
    ``asyncio.wait_for``; removing it hangs this test forever.
    """
    import claude_agent_sdk
    from bobi import session as session_mod

    _ConnectHangsClient.instances = 0
    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", _ConnectHangsClient)
    # Tiny bounds so the whole attempt → backoff → recovery budget is a fraction
    # of a second, well inside the 5s outer guard below.
    monkeypatch.setattr(session_mod, "ROTATION_RECONNECT_TIMEOUT", 0.05)
    monkeypatch.setattr(session_mod, "ROTATION_RECONNECT_BACKOFF", 0.0)
    monkeypatch.setattr(session_mod, "ROTATION_MAX_RECONNECT_ATTEMPTS", 2)

    s = Session(name="test-connect-hang", cwd=str(bobi_install.repo_path))
    s._input_ready = asyncio.Event()
    s._client = None  # nothing to disconnect

    # Bounded: _rotate() must RAISE (terminal) well within the outer guard
    # rather than block forever. The outer wait_for(5.0) is the regression
    # tripwire — if the bound were removed, this would trip at 5s instead of
    # the inner ~0.15s terminal raise.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(s._rotate(), timeout=5.0)

    # Terminal-but-loud: error state set by _recover_rotation_failure, no
    # addressable client left behind, rotation never falsely counted.
    assert s.detect_state() == "error"
    assert s._client is None
    assert s._rotation_count == 0
    # 2 bounded reconnect attempts + 1 final recovery client = 3 built; proves
    # the loop didn't hang on the first connect().
    assert _ConnectHangsClient.instances == 3
