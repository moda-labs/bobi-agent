"""Fail-safe runtime wiring for metadata-only fine-grained metrics."""

from __future__ import annotations

import atexit
import hashlib
import itertools
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from bobi.metrics.events import MetricsEvent, uuid7
from bobi.metrics.producer import MetricsProducer
from bobi.metrics.router import RouterDecision, load_experiment, route

if TYPE_CHECKING:
    from bobi.brain.base import BrainInvocation, BrainToolExecution, BrainUsage, TurnResult

log = logging.getLogger(__name__)

METRICS_MODE_ENV = "BOBI_METRICS_MODE"
DISABLED = "disabled"
ENABLED_MODES = frozenset({"shadow", "full"})


def resolve_mode(env: Mapping[str, str] | None = None) -> str:
    value = str((env or os.environ).get(METRICS_MODE_ENV, DISABLED)).strip().lower()
    if value in {"", "0", "off", "false", "disabled"}:
        return DISABLED
    if value in ENABLED_MODES:
        return value
    log.warning("metrics: unsupported mode %r; telemetry disabled", value)
    return DISABLED


def _stable_id(prefix: str, *parts: object) -> str:
    material = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()}"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _brain_name(provider: str) -> str:
    return {"anthropic": "claude", "openai": "codex"}.get(provider, provider or "unknown")


