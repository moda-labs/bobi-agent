"""Provider accounting and runtime telemetry regressions."""

import json
import pytest
from bobi.brain.base import (
    AssistantText,
    BrainCost,
    BrainInvocation,
    BrainToolExecution,
    BrainUsage,
    TurnResult,
)
from bobi.brain.claude import _result_to_turn
from bobi.brain.codex import _CodexSession
from bobi.brain.turns import drain_turn
from bobi.inbox import Message
from bobi.metrics import runtime as metrics_runtime
from bobi.metrics.collector import MetricsCollectorService
from bobi.metrics.faults import FaultInjectionRefused, arm_fault, read_fault
from bobi.metrics.providers import (
    claude_usage,
    codex_usage,
    parse_claude_transcript,
    parse_codex_rollout,
    parse_codex_stdout,
)
from bobi.metrics.runtime import MetricsRuntime, _aggregate_usage
from bobi.metrics.store import connect
from bobi.sdk import SessionEntry, get_registry
from bobi.session import Session
from tests.metrics.helpers import JSONL, metrics_fixtures


@pytest.mark.parametrize("value", [None, "", "disabled", "off", "0", "false", "shadow", "full", "invalid"])
def test_metrics_mode_defaults_or_fails_closed(value):
    env = {} if value is None else {"BOBI_METRICS_MODE": value}
    assert metrics_runtime.resolve_mode(env) == "disabled"


def test_metrics_mode_requires_explicit_enablement():
    assert metrics_runtime.resolve_mode({"BOBI_METRICS_MODE": " ENABLED "}) == "enabled"


