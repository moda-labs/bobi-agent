import json
from pathlib import Path

import pytest

from bobi.metrics.collector import MetricsCollectorService
from bobi.metrics.outcomes import record_quality_outcome
from bobi.metrics.runtime import MetricsRuntime
from bobi.metrics.store import connect


@pytest.mark.parametrize("failure", [0, 1, "true", [], {}])
def test_quality_failure_flag_requires_boolean(tmp_path, failure):
    runtime = MetricsRuntime(tmp_path, mode="disabled")
    with pytest.raises(ValueError, match="failure flag must be boolean"):
        record_quality_outcome(runtime, router_decision_id="route-1",
            outcome_name="quality", outcome_definition_version="v1",
            outcome_source="evaluator", evaluator_name="judge", evaluator_version="1",
            outcome_value=0.5, idempotency_key="assessment", is_failure=failure)


def test_quality_outcome_requires_external_versioned_evaluator(tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="disabled")
    with pytest.raises(ValueError, match="evaluator or human"):
        record_quality_outcome(
            runtime,
            router_decision_id="route-1",
            outcome_name="quality",
            outcome_definition_version="v1",
            outcome_source="runtime",
            evaluator_name="router-under-test",
            evaluator_version="1",
            outcome_value=0.9,
            idempotency_key="assessment-1",
        )
    with pytest.raises(ValueError, match="outcome_value or outcome_text"):
        record_quality_outcome(
            runtime,
            router_decision_id="route-1",
            outcome_name="quality",
            outcome_definition_version="v1",
            outcome_source="evaluator",
            evaluator_name="judge",
            evaluator_version="1",
            idempotency_key="assessment-1",
        )
    with pytest.raises(ValueError, match="finite number"):
        record_quality_outcome(
            runtime,
            router_decision_id="route-1",
            outcome_name="quality",
            outcome_definition_version="v1",
            outcome_source="evaluator",
            evaluator_name="judge",
            evaluator_version="1",
            outcome_value="high",
            idempotency_key="assessment-2",
        )
    with pytest.raises(ValueError, match="finite number"):
        record_quality_outcome(
            runtime,
            router_decision_id="route-1",
            outcome_name="quality",
            outcome_definition_version="v1",
            outcome_source="evaluator",
            evaluator_name="judge",
            evaluator_version="1",
            outcome_value=float("nan"),
            idempotency_key="assessment-3",
        )


def test_quality_outcome_is_idempotent_and_contains_no_content_metadata(
    monkeypatch, tmp_path
):
    # Parent facts are emitted through the same asynchronous pipeline.
    monkeypatch.setenv(
        "BOBI_METRICS_EXPERIMENT_JSON",
        '{"experiment_id":"exp","router_name":"jev","router_version":"1",'
        '"policy_version":"p1","feature_schema_version":"f1",'
        '"control_model":"control","variants":['
        '{"variant_id":"control","weight":0.5,"model":"control"},'
        '{"variant_id":"treatment","weight":0.5,"model":"treatment"}]}',
    )
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "public-test-secret")
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    observation = runtime.begin_turn(
        "agent", provider="test", model_requested="control",
        experiment_subject="subject",
    )
    assignment = json.loads(Path(
        "tests/fixtures/metrics/historical-router-assignment.json"
    ).read_text())
    observation.router_decision_id = "historical-route"
    assert runtime.emit(
        "router_decision.recorded",
        {**assignment, "router_decision_id": observation.router_decision_id,
         "turn_id": observation.turn_id},
        session_id=observation.session_id, turn_id=observation.turn_id,
    )
    assert record_quality_outcome(
        runtime,
        router_decision_id=observation.router_decision_id,
        outcome_name="task_quality",
        outcome_definition_version="rubric-v2",
        outcome_source="evaluator",
        evaluator_name="offline-judge",
        evaluator_version="2026-09",
        outcome_value=0.8,
        idempotency_key="assessment-1",
        session_id=observation.session_id,
        turn_id=observation.turn_id,
    )
    observation.finish(status="completed")
    assert runtime.close(timeout=2)
    MetricsCollectorService(tmp_path).collect_once()
    conn = connect(tmp_path / "state/metrics/metrics.db", readonly=True)
    outcome = conn.execute(
        "SELECT * FROM experiment_outcomes WHERE outcome_name='task_quality'"
    ).fetchone()
    conn.close()
    assert outcome["outcome_definition_version"] == "rubric-v2"
    assert outcome["outcome_source"] == "evaluator"
    assert outcome["evaluator_name"] == "offline-judge"
    assert outcome["metadata_json"] == "{}"
