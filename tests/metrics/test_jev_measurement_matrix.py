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
            "candidate_models": ["control", "alternative", "advanced"], "min_confidence": 0.85,
            "scope": {"entry_points": ["session_start"], "roles": ["engineer"]},
            "egress": {"prompt": "none"}, "credential_env": "TEST_TYPESAFE_KEY",
            "options": {"instructions": "Choose the adequate candidate", "criteria": {
                "control": "Routine tasks", "alternative": "Demanding reasoning",
                "advanced": "Multi-constraint architectural reasoning"}}}}

def test_comparison_config_is_isolated_and_keeps_criteria_without_difficulty_labels():
    original = comparison_config()
    before = copy.deepcopy(original)
    prepared = prepare_comparison(original, ["control"])
    assert original == before
    assert prepared["experiment_id"] != original["experiment_id"]
    assert prepared["policy"]["mode"] == "enforce" and prepared["policy"]["egress"]["prompt"] == "redacted"
    assert set(prepared["policy"]["options"]["criteria"]) == {"control"}
    cases = load_cases(Path("tests/fixtures/metrics/jev-bobi-question-cases.json"))
    assert len(cases) == 30 and all(sum(case["tier"] == tier for case in cases) == 10 for tier in ("simple", "medium", "complex"))
    for case in cases:
        state, _ = build_features(prompt=case["prompt"], repo_path="", entry_point="session_start",
            role="engineer", brain="codex", prompt_egress="redacted")
        assert state["task"] == case["prompt"]
        assert not {"tier", "expected", "recommended_model"} & state.keys()
        assert quality_passed(json.dumps(case["expected"]), case["expected"])

@pytest.mark.parametrize("brain,models,provider_turns", [
    ("codex", ["control", "alternative", "advanced"], 120),
    ("claude", ["control", "alternative"], 90),
])
def test_default_preview_never_calls_provider_or_policy_or_changes_environment(monkeypatch, tmp_path, brain, models, provider_turns):
    environment = tmp_path / "private.env"
    raw = comparison_config()
    raw["policy"].update(brain=brain, candidate_models=models)
    raw["policy"]["options"]["criteria"] = {model: raw["policy"]["options"]["criteria"][model] for model in models}
    environment.write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(raw) + "\n")
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
    assert plan["provider_turns"] == provider_turns and plan["policy_calls"] == 30
    assert plan["brain"] == brain and all(state["state"]["brain"] == brain for state in plan["policy_states"])
    assert all(item["state"]["task"] for item in plan["policy_states"])
    assert len({item["state"]["prompt_bytes"] for item in plan["policy_states"]}) == 1
    assert len({item["state"]["task"] for item in plan["policy_states"]}) == 30
    assert not (args.artifacts / "runtime").exists()