class MetricsRuntime:
    """One process-local producer; every public operation fails closed to no-op."""

    def __init__(
        self,
        root: Path | str,
        *,
        mode: str | None = None,
        capacity: int = 8192,
        producer_factory=MetricsProducer,
    ) -> None:
        self.root = Path(root).resolve()
        self.mode = resolve_mode() if mode is None else mode
        self.pid = os.getpid()
        self.producer_id = f"producer-{self.pid}-{uuid7()}"
        self._sequence = itertools.count()
        self._turn_indexes: dict[str, int] = {}
        self._session_starts: dict[str, int] = {}
        self._emitted_sessions: set[str] = set()
        self._provider_sessions: dict[str, str] = {}
        self._fault_injection_enabled = False
        self._producer: MetricsProducer | None = None
        self._init_error = ""
        self.events_emitted = 0
        self._experiment = None

        try:
            from bobi.metrics.faults import (
                fault_injection_enabled,
                validate_fault_injection,
            )

            validate_fault_injection(self.root)
            self._fault_injection_enabled = fault_injection_enabled(self.root)
        except Exception as exc:
            self.mode = DISABLED
            self._init_error = type(exc).__name__
            log.warning("metrics: fault injection refused: %s", exc)
            return

        if self.mode not in ENABLED_MODES:
            self.mode = DISABLED
            return

        try:
            self._experiment = load_experiment()
        except Exception as exc:
            # Bad experiment configuration disables routing, never the turn or
            # the metrics producer.  The warning intentionally contains no
            # configuration value because it may include deployment metadata.
            log.warning("metrics: experiment routing disabled: %s", exc)

        metrics_root = self.root / "state" / "metrics"
        segment = (
            metrics_root
            / "spool"
            / self.producer_id
            / f"{time.time_ns()}-{uuid7()}.telemetry"
        )
        health_path = segment.parent / "health.json"
        try:
            self._producer = producer_factory(
                segment,
                capacity=capacity,
                health_path=health_path,
                producer_id=self.producer_id,
            )
            self._producer.start()
        except Exception as exc:
            self._producer = None
            self._init_error = type(exc).__name__
            log.debug("metrics: producer startup failed", exc_info=True)

    @property
    def enabled(self) -> bool:
        return self._producer is not None

    def session_id(self, session_name: str) -> str:
        return _stable_id("ses", self.producer_id, session_name)

    def resolve_model(
        self,
        requested_model: str,
        *,
        experiment_subject: str = "",
        run_key: str = "",
        session_name: str = "",
    ) -> str:
        """Resolve a treatment only when its decision can enter the spool."""
        if not self.enabled or self._experiment is None:
            return requested_model
        try:
            decision = route(
                self._experiment,
                requested_model="",
                experiment_subject=(
                    experiment_subject
                    or os.environ.get("BOBI_METRICS_EXPERIMENT_SUBJECT", "")
                ),
                run_key=run_key,
                session_id=session_name,
            )
        except Exception:
            log.debug("metrics: model routing failed open", exc_info=True)
            return requested_model
        if decision is None or decision.assignment_status != "assigned":
            return requested_model
        return decision.model_selected or requested_model

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        invocation_id: str | None = None,
        source: str = "runtime",
    ) -> bool:
        producer = self._producer
        if producer is None:
            return False
        try:
            event = MetricsEvent(
                event_type=event_type,
                producer_id=self.producer_id,
                producer_sequence=next(self._sequence),
                source=source,
                payload=payload,
                session_id=session_id,
                turn_id=turn_id,
                invocation_id=invocation_id,
                _validate_payload=False,
            )
            accepted = producer.try_emit(event)
            if accepted:
                self.events_emitted += 1
            return accepted
        except Exception:
            log.debug("metrics: event emission failed", exc_info=True)
            return False

    def close(self, *, timeout: float = 2.0) -> bool:
        producer = self._producer
        self._producer = None
        if producer is None:
            return True
        try:
            return producer.close(timeout=timeout)
        except Exception:
            log.debug("metrics: producer shutdown failed", exc_info=True)
            return False

    def health(self) -> dict[str, object]:
        producer = self._producer
        health: dict[str, object] = {
            "mode": self.mode,
            "enabled": producer is not None,
            "producer_id": self.producer_id,
            "events_emitted": self.events_emitted,
            "init_error": self._init_error or None,
        }
        if producer is not None:
            health.update(producer.health())
        return health

    def begin_turn(
        self,
        session_name: str,
        *,
        provider: str,
        brain: str = "",
        role: str = "",
        run_key: str = "",
        project: str = "",
        workflow_name: str = "",
        workflow_step_id: str | None = None,
        trigger_kind: str = "agent",
        trigger_id: str = "",
        is_user_initiated: bool = False,
        model_requested: str = "",
        prompt_bytes: int | None = None,
        experiment_subject: str = "",
        started_at_us: int | None = None,
    ) -> "TurnObservation":
        started = started_at_us or time.time_ns() // 1000
        session_id = self.session_id(session_name)
        self._session_starts.setdefault(session_id, started)
        turn_index = self._turn_indexes.get(session_id, 0) + 1
        self._turn_indexes[session_id] = turn_index
        observation = TurnObservation(
            runtime=self,
            session_id=session_id,
            session_name=session_name,
            turn_id=f"turn_{uuid7()}",
            turn_index=turn_index,
            provider=provider or "unknown",
            brain=brain or _brain_name(provider),
            role=role,
            run_key=run_key,
            project=project,
            workflow_name=workflow_name,
            workflow_step_id=workflow_step_id,
            trigger_kind=trigger_kind,
            trigger_id=trigger_id,
            is_user_initiated=is_user_initiated,
            model_requested=model_requested,
            prompt_bytes=prompt_bytes,
            experiment_subject=(
                experiment_subject
                or os.environ.get("BOBI_METRICS_EXPERIMENT_SUBJECT", "")
            ),
            started_at_us=started,
        )
        observation.start()
        return observation

    def begin_workflow_step(
        self,
        session_name: str,
        *,
        workflow_name: str,
        step_name: str,
        step_index: int | None,
        attempt: int,
        step_type: str,
        run_key: str = "",
        provider: str = "unknown",
        brain: str = "unknown",
        role: str = "",
        turn_id: str | None = None,
    ) -> "WorkflowStepObservation":
        started = time.time_ns() // 1000
        session_id = self.session_id(session_name)
        self._session_starts.setdefault(session_id, started)
        return WorkflowStepObservation(
            runtime=self,
            workflow_step_id=f"step_{uuid7()}",
            session_id=session_id,
            session_name=session_name,
            workflow_name=workflow_name,
            step_name=step_name,
            step_index=step_index,
            attempt=attempt,
            step_type=step_type,
            run_key=run_key,
            provider=provider,
            brain=brain,
            role=role,
            turn_id=turn_id,
            started_at_us=started,
        )


