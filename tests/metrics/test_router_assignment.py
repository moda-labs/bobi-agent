import json
import multiprocessing
from pathlib import Path

import pytest

from bobi.metrics.router import (
    ExperimentConfig,
    assign_variant,
    choose_assignment_key,
    load_experiment,
)


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


@pytest.mark.parametrize("selected_index", [0, 1])
def test_full_allocation_never_assigns_zero_weight_arm(selected_index, monkeypatch):
    fixture = json.loads(FIXTURE.read_text())
    raw = fixture["config"]
    for index, variant in enumerate(raw["variants"]):
        variant["weight"] = int(index == selected_index)
    config = ExperimentConfig.from_mapping(raw)
    for index in range(1000):
        selected, _, _ = assign_variant(config, fixture["secret"].encode(),
            assignment_unit="run_key", assignment_key=f"synthetic-{index}")
        assert selected.variant_id == config.variants[selected_index].variant_id

    class BoundaryDigest:
        def digest(self):
            return b"\xff" * 32

        def hexdigest(self):
            return self.digest().hex()

    monkeypatch.setattr("bobi.metrics.router.hmac.new", lambda *args, **kwargs: BoundaryDigest())
    selected, _, _ = assign_variant(config, b"synthetic-secret",
        assignment_unit="run_key", assignment_key="boundary")
    assert selected.variant_id == config.variants[selected_index].variant_id


@pytest.mark.parametrize("weights", [(-0.1, 1.1), (0, 0), (1, 1)])
def test_invalid_allocation_weights_are_rejected(weights):
    raw = json.loads(FIXTURE.read_text())["config"]
    for variant, weight in zip(raw["variants"], weights):
        variant["weight"] = weight
    with pytest.raises(ValueError, match="weight"):
        ExperimentConfig.from_mapping(raw)


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