def test_mismatched_runtime_and_policy_brains_are_rejected_before_execution(tmp_path):
    environment = tmp_path / "private.env"
    environment.write_text("BOBI_BRAIN=claude\nBOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(comparison_config()) + "\n")
    args = argparse.Namespace(cases=Path("tests/fixtures/metrics/jev-bobi-question-cases.json"),
        repeats=1, limit=3, offset=0, env_file=environment, models=None, artifacts=tmp_path / "mismatch", execute=True)
    with pytest.raises(ValueError, match="must match the policy brain"):
        asyncio.run(measure(args))
    assert not args.artifacts.exists()

def test_two_candidate_claude_pilot_plans_nine_turns_and_three_policy_calls(tmp_path):
    raw = comparison_config()
    raw["control_model"] = raw["variants"][0]["model"] = "provider/control"
    raw["policy"].update(brain="claude", candidate_models=["provider/control", "provider/reasoning"])
    raw["policy"]["options"]["criteria"] = {
        "provider/control": "Routine", "provider/reasoning": "Demanding reasoning"}
    environment = tmp_path / "private.env"
    environment.write_text("BOBI_BRAIN=claude\nBOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(raw) + "\n")
    args = argparse.Namespace(cases=Path("tests/fixtures/metrics/jev-bobi-question-cases.json"),
        repeats=1, limit=3, offset=6, env_file=environment, models=None, artifacts=tmp_path / "pilot", execute=False)
    assert asyncio.run(measure(args)) == 0
    plan = json.loads((args.artifacts / "plan.json").read_text())
    assert plan["provider_turns"] == 9 and plan["policy_calls"] == 3 and plan["within_live_budget"]
    assert plan["strategies"] == ["fixed:provider/control", "fixed:provider/reasoning", "jev"]
    assert set(plan["experiment"]["policy"]["options"]["criteria"]) == {"provider/control", "provider/reasoning"}

@pytest.mark.parametrize("aliases", [[], {"unknown": "model"}, {"control": ""}, {"control": 123},
    {"control": "duplicate", "alternative": "duplicate"}, {"control": "alternative"}])
def test_invalid_measurement_aliases_are_rejected_before_execution(tmp_path, aliases):
    environment = tmp_path / "private.env"
    environment.write_text("BOBI_MEASUREMENT_MODEL_ALIASES_JSON=" + json.dumps(aliases)
        + "\nBOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(comparison_config()) + "\n")
    args = argparse.Namespace(cases=Path("tests/fixtures/metrics/jev-bobi-question-cases.json"),
        repeats=1, limit=1, offset=0, env_file=environment, models=None, artifacts=tmp_path / "invalid", execute=True)
    with pytest.raises(ValueError, match="explicit measurement model aliases"):
        asyncio.run(measure(args))
    assert not args.artifacts.exists()

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
    ("advanced", 0.99, "advanced", None),
    ("advanced", 0.85, "advanced", None),
    ("advanced", 0.849999, "control", "policy_low_confidence"),
    ("alternative", 0.70, "control", "policy_low_confidence"),
])
@pytest.mark.parametrize("measurement", ["exact", "aliased", "missing", "wrong_model"])
@pytest.mark.parametrize("brain", ["codex", "claude"])
@pytest.mark.parametrize("case_id", [case["id"] for case in
    load_cases(Path("tests/fixtures/metrics/jev-bobi-question-cases.json"))])
def test_mocked_comparison_executes_guarded_choice_and_reports_routing_overhead(
        monkeypatch, tmp_path, selected, confidence, executed_model, fallback, measurement, brain, case_id):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.setattr(paths, "_root", None)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-only-native-token")
    raw = comparison_config()
    raw["policy"]["brain"] = brain
    aliases = {model: "canonical-" + model for model in raw["policy"]["candidate_models"]} if measurement == "aliased" else {}
    environment = tmp_path / "private.env"
    environment.write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(raw)
        + "\nTEST_TYPESAFE_KEY=test-only-policy-secret\nBOBI_GATEWAY_API_KEY=test-only-gateway-secret"
        + "\nLLM_GATEWAY_URL=http://localhost:1/v1\nBOBI_MEASUREMENT_MODEL_ALIASES_JSON=" + json.dumps(aliases) + "\n")
    cases = load_cases(Path("tests/fixtures/metrics/jev-bobi-question-cases.json"))
    case_index = next(index for index, case in enumerate(cases) if case["id"] == case_id)
    case = cases[case_index]
    policy_requests = []
    def policy_handler(request):
        policy_requests.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"route": {
            "type": "choice", "choice": selected, "confidence": confidence,
            "probabilities": {model: float(model == selected) for model in raw["policy"]["candidate_models"]}}},
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
            yield TurnResult(session_id="synthetic-provider", usage=[] if measurement == "missing" else [BrainUsage(
                model="not-requested" if measurement == "wrong_model" else aliases.get(self.model, self.model),
                input_tokens=10, output_tokens=2)])
        async def disconnect(self):
            pass
    class FakeBrain:
        def make_session(self, *, cwd, system_prompt, options):
            assert not any(Path(cwd).iterdir())
            assert os.environ["BOBI_BRAIN"] == brain
            if brain == "claude":
                assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
                assert os.environ["ANTHROPIC_BASE_URL"] == "http://localhost:1"
                assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "test-only-gateway-secret"
                assert os.environ["ANTHROPIC_API_KEY"] == ""
                assert Path(os.environ["CLAUDE_CONFIG_DIR"]).is_dir()
                assert options["tools"] == [] and options["setting_sources"] == []
                assert options["permission_mode"] == "default" and options["strict_mcp_config"]
            executed.append(options["model"])
            return FakeSession(options["model"])
    def fake_brain(kind):
        assert kind == brain
        return FakeBrain()
    monkeypatch.setattr("scripts.jev_measurement_matrix.get_brain", fake_brain)
    args = argparse.Namespace(cases=Path("tests/fixtures/metrics/jev-bobi-question-cases.json"),
        repeats=1, limit=1, offset=case_index, env_file=environment, models=None, artifacts=tmp_path / "live-mock", execute=True)
    verified = measurement in {"exact", "aliased"}
    assert asyncio.run(measure(args)) == int(not verified)
    assert executed == ["control", "alternative", "advanced", executed_model]
    assert len(policy_requests) == 1 and policy_requests[0]["state"]["task"].rstrip() == case["prompt"]
    report = json.loads((args.artifacts / "report.json").read_text())
    assert report["brain"] == brain and policy_requests[0]["state"]["brain"] == brain
    assert set(report["summary"]) == {"fixed:control", "fixed:alternative", "fixed:advanced", "jev"}
    assert report["usage_verified"] == verified
    assert all(row["usage_verified"] == verified for row in report["rows"])
    assert report["measurement_model_aliases"] == aliases
    assert all(row["quality_passed"] for row in report["rows"])
    if verified:
        assert all(row["input_tokens"] == 10 for row in report["rows"])
        assert all(row["model"] == aliases.get(row["requested_model"], row["requested_model"]) for row in report["rows"])
    routed = report["rows"][-1]
    assert routed["requested_model"] == executed_model and routed["recommended_model"] == selected
    assert routed["fallback_reason"] == fallback
    assert routed["wall_ms"] >= routed["execution_wall_ms"]
    assert routed["routing_wall_ms"] > 0 and routed["policy_status"] == ("failed" if fallback else "decided")
    assert json.loads(environment.read_text().splitlines()[0].partition("=")[2]) == raw
