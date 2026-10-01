from dataclasses import replace
import json
import sqlite3
import pytest

from bobi.metrics.collector import MetricsCollectorService
from bobi.metrics.query import MetricsQueries
from bobi.metrics.query import MetricsQueryError
from bobi.metrics.runtime import MetricsRuntime
from tests.metrics.test_route_admission import decision


def test_analysis_rows_share_experiment_response_budget(tmp_path, monkeypatch):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    routed = decision()
    assert runtime.admit_route("agent", routed, brain="codex")
    runtime.begin_turn("agent", provider="openai").finish(status="completed")
    assert runtime.close(timeout=2)
    MetricsCollectorService(tmp_path).collect_once()
    monkeypatch.setattr("bobi.metrics.query.MAX_ROWS", 1)
    with pytest.raises(MetricsQueryError, match="200 rows"):
        MetricsQueries(tmp_path).experiment({"experiment_id": routed.experiment_id})

@pytest.mark.parametrize("cost,known,unknown", [(0.01, 0.01, 0), (None, 0, 1), (float("inf"), 0, 1)])
def test_policy_analysis_counts_call_cost_once_across_turns(tmp_path, cost, known, unknown):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    routed = replace(decision(), variant_id="treatment_jev", policy_metadata_json=json.dumps({
        "mode": "shadow", "status": "decided", "recommended_model": "cheap",
        "confidence": 0.94, "tier": "simple", "call_id": "one-call", "cost_usd": cost,
    }))
    assert runtime.admit_route("agent", routed, brain="codex")
    for _ in range(2):
        turn = runtime.begin_turn("agent", provider="openai", model_requested="model-control")
        turn.finish(status="completed")
    assert runtime.close(timeout=2)
    MetricsCollectorService(tmp_path).collect_once()
    report = MetricsQueries(tmp_path).experiment({"experiment_id": routed.experiment_id})
    policy = report["policy_breakdown"]
    assert policy["interpretation"] == "descriptive_only_not_causal"
    assert policy["truncated"] is False
    assert policy["high_confidence_runtime_error_units"] == 0
    assert policy["unique_policy_calls"] == 1
    assert policy["known_policy_cost_usd"] == known
    assert policy["unknown_policy_cost_calls"] == unknown
    assert policy["groups"][0]["turn_count"] == 2
    assert policy["groups"][0]["assignment_unit_count"] == 1
    arm = report["intent_to_treat"]["arms"][0]
    assert arm["assignment_unit_count"] == 1
    assert arm["turns_per_unit"] == 2
    assert arm["observed_workflow_retry_steps_per_unit"] == 0
    assert arm["completion_rate"] == 1
    assert arm["known_reported_cost_usd_per_unit"] is None
    assert arm["units_without_reported_cost"] == 1
    assert arm["known_policy_cost_usd"] == known
    assert arm["unknown_policy_cost_calls"] == unknown
    assert arm["reported_total_cost_usd_per_unit"] is None
    from bobi.supervisor.admin import AdminListener
    from tests.test_admin_listener import FakeSupervisor, FakeTelemetry
    listener = AdminListener(supervisor=FakeSupervisor(), telemetry=FakeTelemetry(), project_root=tmp_path)
    try:
        assert listener._execute_metrics_query("usage_experiment", {
            "experiment_id": routed.experiment_id,
        }) == {"usage_experiment": report}
    finally:
        listener.stop()


@pytest.mark.parametrize("policy_cost,expected", [(0.01, 0.41), (None, None)])
def test_arm_total_cost_includes_one_policy_call(tmp_path, policy_cost, expected):
    from bobi.brain.base import BrainInvocation, BrainUsage, TurnResult

    runtime = MetricsRuntime(tmp_path, mode="enabled")
    routed = replace(decision(), policy_metadata_json=json.dumps({
        "status": "decided", "mode": "enforce", "call_id": "one-call",
        "cost_usd": policy_cost,
    }))
    assert runtime.admit_route("agent", routed, brain="codex")
    for _ in range(2):
        turn = runtime.begin_turn("agent", provider="openai")
        usage = BrainUsage(model="model-control", input_tokens=1, output_tokens=1)
        turn.record_result(TurnResult(session_id="provider", usage=[usage], total_cost_usd=0.2,
            invocations=[BrainInvocation(model="model-control", usage=usage)]))
        turn.finish(status="completed")
    assert runtime.close(timeout=2)
    MetricsCollectorService(tmp_path).collect_once()
    arm = MetricsQueries(tmp_path).experiment({"experiment_id": routed.experiment_id})["intent_to_treat"]["arms"][0]
    if expected is None:
        assert arm["reported_total_cost_usd_per_unit"] is None
    else:
        assert arm["reported_total_cost_usd_per_unit"] == pytest.approx(expected)

