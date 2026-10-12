"""The short JEV routing config and privacy-safe router decision contracts."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Mapping

from bobi.config import _PROVIDER_CLEARED_ENV
from bobi.metrics.policy import POLICY_CREDENTIAL_ENV, PolicyConfig

EXPERIMENT_CONFIG_ENV = "BOBI_METRICS_EXPERIMENT_JSON"
# A disabled policy, kept as a real key (not a comment) so every .env rewrite
# preserves it for re-enabling. Routing never reads it.
DISABLED_EXPERIMENT_CONFIG_ENV = "BOBI_METRICS_EXPERIMENT_DISABLED_JSON"
# Every routed decision records this variant: the config always calls the policy.
TREATMENT_VARIANT = "treatment_jev"
LONG_FORM_ERROR = "long-form experiment config is no longer supported; save from the editor to rewrite it"

# Short-form settings and the value each takes when the config omits it.
SHORT_DEFAULTS: dict[str, object] = {
    "mode": "enforce",
    "roles": ["director", "engineer"],
    "entry_points": ["subagent_phase", "subagent_persistent", "subagent_supervised", "workflow_start"],
    "brain": "auto",
    "prompt_egress": "none",
    "min_confidence": 0.85,
    "deadline_ms": 3000,
    "max_prompt_bytes": 8192,
    "policy_version": "jev-1.13.0",
}
_LABEL_DEFAULTS = {
    "experiment_id": "jev-routing-v1",
    "router_name": "jev-router",
    "router_version": "v1",
    "feature_schema_version": "jev-features-v1",
}
_SHORT_FIELDS = (
    {"control_model", "candidate_models", "instructions", "criteria", "endpoint", "cohort"}
    | set(SHORT_DEFAULTS) | set(_LABEL_DEFAULTS)
)


def default_criteria(model: str) -> str:
    return f"Choose {model} when the routing instructions recommend it."


def provider_subprocess_env(
    base: Mapping[str, str] | None = None,
    *, blank_inherited: bool = False,
) -> dict[str, str]:
    """Remove the routing config (active or disabled) and the policy credential from provider subprocesses."""
    env = dict(os.environ if base is None else base)
    env.pop(_PROVIDER_CLEARED_ENV, None)
    removed = {EXPERIMENT_CONFIG_ENV, DISABLED_EXPERIMENT_CONFIG_ENV, POLICY_CREDENTIAL_ENV}
    for name in removed:
        env.pop(name, None)
    if blank_inherited:
        env.update(dict.fromkeys(os.environ.keys() - env.keys(), ""))
        env.update(dict.fromkeys(removed, ""))
        env[_PROVIDER_CLEARED_ENV] = ",".join(sorted(removed))
    return env


@dataclass(frozen=True)
class ExperimentConfig:
    """A validated short JEV config: one policy routing between candidate models."""

    experiment_id: str
    control_model: str
    policy: PolicyConfig
    router_name: str = "jev-router"
    router_version: str = "v1"
    feature_schema_version: str = "jev-features-v1"
    cohort: str | None = None

    @property
    def policy_version(self) -> str:
        return self.policy.version

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ExperimentConfig":
        if "variants" in raw or "policy" in raw:
            raise ValueError(LONG_FORM_ERROR)
        unknown = sorted(set(raw) - _SHORT_FIELDS)
        if unknown:
            raise ValueError("unsupported experiment config fields: " + ", ".join(unknown))

        def label(name: str) -> str:
            value = raw.get(name, _LABEL_DEFAULTS[name])
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"experiment config requires non-empty {name}")
            return value.strip()

        candidates = raw.get("candidate_models")
        if (not isinstance(candidates, list) or not candidates
                or any(not isinstance(model, str) or not model.strip() for model in candidates)):
            raise ValueError("candidate_models must be non-empty model names")
        candidates = [model.strip() for model in candidates]
        control = raw.get("control_model")
        if not isinstance(control, str) or control.strip() not in candidates:
            raise ValueError("control_model must be one of candidate_models")
        cohort = raw.get("cohort")
        if cohort is not None and (not isinstance(cohort, str) or not cohort.strip()):
            raise ValueError("cohort must be a non-empty string when supplied")
        settings = {name: raw.get(name, default) for name, default in SHORT_DEFAULTS.items()}
        settings.update(
            candidate_models=candidates, instructions=raw.get("instructions"),
            criteria=raw.get("criteria", {model: default_criteria(model) for model in candidates}),
        )
        if "endpoint" in raw:
            settings["endpoint"] = raw["endpoint"]
        policy = PolicyConfig.from_mapping(settings)
        from bobi.metrics.policies.typesafe import TypeSafePolicy

        TypeSafePolicy(policy)  # instructions, criteria, endpoint and version pin
        return cls(
            experiment_id=label("experiment_id"), control_model=control.strip(), policy=policy,
            router_name=label("router_name"), router_version=label("router_version"),
            feature_schema_version=label("feature_schema_version"),
            cohort=cohort.strip() if isinstance(cohort, str) else None,
        )

    def to_mapping(self) -> dict[str, object]:
        """The minimal short form: key fields plus every setting or label that
        differs from its default. ``from_mapping`` of the result is ``self``."""
        policy = self.policy
        options = json.loads(policy.options_json)
        candidates = list(policy.candidate_models)
        result: dict[str, object] = {
            "mode": policy.mode, "control_model": self.control_model, "candidate_models": candidates,
            "prompt_egress": policy.prompt_egress, "instructions": options["instructions"],
        }
        if options["criteria"] != {model: default_criteria(model) for model in candidates}:
            result["criteria"] = options["criteria"]
        if "endpoint" in options:
            result["endpoint"] = options["endpoint"]
        settings = {"roles": list(policy.roles), "entry_points": list(policy.entry_points), "brain": policy.brain,
                    "min_confidence": policy.min_confidence, "deadline_ms": policy.deadline_ms,
                    "max_prompt_bytes": policy.max_prompt_bytes, "policy_version": policy.version}
        for name, value in settings.items():
            default = SHORT_DEFAULTS[name]
            if (set(value) != set(default)) if isinstance(value, list) else value != default:
                result[name] = value
        for name, default in _LABEL_DEFAULTS.items():
            if getattr(self, name) != default:
                result[name] = getattr(self, name)
        if self.cohort is not None:
            result["cohort"] = self.cohort
        return result

    def fingerprint(self) -> str:
        """Hash of everything that shapes a decision; the endpoint is excluded."""
        public = {name: value for name, value in self.to_mapping().items() if name != "endpoint"}
        return hashlib.sha256(json.dumps(public, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class RouterDecision:
    experiment_id: str
    cohort: str | None
    router_name: str
    router_version: str
    policy_version: str
    feature_schema_version: str
    candidate_models: tuple[str, ...]
    model_selected: str
    control_model: str
    router_reason: str
    router_latency_ms: float
    fallback_reason: str | None
    decided_at_us: int
    config_fingerprint: str
    entry_point: str = ""
    policy_metadata_json: str = "{}"


def load_experiment(env: Mapping[str, str] | None = None) -> ExperimentConfig | None:
    values = os.environ if env is None else env
    encoded = str(values.get(EXPERIMENT_CONFIG_ENV, "")).strip()
    if not encoded:
        return None
    try:
        raw = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{EXPERIMENT_CONFIG_ENV} is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{EXPERIMENT_CONFIG_ENV} must contain an object")
    return ExperimentConfig.from_mapping(raw)


def projection_fields(
    decision: RouterDecision,
    *,
    decided_at_us: int | None = None,
) -> dict[str, object]:
    """Return the privacy-safe fields used by router decision projections."""
    metadata: dict[str, object] = {"config_fingerprint": decision.config_fingerprint}
    if decision.entry_point:
        metadata["entry_point"] = decision.entry_point
        policy = json.loads(decision.policy_metadata_json)
        if not isinstance(policy, dict):
            raise ValueError("policy metadata must be an object")
        metadata["policy"] = policy
    return {
        "experiment_id": decision.experiment_id,
        "variant_id": TREATMENT_VARIANT,
        "assignment_status": "assigned",
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