@dataclass
class TurnObservation:
    runtime: MetricsRuntime
    session_id: str
    session_name: str
    turn_id: str
    turn_index: int
    provider: str
    brain: str
    role: str
    run_key: str
    project: str
    workflow_name: str
    workflow_step_id: str | None
    trigger_kind: str
    trigger_id: str
    is_user_initiated: bool
    model_requested: str
    prompt_bytes: int | None
    experiment_subject: str
    started_at_us: int
    first_output_at_us: int | None = None
    results: list["TurnResult"] = field(default_factory=list)
    router_decision_id: str | None = None
    router_decision: RouterDecision | None = None
    _finished: bool = False

    def start(self) -> None:
        if self.session_id not in self.runtime._emitted_sessions:
            _emit_session(
                self,
                self.runtime._provider_sessions.get(self.session_id, ""),
            )
        _emit_turn_row(self, status="running", ended_at_us=None, error_kind="")
        self.router_decision = route(
            self.runtime._experiment,
            requested_model=self.model_requested,
            experiment_subject=self.experiment_subject,
            run_key=self.run_key,
            # The Bobi session name is stable across process restarts; the
            # telemetry session UUID intentionally is not.
            session_id=self.session_name,
        )
        if self.router_decision is not None:
            self.router_decision_id = _stable_id(
                "route", self.turn_id, self.router_decision.experiment_id
            )
            _emit_router_decision(self)

    def mark_first_output(self) -> None:
        if self.first_output_at_us is None:
            self.first_output_at_us = time.time_ns() // 1000

    def record_result(self, result: "TurnResult") -> None:
        if not self._finished:
            self.results.append(result)

    def finish(self, *, status: str, error_kind: str = "") -> None:
        if self._finished:
            return
        self._finished = True
        try:
            _emit_turn(self, status=status, error_kind=error_kind)
        except Exception:
            log.debug("metrics: turn observation failed", exc_info=True)


@dataclass
class WorkflowStepObservation:
    runtime: MetricsRuntime
    workflow_step_id: str
    session_id: str
    session_name: str
    workflow_name: str
    step_name: str
    step_index: int | None
    attempt: int
    step_type: str
    run_key: str
    provider: str
    brain: str
    role: str
    turn_id: str | None
    started_at_us: int
    _finished: bool = False

    def __post_init__(self) -> None:
        self.runtime.emit(
            "session.recorded",
            {
                "session_id": self.session_id,
                "session_name": self.session_name,
                "provider_session_id": None,
                "brain": self.brain,
                "provider": self.provider,
                "role": self.role or None,
                "run_key": self.run_key or None,
                "project": None,
                "workflow_name": self.workflow_name,
                "started_at_us": self.runtime._session_starts[self.session_id],
                "ended_at_us": None,
                "status": "running",
                "terminal_error_kind": None,
                "parent_session_id": None,
                "metadata_json": "{}",
            },
            session_id=self.session_id,
        )

    def finish(self, *, status: str, error_kind: str = "") -> None:
        if self._finished:
            return
        self._finished = True
        ended = time.time_ns() // 1000
        self.runtime.emit(
            "workflow_step.recorded",
            {
                "workflow_step_id": self.workflow_step_id,
                "session_id": self.session_id,
                "turn_id": self.turn_id,
                "run_key": self.run_key or None,
                "workflow_name": self.workflow_name,
                "step_name": self.step_name,
                "step_index": self.step_index,
                "attempt": self.attempt,
                "step_type": self.step_type,
                "started_at_us": self.started_at_us,
                "ended_at_us": ended,
                "status": status,
                "error_kind": error_kind or None,
            },
            session_id=self.session_id,
            turn_id=self.turn_id,
        )


def _sum_reported(items: list[int | None]) -> int | None:
    """Sum a dimension only when every contributing invocation reports it."""
    if not items or any(item is None for item in items):
        return None
    return sum(items)


