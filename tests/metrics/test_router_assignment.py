import json
import multiprocessing
from pathlib import Path

import pytest

from bobi.brain.base import BrainInvocation, BrainUsage, TurnResult
from bobi.metrics.collector import MetricsCollectorService
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
