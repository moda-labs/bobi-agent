"""Deterministic, privacy-safe experiment assignment for metrics telemetry.

The router is deliberately independent from provider clients.  It describes
the intended treatment and records it before a provider invocation; callers
remain responsible for constructing a client for the selected model.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Mapping

ASSIGNMENT_ALGORITHM = "hmac-sha256-u64-v1"
EXPERIMENT_CONFIG_ENV = "BOBI_METRICS_EXPERIMENT_JSON"
ASSIGNMENT_SECRET_ENV = "BOBI_METRICS_ASSIGNMENT_SECRET"
EXPERIMENT_SUBJECT_ENV = "BOBI_METRICS_EXPERIMENT_SUBJECT"
ROUTING_ENV_NAMES = (
    EXPERIMENT_CONFIG_ENV,
    ASSIGNMENT_SECRET_ENV,
    EXPERIMENT_SUBJECT_ENV,
)


def provider_subprocess_env(
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Remove experiment assignment material from provider subprocesses."""
    env = dict(os.environ if base is None else base)
    for name in ROUTING_ENV_NAMES:
        env.pop(name, None)
    return env


@dataclass(frozen=True)
class ExperimentVariant:
    variant_id: str
    weight: float
    model: str


@dataclass(frozen=True)
class ExperimentConfig:
    experiment_id: str
    variants: tuple[ExperimentVariant, ...]
    router_name: str
    router_version: str
    policy_version: str
    feature_schema_version: str
    control_model: str
    cohort: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ExperimentConfig":
        allowed = {
            "experiment_id", "variants", "router_name", "router_version",
            "policy_version", "feature_schema_version", "control_model",
            "cohort",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(
                "unsupported experiment config fields: " + ", ".join(unknown)
            )

        def required(name: str) -> str:
            value = raw.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"experiment config requires non-empty {name}")
            return value.strip()

        source_variants = raw.get("variants")
        if not isinstance(source_variants, list) or len(source_variants) < 2:
            raise ValueError("experiment config requires at least two variants")
        variants: list[ExperimentVariant] = []
        seen: set[str] = set()
        for item in source_variants:
            if not isinstance(item, dict):
                raise ValueError("each experiment variant must be an object")
            unknown_variant = sorted(set(item) - {"variant_id", "weight", "model"})
            if unknown_variant:
                raise ValueError(
                    "unsupported experiment variant fields: "
                    + ", ".join(unknown_variant)
                )
            variant_id = item.get("variant_id")
            model = item.get("model")
            weight = item.get("weight")
            if not isinstance(variant_id, str) or not variant_id.strip():
                raise ValueError("variant_id must be a non-empty string")
            if variant_id in seen:
                raise ValueError(f"duplicate variant_id {variant_id}")
            if not isinstance(model, str) or not model.strip():
                raise ValueError(f"variant {variant_id} requires model")
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise ValueError(f"variant {variant_id} requires numeric weight")
            if not 0 < float(weight) <= 1:
                raise ValueError(f"variant {variant_id} weight must be in (0, 1]")
            seen.add(variant_id)
            variants.append(ExperimentVariant(variant_id, float(weight), model.strip()))
        if abs(sum(item.weight for item in variants) - 1.0) > 1e-9:
            raise ValueError("experiment variant weights must sum to 1")
        cohort = raw.get("cohort")
        if cohort is not None and (not isinstance(cohort, str) or not cohort.strip()):
            raise ValueError("cohort must be a non-empty string when supplied")
        config = cls(
            experiment_id=required("experiment_id"),
            variants=tuple(variants),
            router_name=required("router_name"),
            router_version=required("router_version"),
            policy_version=required("policy_version"),
            feature_schema_version=required("feature_schema_version"),
            control_model=required("control_model"),
            cohort=cohort.strip() if isinstance(cohort, str) else None,
        )
        if config.control_model not in {item.model for item in config.variants}:
            raise ValueError("control_model must match one configured variant model")
        return config


@dataclass(frozen=True)
class RouterDecision:
    experiment_id: str | None
    variant_id: str | None
    assignment_unit: str | None
    assignment_status: str
    assignment_key_hash: str | None
    assignment_algorithm: str | None
    cohort: str | None
    router_name: str
    router_version: str
    policy_version: str | None
    feature_schema_version: str | None
    candidate_models: tuple[str, ...]
    model_selected: str
    control_model: str | None
    router_score: float | None
    router_reason: str
    router_latency_ms: float
    fallback_reason: str | None
    decided_at_us: int
    expected_weight: float | None
    expected_weights: tuple[tuple[str, float], ...]
    config_fingerprint: str


