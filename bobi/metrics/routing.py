"""Fresh-session routing coordinator; never called from turn observation."""

from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime
import json
import logging
import os
import re
import time

from bobi.brain.base import ERROR_KIND_AUTHENTICATION, ERROR_KIND_CREDITS_EXHAUSTED, ERROR_KIND_MAX_TURNS
from bobi.metrics.events import uuid7
from bobi.metrics.features import build_features
from bobi.metrics.policies.typesafe import TypeSafePolicy
from bobi.metrics.policy import PolicyRequest, call_policy, policy_breaker
from bobi.metrics.router import EXPERIMENT_CONFIG_ENV, RouterDecision, load_experiment
from bobi.metrics.runtime import MetricsRuntime, current_runtime, get_runtime
from bobi.sdk import load_session_brain, load_session_id, load_session_model, load_session_route, save_session_route

log = logging.getLogger(__name__)

FALLBACK_LOG_NAME = "jev_routing_fallback.log"

# Turn failures that are not the model's fault: the control model would fail the same way.
NO_MODEL_FALLBACK_KINDS = (ERROR_KIND_AUTHENTICATION, ERROR_KIND_CREDITS_EXHAUSTED, ERROR_KIND_MAX_TURNS)


def log_routing_incident(record: dict[str, object], root=None) -> None:
    """Write one routing incident to the agent log and its dedicated file.

    ``state/jev_routing_fallback.log`` holds one JSON object per line, so an
    operator can read every no-decision fallback, refused admission and failed
    routed model without searching the manager log. Fallbacks the policy step
    decided (``policy_*``, ``route_persistence_failed``) live on the decision row."""
    line = json.dumps(record, sort_keys=True, ensure_ascii=True)
    log.warning("metrics: %s", line, extra=dict(record))
    try:
        from bobi import paths

        logged_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        with open(paths.state_dir(root) / FALLBACK_LOG_NAME, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"logged_at": logged_at, **record}, sort_keys=True, ensure_ascii=True) + "\n")
    except OSError as exc:
        log.debug("metrics: routing fallback log unavailable (%s)", type(exc).__name__)


def may_rerun_on_control(decision: RouterDecision | None, *, error_kind: str, tools_ran: bool,
                         rebuilds_context: bool = False) -> bool:
    """Whether a failed first turn may run again on the control model.

    Only when nothing happened that a rerun would repeat or could not fix: no
    tool ran (a rerun would repeat its side effects, a push or a post), the
    error is not one the control model shares (authentication, credits, turn
    cap), and the decision was made fresh for this session; a route reused
    from a saved session belongs to a transcript a fresh session would lose.
    ``rebuilds_context`` lifts that last condition for a caller whose rerun
    resends everything the saved transcript was given (a workflow's
    background block) and that passes the decision only until its route's
    first turn has run, whichever process runs it."""
    if decision is None or tools_ran or error_kind in NO_MODEL_FALLBACK_KINDS:
        return False
    if rebuilds_context:
        return True
    try:
        metadata = json.loads(decision.policy_metadata_json or "{}")
    except (TypeError, ValueError):
        metadata = {}
    return metadata.get("status") != "reused"


def candidate_failed(decision: RouterDecision | None, *, session_name: str, brain: str,
                     model: str, error: str, root=None, runtime: MetricsRuntime | None = None) -> str | None:
    """The control model to retry on when a policy-chosen model failed, else None.

    Only a session the policy moved off its control model qualifies; the
    incident (attempted model, brain, provider, error, fallback) is logged, and
    the session's later turns record the fallback instead of the failed pick."""
    if decision is None or not decision.control_model or not model or model == decision.control_model:
        return None
    from bobi.costs import model_provider
    from bobi.redact import redact_secrets

    log_routing_incident({
        "event": "jev_candidate_failed", "session": session_name, "attempted_model": model,
        "brain": brain, "provider": model_provider(model) or "unknown",
        "error": redact_secrets(str(error or "unknown error"))[0][:500],
        "fallback_model": decision.control_model,
    }, root)
    runtime = runtime or current_runtime()
    if runtime is not None:
        runtime.reroute(session_name, rerouted_to_control(decision))
    if decision.entry_point != "workflow_start":
        # The session now runs the control model; its saved route must say so,
        # or the next resume finds a stale route. A workflow keeps its route in
        # its own checkpoint.
        from bobi.brain import session_brain_label

        try:
            save_session_route(session_name, {"brain": session_brain_label(),
                "decision": asdict(rerouted_to_control(decision))}, root=root)
        except (OSError, ValueError, TypeError) as exc:
            log.warning("metrics: corrected session route not saved (%s)", type(exc).__name__)
    return decision.control_model


