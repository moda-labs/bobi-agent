"""Opt-in routing and token persistence proof against an operator gateway."""

from dataclasses import replace
import json
import os
import shutil
import subprocess
import sys

import pytest

from bobi import paths
from bobi.brain import get_brain
from bobi.metrics.collector import MetricsCollectorService
from bobi.metrics.query import MetricsQueries
from bobi.metrics.routing import resolve_route
from bobi.metrics.router import provider_subprocess_env
from bobi.metrics.runtime import MetricsRuntime
from bobi.metrics.store import connect
from bobi.sdk import save_session_id
from tests.metrics.test_routing import configured, context
from .conftest import _drain


@pytest.mark.skipif(os.environ.get("BOBI_JEV_LIVE_TYPESAFE") != "1",
                    reason="requires approved live TypeSafe and execution gateway opt-in")
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_typesafe_shadow_gateway_tokens_and_runtime_handoff(tmp_path, monkeypatch):
    assert shutil.which("codex"), "Codex CLI is required for live acceptance"
    model = os.environ["BOBI_BRAIN_MODEL"]
    alternative = os.environ["BOBI_JEV_CANDIDATE_MODEL"]
    assert model != alternative and os.environ["TYPESAFE_API_KEY"]
    assert os.environ["BOBI_GATEWAY_BASE_URL"] and os.environ["BOBI_GATEWAY_API_KEY"]
    root = tmp_path / "runtime"
    root.mkdir()
    paths.bind_root(None)
    paths.bind_root(root)
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    (tmp_path / "codex-home").mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("BOBI_BRAIN", "codex")
    monkeypatch.setenv("BOBI_GATEWAY_WIRE_API", "responses")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    raw = configured()
    raw.update(experiment_id="isolated-typesafe-gateway-acceptance", cohort="synthetic-only",
               control_model=model)
    raw["variants"][0]["model"] = model
    raw["variants"][1]["policy"] = "typesafe-jev"
    raw["policy"].update(name="typesafe-jev", version="jev-1.13.0", mode="shadow",
        deadline_ms=3000, credential_env="TYPESAFE_API_KEY", candidate_models=[model, alternative],
        options={"instructions": f"Choose the least costly capable candidate. When task information is insufficient, choose {model}.",
                 "criteria": {model: "General engineering or insufficient task information",
                              alternative: "Small, well-defined tasks with low reasoning complexity"}})
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    environment_file = root / ".env"
    with os.fdopen(os.open(environment_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        for name in ("BOBI_METRICS_EXPERIMENT_JSON", "BOBI_METRICS_ASSIGNMENT_SECRET", "TYPESAFE_API_KEY"):
            stream.write(f"{name}={os.environ[name]}\n")
    handoff = subprocess.run([sys.executable, "-c", (
        "import os; from pathlib import Path; from bobi.env import child_agent_env; "
        "from bobi.metrics.router import load_experiment, provider_subprocess_env; "
        "assert not os.environ.get('TYPESAFE_API_KEY'); "
        f"child=child_agent_env(Path({str(root)!r})); "
        "config, secret=load_experiment(child); "
        "assert config.policy.mode == 'shadow' and secret; "
        "assert child.get('TYPESAFE_API_KEY'); "
        "assert not {'TYPESAFE_API_KEY', 'BOBI_METRICS_ASSIGNMENT_SECRET', "
        "'BOBI_METRICS_EXPERIMENT_JSON'} & provider_subprocess_env(child).keys()"
    )], env=provider_subprocess_env(), capture_output=True, timeout=15)
    assert handoff.returncode == 0, "Root-bound CLI routing configuration handoff failed"
    canary = "synthetic-routing-privacy-canary-20261001"
    ctx = replace(context(raw, "treatment"), prompt="Reply with OK only. " + canary)
    receipts = []
    resume_id = None
    for fresh in (True, False):
        runtime = MetricsRuntime(root, mode="enabled")
        client = None
        try:
            outcome = await resolve_route(replace(ctx, fresh=fresh), runtime=runtime)
            assert outcome.decision and outcome.model == model
            metadata = json.loads(outcome.decision.policy_metadata_json)
            assert metadata["status"] == ("decided" if fresh else "reused")
            assert outcome.decision.fallback_reason is None and metadata["mode"] == "shadow"
            assert metadata["recommended_model"] in {model, alternative}
            assert metadata["cost_usd"] is None and metadata["breaker_state"] == "closed"
            assert "TYPESAFE_API_KEY" not in provider_subprocess_env()
            client = get_brain("codex").make_session(cwd=str(root), resume=resume_id,
                system_prompt="Reply with OK only. Do not use tools or read files.",
                options={"model": outcome.model, "mcp_servers": {}, "max_turns": 1})
            assert client.provider == "gateway"
            await client.connect()
            turn = runtime.begin_turn("agent", provider="gateway", brain="codex", model_requested=model)
            await client.query("Reply with OK only. Do not use tools.")
            final_text, result = await _drain(client)
            assert result is not None and not result.is_error and result.session_id
            assert final_text.strip() == "OK"
            if fresh:
                assert result.usage and all(usage.input_tokens > 0 and usage.output_tokens > 0 for usage in result.usage)
            assert all(usage.model in {model, model.rsplit("/", 1)[-1]} for usage in result.usage)
            turn.record_result(result)
            turn.finish(status="completed")
            resume_id = result.session_id
            save_session_id("agent", resume_id, model=model, root=root)
            receipts.append({"status": metadata["status"], "recommendation": metadata["recommended_model"],
                "latency_ms": metadata["latency_ms"], "confidence": metadata["confidence"],
                "input_tokens": sum(usage.input_tokens for usage in result.usage) if result.usage else None,
                "output_tokens": sum(usage.output_tokens for usage in result.usage) if result.usage else None})
        finally:
            if client is not None:
                await client.disconnect()
            assert runtime.close(timeout=2)
        MetricsCollectorService(root).collect_once()
        def no_policy(config):
            raise AssertionError("Sticky reuse loaded the policy")

        monkeypatch.setattr("bobi.metrics.routing.load_policy", no_policy)
    with connect(root / "state/metrics/metrics.db", readonly=True) as connection:
        decisions = connection.execute("SELECT metadata_json FROM router_decisions ORDER BY decided_at_us").fetchall()
        assert [json.loads(row[0])["policy"]["status"] for row in decisions] == ["decided", "reused"]
        tokens = connection.execute("SELECT SUM(input_tokens),SUM(output_tokens) FROM best_usage WHERE scope='turn'").fetchone()
        assert tuple(tokens) == tuple(sum(receipt[name] for receipt in receipts if receipt[name] is not None)
                                      for name in ("input_tokens", "output_tokens"))
    report = MetricsQueries(root).experiment({"experiment_id": raw["experiment_id"]})
    assert report["policy_breakdown"]["unique_policy_calls"] == 1
    assert report["policy_breakdown"]["unknown_policy_cost_calls"] == 1
    for artifact in (root / "state/metrics").rglob("*"):
        if artifact.is_file():
            contents = artifact.read_bytes()
            assert os.environ["TYPESAFE_API_KEY"].encode() not in contents
            assert canary.encode() not in contents
    print(json.dumps({"engine": "codex", "policy": "typesafe-jev", "mode": "shadow",
                      "model": model, "cohort": "synthetic-only", "turns": receipts,
                      "unique_policy_calls": 1, "runtime_handoff": "passed"}))


@pytest.mark.skipif(os.environ.get("BOBI_JEV_LIVE_GATEWAY") != "1",
                    reason="requires explicit live gateway routing opt-in")
@pytest.mark.parametrize("engine", ["codex", "claude"])
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_routed_gateway_model_persists_tokens_and_reuses_decision(tmp_path, monkeypatch, engine):
    if not shutil.which(engine):
        pytest.skip(f"{engine} CLI not installed")
    model = os.environ.get("BOBI_BRAIN_MODEL", "")
    assert model and os.environ.get("BOBI_GATEWAY_BASE_URL", "")
    assert os.environ.get("BOBI_GATEWAY_API_KEY", "")
    root = tmp_path / "runtime"
    root.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    paths.bind_root(None)
    paths.bind_root(root)
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setenv("BOBI_BRAIN", engine)
    if engine == "claude":
        monkeypatch.setenv("BOBI_GATEWAY_BASE_URL", os.environ["BOBI_GATEWAY_BASE_URL"].removesuffix("/v1"))
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", os.environ["BOBI_GATEWAY_API_KEY"])
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("BOBI_GATEWAY_WIRE_API", "responses")
    monkeypatch.delenv("BOBI_BRAIN_EFFORT", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    raw = configured()
    raw["control_model"] = "unused-control-model"
    raw["variants"][0]["model"] = raw["control_model"]
    raw["policy"].update(brain=engine, mode="enforce", candidate_models=[raw["control_model"], model],
                         options={"model": model})
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    ctx = replace(context(raw, "treatment"), brain=engine)
    receipts = []
    resume_id = None
    for fresh in (True, False):
        runtime = MetricsRuntime(root, mode="enabled")
        client = None
        try:
            outcome = await resolve_route(replace(ctx, fresh=fresh), runtime=runtime)
            assert outcome.decision and outcome.model == model
            client = get_brain(engine).make_session(cwd=str(root), resume=resume_id,
                system_prompt="Reply concisely. Do not call tools or read files.",
                options={"model": outcome.model, "mcp_servers": {}, "max_turns": 1})
            assert client.provider == "gateway"
            await client.connect()
            turn = runtime.begin_turn("agent", provider="gateway", brain=engine,
                                      model_requested=outcome.model)
            await client.query("Reply with OK only. Do not use tools.")
            _, result = await _drain(client)
            assert result is not None and not result.is_error
            assert result.session_id
            if fresh:
                assert result.usage
            assert result.invocations and all(invocation.model in {model, model.rsplit("/", 1)[-1]}
                                              for invocation in result.invocations)
            assert all(usage.model in {model, model.rsplit("/", 1)[-1]} for usage in result.usage)
            turn.record_result(result)
            turn.finish(status="completed")
            resume_id = result.session_id
            save_session_id("agent", resume_id, model=model, root=root)
            measurements = result.usage or [invocation.usage for invocation in result.invocations
                                           if invocation.usage is not None]
            assert all(usage.input_tokens is not None and usage.output_tokens is not None for usage in measurements)
            receipts.append({"status": json.loads(outcome.decision.policy_metadata_json)["status"],
                "scope": "turn" if result.usage else "invocation" if measurements else "unknown",
                "input_tokens": sum(usage.input_tokens for usage in measurements) if measurements else None,
                "output_tokens": sum(usage.output_tokens for usage in measurements) if measurements else None})
            if fresh:
                assert receipts[-1]["input_tokens"] > 0 and receipts[-1]["output_tokens"] > 0
        finally:
            if client is not None:
                await client.disconnect()
            assert runtime.close(timeout=2)
        MetricsCollectorService(root).collect_once()

        def no_policy(config):
            raise AssertionError("resume must reuse the decision without loading a policy")

        monkeypatch.setattr("bobi.metrics.routing.load_policy", no_policy)
    with connect(root / "state/metrics/metrics.db", readonly=True) as connection:
        decisions = connection.execute("SELECT model_selected,metadata_json FROM router_decisions ORDER BY decided_at_us").fetchall()
        assert len(decisions) == 2
        assert [json.loads(row["metadata_json"])["policy"]["status"] for row in decisions] == ["decided", "reused"]
        assert all(row["model_selected"] == model for row in decisions)
        usage = connection.execute("SELECT SUM(input_tokens),SUM(output_tokens) FROM best_usage WHERE scope='turn'").fetchone()
        assert usage[0] == sum(receipt["input_tokens"] for receipt in receipts if receipt["scope"] == "turn")
        assert usage[1] == sum(receipt["output_tokens"] for receipt in receipts if receipt["scope"] == "turn")
        measured = connection.execute("SELECT COUNT(DISTINCT turn_id) FROM best_usage").fetchone()[0]
        assert measured == sum(receipt["input_tokens"] is not None for receipt in receipts)
    report = MetricsQueries(root).experiment({"experiment_id": raw["experiment_id"]})
    assert report["policy_breakdown"]["unique_policy_calls"] == 1
    assert report["policy_breakdown"]["known_policy_cost_usd"] == 0.0
    assert report["variants"][0]["tokens"]["input_tokens"] == sum(receipt["input_tokens"] for receipt in receipts if receipt["input_tokens"] is not None)
    assert report["variants"][0]["tokens"]["output_tokens"] == sum(receipt["output_tokens"] for receipt in receipts if receipt["output_tokens"] is not None)
    assert report["variants"][0]["coverage"]["exact_measurement_turns"] == measured
    assert report["variants"][0]["coverage"]["unknown_measurement_turns"] == len(receipts) - measured
    print(json.dumps({"engine": engine, "model": model, "turns": receipts, "tokens_persisted": True,
                      "turns_with_exact_usage": measured, "turns_without_usage": len(receipts) - measured}))
