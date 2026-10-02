#!/usr/bin/env python3
"""Preview or measure synthetic Bobi questions with JEV and fixed-model baselines."""

import argparse
import asyncio
import copy
import csv
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import statistics
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bobi import paths
from bobi.brain import get_brain
from bobi.brain.base import AssistantText, TurnResult
from bobi.config import parse_env_file
from bobi.fsutil import atomic_write_json
from bobi.metrics.collector import MetricsCollectorService
from bobi.metrics.features import build_features
from bobi.metrics.outcomes import record_quality_outcome
from bobi.metrics.policies.typesafe import TypeSafePolicy
from bobi.metrics.router import ExperimentConfig, assign_variant
from bobi.metrics.routing import RoutingContext, resolve_route
from bobi.metrics.runtime import MetricsRuntime
from bobi.redact import redact_secrets


def load_cases(path):
    cases = json.loads(path.read_text())
    if not isinstance(cases, list) or not 1 <= len(cases) <= 30:
        raise ValueError("Expected between one and 30 synthetic cases")
    identities = set()
    for case in cases:
        if (not isinstance(case, dict) or set(case) != {"id", "tier", "prompt", "expected"}
                or not isinstance(case["id"], str)
                or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", case["id"])
                or case["id"] in identities or not isinstance(case["tier"], str)
                or case["tier"] not in {"simple", "medium", "complex"}
                or not isinstance(case["prompt"], str) or not case["prompt"].strip()
                or len(case["prompt"].encode()) > 8192 or not isinstance(case["expected"], dict)):
            raise ValueError("Invalid or duplicate synthetic case")
        identities.add(case["id"])
    return cases


def quality_passed(text, expected):
    try:
        return (len(text.encode()) <= 65536
            and json.dumps(json.loads(text), sort_keys=True) == json.dumps(expected, sort_keys=True))
    except (ValueError, RecursionError):
        return False


def summarize(rows, *, group_by="requested_model"):
    summary = {}
    for model in dict.fromkeys(row[group_by] for row in rows):
        group = [row for row in rows if row[group_by] == model]
        latencies = sorted(row["wall_ms"] for row in group)
        summary[model] = {"turns": len(group), "quality_passes": sum(row["quality_passed"] for row in group),
            "provider_errors": sum(row["error_kind"] is not None for row in group),
            "median_wall_ms": statistics.median(latencies),
            "p95_wall_ms": latencies[math.ceil(0.95 * len(latencies)) - 1], "total_cost_usd": None}
        for dimension in ("input_tokens", "output_tokens"):
            values = [row.get(dimension) for row in group]
            summary[model][dimension] = sum(values) if all(value is not None for value in values) else None
            summary[model][f"missing_{dimension}"] = sum(value is None for value in values)
    return summary


def prepare_comparison(raw, models):
    config = ExperimentConfig.from_mapping(raw)
    if config.policy is None or config.policy.name != "typesafe-jev" or config.policy.brain != "codex":
        raise ValueError("A TypeSafe Codex configuration is required")
    TypeSafePolicy(config.policy)
    models = list(config.policy.candidate_models) if models is None else models
    if (not models or len(set(models)) != len(models)
            or not set(models).issubset(config.policy.candidate_models) or config.control_model not in models):
        raise ValueError("Select allowed models including control")
    prepared = copy.deepcopy(raw)
    prepared["experiment_id"] = "synthetic-jev-" + uuid.uuid4().hex
    prepared["cohort"] = "synthetic-bobi-questions-only"
    prepared["policy"].update(mode="enforce", candidate_models=models,
        egress={"prompt": "redacted", "max_prompt_bytes": 8192},
        scope={"entry_points": ["session_start"], "roles": ["engineer"]})
    criteria = prepared["policy"]["options"]["criteria"]
    prepared["policy"]["options"]["criteria"] = {model: criteria[model] for model in models}
    TypeSafePolicy(ExperimentConfig.from_mapping(prepared).policy)
    return prepared

