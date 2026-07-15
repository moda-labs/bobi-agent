"""Resource-aware launch admission for subagent starts."""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from bobi import paths
from bobi.concurrency_semaphore import (
    _excluded_session_names,
    is_excluded_from_concurrency,
)
from bobi.sdk import get_registry

log = logging.getLogger(__name__)

INIT_HEALTH_FILE = "launch-init-health.json"
INIT_FAILURE = "init_failure"
INIT_SUCCESS = "init_success"


@dataclass(frozen=True)
class LaunchAdmissionSnapshot:
    active_agents: int
    starting_agents: int
    load_1m: float
    cpu_count: int
    mem_available_mb: float | None
    mem_total_mb: float | None
    recent_init_failures: int
    recent_init_successes: int
    metrics_readable: bool


@dataclass(frozen=True)
class LaunchAdmissionPolicy:
    max_concurrent_agents: int
    max_starting_agents: int = 1
    load_per_cpu_soft_limit: float = 1.5
    load_per_cpu_hard_limit: float = 2.0
    min_memory_available_mb: int = 512
    init_failure_window_seconds: int = 600
    init_failure_backoff_threshold: int = 2
    queue_poll_seconds: float = 5.0


@dataclass(frozen=True)
class LaunchAdmissionDecision:
    allowed: bool
    reason: str
    binding_signal: str
    active_agents: int
    starting_agents: int
    effective_cap: int
    load_per_cpu: float | None
    mem_available_mb: float | None
    recent_init_failures: int


@dataclass(frozen=True)
class InitHealthCounts:
    recent_init_failures: int
    recent_init_successes: int


def policy_from_config(max_concurrent_agents: int, raw: dict) -> LaunchAdmissionPolicy:
    return LaunchAdmissionPolicy(
        max_concurrent_agents=max_concurrent_agents,
        max_starting_agents=int(raw.get("max_starting_agents", 1)),
        load_per_cpu_soft_limit=float(raw.get("load_per_cpu_soft_limit", 1.5)),
        load_per_cpu_hard_limit=float(raw.get("load_per_cpu_hard_limit", 2.0)),
        min_memory_available_mb=int(raw.get("min_memory_available_mb", 512)),
        init_failure_window_seconds=int(raw.get("init_failure_window_seconds", 600)),
        init_failure_backoff_threshold=int(raw.get("init_failure_backoff_threshold", 2)),
    )


def evaluate_launch_admission(
    snapshot: LaunchAdmissionSnapshot,
    policy: LaunchAdmissionPolicy,
) -> LaunchAdmissionDecision:
    load_per_cpu = None
    if snapshot.metrics_readable and snapshot.cpu_count > 0:
        load_per_cpu = snapshot.load_1m / snapshot.cpu_count

    def decision(allowed: bool, signal: str, reason: str) -> LaunchAdmissionDecision:
        return LaunchAdmissionDecision(
            allowed=allowed,
            reason=reason,
            binding_signal=signal,
            active_agents=snapshot.active_agents,
            starting_agents=snapshot.starting_agents,
            effective_cap=policy.max_concurrent_agents,
            load_per_cpu=load_per_cpu,
            mem_available_mb=snapshot.mem_available_mb,
            recent_init_failures=snapshot.recent_init_failures,
        )

    if snapshot.active_agents >= policy.max_concurrent_agents:
        return decision(
            False,
            "static_cap",
            (
                f"{snapshot.active_agents} active agents at static cap "
                f"{policy.max_concurrent_agents}"
            ),
        )

    if snapshot.starting_agents >= policy.max_starting_agents:
        return decision(
            False,
            "starting_agents",
            (
                f"{snapshot.starting_agents} agents already starting "
                f"(limit {policy.max_starting_agents})"
            ),
        )

    if snapshot.recent_init_failures >= policy.init_failure_backoff_threshold:
        return decision(
            False,
            "init_health",
            (
                f"{snapshot.recent_init_failures} recent initialize failures "
                f"(threshold {policy.init_failure_backoff_threshold})"
            ),
        )

    if not snapshot.metrics_readable:
        return decision(
            True,
            "metrics_unreadable",
            "resource metrics unreadable; allowing under static/start/init-health guards",
        )

    if load_per_cpu is not None and load_per_cpu >= policy.load_per_cpu_hard_limit:
        return decision(
            False,
            "load",
            (
                f"load per CPU {load_per_cpu:.2f} at/above hard limit "
                f"{policy.load_per_cpu_hard_limit:.2f}"
            ),
        )

    if (
        snapshot.mem_available_mb is not None
        and snapshot.mem_available_mb < policy.min_memory_available_mb
    ):
        return decision(
            False,
            "memory",
            (
                f"available memory {snapshot.mem_available_mb:.0f}MB below "
                f"floor {policy.min_memory_available_mb}MB"
            ),
        )

    if (
        load_per_cpu is not None
        and load_per_cpu >= policy.load_per_cpu_soft_limit
        and snapshot.recent_init_successes == 0
    ):
        return decision(
            False,
            "load",
            (
                f"load per CPU {load_per_cpu:.2f} at/above soft limit "
                f"{policy.load_per_cpu_soft_limit:.2f}"
            ),
        )

    return decision(True, "none", "launch admitted")


def read_host_metrics() -> tuple[float, int, float | None, float | None, bool]:
    try:
        load_1m = os.getloadavg()[0]
        cpu_count = os.cpu_count() or 1
        mem_available_mb, mem_total_mb = _read_meminfo()
        return load_1m, cpu_count, mem_available_mb, mem_total_mb, True
    except Exception:
        log.debug("Could not read host launch-admission metrics", exc_info=True)
        return 0.0, os.cpu_count() or 1, None, None, False