def test_turn_observation_never_routes(monkeypatch, tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    def reject_routing(*args, **kwargs):
        raise AssertionError("turn observation must not route")
    monkeypatch.setattr("bobi.metrics.router.assign_variant", reject_routing)
    observation = runtime.begin_turn("agent", provider="openai", model_requested="configured")
    assert observation.router_decision is None
    observation.finish(status="completed")
    assert runtime.close(timeout=2)


class _Observation:
    def __init__(self):
        self.first_output = 0
        self.results = []
        self.finishes = []

    def mark_first_output(self):
        self.first_output += 1

    def record_result(self, result):
        self.results.append(result)

    def finish(self, **fields):
        self.finishes.append(fields)


class _Client:
    provider = "anthropic"

    def __init__(self, result=None):
        self.result = result or TurnResult(session_id="provider-session")
        self.queries = []

    async def query(self, text):
        self.queries.append(text)

    async def receive_response(self):
        yield AssistantText(text="answer")
        yield self.result


@pytest.mark.asyncio
async def test_shared_drain_attaches_the_observer(monkeypatch):
    observation = _Observation()
    monkeypatch.setattr(
        "bobi.brain.turns.observe_turn", lambda *args, **kwargs: observation
    )
    monkeypatch.setattr("bobi.brain.turns.save_session_id", lambda *args, **kwargs: None)
    monkeypatch.setattr("bobi.brain.turns.log_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "bobi.brain.turns.observe_brain_turn", lambda *args, **kwargs: None
    )

    outcome = await drain_turn(_Client(), "spawn", model="model")

    assert outcome.final_text == "answer"
    assert observation.first_output == 1
    assert len(observation.results) == 1
    assert observation.finishes == [{"status": "completed", "error_kind": ""}]


@pytest.mark.asyncio
async def test_persistent_session_uses_one_observation_for_the_message(
    bobi_install, monkeypatch
):
    observation = _Observation()
    monkeypatch.setattr("bobi.session.observe_turn", lambda *args, **kwargs: observation)
    session = Session(name="metrics-session", cwd=str(bobi_install.repo_path))
    session._input_ready = __import__("asyncio").Event()
    session._set_state("waiting_input")
    session._client = _Client()

    await session._process_message(
        Message(id="message-1", sender="slack", text="hello")
    )

    assert session._client.queries == ["hello"]
    assert len(observation.results) == 1
    assert observation.finishes == [{"status": "completed", "error_kind": ""}]


def test_runtime_projects_turn_invocation_tool_usage_and_cost(tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    observation = runtime.begin_turn(
        "agent",
        provider="anthropic",
        brain="claude",
        trigger_kind="inbox",
        trigger_id="message-1",
        is_user_initiated=True,
        model_requested="claude-test",
    )
    usage = BrainUsage(
        model="claude-test",
        provider_event_id="message-provider-1",
        input_tokens=15,
        uncached_input_tokens=2,
        cache_read_input_tokens=10,
        cache_write_input_tokens=3,
        cache_write_unknown_ttl_input_tokens=3,
        output_tokens=4,
        raw_usage={"input_tokens": 2, "output_tokens": 4},
    )
    observation.record_result(TurnResult(
        session_id="provider-session",
        provider_turn_id="provider-turn",
        total_cost_usd=0.01,
        duration_ms=12,
        api_duration_ms=9,
        usage=[usage],
        invocations=[BrainInvocation(
            provider_event_id="message-provider-1",
            model="claude-test",
            started_at_us=observation.started_at_us,
            ended_at_us=observation.started_at_us + 10,
            usage=usage,
        )],
        tool_executions=[BrainToolExecution(
            provider_tool_call_id="tool-1",
            tool_name="view_file",
            tool_kind="client",
            triggering_provider_event_id="message-provider-1",
            started_at_us=observation.started_at_us + 2,
            ended_at_us=observation.started_at_us + 8,
        )],
    ))
    observation.finish(status="completed")
    assert runtime.close(timeout=2.0)

    service = MetricsCollectorService(tmp_path)
    result = service.collect_once()
    assert result["role"] == "active"
    conn = connect(service.db_path, readonly=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM llm_invocations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tool_executions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM usage_measurements").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM cost_measurements").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM usage_measurements WHERE is_estimated=0"
        ).fetchone()[0] == 2
        raw = "\n".join(
            row[0] for row in conn.execute("SELECT payload_json FROM raw_events")
        )
        assert "message-1" in raw
        assert "hello" not in raw
    finally:
        conn.close()


def test_online_turn_aggregate_preserves_unreported_dimensions_as_null():
    complete = BrainUsage(
        model="claude-test",
        input_tokens=10,
        cache_write_input_tokens=3,
        cache_write_5m_input_tokens=3,
        cache_write_1h_input_tokens=0,
        cache_write_breakdown_complete=True,
        output_tokens=4,
    )
    partial = BrainUsage(
        model="claude-test",
        input_tokens=20,
        cache_write_input_tokens=5,
        cache_write_unknown_ttl_input_tokens=5,
        cache_write_breakdown_complete=False,
        output_tokens=None,
    )

    aggregate = _aggregate_usage([complete, partial])

    assert aggregate["input_tokens"] == 30
    assert aggregate["output_tokens"] is None
    assert aggregate["cache_write_5m_input_tokens"] is None
    assert aggregate["cache_write_unknown_ttl_input_tokens"] == 5
    assert aggregate["cache_write_breakdown_complete"] == 0


def test_turn_aggregate_fills_missing_dimensions_from_exact_invocations(tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    observation = runtime.begin_turn(
        "agent", provider="anthropic", model_requested="claude-test"
    )
    turn_usage = BrainUsage(
        model="claude-test",
        input_tokens=10,
        output_tokens=4,
    )
    invocation_usage = BrainUsage(
        model="claude-test",
        input_tokens=10,
        cache_read_input_tokens=7,
        output_tokens=4,
    )
    observation.record_result(TurnResult(
        usage=[turn_usage],
        invocations=[BrainInvocation(
            model="claude-test",
            provider_event_id="message-1",
            usage=invocation_usage,
        )],
    ))
    observation.finish(status="completed")
    runtime.finish_session("agent", status="completed")
    assert runtime.close(timeout=2)
    service = MetricsCollectorService(tmp_path)
    service.collect_once()
    conn = connect(service.db_path, readonly=True)
    try:
        row = conn.execute(
            "SELECT cache_read_input_tokens FROM best_usage WHERE scope='turn'"
        ).fetchone()
    finally:
        conn.close()

    assert row["cache_read_input_tokens"] == 7


def test_turn_aggregate_fills_alias_dimension_without_rewriting_model(tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    observation = runtime.begin_turn(
        "agent", provider="anthropic", model_requested="claude-sonnet"
    )
    turn_usage = BrainUsage(
        model="claude-3-7-sonnet-20250219",
        input_tokens=10,
        output_tokens=4,
    )
    invocation_usage = BrainUsage(
        model="claude-sonnet-4-5-20250929",
        input_tokens=10,
        cache_read_input_tokens=7,
        output_tokens=4,
    )
    observation.record_result(TurnResult(
        usage=[turn_usage],
        invocations=[BrainInvocation(
            model="claude-sonnet-4-5-20250929",
            provider_event_id="message-1",
            usage=invocation_usage,
        )],
    ))
    observation.finish(status="completed")
    runtime.finish_session("agent", status="completed")
    assert runtime.close(timeout=2)
    service = MetricsCollectorService(tmp_path)
    service.collect_once()
    conn = connect(service.db_path, readonly=True)
    try:
        row = conn.execute(
            "SELECT model,cache_read_input_tokens FROM best_usage WHERE scope='turn'"
        ).fetchone()
    finally:
        conn.close()

    assert tuple(row) == ("claude-3-7-sonnet-20250219", 7)


def test_runtime_startup_failure_never_reaches_the_turn(tmp_path):
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    runtime = MetricsRuntime(tmp_path, mode="enabled", producer_factory=fail)
    observation = runtime.begin_turn("agent", provider="anthropic")
    observation.record_result(TurnResult(session_id="provider-session"))
    observation.finish(status="completed")

    assert runtime.enabled is False
    assert runtime.health()["init_error"] == "OSError"


def test_turn_indexes_are_scoped_to_each_process_local_session(tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="disabled")

    assert runtime.begin_turn("one", provider="test").turn_index == 1
    assert runtime.begin_turn("one", provider="test").turn_index == 2
    assert runtime.begin_turn("two", provider="test").turn_index == 1


def test_turn_start_is_durable_before_terminal_result(tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    observation = runtime.begin_turn(
        "agent",
        provider="anthropic",
        brain="claude",
        trigger_kind="inbox",
        prompt_bytes=12,
    )
    assert runtime.close(timeout=2)

    service = MetricsCollectorService(tmp_path)
    service.collect_once()
    conn = connect(service.db_path, readonly=True)
    try:
        turn = conn.execute(
            "SELECT * FROM turns WHERE turn_id=?", (observation.turn_id,)
        ).fetchone()
        assert turn["status"] == "running"
        assert turn["ended_at_us"] is None
        assert turn["prompt_bytes"] == 12
    finally:
        conn.close()


def test_session_row_emits_once_then_only_when_provider_correlation_changes(
    tmp_path,
):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    first = runtime.begin_turn("agent", provider="anthropic", brain="claude")
    first.record_result(TurnResult(session_id="provider-session"))
    first.finish(status="completed")
    second = runtime.begin_turn("agent", provider="anthropic", brain="claude")
    second.record_result(TurnResult(session_id="provider-session"))
    second.finish(status="completed")
    assert runtime.close(timeout=2)

    segment = next((tmp_path / "state" / "metrics" / "spool").rglob("*.telemetry"))
    from bobi.metrics.spool import iter_frames

    session_events = [
        frame.event
        for frame in iter_frames(segment)
        if frame.event.event_type == "session.recorded"
    ]
    assert len(session_events) == 3
    assert session_events[0].payload["provider_session_id"] is None
    assert session_events[1].payload["provider_session_id"] == "provider-session"
    assert session_events[2].payload["status"] == "stopped"
    assert session_events[2].payload["ended_at_us"] is not None


def test_session_finalization_is_terminal_and_evicts_runtime_state(tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    observation = runtime.begin_turn("agent", provider="anthropic", brain="claude")
    observation.finish(status="completed")

    assert runtime.finish_session("agent", status="completed") is True
    assert observation.session_id not in runtime._turn_indexes
    assert observation.session_id not in runtime._session_starts
    assert observation.session_id not in runtime._emitted_sessions
    assert observation.session_id not in runtime._provider_sessions
    assert observation.session_id not in runtime._session_contexts
    assert runtime.close(timeout=2)

    service = MetricsCollectorService(tmp_path)
    service.collect_once()
    conn = connect(service.db_path, readonly=True)
    try:
        session = conn.execute(
            "SELECT status,ended_at_us FROM sessions WHERE session_id=?",
            (observation.session_id,),
        ).fetchone()
    finally:
        conn.close()
    assert session["status"] == "completed"
    assert session["ended_at_us"] is not None


def test_rejected_session_finalization_keeps_state_for_retry(tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    observation = runtime.begin_turn("agent", provider="anthropic")
    real_emit = runtime.emit

    def reject_terminal(event_type, payload, **ids):
        if event_type == "session.recorded" and payload.get("ended_at_us") is not None:
            return False
        return real_emit(event_type, payload, **ids)

    runtime.emit = reject_terminal

    assert runtime.finish_session("agent", status="completed") is False
    assert observation.session_id in runtime._session_contexts
    runtime.emit = real_emit
    assert runtime.close(timeout=2)


def test_restarting_a_finished_name_creates_a_new_metrics_session(tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    first = runtime.begin_turn("agent", provider="anthropic")
    first.finish(status="completed")
    assert runtime.finish_session("agent", status="completed")

    second = runtime.begin_turn("agent", provider="anthropic")

    assert second.session_id != first.session_id
    assert second.turn_index == 1
    assert runtime.close(timeout=2)


def test_rejected_session_event_is_retried_on_the_next_turn(tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    real_emit = runtime.emit
    rejected = False

    def reject_once(event_type, payload, **ids):
        nonlocal rejected
        if event_type == "session.recorded" and not rejected:
            rejected = True
            return False
        return real_emit(event_type, payload, **ids)

    runtime.emit = reject_once
    first = runtime.begin_turn("agent", provider="anthropic", brain="claude")
    assert first.session_id not in runtime._emitted_sessions
    runtime.begin_turn("agent", provider="anthropic", brain="claude")
    assert first.session_id in runtime._emitted_sessions
    assert runtime.close(timeout=2)


def test_runtime_replaces_same_process_root_and_forked_singletons(
    monkeypatch, tmp_path
):
    metrics_runtime.reset_runtime_for_tests()
    monkeypatch.setenv("BOBI_METRICS_MODE", "disabled")
    first = metrics_runtime.get_runtime(tmp_path / "one")
    second = metrics_runtime.get_runtime(tmp_path / "two")

    assert second is not first
    assert second.root == (tmp_path / "two").resolve()

    monkeypatch.setattr(metrics_runtime.os, "getpid", lambda: first.pid + 1)
    forked = metrics_runtime.get_runtime(tmp_path / "two")
    assert forked is not second
    assert forked.pid == first.pid + 1
    metrics_runtime.reset_runtime_for_tests()


def test_unbound_runtime_never_reuses_an_agent_runtime(monkeypatch, tmp_path):
    metrics_runtime.reset_runtime_for_tests()
    monkeypatch.setenv("BOBI_METRICS_MODE", "disabled")
    active = metrics_runtime.get_runtime(tmp_path / "agent")
    monkeypatch.setattr("bobi.paths.bound_root", lambda: None)

    unbound = metrics_runtime.get_runtime()

    assert unbound is not active
    assert unbound.mode == "disabled"
    metrics_runtime.reset_runtime_for_tests()


@pytest.mark.parametrize("step_type", ["route", "action", "notify", "await"])
def test_deterministic_workflow_steps_project_without_a_turn(tmp_path, step_type):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    observation = runtime.begin_workflow_step(
        "workflow-session",
        workflow_name="workflow",
        step_name=f"{step_type}-step",
        step_index=1,
        attempt=1,
        step_type=step_type,
        run_key="run-1",
    )
    observation.finish(status="waiting" if step_type == "await" else "completed")
    assert runtime.close(timeout=2)

    service = MetricsCollectorService(tmp_path)
    service.collect_once()
    conn = connect(service.db_path, readonly=True)
    try:
        row = conn.execute(
            "SELECT step_type, status, turn_id FROM workflow_steps"
        ).fetchone()
        assert tuple(row) == (
            step_type,
            "waiting" if step_type == "await" else "completed",
            None,
        )
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_shadow_metrics_match_the_legacy_session_cost_writer(
    bobi_install, monkeypatch
):
    metrics_runtime.reset_runtime_for_tests()
    monkeypatch.setenv("BOBI_METRICS_MODE", "enabled")
    usage = BrainUsage(
        model="claude-test",
        provider_event_id="message-provider-1",
        input_tokens=15,
        uncached_input_tokens=2,
        cache_read_input_tokens=10,
        cache_write_input_tokens=3,
        cache_write_unknown_ttl_input_tokens=3,
        output_tokens=4,
        raw_usage={"input_tokens": 2, "output_tokens": 4},
    )
    result = TurnResult(
        session_id="provider-session",
        provider_turn_id="provider-turn",
        total_cost_usd=0.01,
        usage=[usage],
        costs=[BrainCost(
            model="claude-test",
            input_tokens=15,
            output_tokens=4,
            cached_input_tokens=10,
        )],
    )
    registry = get_registry()
    registry.register(SessionEntry(name="metrics-parity", role="engineer"))
    session = Session(name="metrics-parity", cwd=str(bobi_install.repo_path))
    session._input_ready = __import__("asyncio").Event()
    session._set_state("waiting_input")
    session._client = _Client(result)

    await session._process_message(
        Message(id="message-1", sender="slack", text="secret prompt text")
    )
    metrics_runtime.reset_runtime_for_tests()

    legacy = json.loads(
        (bobi_install.sessions_dir / "metrics-parity" / "state.json").read_text()
    )
    service = MetricsCollectorService(bobi_install.repo_path)
    service.collect_once()
    conn = connect(service.db_path, readonly=True)
    try:
        exact = conn.execute(
            "SELECT input_tokens, cache_read_input_tokens, output_tokens "
            "FROM best_usage WHERE scope='turn'"
        ).fetchone()
        cost = conn.execute(
            "SELECT amount_usd FROM cost_measurements WHERE scope='turn'"
        ).fetchone()[0]
        raw = "\n".join(
            row[0] for row in conn.execute("SELECT payload_json FROM raw_events")
        )
    finally:
        conn.close()

    legacy_usage = legacy["model_usage"]["anthropic:claude-test"]
    assert tuple(exact) == (
        legacy_usage["input_tokens"],
        legacy_usage["cached_input_tokens"],
        legacy_usage["output_tokens"],
    )
    assert cost == pytest.approx(legacy["total_cost_usd"])
    assert "secret prompt text" not in raw


def _jsonl(name):
    return [json.loads(line) for line in JSONL[name].splitlines()]


def test_claude_fixture_preserves_ttl_cache_dimensions_and_deduplicates(metrics_fixtures, ):
    usage = parse_claude_transcript(metrics_fixtures / "claude-transcript.jsonl")
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


def test_claude_transcript_keeps_valid_prefix_before_invalid_utf8_tail(metrics_fixtures, tmp_path):
    path = tmp_path / "transcript.jsonl"
    fixture = (metrics_fixtures / "claude-transcript.jsonl").read_bytes()
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


def test_claude_usage_keeps_normalized_model_over_raw_cli_alias():
    usage = claude_usage(
        {
            "inputTokens": 7,
            "outputTokens": 3,
            "canonicalModel": "claude-opus-5",
        },
        model="deepseek-flash",
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


def test_codex_rollout_uses_last_exact_record_and_turn_model(metrics_fixtures, ):
    usage = parse_codex_rollout(metrics_fixtures / "codex-rollout.jsonl")
    assert len(usage) == 1
    assert usage[0].model == "gpt-test"
    assert usage[0].input_tokens == 4096
    assert usage[0].output_tokens == 30
    assert usage[0].provider_event_id == "resp-fixture-final"


def test_codex_rollout_keeps_valid_prefix_before_invalid_utf8_tail(metrics_fixtures, tmp_path):
    path = tmp_path / "rollout.jsonl"
    fixture = (metrics_fixtures / "codex-rollout.jsonl").read_bytes()
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


def _smoke_root(tmp_path):
    home = tmp_path / "home"
    (home / ".bobi-metrics-smoke.json").parent.mkdir(parents=True)
    (home / ".bobi-metrics-smoke.json").write_text("{}\n")
    root = home / "agents" / "smoke" / "run"
    root.mkdir(parents=True)
    return root


def test_fault_injection_is_refused_outside_disposable_home(monkeypatch, tmp_path):
    monkeypatch.setenv("BOBI_METRICS_FAULT_INJECTION", "1")

    runtime = MetricsRuntime(tmp_path / "normal", mode="enabled")

    assert runtime.enabled is False
    assert runtime.health()["init_error"] == "FaultInjectionRefused"


def test_fault_cannot_be_armed_outside_disposable_home(tmp_path):
    with pytest.raises(FaultInjectionRefused, match="disposable metrics smoke home"):
        arm_fault(tmp_path / "normal", "drop-next-online-usage")


def test_drop_online_usage_is_one_shot_and_preserves_reconciliation_metadata(
    monkeypatch, tmp_path
):
    root = _smoke_root(tmp_path)
    monkeypatch.setenv("BOBI_METRICS_FAULT_INJECTION", "1")
    arm_fault(root, "drop-next-online-usage", hold_seconds=0)
    runtime = MetricsRuntime(root, mode="enabled")
    assert runtime._fault_injection_enabled is True
    observation = runtime.begin_turn(
        "agent",
        provider="anthropic",
        brain="claude",
        prompt_bytes=10,
        is_user_initiated=True,
    )
    usage = BrainUsage(
        model="claude-test",
        provider_event_id="message-1",
        input_tokens=10,
        output_tokens=2,
    )
    observation.record_result(TurnResult(
        session_id="provider-session",
        usage=[usage],
        invocations=[BrainInvocation(
            provider_event_id="message-1",
            model="claude-test",
            usage=usage,
        )],
    ))
    observation.finish(status="completed")
    runtime.close()
    service = MetricsCollectorService(root, reconcile_interval=3600)
    service.collect_once()

    state = read_fault(root)
    assert state["status"] == "consumed"
    assert state["turn_id"] == observation.turn_id
    conn = connect(service.db_path, readonly=True)
    try:
        assert conn.execute(
            "SELECT provider_session_id FROM sessions"
        ).fetchone()[0] == "provider-session"
        assert conn.execute(
            "SELECT status FROM turns WHERE turn_id=?", (observation.turn_id,)
        ).fetchone()[0] == "completed"
        assert conn.execute(
            "SELECT COUNT(*) FROM usage_measurements"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM llm_invocations"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_background_turn_cannot_consume_recovery_fault(monkeypatch, tmp_path):
    root = _smoke_root(tmp_path)
    monkeypatch.setenv("BOBI_METRICS_FAULT_INJECTION", "1")
    arm_fault(root, "drop-next-online-usage", hold_seconds=0)
    runtime = MetricsRuntime(root, mode="enabled")
    usage = BrainUsage(
        model="claude-test",
        provider_event_id="message-1",
        input_tokens=10,
        output_tokens=2,
    )
    background = runtime.begin_turn(
        "curator",
        provider="anthropic",
        brain="claude",
        is_user_initiated=False,
    )
    background.record_result(TurnResult(
        session_id="provider-session",
        usage=[usage],
        invocations=[BrainInvocation(
            provider_event_id="message-1",
            model="claude-test",
            usage=usage,
        )],
    ))
    background.finish(status="completed")

    assert read_fault(root)["status"] == "armed"

    foreground = runtime.begin_turn(
        "manager",
        provider="anthropic",
        brain="claude",
        is_user_initiated=True,
    )
    foreground.record_result(TurnResult(
        session_id="provider-session",
        usage=[usage],
        invocations=[BrainInvocation(
            provider_event_id="message-1",
            model="claude-test",
            usage=usage,
        )],
    ))
    foreground.finish(status="completed")
    runtime.close()

    state = read_fault(root)
    assert state["status"] == "consumed"
    assert state["turn_id"] == foreground.turn_id
