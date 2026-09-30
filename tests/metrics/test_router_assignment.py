import json
import multiprocessing
import time
from pathlib import Path

import pytest

from bobi.brain.base import BrainInvocation, BrainUsage, TurnResult
from bobi.metrics.collector import MetricsCollectorService, rebuild_database
from bobi.metrics.producer import MetricsProducer
from bobi.metrics.query import MetricsQueries
from bobi.metrics.router import (
    ASSIGNMENT_ALGORITHM,
    ExperimentConfig,
    assign_variant,
    choose_assignment_key,
    load_experiment,
    route,
)
from bobi.metrics.runtime import MetricsRuntime
from bobi.metrics.store import connect


FIXTURE = Path("tests/fixtures/metrics/jev-assignment-vectors.json")


def _assign_in_process(payload):
    config, secret, unit, key = payload
    result = assign_variant(config, secret, assignment_unit=unit, assignment_key=key)
    return result[0].variant_id, result[1], result[2]


def test_golden_assignment_vectors_are_stable_across_processes():
    fixture = json.loads(FIXTURE.read_text())
    config = ExperimentConfig.from_mapping(fixture["config"])
    secret = fixture["secret"].encode()
    inputs = []
    expected = []
    for vector in fixture["vectors"]:
        inputs.append((config, secret, fixture["assignment_unit"], vector["assignment_key"]))
        expected.append((vector["variant_id"], vector["bucket"], vector["assignment_key_hash"]))
    assert [_assign_in_process(item) for item in inputs] == expected
    with multiprocessing.get_context("spawn").Pool(2) as pool:
        assert pool.map(_assign_in_process, inputs) == expected


def test_assignment_key_precedence_and_unassigned_state():
    assert choose_assignment_key(
        experiment_subject="subject", run_key="run", session_id="session"
    ) == ("experiment_subject", "subject")
    assert choose_assignment_key(run_key="run", session_id="session") == ("run_key", "run")
    assert choose_assignment_key(session_id="session") == ("session_id", "session")
    assert choose_assignment_key() == (None, None)


def test_config_is_strict_and_requires_runtime_secret():
    fixture = json.loads(FIXTURE.read_text())
    encoded = json.dumps(fixture["config"])
    config, secret = load_experiment({
        "BOBI_METRICS_EXPERIMENT_JSON": encoded,
        "BOBI_METRICS_ASSIGNMENT_SECRET": fixture["secret"],
    })
    assert config.experiment_id == "jev-routing-v1"
    assert secret == fixture["secret"].encode()
    with pytest.raises(ValueError, match="ASSIGNMENT_SECRET"):
        load_experiment({"BOBI_METRICS_EXPERIMENT_JSON": encoded})
    with pytest.raises(ValueError, match="unsupported experiment config fields"):
        ExperimentConfig.from_mapping({**fixture["config"], "typo": True})
    invalid_control = {**fixture["config"], "control_model": "not-a-variant"}
    with pytest.raises(ValueError, match="control_model"):
        ExperimentConfig.from_mapping(invalid_control)


def test_preconfigured_model_fallback_preserves_intent_to_treat():
    fixture = json.loads(FIXTURE.read_text())
    configured = (
        ExperimentConfig.from_mapping(fixture["config"]), fixture["secret"].encode()
    )
    decision = route(
        configured,
        requested_model="already-constructed-model",
        experiment_subject="subject-001",
    )
    assert decision.variant_id == "control"
    assert decision.model_selected == "already-constructed-model"
    assert decision.fallback_reason == "preconfigured_client_model"


def test_runtime_model_resolution_requires_live_producer(monkeypatch, tmp_path):
    fixture = json.loads(FIXTURE.read_text())
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(fixture["config"]))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", fixture["secret"])
    disabled = MetricsRuntime(tmp_path / "disabled", mode="disabled")
    assert disabled.resolve_model(
        "model-control", experiment_subject="subject-002", session_name="agent"
    ) == "model-control"
    enabled = MetricsRuntime(tmp_path / "enabled", mode="shadow")
    try:
        assert enabled.resolve_model(
            "model-control", experiment_subject="subject-002", session_name="agent"
        ) == "model-treatment"
    finally:
        enabled.close(timeout=2)


def test_producer_startup_failure_preserves_requested_model(monkeypatch, tmp_path):
    fixture = json.loads(FIXTURE.read_text())
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(fixture["config"]))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", fixture["secret"])

    def fail_producer(*args, **kwargs):
        raise OSError("spool unavailable")

    runtime = MetricsRuntime(
        tmp_path / "failed-producer", mode="shadow", producer_factory=fail_producer
    )
    assert runtime.resolve_model(
        "model-control", experiment_subject="subject-002", session_name="agent"
    ) == "model-control"