def test_failed_policy_call_is_counted_without_recommendation(tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    routed = replace(decision(), policy_metadata_json=json.dumps({
        "status": "failed", "mode": "shadow", "call_id": "failed-call",
    }))
    assert runtime.admit_route("agent", routed, brain="codex")
    turn = runtime.begin_turn("agent", provider="openai")
    turn.finish(status="completed")
    assert runtime.close(timeout=2)
    MetricsCollectorService(tmp_path).collect_once()
    policy = MetricsQueries(tmp_path).experiment({"experiment_id": routed.experiment_id})["policy_breakdown"]
    assert policy["groups"] == []
    assert policy["unique_policy_calls"] == 1
    assert policy["unknown_policy_cost_calls"] == 1

def test_precision_check_clusters_multiple_errors_in_one_unit(tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    routed = replace(decision(), policy_metadata_json=json.dumps({
        "status": "decided", "mode": "enforce", "recommended_model": "cheap",
        "confidence": 0.95, "call_id": "policy-call",
    }))
    assert runtime.admit_route("agent", routed, brain="codex")
    for _ in range(2):
        turn = runtime.begin_turn("agent", provider="openai")
        turn.finish(status="failed", error_kind="provider_error")
    assert runtime.close(timeout=2)
    MetricsCollectorService(tmp_path).collect_once()
    report = MetricsQueries(tmp_path).experiment({"experiment_id": routed.experiment_id})
    assert report["policy_breakdown"]["high_confidence_runtime_error_units"] == 1
    assert report["intent_to_treat"]["arms"][0]["error_rate"] == 1

@pytest.mark.parametrize("failure,estimated,expected", [(True, False, 1),
    (False, False, 0), (None, False, 0), (True, True, 0)])
def test_quality_averages_within_unit_before_arm_summary(tmp_path, failure, estimated, expected):
    from bobi.metrics.outcomes import record_quality_outcome

    runtime = MetricsRuntime(tmp_path, mode="enabled")
    routed = replace(decision(), policy_metadata_json=json.dumps({
        "confidence": 0.95, "recommended_model": "cheap", "mode": "enforce",
    }))
    assert runtime.admit_route("agent", routed, brain="codex")
    for index, value in enumerate((0.2, 0.8)):
        turn = runtime.begin_turn("agent", provider="openai")
        assert record_quality_outcome(runtime, router_decision_id=turn.router_decision_id,
            outcome_name="quality", outcome_definition_version="rubric-v1",
            outcome_source="evaluator", evaluator_name="external-judge", evaluator_version="v1",
            outcome_value=value, idempotency_key=str(index),
            is_failure=failure if index == 1 else False, is_estimated=estimated,
            session_id=turn.session_id, turn_id=turn.turn_id)
        turn.finish(status="completed")
    assert runtime.close(timeout=2)
    MetricsCollectorService(tmp_path).collect_once()
    precision = MetricsQueries(tmp_path).experiment({"experiment_id": routed.experiment_id})["policy_breakdown"]
    assert precision["high_confidence_evaluator_failure_units"] == expected
    excluded = MetricsQueries(tmp_path).experiment({"experiment_id": routed.experiment_id,
        "outcome_definition_version": "different-rubric"})["policy_breakdown"]
    assert excluded["high_confidence_evaluator_failure_units"] == 0
    report = MetricsQueries(tmp_path).experiment({"experiment_id": routed.experiment_id})["intent_to_treat"]
    quality = report["quality"]
    assert len(quality) == 1
    assert quality[0]["assignment_unit_count"] == 1
    assert quality[0]["mean_unit_value"] == 0.5
    assert quality[0]["evaluator_name"] == "external-judge"
    assert report["quality_missingness"][0]["units_with_outcome"] == 1
    assert report["quality_missingness"][0]["units_without_outcome"] == 0
    assert report["quality_missingness"][0]["missing_rate"] == 0
    with sqlite3.connect(tmp_path / "state/metrics/metrics.db") as connection:
        connection.execute(
            "UPDATE router_decisions SET assignment_key_hash='another-unit' "
            "WHERE turn_id=?", (turn.turn_id,),
        )
        connection.execute("DELETE FROM experiment_outcomes WHERE router_decision_id=?", (turn.router_decision_id,))
    missing = MetricsQueries(tmp_path).experiment({"experiment_id": routed.experiment_id})["intent_to_treat"]["quality_missingness"][0]
    assert missing["assignment_unit_count"] == 2
    assert missing["units_without_outcome"] == 1
    assert missing["missing_rate"] == 0.5


def test_precision_check_includes_errors_on_other_turns_in_unit(tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    routed = replace(decision(), policy_metadata_json=json.dumps({
        "status": "decided", "mode": "enforce", "recommended_model": "cheap",
        "confidence": 0.95, "call_id": "policy-call",
    }))
    assert runtime.admit_route("agent", routed, brain="codex")
    runtime.begin_turn("agent", provider="openai").finish(status="completed")
    failed_turn = runtime.begin_turn("agent", provider="openai")
    failed_turn.finish(status="failed", error_kind="provider_error")
    assert runtime.close(timeout=2)
    collector = MetricsCollectorService(tmp_path)
    collector.collect_once()
    with sqlite3.connect(collector.db_path) as connection:
        connection.execute(
            "UPDATE router_decisions SET metadata_json='{}' WHERE turn_id=?",
            (failed_turn.turn_id,),
        )
    report = MetricsQueries(tmp_path).experiment({"experiment_id": routed.experiment_id})
    assert report["policy_breakdown"]["high_confidence_runtime_error_units"] == 1
    with sqlite3.connect(collector.db_path) as connection:
        connection.execute("UPDATE router_decisions SET variant_id='other-arm' WHERE turn_id=?",
                           (failed_turn.turn_id,))
    report = MetricsQueries(tmp_path).experiment({"experiment_id": routed.experiment_id})
    assert report["policy_breakdown"]["high_confidence_runtime_error_units"] == 0
