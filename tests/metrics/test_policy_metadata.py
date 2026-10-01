import json

from bobi.metrics.router import RouterDecision, projection_fields

def test_policy_metadata_preserves_assignment_and_executed_model():
    decision = RouterDecision(
        experiment_id="policy-experiment", variant_id="treatment_jev",
        assignment_unit="run_key", assignment_status="assigned",
        assignment_key_hash="hashed", assignment_algorithm="hmac-sha256-u64-v1",
        cohort=None, router_name="bobi-arm", router_version="1", policy_version="p1",
        feature_schema_version="jev-features-v1", candidate_models=("control", "cheap"),
        model_selected="control", control_model="control", router_score=0.7,
        router_reason="policy_shadow", router_latency_ms=10, fallback_reason=None,
        decided_at_us=100, expected_weight=0.5,
        expected_weights=(("control", 0.5), ("treatment_jev", 0.5)),
        config_fingerprint="fingerprint", entry_point="session_start",
        policy_metadata_json=json.dumps({"mode": "shadow", "recommended_model": "cheap",
                                         "status": "decided", "call_id": "policy-call"}),
    )
    fields = projection_fields(decision)
    metadata = json.loads(fields["metadata_json"])
    assert fields["model_selected"] == "control"
    assert fields["router_score"] == 0.7
    assert fields["fallback_reason"] is None
    assert metadata["policy"]["recommended_model"] == "cheap"
    assert metadata["policy"]["call_id"] == "policy-call"
    assert metadata["config_fingerprint"] == "fingerprint"
    assert metadata["expected_weights"] == {"control": 0.5, "treatment_jev": 0.5}
