"""TypeSafe JEV policy settings, guards, and circuit breaker for fresh-session routing."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import json
import logging
import math
import re
import threading
import time
from typing import Callable, ClassVar, Mapping, Protocol

log = logging.getLogger(__name__)

POLICY_NAME = "typesafe-jev"
POLICY_CREDENTIAL_ENV = "TYPESAFE_API_KEY"
ENTRY_POINTS = ("subagent_phase", "subagent_persistent", "subagent_supervised", "workflow_start")


@dataclass(frozen=True)
class PolicyConfig:
    """The TypeSafe JEV policy settings carried by the short routing config."""

    name: ClassVar[str] = POLICY_NAME
    credential_env: ClassVar[str] = POLICY_CREDENTIAL_ENV

    version: str
    brain: str
    mode: str
    candidate_models: tuple[str, ...]
    entry_points: tuple[str, ...]
    roles: tuple[str, ...]
    prompt_egress: str
    options_json: str
    min_confidence: float = 0.85
    deadline_ms: int = 3000
    max_prompt_bytes: int = 8192
    max_in_flight: int = 8

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "PolicyConfig":
        """Validate the policy fields of a short routing config.

        ``raw`` holds the short-form keys (mode, brain, candidate_models,
        roles, entry_points, prompt_egress, max_prompt_bytes, min_confidence,
        deadline_ms, policy_version, instructions, criteria, endpoint) with
        their defaults already applied."""

        def text(name: str) -> str:
            value = raw.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"policy requires {name}")
            return value.strip()

        def strings(name: str) -> tuple[str, ...]:
            value = raw.get(name)
            if not isinstance(value, list) or not value:
                raise ValueError(f"policy requires non-empty {name}")
            if any(not isinstance(item, str) or not item.strip() for item in value):
                raise ValueError(f"invalid policy {name}")
            result = tuple(item.strip() for item in value)
            if len(set(result)) != len(result):
                raise ValueError(f"duplicate policy {name}")
            return result

        def integer(name: str, minimum: int, maximum: int) -> int:
            value = raw.get(name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"invalid policy {name}")
            return value

        mode = text("mode")
        if mode not in ("shadow", "enforce"):
            raise ValueError("invalid policy mode")
        brain = text("brain")
        # "auto" routes for whichever brain the agent runs.
        if brain not in ("auto", "claude", "codex"):
            raise ValueError("invalid policy brain")
        if raw.get("prompt_egress") not in ("none", "redacted"):
            raise ValueError("policy requires explicit prompt egress")
        confidence = raw.get("min_confidence")
        if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence) or not 0 <= confidence <= 1):
            raise ValueError("invalid policy min_confidence")
        entry_points = strings("entry_points")
        if set(entry_points) - set(ENTRY_POINTS):
            raise ValueError("unsupported policy entry_points")
        options = {"instructions": raw.get("instructions"), "criteria": raw.get("criteria")}
        if "endpoint" in raw:
            options["endpoint"] = raw["endpoint"]
        try:
            options_json = json.dumps(options, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("policy options must contain JSON values") from None
        return cls(
            version=text("policy_version"), brain=brain, mode=mode,
            candidate_models=strings("candidate_models"), entry_points=entry_points,
            roles=strings("roles"), prompt_egress=raw["prompt_egress"], options_json=options_json,
            min_confidence=float(confidence),
            deadline_ms=integer("deadline_ms", 50, 3000),
            max_prompt_bytes=integer("max_prompt_bytes", 1, 65536),
        )


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
    async def decide(self, request: PolicyRequest, *, timeout_s: float) -> PolicyResult: ...


class PolicyError(Exception):
    def __init__(self, reason: str, *, authentication: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.authentication = authentication

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
            # Only a failure can trip the circuit: a successful probe after an outage
            # must close it even though the window still holds the outage's failures.
            if failure and (authentication or probe or self._failures >= 5 or (
                len(self._history) >= 10 and sum(self._history) / len(self._history) >= 0.5
            )):
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
_breakers_lock = threading.Lock()

def policy_health() -> dict[str, object]:
    with _breakers_lock:
        breakers = list(_breakers.items())
    health: dict[str, list[dict[str, object]]] = {}
    for (name, _), breaker in breakers:
        health.setdefault(name, []).append(breaker.health())
    return health

def policy_breaker(config: PolicyConfig) -> CircuitBreaker:
    endpoint = json.loads(config.options_json).get("endpoint", "")
    if not isinstance(endpoint, str):
        raise ValueError("policy endpoint must be a string")
    with _breakers_lock:
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
