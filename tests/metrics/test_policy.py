import asyncio
import copy
import json
from pathlib import Path

import pytest

from bobi.metrics.policy import PolicyRequest, StaticPolicy
from bobi.metrics.router import ExperimentConfig, RouterDecision, projection_fields, public_config


def test_static_policy_selects_only_an_allowed_model():
    request = PolicyRequest("jev-features-v1", {}, ("control", "cheap"), "control", "v1")
    result = asyncio.run(StaticPolicy("cheap").decide(request, timeout_s=1))
    assert result.model == "cheap"
    assert result.model_version == "v1"
    assert result.confidence == 1.0
    with pytest.raises(ValueError, match="candidate"):
        asyncio.run(StaticPolicy("unknown").decide(request, timeout_s=1))
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(StaticPolicy("cheap").decide(request, timeout_s=0))
    with pytest.raises(ValueError, match="requires a model"):
        StaticPolicy("")

def config():
    raw = json.loads(Path("tests/fixtures/metrics/jev-assignment-vectors.json").read_text())["config"]
    raw["variants"][1] = {"variant_id": "treatment_jev", "weight": 0.5, "policy": "static"}
    raw["policy"] = {
        "name": "static", "version": "v1", "brain": "codex", "mode": "shadow",
        "candidate_models": ["model-control", "cheap"],
        "scope": {"entry_points": ["session_start"], "roles": ["engineer"]},
        "egress": {"prompt": "none"}, "credential_env": "POLICY_API_KEY",
        "options": {"model": "cheap", "endpoint": "https://example.com"},
    }
    return raw

def test_policy_config_preserves_arm_and_public_fingerprint_contract():
    raw = config()
    parsed = ExperimentConfig.from_mapping(raw)
    assert parsed.variants[1].policy == "static"
    assert parsed.variants[1].model == ""
    assert parsed.policy.deadline_ms == 1000
    public = public_config(parsed)
    assert "endpoint" not in public["policy"]["options"]
    changed = copy.deepcopy(raw)
    changed["policy"]["options"]["endpoint"] = "https://other.example.com"
    assert public_config(ExperimentConfig.from_mapping(changed)) == public
    changed["policy"]["mode"] = "enforce"
    assert public_config(ExperimentConfig.from_mapping(changed)) != public

@pytest.mark.parametrize("field,value", [
    ("mode", "full"), ("brain", "unknown"), ("min_confidence", float("nan")),
    ("min_confidence", True), ("deadline_ms", 49), ("deadline_ms", 3001),
    ("deadline_ms", True), ("max_in_flight", 0), ("store_reason_text", "false"),
    ("credential_env", "bad-name"), ("candidate_models", ["cheap"]),
    ("candidate_models", ["model-control", "model-control"]),
    ("scope", {"entry_points": [], "roles": ["engineer"]}),
    ("egress", {}), ("egress", {"prompt": "redacted", "max_prompt_bytes": 0}),
    ("options", {"nested": [{"api_key": "never-store"}]}),
    ("options", {"rate": float("inf")}), ("unexpected", "field"),
])
def test_policy_config_rejects_invalid_input(field, value):
    raw = config()
    raw["policy"][field] = value
    with pytest.raises(ValueError):
        ExperimentConfig.from_mapping(raw)

def test_policy_and_variant_contract_is_strict():
    for change in ("missing", "mismatch", "both", "unused", "duplicate-control"):
        raw = config()
        if change == "missing":
            del raw["policy"]
        elif change == "mismatch":
            raw["variants"][1]["policy"] = "other"
        elif change == "both":
            raw["variants"][1]["model"] = "cheap"
        elif change == "unused":
            raw["variants"][1] = {"variant_id": "other", "weight": 0.5, "model": "cheap"}
        else:
            raw["variants"][0]["weight"] = 0.25
            raw["variants"].append({"variant_id": "second-control", "weight": 0.25, "model": "model-control"})
        with pytest.raises(ValueError):
            ExperimentConfig.from_mapping(raw)

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
