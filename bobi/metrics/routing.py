"""Fresh-session routing coordinator; never called from turn observation."""

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import logging
import math
import os
import re
import time

from bobi.metrics.events import uuid7
from bobi.metrics.features import build_features, prepare_task
from bobi.metrics.policy import PolicyRequest, call_policy, load_policy, policy_breaker
from bobi.metrics.router import (
    ASSIGNMENT_ALGORITHM, RouterDecision, assign_variant, choose_assignment_key,
    load_experiment, public_config,
)
from bobi.metrics.runtime import MetricsRuntime, get_runtime
from bobi.sdk import load_session_brain, load_session_id, load_session_model, load_session_route, save_session_route

log = logging.getLogger(__name__)

@dataclass(frozen=True)
class RoutingContext:
    session_name: str
    entry_point: str
    brain: str
    configured_model: str
    explicit_model: bool
    prompt: str
    role: str
    fresh: bool
    repo_path: str = ""
    run_key: str = ""
    experiment_subject: str = ""
    phase: str = ""
    workflow_name: str = ""
    step_name: str = ""
    existing_transcript: bool = False

@dataclass(frozen=True)
class RouteOutcome:
    model: str
    decision: RouterDecision | None = None
    reason: str | None = None

async def resolve_route(ctx: RoutingContext, *, runtime: MetricsRuntime | None = None,
                        sticky_record: dict[str, object] | None = None) -> RouteOutcome:
    fallback = RouteOutcome(ctx.configured_model)
    try:
        runtime = runtime or get_runtime()
        if not runtime.enabled or ctx.explicit_model:
            return fallback
        configured = load_experiment()
        if configured is None:
            return fallback
        config, secret = configured
        fingerprint = hashlib.sha256(json.dumps(public_config(config), sort_keys=True,
                                                separators=(",", ":")).encode()).hexdigest()
        policy_config = config.policy
        if policy_config and (ctx.brain != policy_config.brain
                or ctx.entry_point not in policy_config.entry_points or ctx.role not in policy_config.roles):
            return fallback
        saved_id = load_session_id(ctx.session_name, root=runtime.root)
        if not ctx.fresh and (saved_id or sticky_record):
            from bobi.brain import session_brain_label

            provenance = session_brain_label()
            record = sticky_record if ctx.entry_point == "workflow_start" else (
                sticky_record or load_session_route(ctx.session_name, root=runtime.root))
            if (record and record.get("brain") == provenance
                    and (not saved_id or load_session_brain(ctx.session_name, root=runtime.root) == provenance)):
                raw = record.get("decision")
                if isinstance(raw, dict) and raw.get("experiment_id") == config.experiment_id and raw.get("config_fingerprint") == fingerprint:
                    raw = dict(raw)
                    raw["candidate_models"] = tuple(raw["candidate_models"])
                    raw["expected_weights"] = tuple(tuple(item) for item in raw["expected_weights"])
                    decision = RouterDecision(**raw)
                    if saved_id and load_session_model(ctx.session_name, root=runtime.root) != decision.model_selected:
                        return fallback
                    if (decision.assignment_unit not in {"experiment_subject", "run_key", "session_id"}
                            or not isinstance(decision.assignment_key_hash, str)
                            or not re.fullmatch(r"[0-9a-f]{64}", decision.assignment_key_hash)):
                        return fallback
                    if (type(decision.decided_at_us) is not int or decision.decided_at_us <= 0
                            or isinstance(decision.router_latency_ms, bool)
                            or not isinstance(decision.router_latency_ms, (int, float))
                            or not math.isfinite(decision.router_latency_ms)
                            or decision.router_latency_ms < 0):
                        return fallback
                    models = set(policy_config.candidate_models) if policy_config else {item.model for item in config.variants}
                    if decision.model_selected not in models or decision.variant_id not in {item.variant_id for item in config.variants}:
                        return fallback
                    variant = next(item for item in config.variants if item.variant_id == decision.variant_id)
                    expected_candidates = policy_config.candidate_models if variant.policy and policy_config else tuple(
                        dict.fromkeys(item.model for item in config.variants if item.model))
                    if (decision.assignment_status != "assigned"
                            or decision.candidate_models != expected_candidates
                            or decision.router_reason not in {"control_arm", "fixed_arm", "policy_fallback", "policy_shadow", "policy_selected"}
                            or decision.fallback_reason not in {None, "policy_timeout", "policy_unavailable",
                                "policy_circuit_open", "policy_invalid_response", "policy_version_drift",
                                "policy_low_confidence", "route_persistence_failed"}
                            or decision.assignment_algorithm != ASSIGNMENT_ALGORITHM
                            or decision.router_name != config.router_name
                            or decision.router_version != config.router_version
                            or decision.policy_version != config.policy_version
                            or decision.feature_schema_version != config.feature_schema_version
                            or decision.cohort != config.cohort
                            or decision.expected_weight != variant.weight
                            or decision.expected_weights != tuple((item.variant_id, item.weight) for item in config.variants)
                            or decision.control_model != config.control_model
                            or (variant.model and decision.model_selected != variant.model)
                            or (variant.policy and policy_config and policy_config.mode == "shadow"
                                and decision.model_selected != config.control_model)
                            or (decision.fallback_reason and decision.model_selected != config.control_model)):
                        return fallback
                    metadata = json.loads(decision.policy_metadata_json)
                    if not isinstance(metadata, dict):
                        return fallback
                    if policy_config and any(metadata.get(name) != expected for name, expected in {
                        "name": policy_config.name, "version": policy_config.version,
                        "mode": policy_config.mode, "egress": policy_config.prompt_egress,
                    }.items()):
                        return fallback
                    recommendation = metadata.get("recommended_model")
                    if recommendation is not None and recommendation not in models:
                        return fallback
                    tier = metadata.get("tier")
                    if tier is not None and (not isinstance(tier, str)
                            or not re.fullmatch(r"[a-z0-9_]{1,64}", tier)):
                        return fallback
                    if metadata.get("breaker_state") not in {None, "closed", "open", "half-open"}:
                        return fallback
                    redactions = metadata.get("redactions")
                    if redactions is not None and (type(redactions) is not int or redactions < 0):
                        return fallback
                    call_id = metadata.get("call_id")
                    if call_id is not None and (not isinstance(call_id, str)
                            or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", call_id)):
                        return fallback
                    for name in ("confidence", "cost_usd", "latency_ms"):
                        value = metadata.get(name)
                        if value is not None and (isinstance(value, bool)
                                or not isinstance(value, (int, float))
                                or not math.isfinite(value) or value < 0
                                or (name == "confidence" and value > 1)):
                            return fallback
                    metadata = {name: value for name, value in metadata.items() if name in {
                        "status", "name", "version", "mode", "egress", "call_id",
                        "latency_ms", "breaker_state", "redactions", "recommended_model",
                        "confidence", "cost_usd", "tier", "reason_text",
                    }}
                    reason_text = metadata.pop("reason_text", None)
                    if policy_config and policy_config.store_reason_text and isinstance(reason_text, str):
                        metadata["reason_text"], _ = prepare_task(
                            reason_text, repo_path=ctx.repo_path, max_bytes=1024,
                        )
                    metadata["status"] = "reused"
                    decision = replace(decision, policy_metadata_json=json.dumps(metadata, sort_keys=True, separators=(",", ":"), allow_nan=False))
                    if runtime.admit_route(ctx.session_name, decision, brain=ctx.brain,
                            role=ctx.role, run_key=ctx.run_key, workflow_name=ctx.workflow_name):
                        return RouteOutcome(decision.model_selected, decision)
                    return RouteOutcome(decision.model_selected, reason="admission_rejected")
            return fallback
        if not ctx.prompt.strip() or (not ctx.fresh and ctx.existing_transcript):
            return fallback
        policy = load_policy(policy_config) if policy_config else None
        unit, key = choose_assignment_key(
            experiment_subject=ctx.experiment_subject or os.environ.get("BOBI_METRICS_EXPERIMENT_SUBJECT", ""),
            run_key=ctx.run_key, session_id=ctx.session_name,
        )
        if unit is None or key is None:
            return fallback
        started = time.monotonic()
        variant, bucket, key_hash = assign_variant(config, secret, assignment_unit=unit, assignment_key=key)
        model = variant.model or config.control_model
        reason = "control_arm" if variant.model == config.control_model else "fixed_arm"
        failure = None
        metadata: dict[str, object] = {"status": "not_called"}
        if policy_config:
            metadata.update(name=policy_config.name, version=policy_config.version,
                            mode=policy_config.mode, egress=policy_config.prompt_egress)
        if variant.policy and policy_config and policy:
            features, redactions = build_features(
                prompt=ctx.prompt, repo_path=ctx.repo_path, entry_point=ctx.entry_point,
                role=ctx.role, brain=ctx.brain, phase=ctx.phase,
                workflow_name=ctx.workflow_name, step_name=ctx.step_name,
                prompt_egress=policy_config.prompt_egress,
                max_prompt_bytes=policy_config.max_prompt_bytes,
            )
            request = PolicyRequest(config.feature_schema_version, features,
                policy_config.candidate_models, config.control_model, policy_config.version)
            call_started = time.monotonic()
            metadata["call_id"] = None
            result, failure = await call_policy(policy, request, policy_config,
                on_call=lambda: metadata.update(call_id=str(uuid7())))
            metadata.update(status="skipped" if metadata["call_id"] is None else "failed" if failure else "decided",
                latency_ms=(time.monotonic() - call_started) * 1000,
                breaker_state=policy_breaker(policy_config).health()["state"], redactions=redactions)
            reason = "policy_fallback" if failure else "policy_shadow" if policy_config.mode == "shadow" else "policy_selected"
            if result and failure in {None, "policy_low_confidence", "policy_version_drift"}:
                metadata.update(recommended_model=result.model, confidence=result.confidence, cost_usd=result.cost_usd)
                if result.tier and re.fullmatch(r"[a-z0-9_]{1,64}", result.tier):
                    metadata["tier"] = result.tier
                if policy_config.store_reason_text and isinstance(result.reason_text, str):
                    metadata["reason_text"], _ = prepare_task(
                        result.reason_text, repo_path=ctx.repo_path, max_bytes=1024,
                    )
                if policy_config.mode == "enforce" and not failure:
                    model = result.model
        decision = RouterDecision(
            config.experiment_id, variant.variant_id, unit, "assigned", key_hash,
            ASSIGNMENT_ALGORITHM, config.cohort, config.router_name, config.router_version,
            config.policy_version, config.feature_schema_version,
            policy_config.candidate_models if variant.policy and policy_config else tuple(
                dict.fromkeys(item.model for item in config.variants if item.model)),
            model, config.control_model, bucket, reason, (time.monotonic() - started) * 1000,
            failure, time.time_ns() // 1000, variant.weight,
            tuple((item.variant_id, item.weight) for item in config.variants),
            fingerprint,
            ctx.entry_point, json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        )
        if not runtime.admit_route(ctx.session_name, decision, brain=ctx.brain,
                role=ctx.role, run_key=ctx.run_key, workflow_name=ctx.workflow_name):
            return RouteOutcome(config.control_model, reason="admission_rejected")
        if ctx.entry_point == "workflow_start":
            return RouteOutcome(model, decision)
        from bobi.brain import session_brain_label

        try:
            save_session_route(ctx.session_name, {"brain": session_brain_label(),
                "decision": asdict(decision)}, root=runtime.root)
        except (OSError, ValueError, TypeError):
            control = replace(decision, model_selected=config.control_model,
                              fallback_reason="route_persistence_failed", router_reason="policy_fallback")
            if not runtime.admit_route(ctx.session_name, control, brain=ctx.brain,
                    role=ctx.role, run_key=ctx.run_key, workflow_name=ctx.workflow_name):
                runtime.discard_route(ctx.session_name)
            return RouteOutcome(config.control_model, reason="route_persistence_failed")
        return RouteOutcome(model, decision)
    except Exception as exc:
        log.warning("metrics: routing unavailable (%s)", type(exc).__name__)
        return fallback
