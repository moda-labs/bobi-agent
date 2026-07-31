"""Supervisor tunables — a faithful port of the public watchdog's config.

The env var names are deliberately kept identical to the former
``bobi/watchdog.py`` (``WATCHDOG_*``) so operator muscle memory and existing
Fly/K8s env carried over unchanged when the fleet moved from the in-repo
watchdog to the sidecar. The sidecar owns its own copy of this config because
the public watchdog was deleted in bobi-agent #720 (see issue #5): this is a
port, not an import.
"""

from __future__ import annotations

import dataclasses
import logging
import os

log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("supervisor: ignoring non-numeric %s=%r, using %s",
                    name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("supervisor: ignoring non-numeric %s=%r, using %s",
                    name, raw, default)
        return default


def _env_backoff(name: str, default: tuple[float, ...]) -> tuple[float, ...]:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        vals = tuple(float(x) for x in raw.replace(",", " ").split())
        return vals or default
    except ValueError:
        log.warning("supervisor: ignoring malformed %s=%r, using %s",
                    name, raw, default)
        return default


@dataclasses.dataclass
class SupervisorConfig:
    """Supervision + telemetry tunables (ported ``WATCHDOG_*`` defaults)."""

    poll_interval: float = 30.0       # WATCHDOG_POLL_INTERVAL
    stall_threshold: float = 600.0    # WATCHDOG_STALL_THRESHOLD (10 min)
    confirm_polls: int = 2            # WATCHDOG_CONFIRM_POLLS
    max_restarts: int = 3             # WATCHDOG_MAX_RESTARTS
    restart_window: float = 1800.0    # WATCHDOG_RESTART_WINDOW (30 min)
    backoff: tuple[float, ...] = (30.0, 60.0, 120.0)  # WATCHDOG_BACKOFF
    min_healthy_uptime: float = 120.0  # WATCHDOG_MIN_HEALTHY_UPTIME
    term_grace: float = 10.0          # SIGTERM->SIGKILL grace on the child
    # Status-file freshness bound for the OUTSIDE verdict: a manager whose
    # session state.json has not advanced for longer than this (while the
    # health body still claims an active turn) is corroborated as wedged.
    # Defaults to the stall threshold so the two liveness signals agree.
    status_file_stale: float = 600.0  # WATCHDOG_STATUS_FILE_STALE
    # Incident alerting (issue #4): ``machine_restart_cap`` mirrors the
    # orchestrator's machine-restart bound (the fly.toml [[restart]]
    # max_retries from #3, stamped by the provisioner) so the exhaustion alert
    # can say how close the instance is to parking; it changes no restart
    # decision here.
    machine_restart_cap: int = 10      # WATCHDOG_MACHINE_RESTART_CAP

    @classmethod
    def from_env(cls) -> "SupervisorConfig":
        stall = _env_float("WATCHDOG_STALL_THRESHOLD", 600.0)
        return cls(
            poll_interval=_env_float("WATCHDOG_POLL_INTERVAL", 30.0),
            stall_threshold=stall,
            confirm_polls=_env_int("WATCHDOG_CONFIRM_POLLS", 2),
            max_restarts=_env_int("WATCHDOG_MAX_RESTARTS", 3),
            restart_window=_env_float("WATCHDOG_RESTART_WINDOW", 1800.0),
            backoff=_env_backoff("WATCHDOG_BACKOFF", (30.0, 60.0, 120.0)),
            min_healthy_uptime=_env_float("WATCHDOG_MIN_HEALTHY_UPTIME", 120.0),
            term_grace=_env_float("WATCHDOG_TERM_GRACE", 10.0),
            status_file_stale=_env_float("WATCHDOG_STATUS_FILE_STALE", stall),
            machine_restart_cap=_env_int("WATCHDOG_MACHINE_RESTART_CAP", 10),
        )