def rerouted_to_control(decision: RouterDecision) -> RouterDecision:
    """*decision* after its model failed and the control model took over."""
    return replace(decision, model_selected=decision.control_model,
                   router_reason="policy_fallback", fallback_reason="candidate_failed")


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
    stage = "runtime"

    def recorded_fallback(reason: str, error_kind: str = "", model: str | None = None) -> RouteOutcome:
        """Run *model* (default: the configured one) and record why routing did not decide."""
        log_routing_incident({"event": "jev_routing_fallback", "session": ctx.session_name,
                              "fallback_reason": reason, "error_kind": error_kind},
                             runtime.root if runtime is not None else None)
        if runtime is not None:
            try:
                runtime.admit_route(ctx.session_name, None, brain=ctx.brain,
                    role=ctx.role, run_key=ctx.run_key, workflow_name=ctx.workflow_name,
                    fallback_reason=reason, error_kind=error_kind)
            except Exception as recording_error:
                log.warning("metrics: routing fallback recording failed (%s)", type(recording_error).__name__)
        return RouteOutcome(ctx.configured_model if model is None else model, reason=reason)

    def admission_rejected(model: str) -> RouteOutcome:
        # The store refused the session record (turns already ran or the spool is
        # full), so the reason can only go to the log.
        log_routing_incident({"event": "jev_routing_fallback", "session": ctx.session_name,
                              "fallback_reason": "admission_rejected", "error_kind": ""},
                             runtime.root if runtime is not None else None)
        return RouteOutcome(model, reason="admission_rejected")

    try:
        runtime = runtime or get_runtime()
        if ctx.explicit_model:
            return fallback
        if not runtime.enabled:
            if os.environ.get(EXPERIMENT_CONFIG_ENV):
                stage = "metrics"
                raise RuntimeError("routing requires available telemetry")
            return fallback
        stage = "config"
        config = load_experiment()
        if config is None:
            from bobi.config import _UNRESTORED_PROVIDER_ENV

            if EXPERIMENT_CONFIG_ENV in _UNRESTORED_PROVIDER_ENV:
                # A provider launch withheld the parent's config and this runtime's .env has none.
                return recorded_fallback("routing_config_unrestored")
            if not ctx.fresh and _saved_record(ctx, runtime, sticky_record):
                # Routing was turned off under a routed session: it resumes on its configured model.
                return recorded_fallback("routing_disabled_resumed")
            return fallback
        policy_config = config.policy
        fingerprint = config.fingerprint()
        if policy_config.brain not in ("auto", ctx.brain):
            # A policy pinned to another brain can never route this agent; say so.
            return recorded_fallback("routing_brain_mismatch")
        saved_id = load_session_id(ctx.session_name, root=runtime.root)
        # A workflow resumes only its checkpoint record: its shared provider
        # session may last have run another agent's step.
        resuming = not ctx.fresh and bool(
            sticky_record if ctx.entry_point == "workflow_start" else saved_id or sticky_record)
        if ctx.entry_point not in policy_config.entry_points or ctx.role not in policy_config.roles:
            from bobi.brain import session_brain_label

            record = _saved_record(ctx, runtime, sticky_record) if resuming else None
            if record and record.get("brain") == session_brain_label():
                # A routed session the policy no longer covers runs the control model.
                return recorded_fallback("routing_out_of_scope", model=config.control_model)
            return fallback
        if resuming:
            stage = "sticky"
            if ctx.entry_point == "workflow_start" and "decision" not in sticky_record:
                # The run started on a fallback without a decision (for example
                # a refused admission on the control model): it resumes on it.
                return recorded_fallback(str(sticky_record.get("reason") or "routing_sticky_stale"),
                                         model=_resumed_model(ctx, runtime, saved_id, sticky_record))
            decision = _saved_decision(ctx, runtime, sticky_record, saved_id, fingerprint,
                                       policy_config.candidate_models)
            if decision == _MODEL_REMOVED:
                # The routed model was taken out of the policy; never keep running it.
                return recorded_fallback("routing_sticky_model_removed", model=config.control_model)
            if decision is None:
                # Never switch a resumed session's model: keep the one its transcript ran.
                return recorded_fallback("routing_sticky_stale",
                                         model=_resumed_model(ctx, runtime, saved_id, sticky_record))
            if runtime.admit_route(ctx.session_name, decision, brain=ctx.brain,
                    role=ctx.role, run_key=ctx.run_key, workflow_name=ctx.workflow_name):
                return RouteOutcome(decision.model_selected, decision)
            return admission_rejected(decision.model_selected)
        if not ctx.prompt.strip() or (not ctx.fresh and ctx.existing_transcript):
            return fallback
        stage = "policy"
        policy = TypeSafePolicy(policy_config)
        stage = "routing"
        started = time.monotonic()
        model = config.control_model
        metadata: dict[str, object] = {"name": policy_config.name, "version": policy_config.version,
                                       "mode": policy_config.mode, "egress": policy_config.prompt_egress}
        secrets: tuple[str, ...] = ()
        if policy_config.prompt_egress != "none":
            from bobi.config import project_env
            from bobi.redact import env_secret_values

            # The runtime's own credentials never leave in the task text, whatever their shape.
            secrets = env_secret_values(project_env(runtime.root))
        features, redactions = build_features(
            prompt=ctx.prompt, repo_path=ctx.repo_path, entry_point=ctx.entry_point,
            role=ctx.role, brain=ctx.brain, phase=ctx.phase,
            workflow_name=ctx.workflow_name, step_name=ctx.step_name,
            prompt_egress=policy_config.prompt_egress,
            max_prompt_bytes=policy_config.max_prompt_bytes, secrets=secrets,
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
            if policy_config.mode == "enforce" and not failure:
                model = result.model
        decision = RouterDecision(
            config.experiment_id, config.cohort, config.router_name, config.router_version,
            config.policy_version, config.feature_schema_version, policy_config.candidate_models,
            model, config.control_model, reason, (time.monotonic() - started) * 1000,
            failure, time.time_ns() // 1000, fingerprint,
            ctx.entry_point, json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        )
        if not runtime.admit_route(ctx.session_name, decision, brain=ctx.brain,
                role=ctx.role, run_key=ctx.run_key, workflow_name=ctx.workflow_name):
            return admission_rejected(config.control_model)
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
        reason = {"config": "routing_config_invalid", "policy": "routing_policy_unavailable",
                  "metrics": "routing_metrics_unavailable", "sticky": "routing_sticky_invalid",
                  }.get(stage, "routing_unavailable")
        resumed = None
        if stage == "sticky":
            try:
                resumed = _resumed_model(ctx, runtime, saved_id, sticky_record)
            except Exception:
                pass
        return recorded_fallback(reason, type(exc).__name__, model=resumed)


_DECISION_FIELDS = frozenset(field.name for field in fields(RouterDecision))


def _resumed_model(ctx: RoutingContext, runtime: MetricsRuntime, saved_id: str,
                   sticky_record: dict[str, object] | None) -> str | None:
    """The model the resumed session ran, when this brain can resume it; else None.

    For a workflow that is the model its checkpoint record selected (or the
    fallback model it recorded without a decision), which is what the route
    agent's steps ran; the run's shared provider session may last have run
    another agent's step or a step-level model."""
    from bobi.brain import session_brain_label

    if ctx.entry_point == "workflow_start":
        record = sticky_record or {}
        raw = record.get("decision")
        model = raw.get("model_selected") if isinstance(raw, dict) else record.get("model")
        return model if record.get("brain") == session_brain_label() and isinstance(model, str) and model else None
    if not saved_id or load_session_brain(ctx.session_name, root=runtime.root) != session_brain_label():
        return None
    return load_session_model(ctx.session_name, root=runtime.root)


# _saved_decision's answer when the saved route picked a model the policy dropped.
_MODEL_REMOVED = "model_removed"


def _saved_record(ctx: RoutingContext, runtime: MetricsRuntime,
                  sticky_record: dict[str, object] | None) -> dict[str, object] | None:
    """The saved route record; a workflow uses only its own checkpoint record."""
    if ctx.entry_point == "workflow_start":
        return sticky_record
    return sticky_record or load_session_route(ctx.session_name, root=runtime.root)


def _saved_decision(ctx: RoutingContext, runtime: MetricsRuntime, sticky_record: dict[str, object] | None,
                    saved_id: str, fingerprint: str,
                    candidates: tuple[str, ...]) -> RouterDecision | str | None:
    """The saved route to reuse, :data:`_MODEL_REMOVED`, or None when it is stale.

    A route is reused only when it was saved under the same brain and the same
    config fingerprint, its model is still a candidate, and a saved provider
    session ran that model. A workflow checkpoint is the record of its own
    route: its shared session may last have run another agent's step."""
    from bobi.brain import session_brain_label

    provenance = session_brain_label()
    record = _saved_record(ctx, runtime, sticky_record)
    if not record or record.get("brain") != provenance:
        return None
    raw = record.get("decision")
    if isinstance(raw, dict) and raw.get("model_selected") not in (*candidates, None):
        return _MODEL_REMOVED
    workflow = ctx.entry_point == "workflow_start"
    if saved_id and not workflow and load_session_brain(ctx.session_name, root=runtime.root) != provenance:
        return None
    if not isinstance(raw, dict) or raw.get("config_fingerprint") != fingerprint:
        return None
    decision = RouterDecision(**{**{name: value for name, value in raw.items() if name in _DECISION_FIELDS},
                                 "candidate_models": tuple(raw["candidate_models"])})
    if (saved_id and not workflow
            and load_session_model(ctx.session_name, root=runtime.root) != decision.model_selected):
        return None
    metadata = json.loads(decision.policy_metadata_json)
    if not isinstance(metadata, dict):
        return None
    metadata["status"] = "reused"
    return replace(decision, policy_metadata_json=json.dumps(metadata, sort_keys=True, separators=(",", ":"),
                                                            allow_nan=False))
