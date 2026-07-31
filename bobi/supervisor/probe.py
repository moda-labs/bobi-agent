"""Outside-in liveness probing and the OUTSIDE manager verdict (issue #5).

The manager cannot report its own wedge or death - an in-process probe fails
precisely when it is needed. So the sidecar fuses three signals it reads from
*outside* the manager into one verdict:

1. the ``/health`` body (what the manager says about itself),
2. process-table liveness of the manager PID (``state/manager.pid``), and
3. status-file freshness (the manager session's ``state.json`` ``last_activity``).

:func:`derive_manager_status` maps those into the heartbeat's
``manager.status`` - ``running`` / ``idle`` / ``starting`` / ``wedged`` /
``down`` - so ``wedged`` and ``down`` are observable states the manager itself
cannot announce.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .config import SupervisorConfig
from .supervision import ACTIVE_STATES, is_wedged

log = logging.getLogger(__name__)


def pid_alive(pid: int) -> bool:
    """Process-table liveness. ``kill(pid, 0)`` never signals, only probes."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; we just may not signal it. Alive for our purposes.
        return True
    except (ValueError, OverflowError):
        return False


def read_manager_pid(project_root: Path | None) -> int | None:
    """The manager PID the manager wrote to ``state/manager.pid``, if any."""
    from bobi import paths
    try:
        return int(paths.manager_pid_path(project_root).read_text().strip())
    except (OSError, ValueError):
        return None


def manager_pid_alive(project_root: Path | None) -> bool:
    """Whether the manager PID on disk is a live process."""
    pid = read_manager_pid(project_root)
    return pid is not None and pid_alive(pid)


def status_file_age(project_root: Path | None, session: str | None,
                    now: float | None = None) -> float | None:
    """Seconds since the manager session's ``state.json`` last advanced.

    Read straight from the on-disk registry (not the health body) so it is an
    INDEPENDENT signal: it stays truthful even when the in-process health
    server keeps answering. Returns None when the session/name is unknown - an
    absent signal is never treated as a wedge (fail-open).
    """
    if not session:
        return None
    from bobi.sdk import SessionRegistry
    try:
        entry = SessionRegistry(project_root).get(session)
    except Exception:
        return None
    if entry is None or entry.last_activity is None:
        return None
    return max(0.0, (now if now is not None else time.time()) - entry.last_activity)


def manager_session_name(health: dict | None) -> str | None:
    """The entry-point (director) session name from the health body."""
    if not health:
        return None
    mgr = health.get("manager") or {}
    return mgr.get("session")


def derive_manager_status(health: dict | None, *, child_alive: bool,
                          manager_pid_alive: bool, status_file_age: float | None,
                          ever_healthy: bool, config: SupervisorConfig) -> str:
    """Fuse the outside signals into the heartbeat ``manager.status``.

    - ``down``: nothing is running (the supervised child has exited AND the
      manager PID is not in the process table).
    - ``wedged``: a process IS running but is not making progress - either the
      health body confirms a stalled active turn, the status file corroborates
      a stall, or the manager was healthy and has gone silent.
    - ``starting`` / ``running`` / ``idle``: the healthy path, trusting the
      health body once a process is alive.
    """
    mgr = (health or {}).get("manager") or {}
    reported = mgr.get("status")
    idle = mgr.get("idle_seconds")

    # Nothing alive at all - the clearest terminal state.
    if not child_alive and not manager_pid_alive:
        return "down"

    if health is not None:
        # Health answered. Trust it unless an outside signal contradicts.
        if is_wedged(reported, idle, config.stall_threshold):
            return "wedged"
        # Status-file corroboration: the body claims an active turn but the
        # on-disk session state has not advanced past the freshness bound.
        if (reported in ACTIVE_STATES and status_file_age is not None
                and status_file_age > config.status_file_stale):
            return "wedged"
        if reported in ("running", "idle", "starting"):
            return reported
        # Some other/terminal status reported while a process is still alive;
        # surface it rather than inventing one.
        return reported or "starting"

    # Health did not answer (port file missing or /health unreachable).
    if not ever_healthy:
        # Still booting - the manager has never been addressable, so an
        # unreachable probe is not a wedge (fail-open during boot).
        return "starting"
    # Was healthy, now silent, but a process is still around: a dead health
    # thread under a live process, or a wedged director. Observable only from
    # outside.
    return "wedged"