def load_experiment(
    env: Mapping[str, str] | None = None,
) -> tuple[ExperimentConfig, bytes] | None:
    values = os.environ if env is None else env
    encoded = str(values.get(EXPERIMENT_CONFIG_ENV, "")).strip()
    if not encoded:
        return None
    secret = str(values.get(ASSIGNMENT_SECRET_ENV, ""))
    if not secret:
        raise ValueError(f"{ASSIGNMENT_SECRET_ENV} is required when experiment routing is enabled")
    try:
        raw = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{EXPERIMENT_CONFIG_ENV} is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{EXPERIMENT_CONFIG_ENV} must contain an object")
    return ExperimentConfig.from_mapping(raw), secret.encode("utf-8")


def choose_assignment_key(
    *,
    experiment_subject: str = "",
    run_key: str = "",
    session_id: str = "",
) -> tuple[str | None, str | None]:
    for unit, value in (
        ("experiment_subject", experiment_subject),
        ("run_key", run_key),
        ("session_id", session_id),
    ):
        if value:
            return unit, value
    return None, None


def assign_variant(
    config: ExperimentConfig,
    secret: bytes,
    *,
    assignment_unit: str,
    assignment_key: str,
) -> tuple[ExperimentVariant, float, str]:
    material = (
        config.experiment_id + "\0" + assignment_unit + "\0" + assignment_key
    ).encode("utf-8")
    digest = hmac.new(secret, material, hashlib.sha256).digest()
    bucket = int.from_bytes(digest[:8], "big") / 2**64
    cumulative = 0.0
    selected = config.variants[-1]
    for variant in config.variants:
        cumulative += variant.weight
        if bucket < cumulative:
            selected = variant
            break
    key_hash = hmac.new(secret, assignment_key.encode("utf-8"), hashlib.sha256).hexdigest()
    return selected, bucket, key_hash


def route(
    configured: tuple[ExperimentConfig, bytes] | None,
    *,
    requested_model: str,
    experiment_subject: str = "",
    run_key: str = "",
    session_id: str = "",
) -> RouterDecision | None:
    """Return an intent-to-treat decision, or None when routing is disabled."""
    if configured is None:
        return None
    started = time.perf_counter_ns()
    config, secret = configured
    config_fingerprint = hashlib.sha256(
        json.dumps(public_config(config), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    expected_weights = tuple((item.variant_id, item.weight) for item in config.variants)
    unit, key = choose_assignment_key(
        experiment_subject=experiment_subject,
        run_key=run_key,
        session_id=session_id,
    )
    decided_at = time.time_ns() // 1000
    candidates = tuple(dict.fromkeys(item.model for item in config.variants))
    if unit is None or key is None:
        return RouterDecision(
            experiment_id=config.experiment_id,
            variant_id=None,
            assignment_unit=None,
            assignment_status="unassigned",
            assignment_key_hash=None,
            assignment_algorithm=ASSIGNMENT_ALGORITHM,
            cohort=config.cohort,
            router_name=config.router_name,
            router_version=config.router_version,
            policy_version=config.policy_version,
            feature_schema_version=config.feature_schema_version,
            candidate_models=candidates,
            model_selected=requested_model or config.control_model,
            control_model=config.control_model,
            router_score=None,
            router_reason="missing_stable_assignment_key",
            router_latency_ms=(time.perf_counter_ns() - started) / 1_000_000,
            fallback_reason=None,
            decided_at_us=decided_at,
            expected_weight=None,
            expected_weights=expected_weights,
            config_fingerprint=config_fingerprint,
        )
    selected, bucket, key_hash = assign_variant(
        config, secret, assignment_unit=unit, assignment_key=key
    )
    fallback = bool(requested_model and requested_model != selected.model)
    return RouterDecision(
        experiment_id=config.experiment_id,
        variant_id=selected.variant_id,
        assignment_unit=unit,
        assignment_status="assigned",
        assignment_key_hash=key_hash,
        assignment_algorithm=ASSIGNMENT_ALGORITHM,
        cohort=config.cohort,
        router_name=config.router_name,
        router_version=config.router_version,
        policy_version=config.policy_version,
        feature_schema_version=config.feature_schema_version,
        candidate_models=candidates,
        model_selected=requested_model if fallback else selected.model,
        control_model=config.control_model,
        router_score=bucket,
        router_reason="deterministic_weighted_assignment",
        router_latency_ms=(time.perf_counter_ns() - started) / 1_000_000,
        fallback_reason="preconfigured_client_model" if fallback else None,
        decided_at_us=decided_at,
        expected_weight=selected.weight,
        expected_weights=expected_weights,
        config_fingerprint=config_fingerprint,
    )


def public_config(config: ExperimentConfig) -> dict[str, object]:
    """Return a secret-free representation suitable for test artifacts."""
    return {
        "experiment_id": config.experiment_id,
        "variants": [
            {"variant_id": item.variant_id, "weight": item.weight, "model": item.model}
            for item in config.variants
        ],
        "router_name": config.router_name,
        "router_version": config.router_version,
        "policy_version": config.policy_version,
        "feature_schema_version": config.feature_schema_version,
        "control_model": config.control_model,
        "cohort": config.cohort,
    }
