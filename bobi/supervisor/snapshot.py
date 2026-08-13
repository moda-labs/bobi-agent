"""Tier-1 heartbeat assembly (issue #5).

Pure-ish assembly of the compact status snapshot the sidecar publishes on every
poll interval. The manager-liveness fields (status/healthy/idle/restart) are the
headline; ``resources`` / ``versions`` / ``expectations`` are best-effort
enrichment that fails open to null/empty rather than ever blocking a heartbeat.

``manager.status`` here is the sidecar's OUTSIDE verdict (see
:mod:`.probe`), not a raw echo of ``/health``.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from .supervision import SupervisorState

log = logging.getLogger(__name__)

# The sidecar's own version, surfaced in supervisor.version + versions. In the
# container this ships as bundled source with no installed distribution, so an
# env stamp (set by the image build) or this literal is the source of truth.
#
# 0.2.0 adds the single-agent view's six commands (runs / overview /
# run_details / resume_run / remind_run / close_run) and the additive `detail`
# arg on `transcript`. Minor, because the change is purely additive per
# docs/ADMIN_PROTOCOL.md's compatibility promise - and readable by a consumer,
# which is the point: an instance still on 0.1.0 drops those verbs with no
# reply, and this is how the runtime tells "too old" from "not answering".
SUPERVISOR_VERSION = "0.2.0"


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).isoformat()


def _bobi_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("bobi")
    except Exception:
        return None


def _team_package_version(project_root: Path | None) -> str | None:
    """The installed team package version, best-effort from its manifest."""
    try:
        from bobi import paths
        from bobi.config import Config
        cfg = Config.load(project_root or paths.bobi_root())
        return getattr(cfg, "version", None) or None
    except Exception:
        return None


def _resources(project_root: Path | None) -> dict:
    """Best-effort resource gauges. Missing values are null, never fatal."""
    disk_free_mb = mem_free_mb = mem_pct = None
    try:
        target = str(project_root or Path(os.environ.get("BOBI_HOME", "/")))
        disk_free_mb = round(shutil.disk_usage(target).free / (1024 * 1024), 1)
    except Exception:
        pass
    # Linux container memory via /proc/meminfo (no psutil dependency).
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = int(rest.strip().split()[0])  # kB
        total = info.get("MemTotal")
        available = info.get("MemAvailable")
        if total and available is not None:
            mem_free_mb = round(available / 1024, 1)
            mem_pct = round(100.0 * (total - available) / total, 1)
    except Exception:
        pass
    return {"disk_free_mb": disk_free_mb, "mem_free_mb": mem_free_mb,
            "mem_pct": mem_pct}


def _versions(project_root: Path | None) -> dict:
    return {
        "image": os.environ.get("BOBI_IMAGE") or os.environ.get("FLY_IMAGE_REF"),
        "team_package": _team_package_version(project_root),
        "bobi": _bobi_version(),
    }


def _expectations(project_root: Path | None) -> dict:
    """Declared subscriptions + monitor schedules - the silence-detection seam.

    What the read model later diffs against actual traffic to derive silence.
    Best-effort: an error yields empty lists (silence detection simply has less
    to assert), never a failed heartbeat.
    """
    subscriptions: list[str] = []
    monitors: list[dict] = []
    try:
        from bobi.events import discover_subscriptions
        subscriptions = sorted(set(discover_subscriptions(project_root) or []))
    except Exception:
        pass
    try:
        from bobi.config import Config
        from bobi import paths
        cfg = Config.load(project_root or paths.bobi_root())
        for mon in (getattr(cfg, "monitors", None) or []):
            if isinstance(mon, dict):
                monitors.append({"name": mon.get("name"),
                                 "schedule": mon.get("schedule")})
            else:
                monitors.append({"name": getattr(mon, "name", None),
                                 "schedule": getattr(mon, "schedule", None)})
    except Exception:
        pass
    return {"subscriptions": subscriptions, "monitors": monitors}


def _sessions(health: dict | None) -> list[dict]:
    if not health:
        return []
    out = []
    for s in health.get("sessions") or []:
        out.append({"name": s.get("name"), "role": s.get("role"),
                    "status": s.get("status")})
    return out


def build_heartbeat(*, identity: dict, state: SupervisorState,
                    derived_status: str, healthy: bool,
                    manager_pid: int | None, now: float,
                    project_root: Path | None,
                    supervisor_version: str = SUPERVISOR_VERSION) -> dict:
    """Assemble the tier-1 heartbeat snapshot."""
    health = state.health or {}
    mgr = health.get("manager") or {}

    return {
        "deployment": identity,
        "supervisor": {
            "pid": os.getpid(),
            "uptime_s": round(state.supervisor_uptime_s, 1),
            "version": supervisor_version,
        },
        "manager": {
            "status": derived_status,
            "pid": manager_pid,
            "healthy": healthy,
            "idle_seconds": mgr.get("idle_seconds"),
            "restart_count": state.restart_count,
            "last_restart_reason": state.last_restart_reason,
            "last_restart_at": state.last_restart_at,
        },
        "sessions": _sessions(health),
        # Load grace (MOD-364, additive): null unless a liveness verdict was
        # deferred THIS poll because the host is pegged by the manager's own
        # busy worker tree. A working poll clears it, so the block is an
        # instantaneous "currently deferring" flag, not an accumulating
        # counter: {active, since, spell_s, deferred, load1, ncpu,
        # busy_descendants}.
        "load_grace": state.load_grace,
        # Phase A telemetry is a pure HTTP publish with no persistent WS client,
        # and /health does not expose the manager's bus-client stats yet, so the
        # block is reported null. Populated when the sidecar gains its own WS
        # subscription (Phase B) or /health surfaces the manager's client.
        "event_client": None,
        "resources": _resources(project_root),
        "versions": _versions(project_root),
        "expectations": _expectations(project_root),
        "generated_at": _iso(now),
    }
