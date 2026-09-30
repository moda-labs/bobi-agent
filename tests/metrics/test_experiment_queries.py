import json

import pytest

from bobi.metrics.experiment import sample_ratio_mismatch
from bobi.metrics.query import MetricsQueries
from bobi.metrics.store import connect, migrate


def test_srm_detects_required_55_45_split():
    result = sample_ratio_mismatch([5500, 4500], [0.5, 0.5])
    assert result["detected"] is True
    assert result["p_value"] < 0.001


def test_experiment_diagnostics_report_srm_missingness_and_coverage(tmp_path):
    root = tmp_path / "agent"
    conn = connect(root / "state/metrics/metrics.db")
    migrate(conn)
    for index in range(10):
        variant = "control" if index < 7 else "treatment"
        session = f"s{index}"
        turn = f"t{index}"
        decision = f"r{index}"
        invocation = f"i{index}"
        metadata = json.dumps({
            "config_fingerprint": "same",
            "expected_weights": {"control": 0.5, "treatment": 0.5},
        })
        conn.execute(
            "INSERT INTO sessions(session_id,session_name,brain,provider,started_at_us,status) "
            "VALUES(?,?,?,?,?,?)", (session, session, "test", "test", index + 1, "completed")
        )
        conn.execute(
            "INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,is_user_initiated,"
            "started_at_us,ended_at_us,status) VALUES(?,?,?,?,?,?,?,?)",
            (turn, session, 1, "test", 0, index + 1, index + 2, "completed"),
        )
        conn.execute(
            "INSERT INTO router_decisions(router_decision_id,turn_id,experiment_id,variant_id,"
            "assignment_status,router_name,router_version,policy_version,feature_schema_version,"
            "candidate_models_json,model_selected,control_model,router_latency_ms,decided_at_us,metadata_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision, turn, "exp", variant, "assigned", "jev", "1", "p1", "f1",
             '["control","treatment"]', variant, "control", 0.1, index + 1, metadata),
        )
        conn.execute(
            "INSERT INTO llm_invocations(invocation_id,turn_id,router_decision_id,invocation_index,"
            "provider,model_selected,started_at_us,ended_at_us,status) VALUES(?,?,?,?,?,?,?,?,?)",
            (invocation, turn, decision, 1, "test", variant, index + 1, index + 2, "completed"),
        )
        if index != 9:
            conn.execute(
                "INSERT INTO usage_measurements(measurement_id,scope,turn_id,invocation_id,provider,"
                "model,measurement_source,is_estimated,token_semantics_version,input_tokens,output_tokens,observed_at_us) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"u{index}", "invocation", turn, invocation, "test", variant,
                 "provider_stream", 0, 1, 10, 2, index + 2),
            )
        if index < 8:
            conn.execute(
                "INSERT INTO experiment_outcomes(outcome_id,router_decision_id,outcome_name,outcome_value,"
                "outcome_definition_version,outcome_source,evaluator_name,evaluator_version,is_estimated,observed_at_us) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"o{index}", decision, "quality", 1, "v1", "evaluator", "judge", "1", 0, index + 2),
            )
    conn.commit()
    conn.close()

    result = MetricsQueries(root).experiment({"experiment_id": "exp"})
    assert result["diagnostics"]["sample_ratio_mismatch"]["sample_size"] == 10
    assert result["diagnostics"]["missing_outcomes"] == 2
    assert result["diagnostics"]["coverage_rates"]["control"]["exact_rate"] == 1
    assert result["diagnostics"]["coverage_rates"]["treatment"]["exact_rate"] == 2 / 3
    assert result["diagnostics"]["exact_coverage_rate_spread"] == pytest.approx(1 / 3)
    assert result["diagnostics"]["config_consistent"] is True


def test_experiment_srm_includes_configured_variant_with_zero_observations(tmp_path):
    root = tmp_path / "agent"
    conn = connect(root / "state/metrics/metrics.db")
    migrate(conn)
    metadata = json.dumps({
        "config_fingerprint": "same",
        "expected_weights": {"control": 0.5, "treatment": 0.5},
    })
    for index in range(20):
        conn.execute(
            "INSERT INTO sessions(session_id,session_name,brain,provider,started_at_us,status) "
            "VALUES(?,?,?,?,?,?)",
            (f"s{index}", f"s{index}", "test", "test", index + 1, "completed"),
        )
        conn.execute(
            "INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,is_user_initiated,"
            "started_at_us,ended_at_us,status) VALUES(?,?,?,?,?,?,?,?)",
            (f"t{index}", f"s{index}", 1, "test", 0, index + 1, index + 2, "completed"),
        )
        conn.execute(
            "INSERT INTO router_decisions(router_decision_id,turn_id,experiment_id,variant_id,"
            "assignment_status,router_name,router_version,policy_version,feature_schema_version,"
            "candidate_models_json,model_selected,control_model,router_latency_ms,decided_at_us,metadata_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"r{index}", f"t{index}", "exp", "control", "assigned", "jev", "1", "p1", "f1",
             '["control","treatment"]', "control", "control", 0.1, index + 1, metadata),
        )
    conn.commit()
    conn.close()

    result = MetricsQueries(root).experiment({"experiment_id": "exp"})
    srm = result["diagnostics"]["sample_ratio_mismatch"]
    assert srm["observed"] == {"control": 20, "treatment": 0}
    assert srm["detected"] is True
