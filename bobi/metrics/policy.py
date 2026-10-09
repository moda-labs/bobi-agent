"""Vendor-neutral model policy contract for fresh-session routing."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from importlib.metadata import entry_points
import json
import logging
import math
import re
import threading
import time
from typing import Callable, Mapping, Protocol

log = logging.getLogger(__name__)

@dataclass(frozen=True)
class PolicyConfig:
    name: str
    version: str
    brain: str
    mode: str
    candidate_models: tuple[str, ...]
    entry_points: tuple[str, ...]
    roles: tuple[str, ...]
    prompt_egress: str
    credential_env: str
    options_json: str
    min_confidence: float = 0.85
    deadline_ms: int = 1000
    max_in_flight: int = 8
    max_prompt_bytes: int = 8192
    store_reason_text: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "PolicyConfig":
        allowed = {
            "name", "version", "brain", "mode", "candidate_models", "scope",
            "egress", "credential_env", "options", "min_confidence",
            "deadline_ms", "max_in_flight", "store_reason_text",
        }
        if set(raw) - allowed:
            raise ValueError("unsupported policy fields")

        def text(name: str) -> str:
            value = raw.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"policy requires {name}")
            return value.strip()

        def strings(value: object, name: str) -> tuple[str, ...]:
            if not isinstance(value, list) or not value:
                raise ValueError(f"policy requires non-empty {name}")
            if any(not isinstance(item, str) or not item.strip() for item in value):
                raise ValueError(f"invalid policy {name}")
            result = tuple(item.strip() for item in value)
            if len(set(result)) != len(result):
                raise ValueError(f"duplicate policy {name}")
            return result

        def integer(value: object, name: str, minimum: int, maximum: int) -> int:
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"invalid policy {name}")
            return value

        scope = raw.get("scope")
        egress = raw.get("egress")
        if not isinstance(scope, dict) or set(scope) != {"entry_points", "roles"}:
            raise ValueError("policy requires scope allowlists")
        if not isinstance(egress, dict) or set(egress) - {"prompt", "max_prompt_bytes"}:
            raise ValueError("invalid policy egress")
        if egress.get("prompt") not in ("none", "redacted"):
            raise ValueError("policy requires explicit prompt egress")
        confidence = raw.get("min_confidence", 0.85)
        if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence) or not 0 <= confidence <= 1):
            raise ValueError("invalid policy min_confidence")
        mode = text("mode")
        if mode not in ("shadow", "enforce"):
            raise ValueError("invalid policy mode")
        brain = text("brain")
        # "auto" routes for whichever brain the agent runs.
        if brain not in ("auto", "claude", "codex"):
            raise ValueError("invalid policy brain")
        credential_env = text("credential_env")
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", credential_env):
            raise ValueError("invalid policy credential_env")
        store_reason = raw.get("store_reason_text", False)
        if type(store_reason) is not bool:
            raise ValueError("invalid policy store_reason_text")
        options = raw.get("options", {})
        if not isinstance(options, dict):
            raise ValueError("policy options must be an object")

        def validate_options(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if not isinstance(key, str) or re.search(
                        r"secret|token|password|passwd|credential|api.?key|authorization", key, re.I
                    ):
                        raise ValueError("policy options must not contain credentials")
                    validate_options(item)
            elif isinstance(value, list):
                for item in value:
                    validate_options(item)
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError("policy options must contain finite numbers")
            elif value is not None and not isinstance(value, (str, int, float, bool)):
                raise ValueError("policy options must contain JSON values")

        validate_options(options)
        entry_points = strings(scope.get("entry_points"), "entry_points")
        if set(entry_points) - {
            "session_start", "subagent_phase", "subagent_persistent",
            "subagent_supervised", "workflow_start",
        }:
            raise ValueError("unsupported policy entry_points")
        return cls(
            name=text("name"), version=text("version"), brain=brain, mode=mode,
            candidate_models=strings(raw.get("candidate_models"), "candidate_models"),
            entry_points=entry_points,
            roles=strings(scope.get("roles"), "roles"),
            prompt_egress=egress["prompt"], credential_env=credential_env,
            options_json=json.dumps(options, sort_keys=True, separators=(",", ":"), allow_nan=False),
            min_confidence=float(confidence),
            deadline_ms=integer(raw.get("deadline_ms", 1000), "deadline_ms", 50, 3000),
            max_in_flight=integer(raw.get("max_in_flight", 8), "max_in_flight", 1, 1024),
            max_prompt_bytes=integer(egress.get("max_prompt_bytes", 8192), "max_prompt_bytes", 1, 65536),
            store_reason_text=store_reason,
        )

    def public_config(self) -> dict[str, object]:
        options = json.loads(self.options_json)
        options.pop("endpoint", None)
        return {
            "name": self.name, "version": self.version, "brain": self.brain,
            "mode": self.mode, "candidate_models": list(self.candidate_models),
            "scope": {"entry_points": list(self.entry_points), "roles": list(self.roles)},
            "egress": {"prompt": self.prompt_egress, "max_prompt_bytes": self.max_prompt_bytes},
            "credential_env": self.credential_env, "options": options,
            "min_confidence": self.min_confidence, "deadline_ms": self.deadline_ms,
            "max_in_flight": self.max_in_flight, "store_reason_text": self.store_reason_text,
        }


@dataclass(frozen=True)
class PolicyRequest:
    feature_schema_version: str
    features: Mapping[str, object]
    candidate_models: tuple[str, ...]
    control_model: str
    pinned_version: str


@dataclass(frozen=True)
class PolicyResult:
    model: str
    confidence: float | None
    tier: str | None
    model_version: str
    reason_text: str | None
    cost_usd: float | None
    probabilities: Mapping[str, float] | None = None
    raw_response: Mapping[str, object] | None = None
    outgoing_payload: Mapping[str, object] | None = None


class ModelPolicy(Protocol):
    name: str
    secret_env_names: tuple[str, ...]

    async def decide(self, request: PolicyRequest, *, timeout_s: float) -> PolicyResult: ...


class StaticPolicy:
    name = "static"
    secret_env_names: tuple[str, ...] = ()

    def __init__(self, model: str) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("static policy requires a model")
        self.model = model.strip()

    async def decide(self, request: PolicyRequest, *, timeout_s: float) -> PolicyResult:
        if timeout_s <= 0:
            raise ValueError("policy timeout must be positive")
        if self.model not in request.candidate_models:
            raise ValueError("static policy model must be a candidate")
        return PolicyResult(self.model, 1.0, None, request.pinned_version, None, 0.0)

class PolicyError(Exception):
    def __init__(self, reason: str, *, authentication: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.authentication = authentication

_secret_names: set[str] = set()
_registry_lock = threading.Lock()

def policy_secret_names() -> tuple[str, ...]:
    with _registry_lock:
        return tuple(_secret_names)

def load_policy(config: PolicyConfig) -> ModelPolicy:
    with _registry_lock:
        _secret_names.add(config.credential_env)
    options = json.loads(config.options_json)
    if config.name == "static":
        if set(options) != {"model"}:
            raise ValueError("static policy requires only options.model")
        policy = StaticPolicy(options["model"])
    elif config.name == "typesafe-jev":
        from bobi.metrics.policies.typesafe import TypeSafePolicy

        policy = TypeSafePolicy(config)
    else:
        matches = list(entry_points(group="bobi.model_policies", name=config.name))
        if len(matches) != 1:
            raise ValueError("model policy unavailable or ambiguous")
        policy = matches[0].load()(config)
    if policy.name != config.name or not callable(getattr(policy, "decide", None)):
        raise ValueError("invalid model policy")
    names = policy.secret_env_names
    if any(not isinstance(name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) for name in names):
        raise ValueError("invalid policy secret environment names")
    with _registry_lock:
        _secret_names.update(names)
    return policy

def guard_result(result: PolicyResult, config: PolicyConfig) -> str | None:
    if (not isinstance(result, PolicyResult) or not isinstance(result.model, str)
            or result.model not in config.candidate_models):
        return "policy_invalid_response"
    if (not isinstance(result.model_version, str)
            or (result.tier is not None and not isinstance(result.tier, str))
            or (result.reason_text is not None and not isinstance(result.reason_text, str))):
        return "policy_invalid_response"
    confidence = result.confidence
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        return "policy_invalid_response"
    if result.cost_usd is not None and (
        isinstance(result.cost_usd, bool) or not isinstance(result.cost_usd, (int, float))
        or not math.isfinite(result.cost_usd) or result.cost_usd < 0
    ):
        return "policy_invalid_response"
    if not version_matches(result.model_version, config.version):
        return "policy_version_drift"
    if confidence < config.min_confidence:
        return "policy_low_confidence"
    return None

def _release(version: str) -> tuple[str, str, str] | None:
    match = re.fullmatch(r"(.*?)(\d+)\.(\d+)(?:\.\d+)?(?:[-+].*)?", version)
    return match.groups() if match else None


def version_matches(returned: str, pinned: str) -> bool:
    """True when the policy answered with the pinned release line.

    A patch bump or build suffix (``jev-1.13.2``, ``jev-1.13.0+b7`` against
    ``jev-1.13.0``) is the same model; a minor or major change is drift."""
    if returned == pinned:
        return True
    ours, theirs = _release(pinned), _release(returned)
    return ours is not None and ours == theirs


class CircuitBreaker:
    def __init__(self, *, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._state = "closed"
        self._history: deque[bool] = deque(maxlen=20)
        self._failures = 0
        self._cooldown = 30.0
        self._opened = 0.0
        self._opened_at_us: int | None = None
        self._last_failure: str | None = None
        self._in_flight = 0
        self._authentication_logged = False

    def acquire(self, capacity: int) -> tuple[str, bool]:
        with self._lock:
            if self._state == "open":
                if self._clock() - self._opened < self._cooldown:
                    return "open", False
                if self._in_flight:
                    return "open", False
                self._state = "half-open"
            if self._state == "half-open" and self._in_flight:
                return "open", False
            if self._in_flight >= capacity:
                return "busy", False
            self._in_flight += 1
            return "acquired", self._state == "half-open"

    def finish(self, failure: str | None, *, probe: bool, authentication: bool = False,
               cancelled: bool = False) -> None:
        with self._lock:
            self._in_flight -= 1
            if authentication and not self._authentication_logged:
                log.warning("metrics: policy authentication failed; circuit opened for 600 seconds")
                self._authentication_logged = True
            if cancelled:
                if probe:
                    self._state = "open"
                return
            if failure:
                self._last_failure = failure
            self._history.append(bool(failure))
            self._failures = self._failures + 1 if failure else 0
            if authentication or (probe and failure) or self._failures >= 5 or (
                len(self._history) >= 10 and sum(self._history) / len(self._history) >= 0.5
            ):
                if self._state != "open" or probe or (authentication and self._cooldown < 600.0):
                    self._cooldown = 600.0 if authentication else (
                        min(600.0, self._cooldown * 2) if probe else 30.0
                    )
                    self._opened = self._clock()
                    self._opened_at_us = time.time_ns() // 1000
                self._state = "open"
            elif probe:
                self._state = "closed"
                self._history.clear()
                self._failures = 0
                self._cooldown = 30.0
                self._opened_at_us = None
                self._last_failure = None
                self._authentication_logged = False

    def health(self) -> dict[str, object]:
        with self._lock:
            return {"state": self._state, "consecutive_failures": self._failures,
                    "opened_at_us": self._opened_at_us, "last_failure_kind": self._last_failure}

_breakers: dict[tuple[str, str], CircuitBreaker] = {}

def policy_health() -> dict[str, object]:
    with _registry_lock:
        breakers = list(_breakers.items())
    health: dict[str, list[dict[str, object]]] = {}
    for (name, _), breaker in breakers:
        health.setdefault(name, []).append(breaker.health())
    return health

def policy_breaker(config: PolicyConfig) -> CircuitBreaker:
    endpoint = json.loads(config.options_json).get("endpoint", "")
    if not isinstance(endpoint, str):
        raise ValueError("policy endpoint must be a string")
    with _registry_lock:
        return _breakers.setdefault((config.name, endpoint), CircuitBreaker())

async def call_policy(policy: ModelPolicy, request: PolicyRequest, config: PolicyConfig,
                      *, breaker: CircuitBreaker | None = None,
                      on_call: Callable[[], None] | None = None) -> tuple[PolicyResult | None, str | None]:
    breaker = breaker or policy_breaker(config)
    deadline = time.monotonic() + config.deadline_ms / 1000
    probe = False
    acquired = False
    failure = None
    authentication = False
    cancelled = False
    try:
        async with asyncio.timeout(config.deadline_ms / 1000):
            while not acquired:
                state, probe = breaker.acquire(config.max_in_flight)
                if state == "open":
                    return None, "policy_circuit_open"
                acquired = state == "acquired"
                if not acquired:
                    await asyncio.sleep(min(0.01, max(0, deadline - time.monotonic())))
            if on_call is not None:
                on_call()
            result = await policy.decide(request, timeout_s=max(0, deadline - time.monotonic()))
            reason = guard_result(result, config)
            failure = reason if reason == "policy_invalid_response" else None
            return result, reason
    except TimeoutError:
        failure = "policy_timeout"
        return None, failure
    except PolicyError as exc:
        authentication = exc.authentication is True
        # A missing or rejected credential is not an outage: name it so the operator fixes the key.
        failure = "policy_unauthenticated" if authentication else exc.reason if isinstance(exc.reason, str) and exc.reason in {
            "policy_timeout", "policy_unavailable", "policy_invalid_response",
        } else "policy_unavailable"
        return None, failure
    except asyncio.CancelledError:
        cancelled = True
        raise
    except Exception:
        failure = "policy_unavailable"
        return None, failure
    finally:
        if acquired:
            breaker.finish(failure, probe=probe, authentication=authentication, cancelled=cancelled)