async def measure(args):
    cases = load_cases(args.cases)
    prompt_bytes = max(len(case["prompt"].encode()) for case in cases)
    cases = [{**case, "prompt": case["prompt"] + " " * (prompt_bytes - len(case["prompt"].encode()))}
        for case in cases]
    if (not 1 <= args.repeats <= 3 or not 0 <= args.offset < len(cases)
            or args.limit is not None and not 1 <= args.limit <= len(cases) - args.offset):
        raise ValueError("Invalid repeat count, offset or case limit")
    cases = cases[args.offset:][:args.limit]
    values = parse_env_file(args.env_file)
    raw = prepare_comparison(json.loads(values["BOBI_METRICS_EXPERIMENT_JSON"]), args.models)
    config = ExperimentConfig.from_mapping(raw)
    models = list(config.policy.candidate_models)
    strategies = [(f"fixed:{model}", model) for model in models] + [("jev", config.control_model)]
    budget = len(cases) * len(strategies) * args.repeats
    if args.execute and budget > 40:
        raise ValueError("At most 40 provider turns per run; select a batch with --offset and --limit")
    credential = values.get(config.policy.credential_env, "")
    if args.execute and (not credential or not values.get("BOBI_GATEWAY_API_KEY") or not values.get("LLM_GATEWAY_URL")):
        raise ValueError("Private policy and gateway credentials are required")
    artifact_root = args.artifacts.resolve()
    artifact_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    plan = {"benchmark": "bobi-qa-semantic-paired-v1", "execute": args.execute,
        "case_ids": [case["id"] for case in cases], "strategies": [strategy for strategy, model in strategies],
        "provider_turns": budget, "policy_calls": len(cases) * args.repeats, "within_live_budget": budget <= 40,
        "experiment": raw, "production_modified": False, "policy_states": [
            {"case_id": case["id"], "state": build_features(prompt=case["prompt"], repo_path="",
                entry_point="session_start", role="engineer", brain="codex", prompt_egress="redacted")[0]}
            for case in cases]}
    atomic_write_json(artifact_root / "plan.json", plan)
    if not args.execute:
        print(json.dumps({"preview": str(artifact_root / "plan.json"), "provider_calls_made": 0,
            "policy_calls_made": 0, "planned_provider_turns": budget, "within_live_budget": budget <= 40}))
        return 0
    runtime_root = artifact_root / "runtime"
    runtime_root.mkdir(mode=0o700)
    (artifact_root / "codex-home").mkdir(mode=0o700)
    secret = b"synthetic-measurement-only"
    os.environ.update({"BOBI_ROOT": str(runtime_root), "BOBI_HOME": str(artifact_root / "bobi-home"),
        "CODEX_HOME": str(artifact_root / "codex-home"), "BOBI_BRAIN": "codex", "BOBI_GATEWAY": "1",
        "BOBI_GATEWAY_BASE_URL": values["LLM_GATEWAY_URL"], "BOBI_GATEWAY_WIRE_API": "responses",
        "BOBI_GATEWAY_API_KEY": values["BOBI_GATEWAY_API_KEY"],
        "BOBI_BRAIN_MODEL": config.control_model, "BOBI_BRAIN_EFFORT": values.get("LLM_EFFORT", "medium"),
        "BOBI_METRICS_EXPERIMENT_JSON": json.dumps(raw), "BOBI_METRICS_MODE": "enabled",
        "BOBI_METRICS_ASSIGNMENT_SECRET": secret.decode(), config.policy.credential_env: credential})
    os.environ.pop("OPENAI_API_KEY", None)
    paths.bind_root(runtime_root)
    runtime = MetricsRuntime(runtime_root, mode="enabled")
    rows = []
    report = {"experiment_id": config.experiment_id, "cohort": config.cohort, "mode": "enforce",
        "prompt_egress": "redacted", "quality_definition": "exact-bobi-json-v1", "rows": rows,
        "comparison": "Paired Bobi question-answer benchmark, not randomized production quality or cost savings"}
    try:
        with tempfile.TemporaryDirectory(prefix="bobi-jev-synthetic-") as cwd:
            for repeat in range(args.repeats):
                for case in cases:
                    for strategy_index, (strategy, model) in enumerate(strategies):
                        name = f'{case["id"]}-{repeat}-{strategy_index}'
                        run_key = f'synthetic-{case["id"]}-{repeat}'
                        routing_started = time.monotonic()
                        policy = {"status": "not_called_fixed_baseline"}
                        fallback = None
                        selected_model = model
                        if strategy == "jev":
                            subject = next(f"{name}-{index}" for index in range(1000) if
                                assign_variant(config, secret, assignment_unit="experiment_subject",
                                    assignment_key=f"{name}-{index}")[0].policy)
                            context = RoutingContext(name, "session_start", "codex", model, False,
                                case["prompt"], "engineer", True, run_key=run_key, experiment_subject=subject)
                            outcome = await resolve_route(context, runtime=runtime)
                            if outcome.decision is None:
                                raise RuntimeError("Isolated treatment routing was not admitted")
                            selected_model = outcome.model
                            fallback = outcome.decision.fallback_reason
                            policy = json.loads(outcome.decision.policy_metadata_json)
                        routing_wall_ms = (time.monotonic() - routing_started) * 1000
                        row = {"case_id": case["id"], "tier": case["tier"], "repeat": repeat, "strategy": strategy,
                            "requested_model": selected_model, "selected_model": selected_model,
                            "routing_wall_ms": round(routing_wall_ms, 2),
                            "policy_status": policy["status"], "policy_call_id": policy.get("call_id"),
                            "recommended_model": policy.get("recommended_model"),
                            "confidence": policy.get("confidence"), "policy_latency_ms": policy.get("latency_ms"),
                            "fallback_reason": fallback, "error_kind": None, "quality_passed": False}
                        turn = runtime.begin_turn(name, provider="gateway", brain="codex", role="engineer",
                            run_key=run_key, model_requested=selected_model,
                            prompt_bytes=len(case["prompt"].encode()), trigger_kind="synthetic")
                        row["turn_id"] = turn.turn_id
                        client = None
                        started = time.monotonic()
                        try:
                            async with asyncio.timeout(90):
                                client = get_brain("codex").make_session(cwd=cwd,
                                    system_prompt="Return only the requested JSON. Do not use tools or read files.",
                                    options={"model": selected_model, "mcp_servers": {}, "max_turns": 1})
                                await client.connect()
                                await client.query(case["prompt"])
                                text, result = "", None
                                async for message in client.receive_response():
                                    if isinstance(message, AssistantText):
                                        text += message.text or ""
                                        if len(text.encode()) > 65536:
                                            raise RuntimeError("Provider response exceeded the measurement limit")
                                    elif isinstance(message, TurnResult):
                                        result = message
                                if result is None:
                                    raise RuntimeError("Provider did not return a terminal turn")
                                turn.record_result(result)
                                row["error_kind"] = (result.error_kind or "provider_error") if result.is_error else None
                                if result.is_error:
                                    detail = result.error_text().replace(credential, "[redacted]")
                                    detail = detail.replace(values["BOBI_GATEWAY_API_KEY"], "[redacted]")
                                    row["error_detail"] = redact_secrets(detail)[0][:1000]
                                row["quality_passed"] = not result.is_error and quality_passed(text, case["expected"])
                                row["response"] = text[:65536]
                        except Exception as error:
                            row["error_kind"] = type(error).__name__
                        finally:
                            row["execution_wall_ms"] = round((time.monotonic() - started) * 1000, 2)
                            row["wall_ms"] = round((time.monotonic() - routing_started) * 1000, 2)
                            turn.finish(status="failed" if row["error_kind"] else "completed", error_kind=row["error_kind"])
                            if turn.router_decision_id:
                                record_quality_outcome(runtime, router_decision_id=turn.router_decision_id,
                                    outcome_name="synthetic_json_correct", outcome_definition_version="1",
                                    outcome_source="evaluator", evaluator_name="exact-json", evaluator_version="1",
                                    outcome_value=float(row["quality_passed"]), is_failure=not row["quality_passed"],
                                    idempotency_key=name, session_id=turn.session_id, turn_id=turn.turn_id)
                            if client is not None:
                                try:
                                    async with asyncio.timeout(10):
                                        await client.disconnect()
                                except Exception as error:
                                    row["disconnect_error_kind"] = type(error).__name__
                        rows.append(row)
                        atomic_write_json(artifact_root / "report.json", report)
                        print(json.dumps({key: row[key] for key in ("case_id", "strategy", "requested_model", "policy_status", "quality_passed", "error_kind")}))
    finally:
        closed = runtime.close(timeout=10)
        MetricsCollectorService(runtime_root).collect_once()
        report["producer_closed"] = closed
        database = runtime_root / "state/metrics/metrics.db"
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            for row in rows:
                usage = connection.execute("SELECT model,input_tokens,output_tokens,measurement_source,is_estimated "
                    "FROM best_usage WHERE turn_id=? AND scope='turn'", (row["turn_id"],)).fetchone()
                row.update(dict(usage) if usage else {"model": None, "input_tokens": None, "output_tokens": None,
                    "measurement_source": None, "is_estimated": None})
        report["summary"] = summarize(rows, group_by="strategy")
        atomic_write_json(artifact_root / "report.json", report)
        with (artifact_root / "measurements.csv").open("w", newline="") as stream:
            fields = ["case_id", "tier", "strategy", "requested_model", "model", "policy_status", "recommended_model",
                "confidence", "input_tokens", "output_tokens", "routing_wall_ms", "execution_wall_ms",
                "wall_ms", "quality_passed", "error_kind"]
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    print(json.dumps({"artifacts": str(artifact_root), "summary": report["summary"]}, indent=2))
    return int(not closed or any(row["error_kind"] or not row["quality_passed"] for row in rows))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--cases", type=Path, default=Path(__file__).resolve().parents[1] / "tests/fixtures/metrics/jev-bobi-question-cases.json")
    parser.add_argument("--models", nargs="+")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--execute", action="store_true", help="Allow paid model/policy calls and redacted synthetic task egress; otherwise preview offline")
    sys.exit(asyncio.run(measure(parser.parse_args())))
