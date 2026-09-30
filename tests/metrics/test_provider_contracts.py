import json
from pathlib import Path

import pytest

from bobi.brain.claude import _result_to_turn
from bobi.brain.codex import _CodexSession
from bobi.metrics.providers import (
    claude_usage,
    codex_usage,
    parse_claude_transcript,
    parse_codex_rollout,
    parse_codex_stdout,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "metrics"


def _jsonl(name):
    return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]


def test_claude_fixture_preserves_ttl_cache_dimensions_and_deduplicates():
    usage = parse_claude_transcript(FIXTURES / "claude-transcript.jsonl")
    assert len(usage) == 2
    by_model = {item.model: item for item in usage}
    assert by_model["claude-opus-4-8"].provider_event_id == "msg-fixture-1"
    assert by_model["claude-opus-4-8"].comparable_tokens() == {
        "input_tokens": 39_860,
        "uncached_input_tokens": 2,
        "cache_read_input_tokens": 15_282,
        "cache_write_input_tokens": 24_576,
        "cache_write_5m_input_tokens": 16_384,
        "cache_write_1h_input_tokens": 8_192,
        "cache_write_unknown_ttl_input_tokens": None,
        "cache_write_breakdown_complete": True,
        "output_tokens": 10,
        "reasoning_output_tokens": None,
    }
    assert by_model["claude-haiku-4-5"].comparable_tokens() == {
        "input_tokens": 131,
        "uncached_input_tokens": 3,
        "cache_read_input_tokens": 128,
        "cache_write_input_tokens": 0,
        "cache_write_5m_input_tokens": None,
        "cache_write_1h_input_tokens": None,
        "cache_write_unknown_ttl_input_tokens": 0,
        "cache_write_breakdown_complete": False,
        "output_tokens": 4,
        "reasoning_output_tokens": 1,
    }


def test_claude_transcript_keeps_valid_prefix_before_invalid_utf8_tail(tmp_path):
    path = tmp_path / "transcript.jsonl"
    fixture = (FIXTURES / "claude-transcript.jsonl").read_bytes()
    path.write_bytes(fixture + b'{"type":"assistant","message":"\xff')

    usage = parse_claude_transcript(path)
    assert {item.model for item in usage} == {
        "claude-opus-4-8",
        "claude-haiku-4-5",
    }


def test_claude_missing_cache_dimension_stays_null_not_zero():
    usage = claude_usage(
        {"input_tokens": 7, "output_tokens": 3},
        model="claude-test",
        provider_event_id="req-1",
    )
    assert usage.cache_read_input_tokens is None
    assert usage.cache_write_input_tokens is None
    assert usage.input_tokens == 7


def test_claude_usage_prefers_provider_canonical_model_over_requested_alias():
    usage = claude_usage(
        {
            "inputTokens": 7,
            "outputTokens": 3,
            "canonicalModel": "deepseek-flash",
        },
        model="ds/deepseek-flash",
        provider_event_id="req-1",
    )

    assert usage.model == "deepseek-flash"


def test_codex_stdout_uses_terminal_total_not_progress_counters():
    usage = parse_codex_stdout(_jsonl("codex-stdout.jsonl"), "gpt-test")
    assert usage.input_tokens == 4096
    assert usage.cache_read_input_tokens == 1024
    assert usage.cache_write_unknown_ttl_input_tokens == 128
    assert usage.output_tokens == 30
    assert usage.reasoning_output_tokens == 12


def test_codex_rollout_uses_last_exact_record_and_turn_model():
    usage = parse_codex_rollout(FIXTURES / "codex-rollout.jsonl")
    assert len(usage) == 1
    assert usage[0].model == "gpt-test"
    assert usage[0].input_tokens == 4096
    assert usage[0].output_tokens == 30
    assert usage[0].provider_event_id == "resp-fixture-final"


def test_codex_rollout_keeps_valid_prefix_before_invalid_utf8_tail(tmp_path):
    path = tmp_path / "rollout.jsonl"
    fixture = (FIXTURES / "codex-rollout.jsonl").read_bytes()
    path.write_bytes(fixture + b'{"type":"token_usage_record","payload":"\xff')

    usage = parse_codex_rollout(path)
    assert len(usage) == 1
    assert usage[0].provider_event_id == "resp-fixture-final"


def test_codex_reasoning_is_a_subset_not_added_to_output():
    usage = codex_usage(
        {
            "input_tokens": 10,
            "cached_input_tokens": 4,
            "output_tokens": 8,
            "reasoning_output_tokens": 5,
        },
        model="gpt-test",
    )
    assert usage.output_tokens == 8
    assert usage.reasoning_output_tokens == 5


def test_claude_adapter_exposes_lossless_usage_and_legacy_cost():
    class Result:
        model_usage = {
            "claude-test": {
                "inputTokens": 2,
                "cacheReadInputTokens": 3,
                "cacheCreationInputTokens": 5,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 5,
                    "ephemeral_1h_input_tokens": 0,
                },
                "outputTokens": 7,
            }
        }
        deferred_tool_use = None
        is_error = False
        session_id = "session"
        result = ""

    result = _result_to_turn(Result())
    assert result.costs[0].input_tokens == 10
    assert result.usage[0].uncached_input_tokens == 2
    assert result.usage[0].cache_write_5m_input_tokens == 5


def test_claude_adapter_enriches_primary_model_from_result_usage():
    class Result:
        model_usage = {
            "claude-haiku": {
                "inputTokens": 11,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "outputTokens": 2,
                "thinkingTokens": 1,
            },
            "claude-opus": {
                "inputTokens": 2,
                "cacheReadInputTokens": 3,
                "cacheCreationInputTokens": 5,
                "outputTokens": 7,
                "thinkingTokens": 4,
            },
        }
        usage = {
            "input_tokens": 2,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 5,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 1,
                "ephemeral_1h_input_tokens": 4,
            },
            "output_tokens": 7,
            "output_tokens_details": {"thinking_tokens": 4},
        }
        deferred_tool_use = None
        is_error = False
        session_id = "session"
        result = ""

    result = _result_to_turn(Result())
    usage = {item.model: item for item in result.usage}
    assert set(usage) == {"claude-haiku", "claude-opus"}
    assert usage["claude-opus"].cache_write_5m_input_tokens == 1
    assert usage["claude-opus"].cache_write_1h_input_tokens == 4
    assert usage["claude-opus"].cache_write_breakdown_complete is True
    assert usage["claude-opus"].reasoning_output_tokens == 4
    assert usage["claude-haiku"].cache_write_5m_input_tokens is None
    assert usage["claude-haiku"].reasoning_output_tokens == 1


async def _runner(_argv, _cwd, _stdin):
    for event in _jsonl("codex-stdout.jsonl"):
        yield event


@pytest.mark.asyncio
async def test_codex_adapter_exposes_lossless_usage_and_legacy_cost():
    session = _CodexSession(
        cwd=".", instructions="", model="gpt-test", runner=_runner
    )
    await session.query("fixture")
    messages = [message async for message in session.receive_response()]
    result = messages[-1]
    assert result.costs[0].cached_input_tokens == 1024
    assert result.usage[0].cache_write_input_tokens == 128
    assert result.usage[0].reasoning_output_tokens == 12
