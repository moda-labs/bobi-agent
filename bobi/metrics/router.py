"""Privacy-safe HMAC assignment and historical routing telemetry contracts."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from typing import Mapping

from bobi.config import _PROVIDER_CLEARED_ENV
from bobi.metrics.policy import PolicyConfig, policy_secret_names

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
    *, blank_inherited: bool = False,
) -> dict[str, str]:
    """Remove experiment assignment material from provider subprocesses."""
    env = dict(os.environ if base is None else base)
    env.pop(_PROVIDER_CLEARED_ENV, None)
    removed = set(ROUTING_ENV_NAMES) | {"TYPESAFE_API_KEY"} | set(policy_secret_names())
    try:
        configured = json.loads(env.get(EXPERIMENT_CONFIG_ENV, "{}"))
        policy = configured.get("policy") if isinstance(configured, dict) else None
        credential = policy.get("credential_env") if isinstance(policy, dict) else None
        if isinstance(credential, str) and re.fullmatch(r"[A-Z_][A-Z0-9_]*", credential.strip()):
            removed.add(credential.strip())
    except (ValueError, TypeError):
        pass
    for name in removed:
        env.pop(name, None)
    if blank_inherited:
        env.update(dict.fromkeys(os.environ.keys() - env.keys(), ""))
        env.update(dict.fromkeys(removed, ""))
        env[_PROVIDER_CLEARED_ENV] = ",".join(sorted(removed))
    return env


@dataclass(frozen=True)
class ExperimentVariant:
    variant_id: str
    weight: float
    model: str = ""
    policy: str = ""


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
    policy: PolicyConfig | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ExperimentConfig":
        allowed = {
            "experiment_id", "variants", "router_name", "router_version",
            "policy_version", "feature_schema_version", "control_model",
            "cohort", "policy",
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
            unknown_variant = sorted(set(item) - {"variant_id", "weight", "model", "policy"})
            if unknown_variant:
                raise ValueError(
                    "unsupported experiment variant fields: "
                    + ", ".join(unknown_variant)
                )
            variant_id = item.get("variant_id")
            model = item.get("model")
            policy_name = item.get("policy")
            weight = item.get("weight")
            if not isinstance(variant_id, str) or not variant_id.strip():
                raise ValueError("variant_id must be a non-empty string")
            variant_id = variant_id.strip()
            if variant_id in seen:
                raise ValueError(f"duplicate variant_id {variant_id}")
            if ("model" in item) == ("policy" in item):
                raise ValueError("variant requires exactly one model or policy")
            selected = model if "model" in item else policy_name
            if not isinstance(selected, str) or not selected.strip():
                raise ValueError("variant requires non-empty model or policy")
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise ValueError(f"variant {variant_id} requires numeric weight")
            if not 0 <= float(weight) <= 1:
                raise ValueError(f"variant {variant_id} weight must be in [0, 1]")
            seen.add(variant_id)
            variants.append(ExperimentVariant(
                variant_id, float(weight),
                model.strip() if isinstance(model, str) else "",
                policy_name.strip() if isinstance(policy_name, str) else "",
            ))
        if abs(sum(item.weight for item in variants) - 1.0) > 1e-9:
            raise ValueError("experiment variant weights must sum to 1")
        cohort = raw.get("cohort")
        policy_raw = raw.get("policy")
        if "policy" in raw and not isinstance(policy_raw, dict):
            raise ValueError("experiment policy must be an object")
        policy = PolicyConfig.from_mapping(policy_raw) if isinstance(policy_raw, dict) else None
        policy_variants = [item for item in variants if item.policy]
        if bool(policy_variants) != bool(policy):
            raise ValueError("policy required exactly when a variant names a policy")
        if policy and any(item.policy != policy.name for item in policy_variants):
            raise ValueError("variant policy names must match configured policy")
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
            policy=policy,
        )
        if config.control_model not in {item.model for item in config.variants}:
            raise ValueError("control_model must match one configured variant model")
        if policy:
            if sum(item.model == config.control_model for item in variants) != 1:
                raise ValueError("policy experiment requires exactly one control variant")
            if config.control_model not in policy.candidate_models:
                raise ValueError("control_model must be a policy candidate")
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
    entry_point: str = ""
    policy_metadata_json: str = "{}"


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
    selected = next(variant for variant in reversed(config.variants) if variant.weight > 0)
    for variant in config.variants:
        cumulative += variant.weight
        if bucket < cumulative:
            selected = variant
            break
    key_hash = hmac.new(secret, assignment_key.encode("utf-8"), hashlib.sha256).hexdigest()
    return selected, bucket, key_hash


def public_config(config: ExperimentConfig) -> dict[str, object]:
    """Return a secret-free representation suitable for test artifacts."""
    result = {
        "experiment_id": config.experiment_id,
        "variants": [
            {"variant_id": item.variant_id, "weight": item.weight,
             **({"policy": item.policy} if item.policy else {"model": item.model})}
            for item in config.variants
        ],
        "router_name": config.router_name,
        "router_version": config.router_version,
        "policy_version": config.policy_version,
        "feature_schema_version": config.feature_schema_version,
        "control_model": config.control_model,
        "cohort": config.cohort,
    }
    if config.policy:
        result["policy"] = config.policy.public_config()
    return result


def projection_fields(
    decision: RouterDecision,
    *,
    decided_at_us: int | None = None,
) -> dict[str, object]:
    """Return the privacy-safe fields used by router decision projections."""
    metadata: dict[str, object] = {
        "config_fingerprint": decision.config_fingerprint,
        "expected_weight": decision.expected_weight,
        "expected_weights": dict(decision.expected_weights),
    }
    if decision.entry_point:
        metadata["entry_point"] = decision.entry_point
        policy = json.loads(decision.policy_metadata_json)
        if not isinstance(policy, dict):
            raise ValueError("policy metadata must be an object")
        metadata["policy"] = policy
    return {
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
        "candidate_models_json": json.dumps(
            decision.candidate_models,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
        "model_selected": decision.model_selected,
        "control_model": decision.control_model,
        "router_score": decision.router_score,
        "router_reason": decision.router_reason,
        "router_latency_ms": decision.router_latency_ms,
        "fallback_reason": decision.fallback_reason,
        "decided_at_us": (
            decision.decided_at_us if decided_at_us is None else decided_at_us
        ),
        "metadata_json": json.dumps(
            metadata,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
    }