def _read_meminfo() -> tuple[float, float]:
    values: dict[str, float] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        if key not in ("MemAvailable", "MemTotal"):
            continue
        amount = rest.strip().split()[0]
        values[key] = int(amount) / 1024
    return values["MemAvailable"], values["MemTotal"]


def build_snapshot(root: Path, policy: LaunchAdmissionPolicy) -> LaunchAdmissionSnapshot:
    active_agents = 0
    starting_agents = 0
    excluded_session_names = _excluded_session_names(root)
    for entry in get_registry().list_active():
        if is_excluded_from_concurrency(entry, excluded_session_names):
            continue
        active_agents += 1
        if entry.status == "starting":
            starting_agents += 1

    load_1m, cpu_count, mem_available_mb, mem_total_mb, metrics_readable = read_host_metrics()
    health = load_init_health(root, policy.init_failure_window_seconds)
    return LaunchAdmissionSnapshot(
        active_agents=active_agents,
        starting_agents=starting_agents,
        load_1m=load_1m,
        cpu_count=cpu_count,
        mem_available_mb=mem_available_mb,
        mem_total_mb=mem_total_mb,
        recent_init_failures=health.recent_init_failures,
        recent_init_successes=health.recent_init_successes,
        metrics_readable=metrics_readable,
    )


def wait_for_launch_admission(
    root: Path,
    policy: LaunchAdmissionPolicy,
    timeout: float,
) -> LaunchAdmissionDecision:
    deadline = time.time() + timeout
    alerted = False
    last_decision: LaunchAdmissionDecision | None = None

    while time.time() < deadline:
        decision = evaluate_launch_admission(build_snapshot(root, policy), policy)
        if decision.allowed:
            return decision
        last_decision = decision
        if not alerted:
            emit_launch_admission_alert(decision)
            alerted = True
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        sleep_time = min(policy.queue_poll_seconds, remaining)
        log.info(
            "Launch admission blocked by %s (%s) - retrying in %.0fs",
            decision.binding_signal, decision.reason, sleep_time,
        )
        time.sleep(sleep_time)

    decision = last_decision or evaluate_launch_admission(build_snapshot(root, policy), policy)
    raise RuntimeError(
        "Launch admission: "
        f"{decision.reason}. binding_signal={decision.binding_signal}; "
        f"active_agents={decision.active_agents}; "
        f"starting_agents={decision.starting_agents}; "
        f"effective_cap={decision.effective_cap}; "
        f"load_per_cpu={decision.load_per_cpu}; "
        f"mem_available_mb={decision.mem_available_mb}; "
        f"recent_init_failures={decision.recent_init_failures}. "
        f"Timed out waiting for a launch slot after {timeout:.0f}s."
    )


def emit_launch_admission_alert(decision: LaunchAdmissionDecision) -> None:
    try:
        from bobi.events.publish import post_event

        payload = asdict(decision)
        payload["count"] = decision.active_agents
        payload["cap"] = decision.effective_cap
        payload["text"] = (
            "Launch admission queued: "
            f"{decision.reason} (binding_signal={decision.binding_signal})."
        )
        post_event("system/concurrency.cap.queued", payload)
    except Exception:
        log.warning("Failed to emit launch admission alert", exc_info=True)


def _health_path(root: Path) -> Path:
    return paths.state_path(root) / INIT_HEALTH_FILE


def load_init_health(root: Path, window_seconds: int) -> InitHealthCounts:
    cutoff = time.time() - window_seconds
    events = _read_health_events(root)
    failures = sum(
        1 for event in events
        if event["kind"] == INIT_FAILURE and event["ts"] >= cutoff
    )
    successes = sum(
        1 for event in events
        if event["kind"] == INIT_SUCCESS and event["ts"] >= cutoff
    )
    return InitHealthCounts(
        recent_init_failures=failures,
        recent_init_successes=successes,
    )


def record_init_health(
    root: Path,
    kind: str,
    *,
    now: float | None = None,
    keep_seconds: int = 600,
) -> None:
    if kind not in (INIT_FAILURE, INIT_SUCCESS):
        return
    ts = time.time() if now is None else now
    cutoff = ts - keep_seconds
    with _health_file_lock(root):
        events = [event for event in _read_health_events(root) if event["ts"] >= cutoff]
        events.append({"kind": kind, "ts": ts})
        path = _health_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        tmp.write_text(json.dumps({"events": events}, indent=2))
        os.replace(tmp, path)


def _read_health_events(root: Path) -> list[dict]:
    path = _health_path(root)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        events = data.get("events", [])
        if not isinstance(events, list):
            return []
        result = []
        for event in events:
            if not isinstance(event, dict):
                continue
            kind = event.get("kind")
            if kind not in (INIT_FAILURE, INIT_SUCCESS):
                continue
            try:
                ts = float(event.get("ts", 0))
            except (TypeError, ValueError):
                continue
            result.append({"kind": kind, "ts": ts})
        return result
    except (json.JSONDecodeError, OSError, TypeError):
        return []


@contextmanager
def _health_file_lock(root: Path):
    lock_path = paths.state_path(root) / f"{INIT_HEALTH_FILE}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lock_file:
        try:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def classify_init_failure(error: str) -> bool:
    normalized = error.lower()
    if "initialize" not in normalized:
        return False
    if "_send_control_request" not in normalized and "control request" not in normalized:
        return False
    if "timeout" not in normalized and "timed out" not in normalized and "deadline" not in normalized:
        return False
    if "total_cost_usd: 0" not in normalized and "total_cost_usd=0" not in normalized:
        return False
    if "model_usage: {}" not in normalized and "model_usage={}" not in normalized:
        return False
    return True