def test_known_spool_failure_rejects_later_treatment_admission(monkeypatch, tmp_path):
    fixture = json.loads(FIXTURE.read_text())
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(fixture["config"]))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", fixture["secret"])

    class BrokenWriter:
        def __init__(self, _path):
            pass

        def append(self, _event):
            raise OSError("spool unavailable")

        def flush_if_due(self):
            return False

        def close(self):
            pass

    def producer_factory(path, **kwargs):
        return MetricsProducer(path, writer_factory=BrokenWriter, **kwargs)

    runtime = MetricsRuntime(
        tmp_path, mode="shadow", producer_factory=producer_factory
    )
    assert runtime.resolve_model(
        "model-control", experiment_subject="subject-002", session_name="first"
    ) == "model-treatment"
    deadline = time.monotonic() + 1
    while runtime.health().get("writer_available") is not False:
        assert time.monotonic() < deadline
        time.sleep(0.001)

    assert runtime.resolve_model(
        "model-control", experiment_subject="subject-002", session_name="second"
    ) == "model-control"
    assert runtime.close(timeout=2)


def test_treatment_falls_back_to_control_when_assignment_admission_is_rejected(
    monkeypatch, tmp_path
):
    fixture = json.loads(FIXTURE.read_text())
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(fixture["config"]))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", fixture["secret"])
    runtime = MetricsRuntime(tmp_path, mode="shadow")
    real_emit = runtime.emit
    runtime.emit = lambda *_args, **_kwargs: False

    assert runtime.resolve_model(
        "model-control", experiment_subject="subject-002", session_name="agent"
    ) == "model-control"

    runtime.emit = real_emit
    observation = runtime.begin_turn(
        "agent",
        provider="anthropic",
        model_requested="model-control",
        experiment_subject="subject-002",
    )
    assert observation.router_decision is None
    observation.finish(status="completed")
    assert runtime.close(timeout=2)


def test_declined_assignment_without_a_turn_is_evicted_on_session_finish(
    monkeypatch, tmp_path
):
    fixture = json.loads(FIXTURE.read_text())
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(fixture["config"]))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", fixture["secret"])
    runtime = MetricsRuntime(tmp_path, mode="shadow")
    real_emit = runtime.emit
    runtime.emit = lambda *_args, **_kwargs: False

    assert runtime.resolve_model(
        "model-control", experiment_subject="subject-002", session_name="agent"
    ) == "model-control"
    session_id = runtime._session_ids["agent"]
    runtime.emit = real_emit

    assert runtime.finish_session("agent", status="stopped") is True
    assert "agent" not in runtime._session_ids
    assert session_id not in runtime._declined_router_models
    assert runtime.close(timeout=2)