def _aggregate_usage(items: list["BrainUsage"]) -> dict[str, object]:
    fields = (
        "input_tokens",
        "uncached_input_tokens",
        "cache_read_input_tokens",
        "cache_write_input_tokens",
        "cache_write_5m_input_tokens",
        "cache_write_1h_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    )
    payload = {
        name: _sum_reported([getattr(item, name) for item in items])
        for name in fields
    }
    incomplete = [item for item in items if not item.cache_write_breakdown_complete]
    payload["cache_write_unknown_ttl_input_tokens"] = _sum_reported([
        item.cache_write_unknown_ttl_input_tokens for item in incomplete
    ])
    payload["cache_write_breakdown_complete"] = int(bool(items) and not incomplete)
    payload["token_semantics_version"] = max(
        (item.token_semantics_version for item in items), default=1
    )
    payload["raw_usage_json"] = _json([item.raw_usage for item in items])
    return payload


def _usage_payload(
    usage: "BrainUsage",
    *,
    measurement_id: str,
    scope: str,
    turn_id: str,
    invocation_id: str | None,
    provider: str,
    observed_at_us: int,
) -> dict[str, object]:
    return {
        "measurement_id": measurement_id,
        "scope": scope,
        "turn_id": turn_id,
        "invocation_id": invocation_id,
        "provider": provider,
        "model": usage.model or "unknown",
        "provider_event_id": usage.provider_event_id or None,
        "measurement_source": "provider_stream",
        "is_estimated": 0,
        "estimator_name": None,
        "estimator_version": None,
        "token_semantics_version": usage.token_semantics_version,
        "input_tokens": usage.input_tokens,
        "uncached_input_tokens": usage.uncached_input_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "cache_write_input_tokens": usage.cache_write_input_tokens,
        "cache_write_5m_input_tokens": usage.cache_write_5m_input_tokens,
        "cache_write_1h_input_tokens": usage.cache_write_1h_input_tokens,
        "cache_write_unknown_ttl_input_tokens": usage.cache_write_unknown_ttl_input_tokens,
        "cache_write_breakdown_complete": int(usage.cache_write_breakdown_complete),
        "output_tokens": usage.output_tokens,
        "reasoning_output_tokens": usage.reasoning_output_tokens,
        "observed_at_us": observed_at_us,
        "raw_usage_json": _json(usage.raw_usage),
        "supersedes_measurement_id": None,
    }


def _emit_session(observation: TurnObservation, provider_session_id: str) -> None:
    runtime = observation.runtime
    accepted = runtime.emit(
        "session.recorded",
        {
            "session_id": observation.session_id,
            "session_name": observation.session_name,
            "provider_session_id": provider_session_id or None,
            "brain": observation.brain,
            "provider": observation.provider,
            "role": observation.role or None,
            "run_key": observation.run_key or None,
            "project": observation.project or None,
            "workflow_name": observation.workflow_name or None,
            "started_at_us": runtime._session_starts[observation.session_id],
            "ended_at_us": None,
            "status": "running",
            "terminal_error_kind": None,
            "parent_session_id": None,
            "metadata_json": "{}",
        },
        session_id=observation.session_id,
    )
    if not accepted:
        return
    runtime._emitted_sessions.add(observation.session_id)
    if provider_session_id:
        runtime._provider_sessions[observation.session_id] = provider_session_id


def _emit_turn_row(
    observation: TurnObservation,
    *,
    status: str,
    ended_at_us: int | None,
    error_kind: str,
) -> None:
    duration_ms = sum(result.duration_ms for result in observation.results) or None
    api_duration_ms = sum(result.api_duration_ms for result in observation.results) or None
    provider_turn_id = next(
        (
            result.provider_turn_id
            for result in reversed(observation.results)
            if result.provider_turn_id
        ),
        "",
    )
    observation.runtime.emit(
        "turn.recorded",
        {
            "turn_id": observation.turn_id,
            "session_id": observation.session_id,
            "turn_index": observation.turn_index,
            "trigger_kind": observation.trigger_kind,
            "trigger_id": observation.trigger_id or None,
            "is_user_initiated": int(observation.is_user_initiated),
            "prompt_template_id": None,
            "prompt_template_version": None,
            "prompt_sha256": None,
            "prompt_bytes": observation.prompt_bytes,
            "started_at_us": observation.started_at_us,
            "first_output_at_us": observation.first_output_at_us,
            "ended_at_us": ended_at_us,
            "wall_duration_ms": (
                (ended_at_us - observation.started_at_us) / 1000
                if ended_at_us is not None else None
            ),
            "provider_duration_ms": duration_ms,
            "provider_api_duration_ms": api_duration_ms,
            "status": status,
            "error_kind": error_kind or None,
            "provider_turn_id": provider_turn_id or None,
        },
        session_id=observation.session_id,
        turn_id=observation.turn_id,
    )


def _emit_router_decision(observation: TurnObservation) -> None:
    decision = observation.router_decision
    decision_id = observation.router_decision_id
    if decision is None or decision_id is None:
        return
    observation.runtime.emit(
        "router_decision.recorded",
        {
            "router_decision_id": decision_id,
            "turn_id": observation.turn_id,
            "experiment_id": decision.experiment_id,
            "variant_id": decision.variant_id,
            "assignment_unit": decision.assignment_unit,
            "assignment_status": decision.assignment_status,
            "assignment_key_hash": decision.assignment_key_hash,
            "assignment_algorithm": decision.assignment_algorithm,
            "cohort": decision.cohort,
            "router_name": decision.router_name,
            "router_version": decision.router_version,
            "policy_version": decision.policy_version,
            "feature_schema_version": decision.feature_schema_version,
            "candidate_models_json": _json(decision.candidate_models),
            "model_selected": decision.model_selected,
            "control_model": decision.control_model,
            "router_score": decision.router_score,
            "router_reason": decision.router_reason,
            "router_latency_ms": decision.router_latency_ms,
            "fallback_reason": decision.fallback_reason,
            "decided_at_us": decision.decided_at_us,
            "metadata_json": _json({
                "config_fingerprint": decision.config_fingerprint,
                "expected_weight": decision.expected_weight,
                "expected_weights": dict(decision.expected_weights),
            }),
        },
        session_id=observation.session_id,
        turn_id=observation.turn_id,
    )


def _emit_runtime_outcome(
    observation: TurnObservation,
    *,
    name: str,
    value: float | None = None,
    text: str | None = None,
    observed_at_us: int,
) -> None:
    decision_id = observation.router_decision_id
    if decision_id is None:
        return
    observation.runtime.emit(
        "experiment_outcome.recorded",
        {
            "outcome_id": _stable_id("outcome", decision_id, name),
            "router_decision_id": decision_id,
            "outcome_name": name,
            "outcome_value": value,
            "outcome_text": text,
            "outcome_definition_version": "bobi-runtime-v1",
            "outcome_source": "runtime",
            "evaluator_name": "bobi-runtime",
            "evaluator_version": "1",
            "is_estimated": 0,
            "observed_at_us": observed_at_us,
            "metadata_json": "{}",
        },
        session_id=observation.session_id,
        turn_id=observation.turn_id,
    )


def _invocation_rows(
    observation: TurnObservation,
    ended_at_us: int,
) -> tuple[list[tuple[str, "BrainInvocation", "TurnResult"]], dict[str, str]]:
    rows: list[tuple[str, BrainInvocation, TurnResult]] = []
    provider_ids: dict[str, str] = {}
    for result_index, result in enumerate(observation.results):
        invocations = list(result.invocations)
        if not invocations:
            from bobi.brain.base import BrainInvocation

            model = next((usage.model for usage in result.usage if usage.model), "")
            invocations = [BrainInvocation(
                provider_event_id=(result.provider_turn_id or f"result-{result_index}"),
                model=model or observation.model_requested or "unknown",
                started_at_us=None,
                ended_at_us=None,
                status="failed" if result.is_error else "completed",
            )]
        for invocation in invocations:
            index = len(rows) + 1
            provider_event_id = invocation.provider_event_id or f"invocation-{index}"
            invocation_id = _stable_id(
                "inv", observation.turn_id, provider_event_id, index
            )
            provider_ids[provider_event_id] = invocation_id
            rows.append((invocation_id, invocation, result))
    return rows, provider_ids


def _emit_turn(observation: TurnObservation, *, status: str, error_kind: str) -> None:
    runtime = observation.runtime
    if not runtime.enabled:
        return
    ended_at_us = time.time_ns() // 1000
    provider_session_id = next(
        (result.session_id for result in reversed(observation.results) if result.session_id),
        "",
    )
    fault_action = None
    if runtime._fault_injection_enabled:
        try:
            from bobi.metrics.faults import consume_drop_online_usage

            fault_action = consume_drop_online_usage(
                runtime.root, observation.turn_id
            )
        except Exception:
            log.debug("metrics: fault injection check failed", exc_info=True)
    if (
        provider_session_id
        and runtime._provider_sessions.get(observation.session_id)
        != provider_session_id
    ):
        _emit_session(observation, provider_session_id)
    if fault_action is None:
        _emit_turn_row(
            observation,
            status=status,
            ended_at_us=ended_at_us,
            error_kind=error_kind,
        )

    provider_turn_id = next(
        (
            result.provider_turn_id
            for result in reversed(observation.results)
            if result.provider_turn_id
        ),
        "",
    )

    invocation_rows, provider_ids = _invocation_rows(observation, ended_at_us)
    for index, (invocation_id, invocation, result) in enumerate(invocation_rows, 1):
        started = invocation.started_at_us or observation.started_at_us
        ended = invocation.ended_at_us or ended_at_us
        runtime.emit(
            "invocation.recorded",
            {
                "invocation_id": invocation_id,
                "turn_id": observation.turn_id,
                "workflow_step_id": observation.workflow_step_id,
                "router_decision_id": observation.router_decision_id,
                "parent_invocation_id": None,
                "invocation_index": index,
                "provider": observation.provider,
                "model_requested": observation.model_requested or None,
                "model_selected": invocation.model or observation.model_requested or "unknown",
                "provider_event_id": invocation.provider_event_id or None,
                "provider_request_id": None,
                "started_at_us": started,
                "first_token_at_us": None,
                "ended_at_us": ended,
                "wall_duration_ms": max(0, ended - started) / 1000,
                "provider_latency_ms": None,
                "time_to_first_token_ms": None,
                "status": invocation.status or ("failed" if result.is_error else "completed"),
                "stop_reason": invocation.stop_reason or None,
                "error_kind": result.error_kind or None,
            },
            session_id=observation.session_id,
            turn_id=observation.turn_id,
            invocation_id=invocation_id,
        )
        if invocation.usage is not None and fault_action is None:
            usage = invocation.usage
            measurement_id = _stable_id(
                "use", observation.turn_id, invocation_id, usage.provider_event_id
            )
            runtime.emit(
                "usage.recorded",
                _usage_payload(
                    usage,
                    measurement_id=measurement_id,
                    scope="invocation",
                    turn_id=observation.turn_id,
                    invocation_id=invocation_id,
                    provider=observation.provider,
                    observed_at_us=ended,
                ),
                session_id=observation.session_id,
                turn_id=observation.turn_id,
                invocation_id=invocation_id,
            )

    fallback_invocation_id = invocation_rows[0][0] if invocation_rows else None
    for result in observation.results:
        for tool in result.tool_executions:
            _emit_tool(
                observation,
                tool,
                provider_ids=provider_ids,
                fallback_invocation_id=fallback_invocation_id,
                ended_at_us=ended_at_us,
            )

    usage_by_model: dict[str, list[BrainUsage]] = {}
    for result in observation.results:
        for usage in result.usage:
            usage_by_model.setdefault(usage.model or "unknown", []).append(usage)
    for model, usage_items in usage_by_model.items():
        if fault_action is not None:
            continue
        aggregate = _aggregate_usage(usage_items)
        measurement_id = _stable_id("use", observation.turn_id, "turn", model)
        runtime.emit(
            "usage.recorded",
            {
                "measurement_id": measurement_id,
                "scope": "turn",
                "turn_id": observation.turn_id,
                "invocation_id": None,
                "provider": observation.provider,
                "model": model,
                "provider_event_id": None,
                "measurement_source": "provider_stream",
                "is_estimated": 0,
                "estimator_name": None,
                "estimator_version": None,
                **aggregate,
                "observed_at_us": ended_at_us,
                "supersedes_measurement_id": None,
            },
            session_id=observation.session_id,
            turn_id=observation.turn_id,
        )

    total_cost = sum(result.total_cost_usd or 0.0 for result in observation.results)
    if total_cost > 0:
        models = sorted(usage_by_model)
        runtime.emit(
            "cost.recorded",
            {
                "cost_measurement_id": _stable_id("cost", observation.turn_id, "turn"),
                "scope": "turn",
                "session_id": observation.session_id,
                "turn_id": observation.turn_id,
                "invocation_id": None,
                "provider": observation.provider,
                "model": models[0] if len(models) == 1 else None,
                "amount_usd": total_cost,
                "measurement_source": "provider_stream",
                "is_estimated": 0,
                "price_snapshot_id": None,
                "provider_event_id": provider_turn_id or None,
                "observed_at_us": ended_at_us,
                "raw_cost_json": None,
            },
            session_id=observation.session_id,
            turn_id=observation.turn_id,
        )
    _emit_runtime_outcome(
        observation,
        name="completion",
        value=1.0 if status == "completed" else 0.0,
        observed_at_us=ended_at_us,
    )
    _emit_runtime_outcome(
        observation,
        name="error",
        value=0.0 if status == "completed" and not error_kind else 1.0,
        text=error_kind or None,
        observed_at_us=ended_at_us,
    )
    _emit_runtime_outcome(
        observation,
        name="turn_latency_ms",
        value=max(0, ended_at_us - observation.started_at_us) / 1000,
        observed_at_us=ended_at_us,
    )
    if fault_action is not None:
        time.sleep(fault_action.hold_seconds)
        _emit_turn_row(
            observation,
            status=status,
            ended_at_us=time.time_ns() // 1000,
            error_kind=error_kind,
        )


def _emit_tool(
    observation: TurnObservation,
    tool: "BrainToolExecution",
    *,
    provider_ids: dict[str, str],
    fallback_invocation_id: str | None,
    ended_at_us: int,
) -> None:
    triggering = provider_ids.get(tool.triggering_provider_event_id)
    attribution_method = "provider_ids"
    confidence = 1.0
    if triggering is None:
        triggering = fallback_invocation_id
        attribution_method = "turn_aggregate"
        confidence = 0.0
    if triggering is None:
        return
    consuming = provider_ids.get(tool.consuming_provider_event_id)
    started = tool.started_at_us or observation.started_at_us
    ended = tool.ended_at_us or ended_at_us
    tool_id = _stable_id(
        "tool",
        observation.turn_id,
        tool.provider_tool_call_id or tool.tool_name,
        started,
    )
    observation.runtime.emit(
        "tool_execution.recorded",
        {
            "tool_execution_id": tool_id,
            "turn_id": observation.turn_id,
            "triggering_invocation_id": triggering,
            "consuming_invocation_id": consuming,
            "provider_tool_call_id": tool.provider_tool_call_id or None,
            "tool_name": tool.tool_name or "unknown",
            "tool_kind": tool.tool_kind or "provider",
            "input_sha256": None,
            "input_bytes": None,
            "output_sha256": None,
            "output_bytes": None,
            "output_estimated_tokens": None,
            "started_at_us": started,
            "ended_at_us": ended,
            "status": tool.status or "completed",
            "is_error": int(tool.is_error),
            "attribution_method": attribution_method,
            "attribution_confidence": confidence,
            "metadata_json": "{}",
        },
        session_id=observation.session_id,
        turn_id=observation.turn_id,
        invocation_id=triggering,
    )


_runtime: MetricsRuntime | None = None


def get_runtime(root: Path | str | None = None) -> MetricsRuntime:
    global _runtime
    pid = os.getpid()
    if root is None:
        try:
            from bobi import paths

            root = paths.bound_root()
        except Exception:
            root = None
    resolved = Path(root).resolve() if root is not None else None
    if resolved is None:
        # An unbound process cannot safely select or reuse an agent-local store.
        return MetricsRuntime(Path.cwd(), mode=DISABLED)
    if (
        _runtime is not None
        and _runtime.pid == pid
        and _runtime.root == resolved
    ):
        return _runtime
    previous = _runtime
    if previous is not None:
        try:
            atexit.unregister(previous.close)
        except Exception:
            pass
        # A fork inherits the producer object but not its writer thread. Do not
        # touch the inherited writer from the child; process exit closes its fd.
        if previous.pid == pid:
            previous.close()
    _runtime = MetricsRuntime(resolved)
    atexit.register(_runtime.close)
    return _runtime


def observe_turn(session_name: str, **kwargs: Any) -> TurnObservation:
    try:
        return get_runtime().begin_turn(session_name, **kwargs)
    except Exception:
        log.debug("metrics: turn observer startup failed", exc_info=True)
        return MetricsRuntime(Path.cwd(), mode=DISABLED).begin_turn(
            session_name, **kwargs
        )


def resolve_experiment_model(
    requested_model: str,
    *,
    experiment_subject: str = "",
    run_key: str = "",
    session_name: str = "",
) -> str:
    try:
        return get_runtime().resolve_model(
            requested_model,
            experiment_subject=experiment_subject,
            run_key=run_key,
            session_name=session_name,
        )
    except Exception:
        log.debug("metrics: experiment model resolution failed open", exc_info=True)
        return requested_model


def observe_workflow_step(
    session_name: str, **kwargs: Any
) -> WorkflowStepObservation:
    try:
        return get_runtime().begin_workflow_step(session_name, **kwargs)
    except Exception:
        log.debug("metrics: workflow-step observer startup failed", exc_info=True)
        return MetricsRuntime(Path.cwd(), mode=DISABLED).begin_workflow_step(
            session_name, **kwargs
        )


def reset_runtime_for_tests() -> None:
    global _runtime
    if _runtime is not None:
        _runtime.close()
    _runtime = None
