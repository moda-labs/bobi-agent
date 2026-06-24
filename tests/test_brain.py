"""Unit tests for the pluggable brain layer (epic #485, Phase 1).

Covers the brain registry/selector and the Claude adapter's behavior-preserving
normalization: SDK ``AssistantMessage``/``ResultMessage`` → normalized
``AssistantText``/``TurnResult``, including the model-usage → cost breakdown and
deferred-tool translation the call sites rely on.
"""

import pytest

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from modastack.brain import (
    AssistantText,
    BrainSession,
    ClaudeBrain,
    DEFAULT_BRAIN,
    TurnResult,
    get_brain,
)
from modastack.brain.claude import _ClaudeSession, _result_to_turn


# --- registry / selector ---------------------------------------------------


def test_default_brain_is_claude():
    assert DEFAULT_BRAIN == "claude"
    assert get_brain().name == "claude"
    assert get_brain(None).provider == "anthropic"


def test_explicit_claude_kind():
    assert isinstance(get_brain("claude"), ClaudeBrain)


def test_unknown_brain_kind_fails_loud():
    with pytest.raises(ValueError, match="unknown brain kind"):
        get_brain("gpt-9")


# --- ResultMessage → TurnResult normalization ------------------------------


def _result(**kw):
    base = dict(
        subtype="success",
        duration_ms=12,
        duration_api_ms=6,
        is_error=False,
        num_turns=2,
        session_id="sess-xyz",
        total_cost_usd=0.0,
        usage={},
    )
    base.update(kw)
    return ResultMessage(**base)


def test_result_to_turn_carries_core_fields():
    msg = _result(total_cost_usd=0.5, result="ok")
    turn = _result_to_turn(msg)
    assert isinstance(turn, TurnResult)
    assert turn.session_id == "sess-xyz"
    assert turn.is_error is False
    assert turn.total_cost_usd == 0.5
    assert turn.duration_ms == 12
    assert turn.num_turns == 2
    assert turn.result_text == "ok"
    assert turn.deferred_tool is None


def test_result_to_turn_normalizes_model_usage_list_of_objects():
    """The breakdown is read per-element via getattr (model/input/output) — the
    shape ``session.py`` has always consumed. A list of usage objects populates
    the BrainCost list."""

    class _MU:
        def __init__(self, model, i, o):
            self.model, self.input_tokens, self.output_tokens = model, i, o

    msg = _result(model_usage=[_MU("claude-opus-4-8", 10, 3), _MU("haiku", 1, 1)])
    turn = _result_to_turn(msg)
    assert [(c.model, c.input_tokens, c.output_tokens) for c in turn.costs] == [
        ("claude-opus-4-8", 10, 3),
        ("haiku", 1, 1),
    ]


def test_result_to_turn_dict_model_usage_preserves_legacy_noop():
    """Behavior-preservation guard (#485 Phase 1): the SDK actually types
    ``model_usage`` as ``dict[str, Any]``, but the legacy code (and this faithful
    adapter) iterate it as a list-of-objects, so a dict is wrapped as one element
    whose getattr lookups miss → an empty/zero BrainCost. This is a pre-existing
    latent bug, deliberately preserved here; fixing the dict shape is a tracked
    follow-up, not a Phase-1 behavior change."""
    msg = _result(model_usage={"claude-opus-4-8": {"input_tokens": 10, "output_tokens": 3}})
    turn = _result_to_turn(msg)
    assert len(turn.costs) == 1
    assert turn.costs[0].model == ""
    assert turn.costs[0].input_tokens == 0
    assert turn.costs[0].output_tokens == 0


def test_result_to_turn_handles_error_and_status():
    msg = _result(is_error=True, result="API Error: 529 Overloaded")
    msg.api_error_status = 529
    turn = _result_to_turn(msg)
    assert turn.is_error is True
    assert turn.api_error_status == 529
    assert turn.result_text == "API Error: 529 Overloaded"


def test_result_to_turn_translates_deferred_tool():
    msg = _result()

    class _Deferred:
        name = "AskUserQuestion"
        input = {"q": "which?"}

    msg.deferred_tool_use = _Deferred()
    turn = _result_to_turn(msg)
    assert turn.deferred_tool is not None
    assert turn.deferred_tool.name == "AskUserQuestion"
    assert turn.deferred_tool.input == {"q": "which?"}


# --- session-level message stream conversion -------------------------------


class _FakeSDKClient:
    """Stands in for ClaudeSDKClient: replays SDK messages for one turn."""

    def __init__(self, messages):
        self._messages = messages

    async def receive_response(self):
        for m in self._messages:
            yield m


def _claude_session_over(messages):
    """A _ClaudeSession whose underlying SDK client is swapped for a fake."""
    sess = _ClaudeSession.__new__(_ClaudeSession)
    sess._client = _FakeSDKClient(messages)
    return sess


@pytest.mark.asyncio
async def test_receive_response_converts_assistant_and_result():
    assistant = AssistantMessage(
        content=[TextBlock(text="hello"), TextBlock(text="world")],
        model="claude-opus-4-8",
        usage={"input_tokens": 5, "cache_read_input_tokens": 100},
    )
    out = []
    async for m in _claude_session_over([assistant, _result(total_cost_usd=0.1)]).receive_response():
        out.append(m)

    assert isinstance(out[0], AssistantText)
    assert out[0].text == "hello\nworld"
    assert out[0].usage == {"input_tokens": 5, "cache_read_input_tokens": 100}
    assert isinstance(out[1], TurnResult)
    assert out[1].total_cost_usd == 0.1


@pytest.mark.asyncio
async def test_assistant_without_text_still_carries_usage():
    """An assistant message with no TextBlocks yields empty text but keeps usage
    (the rotation metric reads usage even on a text-less step)."""
    assistant = AssistantMessage(content=[], model="m", usage={"input_tokens": 7})
    out = [m async for m in _claude_session_over([assistant]).receive_response()]
    assert out[0].text == ""
    assert out[0].usage == {"input_tokens": 7}


def test_claude_session_satisfies_brain_session_protocol():
    sess = _claude_session_over([])
    assert isinstance(sess, BrainSession)
    assert sess.provider == "anthropic"