def test_admitted_assignment_recovers_dropped_per_turn_decisions(
    monkeypatch, tmp_path
):
    fixture = json.loads(FIXTURE.read_text())
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(fixture["config"]))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", fixture["secret"])
    runtime = MetricsRuntime(tmp_path, mode="shadow")

    assert runtime.resolve_model(
        "model-control", experiment_subject="subject-002", session_name="agent"
    ) == "model-treatment"
    real_emit = runtime.emit

    def drop_turn_decisions(event_type, payload, **ids):
        if event_type == "router_decision.recorded":
            return False
        return real_emit(event_type, payload, **ids)

    runtime.emit = drop_turn_decisions
    turn_ids = []
    for index in range(2):
        observation = runtime.begin_turn(
            "agent",
            provider="anthropic",
            model_requested="model-treatment",
            experiment_subject="subject-002",
            started_at_us=100 + index,
        )
        turn_ids.append(observation.turn_id)
        usage = BrainUsage(
            model="model-treatment", input_tokens=2, output_tokens=1
        )
        observation.record_result(TurnResult(
            usage=[usage],
            invocations=[BrainInvocation(model="model-treatment", usage=usage)],
        ))
        observation.finish(status="completed")
    runtime.emit = real_emit
    runtime.finish_session("agent", status="completed")
    assert runtime.close(timeout=2)

    MetricsCollectorService(tmp_path).collect_once()
    conn = connect(tmp_path / "state/metrics/metrics.db", readonly=True)
    try:
        raw_router_events = conn.execute(
            "SELECT COUNT(*) FROM raw_events WHERE event_type='router_decision.recorded'"
        ).fetchone()[0]
        decisions = conn.execute(
            "SELECT turn_id,variant_id,model_selected,decided_at_us "
            "FROM router_decisions ORDER BY decided_at_us"
        ).fetchall()
        linked = conn.execute(
            "SELECT COUNT(*) FROM llm_invocations WHERE router_decision_id IS NOT NULL"
        ).fetchone()[0]
        raw_payloads = "".join(
            row[0] for row in conn.execute("SELECT payload_json FROM raw_events")
        )
    finally:
        conn.close()

    assert raw_router_events == 0
    assert {row["turn_id"] for row in decisions} == set(turn_ids)
    assert {row["variant_id"] for row in decisions} == {"treatment"}
    assert {row["model_selected"] for row in decisions} == {"model-treatment"}
    assert [row["decided_at_us"] for row in decisions] == [100, 101]
    assert linked == 2
    assert "subject-002" not in raw_payloads
    assert fixture["secret"] not in raw_payloads
    filtered = MetricsQueries(tmp_path).experiment({
        "experiment_id": fixture["config"]["experiment_id"],
        "from": 0.000101,
        "to": 0.000102,
    })
    assert filtered["variants"][0]["sample_size"] == 1

    rebuilt = tmp_path / "rebuilt.db"
    rebuild_database(tmp_path / "state/metrics/metrics.db", rebuilt)
    conn = connect(rebuilt, readonly=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM router_decisions").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM llm_invocations WHERE router_decision_id IS NOT NULL"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_admitted_session_reemits_provider_context_before_first_turn(
    monkeypatch, tmp_path
):
    fixture = json.loads(FIXTURE.read_text())
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(fixture["config"]))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", fixture["secret"])
    runtime = MetricsRuntime(tmp_path, mode="shadow")

    assert runtime.resolve_model(
        "model-control", experiment_subject="subject-002", session_name="agent"
    ) == "model-treatment"
    observation = runtime.begin_turn(
        "agent",
        provider="anthropic",
        brain="claude",
        model_requested="model-treatment",
        experiment_subject="subject-002",
    )
    observation.finish(status="completed")
    assert runtime._producer is not None
    assert runtime._producer.close(timeout=2)
    runtime._producer = None

    MetricsCollectorService(tmp_path).collect_once()
    conn = connect(tmp_path / "state/metrics/metrics.db", readonly=True)
    try:
        session = conn.execute(
            "SELECT brain,provider,status,ended_at_us FROM sessions WHERE session_id=?",
            (observation.session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert dict(session) == {
        "brain": "claude",
        "provider": "anthropic",
        "status": "running",
        "ended_at_us": None,
    }


def test_runtime_persists_decision_before_invocation_without_raw_key(monkeypatch, tmp_path):
    fixture = json.loads(FIXTURE.read_text())
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(fixture["config"]))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", fixture["secret"])
    runtime = MetricsRuntime(tmp_path, mode="shadow")
    observation = runtime.begin_turn(
        "agent", provider="anthropic", model_requested="model-control",
        experiment_subject="subject-001",
    )
    usage = BrainUsage(model="model-control", input_tokens=2, output_tokens=1)
    observation.record_result(TurnResult(
        session_id="provider-session",
        usage=[usage],
        invocations=[BrainInvocation(model="model-control", usage=usage)],
    ))
    observation.finish(status="completed")
    assert runtime.close(timeout=2)
    MetricsCollectorService(tmp_path).collect_once()
    conn = connect(tmp_path / "state/metrics/metrics.db", readonly=True)
    decision = conn.execute("SELECT * FROM router_decisions").fetchone()
    invocation = conn.execute("SELECT * FROM llm_invocations").fetchone()
    events = conn.execute(
        "SELECT event_type,producer_sequence,payload_json FROM raw_events ORDER BY producer_sequence"
    ).fetchall()
    conn.close()
    assert decision["assignment_algorithm"] == ASSIGNMENT_ALGORITHM
    assert decision["assignment_key_hash"] == fixture["vectors"][0]["assignment_key_hash"]
    assert invocation["router_decision_id"] == decision["router_decision_id"]
    assert next(row["producer_sequence"] for row in events if row["event_type"] == "router_decision.recorded") < next(
        row["producer_sequence"] for row in events if row["event_type"] == "invocation.recorded"
    )
    assert "subject-001" not in "".join(row["payload_json"] for row in events)
    assert fixture["secret"] not in "".join(row["payload_json"] for row in events)
