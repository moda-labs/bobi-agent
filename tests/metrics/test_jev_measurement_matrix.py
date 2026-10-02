import argparse
import asyncio
import copy
import json
import os
from pathlib import Path

import httpx
import pytest

from bobi import paths
from bobi.brain.base import AssistantText, BrainUsage, TurnResult
from bobi.metrics.features import build_features
from scripts.jev_measurement_matrix import load_cases, measure, prepare_comparison, quality_passed, summarize


def test_synthetic_cases_cover_each_difficulty_and_have_unique_ids():
    cases = load_cases(Path("tests/fixtures/metrics/jev-measurement-cases.json"))
    assert len(cases) == 9
    assert len({case["id"] for case in cases}) == 9
    assert all(sum(case["tier"] == tier for case in cases) == 3 for tier in ("simple", "medium", "complex"))
    assert all(quality_passed(json.dumps(case["expected"]), case["expected"]) for case in cases)


@pytest.mark.parametrize("text", ["not JSON", '```json\n{"value":16}\n```', '{"value":17}', '{"value":"16"}', '{"value":16.0}'])
def test_quality_evaluator_does_not_infer_success(text):
    assert not quality_passed(text, {"value": 16})


def test_case_validation_rejects_duplicate_ids(tmp_path):
    case = {"id": "duplicate", "tier": "simple", "prompt": "Synthetic task", "expected": {"value": 1}}
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([case, case]))
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(path)


def test_summary_preserves_unknown_token_dimensions_and_cost():
    rows = [{"requested_model": "candidate", "wall_ms": 100, "quality_passed": True,
             "error_kind": None, "input_tokens": 10, "output_tokens": 2},
            {"requested_model": "candidate", "wall_ms": 200, "quality_passed": False,
             "error_kind": "TimeoutError", "input_tokens": None, "output_tokens": None}]
    result = summarize(rows)["candidate"]
    assert result["input_tokens"] is None and result["output_tokens"] is None
    assert result["missing_input_tokens"] == 1 and result["total_cost_usd"] is None
    assert result["quality_passes"] == 1 and result["provider_errors"] == 1
    assert result["median_wall_ms"] == 150 and result["p95_wall_ms"] == 200

def comparison_config():
    return {"experiment_id": "production-shadow", "router_name": "bobi-arm", "router_version": "1",
        "policy_version": "p1", "feature_schema_version": "jev-features-v1", "control_model": "control",
        "variants": [{"variant_id": "control", "weight": 0.5, "model": "control"},
            {"variant_id": "treatment", "weight": 0.5, "policy": "typesafe-jev"}],
        "policy": {"name": "typesafe-jev", "version": "jev-1.13.0", "brain": "codex", "mode": "shadow",
            "candidate_models": ["control", "alternative"], "min_confidence": 0.85,
            "scope": {"entry_points": ["session_start"], "roles": ["engineer"]},
            "egress": {"prompt": "none"}, "credential_env": "TEST_TYPESAFE_KEY",
            "options": {"instructions": "Choose the adequate candidate", "criteria": {
                "control": "Routine tasks", "alternative": "Demanding reasoning"}}}}

def test_comparison_config_is_isolated_and_keeps_criteria_without_difficulty_labels():
    original = comparison_config()
    before = copy.deepcopy(original)
    prepared = prepare_comparison(original, ["control"])
    assert original == before
    assert prepared["experiment_id"] != original["experiment_id"]
    assert prepared["policy"]["mode"] == "enforce" and prepared["policy"]["egress"]["prompt"] == "redacted"
    assert set(prepared["policy"]["options"]["criteria"]) == {"control"}
    cases = load_cases(Path("tests/fixtures/metrics/jev-bobi-question-cases.json"))
    assert len(cases) == 12 and all(sum(case["tier"] == tier for case in cases) == 4 for tier in ("simple", "medium", "complex"))
    for case in cases:
        state, _ = build_features(prompt=case["prompt"], repo_path="", entry_point="session_start",
            role="engineer", brain="codex", prompt_egress="redacted")
        assert state["task"] == case["prompt"]
        assert not {"tier", "expected", "recommended_model"} & state.keys()
        assert quality_passed(json.dumps(case["expected"]), case["expected"])

