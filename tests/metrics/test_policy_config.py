import copy
import json
from pathlib import Path

import pytest

from bobi.metrics.router import ExperimentConfig, public_config

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
