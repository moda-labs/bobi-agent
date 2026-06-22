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

from claude_agent_sdk import ResultMessage

from modastack.inbox import Message
from modastack.session import COMPACT_SENTINEL, Session, _context_fill_tokens


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
    s = Session(name="test-rot", cwd=str(modastack_install.repo_path))
    s._input_ready = asyncio.Event()
    s._rotation_token_cap = 275_000
    s._client = _FakeClient([
        _result({
            "input_tokens": 2,
            "cache_read_input_tokens": 422_468,
            "cache_creation_input_tokens": 1_262,
            "output_tokens": 3_432,
        })
    ])

    await s._drain_turn()

    assert s._rotate_pending is True


@pytest.mark.asyncio
async def test_no_rotation_below_cap(modastack_install):
    """A small turn well under the cap leaves rotation un-pending."""
    s = Session(name="test-rot-small", cwd=str(modastack_install.repo_path))
    s._input_ready = asyncio.Event()
    s._rotation_token_cap = 275_000
    s._client = _FakeClient([
        _result({
            "input_tokens": 1_000,
            "cache_read_input_tokens": 40_000,
            "cache_creation_input_tokens": 2_000,
            "output_tokens": 500,
        })
    ])

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
