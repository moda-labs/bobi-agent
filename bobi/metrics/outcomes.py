"""Validated ingestion of evaluator and human experiment outcomes."""

from __future__ import annotations

import math
import json
import time
from typing import Any

from bobi.metrics.runtime import MetricsRuntime, _stable_id


def record_quality_outcome(
    runtime: MetricsRuntime,
    *,
    router_decision_id: str,
    outcome_name: str,
    outcome_definition_version: str,
    outcome_source: str,
    evaluator_name: str,
    evaluator_version: str,
    outcome_value: float | None = None,
    outcome_text: str | None = None,
    is_estimated: bool = False,
    is_failure: bool | None = None,
    observed_at_us: int | None = None,
    idempotency_key: str,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> bool:
    """Emit one versioned outcome without allowing self-grading by the router."""
    required = {
        "router_decision_id": router_decision_id,
        "outcome_name": outcome_name,
        "outcome_definition_version": outcome_definition_version,
        "evaluator_name": evaluator_name,
        "evaluator_version": evaluator_version,
        "idempotency_key": idempotency_key,
    }
    missing = [name for name, value in required.items() if not str(value).strip()]
    if missing:
        raise ValueError(f"quality outcome requires {', '.join(missing)}")
    if outcome_source not in {"evaluator", "human"}:
        raise ValueError("quality outcome source must be evaluator or human")
    if is_failure is not None and type(is_failure) is not bool:
        raise ValueError("quality outcome failure flag must be boolean")
    if outcome_value is None and not (outcome_text or "").strip():
        raise ValueError("quality outcome requires outcome_value or outcome_text")
    if outcome_value is not None and (
        isinstance(outcome_value, bool)
        or not isinstance(outcome_value, (int, float))
        or not math.isfinite(outcome_value)
    ):
        raise ValueError("quality outcome value must be a finite number")
    observed = observed_at_us or time.time_ns() // 1000
    return runtime.emit(
        "experiment_outcome.recorded",
        {
            "outcome_id": _stable_id(
                "outcome", router_decision_id, outcome_source, idempotency_key
            ),
            "router_decision_id": router_decision_id,
            "outcome_name": outcome_name,
            "outcome_value": outcome_value,
            "outcome_text": outcome_text,
            "outcome_definition_version": outcome_definition_version,
            "outcome_source": outcome_source,
            "evaluator_name": evaluator_name,
            "evaluator_version": evaluator_version,
            "is_estimated": int(is_estimated),
            "observed_at_us": observed,
            "metadata_json": json.dumps({"is_failure": is_failure}) if is_failure is not None else "{}",
        },
        session_id=session_id,
        turn_id=turn_id,
        source="quality_evaluator",
    )
