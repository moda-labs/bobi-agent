import ast
import json
from pathlib import Path

import pytest

from bobi.brain.base import (
    AssistantText,
    BrainCost,
    BrainInvocation,
    BrainToolExecution,
    BrainUsage,
    TurnResult,
)
from bobi.brain.turns import drain_turn
from bobi.inbox import Message
from bobi.metrics.collector import MetricsCollectorService
from bobi.metrics.runtime import MetricsRuntime
from bobi.metrics import runtime as metrics_runtime
from bobi.metrics.store import connect
from bobi.sdk import SessionEntry, get_registry
from bobi.session import Session


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


def test_every_spawn_style_drain_caller_uses_the_shared_primitive():
    root = Path(__file__).parents[2]
    callers = set()
    for path in (root / "bobi").rglob("*.py"):
        tree = ast.parse(path.read_text())
        if path.name == "turns.py":
            continue
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "drain_turn"
            for node in ast.walk(tree)
        ):
            callers.add(path.relative_to(root).as_posix())
    assert callers == {"bobi/subagent.py", "bobi/workflow/orchestrator.py"}


def test_runtime_projects_turn_invocation_tool_usage_and_cost(tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="shadow")
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


def test_runtime_startup_failure_never_reaches_the_turn(tmp_path):
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    runtime = MetricsRuntime(tmp_path, mode="shadow", producer_factory=fail)
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


@pytest.mark.parametrize("step_type", ["route", "action", "notify", "await"])
def test_deterministic_workflow_steps_project_without_a_turn(tmp_path, step_type):
    runtime = MetricsRuntime(tmp_path, mode="shadow")
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
    monkeypatch.setenv("BOBI_METRICS_MODE", "shadow")
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