def test_default_preview_never_calls_provider_or_policy_or_changes_environment(monkeypatch, tmp_path):
    environment = tmp_path / "private.env"
    environment.write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(comparison_config()) + "\n")
    def forbidden(*args, **kwargs):
        raise AssertionError("Offline preview attempted live execution")
    monkeypatch.setattr("scripts.jev_measurement_matrix.get_brain", forbidden)
    monkeypatch.setattr("scripts.jev_measurement_matrix.resolve_route", forbidden)
    monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient", forbidden)
    args = argparse.Namespace(cases=Path("tests/fixtures/metrics/jev-bobi-question-cases.json"),
        repeats=1, limit=None, offset=0, env_file=environment, models=None, artifacts=tmp_path / "preview", execute=False)
    before = dict(os.environ)
    assert asyncio.run(measure(args)) == 0
    assert dict(os.environ) == before
    plan = json.loads((args.artifacts / "plan.json").read_text())
    assert plan["provider_turns"] == 36 and plan["policy_calls"] == 12
    assert all(item["state"]["task"] for item in plan["policy_states"])
    assert len({item["state"]["prompt_bytes"] for item in plan["policy_states"]}) == 1
    assert len({item["state"]["task"] for item in plan["policy_states"]}) == 12
    assert not (args.artifacts / "runtime").exists()

def test_live_budget_is_checked_before_credentials_or_network(tmp_path):
    environment = tmp_path / "private.env"
    environment.write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(comparison_config()) + "\n")
    args = argparse.Namespace(cases=Path("tests/fixtures/metrics/jev-bobi-question-cases.json"),
        repeats=2, limit=None, offset=0, env_file=environment, models=None, artifacts=tmp_path / "too-large", execute=True)
    with pytest.raises(ValueError, match="40 provider turns"):
        asyncio.run(measure(args))
    assert not args.artifacts.exists()

@pytest.mark.parametrize("selected,confidence,executed_model,fallback", [
    ("control", 0.99, "control", None),
    ("alternative", 0.99, "alternative", None),
    ("alternative", 0.70, "control", "policy_low_confidence"),
])
def test_mocked_comparison_executes_guarded_choice_and_reports_routing_overhead(
        monkeypatch, tmp_path, selected, confidence, executed_model, fallback):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setattr(paths, "_root", None)
    raw = comparison_config()
    environment = tmp_path / "private.env"
    environment.write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(raw)
        + "\nTEST_TYPESAFE_KEY=test-only-policy-secret\nBOBI_GATEWAY_API_KEY=test-only-gateway-secret"
        + "\nLLM_GATEWAY_URL=http://localhost:1/v1\n")
    cases = load_cases(Path("tests/fixtures/metrics/jev-bobi-question-cases.json"))
    case = cases[0]
    policy_requests = []
    def policy_handler(request):
        policy_requests.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"route": {
            "type": "choice", "choice": selected, "confidence": confidence,
            "probabilities": {"control": 0.99 if selected == "control" else 0.01,
                "alternative": 0.99 if selected == "alternative" else 0.01}}},
            "usage": {"input_tokens": 10, "output_tokens": 2}})
    client_class = httpx.AsyncClient
    monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(policy_handler), **kwargs))
    executed = []
    class FakeSession:
        def __init__(self, model):
            self.model = model
        async def connect(self):
            pass
        async def query(self, prompt):
            assert prompt.rstrip() == case["prompt"]
        async def receive_response(self):
            yield AssistantText(json.dumps(case["expected"]))
            yield TurnResult(session_id="synthetic-provider", usage=[BrainUsage(
                model=self.model, input_tokens=10, output_tokens=2)])
        async def disconnect(self):
            pass
    class FakeBrain:
        def make_session(self, *, cwd, system_prompt, options):
            assert not any(Path(cwd).iterdir())
            executed.append(options["model"])
            return FakeSession(options["model"])
    monkeypatch.setattr("scripts.jev_measurement_matrix.get_brain", lambda kind: FakeBrain())
    args = argparse.Namespace(cases=Path("tests/fixtures/metrics/jev-bobi-question-cases.json"),
        repeats=1, limit=1, offset=0, env_file=environment, models=None, artifacts=tmp_path / "live-mock", execute=True)
    assert asyncio.run(measure(args)) == 0
    assert executed == ["control", "alternative", executed_model]
    assert len(policy_requests) == 1 and policy_requests[0]["state"]["task"].rstrip() == case["prompt"]
    report = json.loads((args.artifacts / "report.json").read_text())
    assert set(report["summary"]) == {"fixed:control", "fixed:alternative", "jev"}
    assert all(row["quality_passed"] and row["input_tokens"] == 10 for row in report["rows"])
    routed = report["rows"][-1]
    assert routed["requested_model"] == executed_model and routed["recommended_model"] == selected
    assert routed["fallback_reason"] == fallback
    assert routed["wall_ms"] >= routed["execution_wall_ms"]
    assert routed["routing_wall_ms"] > 0 and routed["policy_status"] == ("failed" if fallback else "decided")
    assert json.loads(environment.read_text().splitlines()[0].partition("=")[2]) == raw
