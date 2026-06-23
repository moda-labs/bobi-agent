"""Context-rotation metric + manual compact (regression for the prod bug).

The deployed eng-team manager grew to ~424K tokens of context without ever
rotating, because the rotation check measured ``input_tokens`` only. With
prompt caching on, ``input_tokens`` is just the *uncached* delta (≈2 on a
warm turn) — the real conversation lives in ``cache_read_input_tokens``.
So the 275K cap compared ~2 >= 275_000 and never fired.

These tests pin the true metric (input + cache_read + cache_creation) and
the manual ``compact`` trigger.
"""

import asyncio

import pytest

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from modastack.inbox import Message
from modastack.session import (
    COMPACT_SENTINEL,
    ROTATION_MAX_FLUSH_ATTEMPTS,
    Session,
    _context_fill_tokens,
)


class _FakeClient:
    """Async client stub whose ``receive_response`` replays canned messages."""

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


def _result(usage: dict | None) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
        total_cost_usd=0.01,
        usage=usage,
        # Mirror the real warm-turn shape: model_usage carries only the tiny
        # uncached delta, exactly what the old check mistakenly trusted.
        model_usage={"claude-opus-4-8": {"input_tokens": 2, "output_tokens": 3}},
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
async def test_rotation_triggers_when_context_is_cached(modastack_install):
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
    s = Session(name="test-rot", cwd=str(modastack_install.repo_path))
    s._input_ready = asyncio.Event()
    s._rotation_token_cap = 275_000
    # A single-call turn: the one assistant message carries the warm usage; the
    # ResultMessage aggregate equals it. Fill = 423_732 >= cap → rotate.
    s._client = _FakeClient([_assistant(warm), _result(warm)])

    await s._drain_turn()

    assert s._rotate_pending is True


@pytest.mark.asyncio
async def test_no_rotation_below_cap(modastack_install):
    """A small turn well under the cap leaves rotation un-pending."""
    small = {
        "input_tokens": 1_000,
        "cache_read_input_tokens": 40_000,
        "cache_creation_input_tokens": 2_000,
        "output_tokens": 500,
    }
    s = Session(name="test-rot-small", cwd=str(modastack_install.repo_path))
    s._input_ready = asyncio.Event()
    s._rotation_token_cap = 275_000
    s._client = _FakeClient([_assistant(small), _result(small)])

    await s._drain_turn()

    assert s._rotate_pending is False


@pytest.mark.asyncio
async def test_compact_sentinel_requests_rotation_without_querying_model(modastack_install):
    """`modastack compact` delivers a sentinel that flags rotation, not a prompt."""
    s = Session(name="test-compact", cwd=str(modastack_install.repo_path))
    s._input_ready = asyncio.Event()
    s._set_state("waiting_input")
    client = _FakeClient([])
    s._client = client

    await s._process_message(Message(id="m1", sender="cli", text=COMPACT_SENTINEL))

    assert s._rotate_pending is True
    assert s._rotate_force is True
    # The sentinel is a control signal — it must never reach the model.
    assert client.queries == []


@pytest.mark.asyncio
async def test_multi_call_turn_does_not_over_count_context(modastack_install):
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

    s = Session(name="test-multicall", cwd=str(modastack_install.repo_path))
    s._input_ready = asyncio.Event()
    s._rotation_token_cap = 275_000
    s._client = _FakeClient([
        _assistant(per_call),
        _assistant(per_call),
        _assistant(per_call),
        _result(cumulative),
    ])

    await s._drain_turn()

    # Real fill 100_010 < 275_000 — no rotation. (Old code: 300_030 >= cap → True.)
    assert s._rotate_pending is False
    assert s._rotate_force is False


@pytest.mark.asyncio
async def test_multi_call_turn_rotates_when_single_call_is_over_cap(modastack_install):
    """When a single call's real fill exceeds the cap, rotation fires and forces.

    Complements the over-count test: the metric must still trip on a genuine
    over-cap, and over-cap auto-rotation force-compacts (#454) so an unchanged
    decision log can't block it later.
    """
    per_call = {
        "input_tokens": 10,
        "cache_read_input_tokens": 300_000,
        "cache_creation_input_tokens": 0,
        "output_tokens": 50,
    }
    s = Session(name="test-multicall-over", cwd=str(modastack_install.repo_path))
    s._input_ready = asyncio.Event()
    s._rotation_token_cap = 275_000
    s._client = _FakeClient([
        _assistant(per_call),
        _assistant(per_call),
        _assistant(per_call),
        _result({"input_tokens": 30, "cache_read_input_tokens": 900_000}),
    ])

    await s._drain_turn()

    assert s._rotate_pending is True
    assert s._rotate_force is True  # over-cap auto-rotation force-compacts


@pytest.mark.asyncio
async def test_over_cap_rotates_even_when_decision_log_unchanged(modastack_install):
    """Over-cap + a no-op (unchanged INDEX.md) flush must STILL rotate (#454).

    The wedge: auto-rotation verified the flush and, on an unchanged decision
    log, logged "Flush no-op … skipping rotation" and never compacted — so a
    real over-cap could never self-heal. With force-compact, an unchanged
    decision log no longer blocks rotation.
    """
    s = Session(name="test-forcerot", cwd=str(modastack_install.repo_path))
    s._input_ready = asyncio.Event()
    # Simulate the over-cap decision _drain_turn now makes: pending + forced.
    s._rotate_pending = True
    s._rotate_force = True
    # Flush turn replays a small ResultMessage so the drain completes cleanly.
    s._client = _FakeClient([
        _result({"input_tokens": 1, "cache_read_input_tokens": 10})
    ])

    rotated = []

    async def _fake_rotate():
        rotated.append(True)
        s._rotate_pending = False
        s._rotate_force = False

    s._rotate = _fake_rotate

    # No INDEX.md exists for this session → the flush is a genuine no-op.
    assert s._verify_flush() is False

    await s._do_flush_and_rotate()

    assert rotated == [True]  # rotated despite the unchanged decision log


@pytest.mark.asyncio
async def test_unforced_rotation_is_bounded_not_livelocked(modastack_install):
    """A non-forced rotation whose flush keeps no-op'ing still rotates, bounded.

    Belt-and-suspenders for #454: even a hypothetical non-forced rotation path
    can't no-op-livelock — after ROTATION_MAX_FLUSH_ATTEMPTS the session rotates
    unconditionally rather than re-injecting flush prompts forever.
    """
    s = Session(name="test-bounded", cwd=str(modastack_install.repo_path))
    s._input_ready = asyncio.Event()
    s._rotate_pending = True
    s._rotate_force = False  # exercise the attempt bound, not the force path

    rotated = []

    async def _fake_rotate():
        rotated.append(s._rotate_attempts)
        s._rotate_pending = False

    s._rotate = _fake_rotate

    # Every flush is a no-op (no INDEX.md) → _verify_flush() False each cycle.
    for _ in range(ROTATION_MAX_FLUSH_ATTEMPTS):
        s._client = _FakeClient([_result({"input_tokens": 1})])
        await s._do_flush_and_rotate()

    # Deferred on attempts 1..N-1, rotated exactly once on the bound.
    assert rotated == [ROTATION_MAX_FLUSH_ATTEMPTS]
