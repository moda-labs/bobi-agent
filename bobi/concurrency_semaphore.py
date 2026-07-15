"""Concurrency semaphore — caps simultaneous subagent sessions.

Counts active (starting/running/idle) non-infrastructure sessions in the
SessionRegistry and blocks new launches when the cap is reached.
Launches beyond the cap queue: the caller polls until a slot opens or
the timeout expires.

State is read live from the registry — no separate persistence needed.
"""

from __future__ import annotations

import logging
import time

from bobi import paths
from bobi.sdk import get_registry

log = logging.getLogger(__name__)

# Default cap when agent.yaml omits max_concurrent_agents (or sets 0).
DEFAULT_CAP = 2

# Polling interval (seconds) when waiting for a slot to open.
_POLL_INTERVAL = 5.0

# Roles excluded from the concurrency count by role alone. The manager is
# excluded by session name because regular workers can also use the entry role.
_EXCLUDED_ROLES = frozenset(("monitor",))


def _excluded_session_names(root=None) -> frozenset[str]:
    """Session names excluded from the concurrency count for a runtime."""
    try:
        root = root or paths.bound_root()
        if root is None:
            return frozenset()
        from bobi.service import manager_session_name
        return frozenset((manager_session_name(root),))
    except Exception:
        log.warning("Failed to resolve manager session for concurrency semaphore",
                    exc_info=True)
        return frozenset()


def is_excluded_from_concurrency(entry, excluded_session_names=None) -> bool:
    """Return True for infrastructure sessions that should not consume slots."""
    session_names = (
        excluded_session_names
        if excluded_session_names is not None
        else _excluded_session_names()
    )
    return entry.role in _EXCLUDED_ROLES or entry.name in session_names


def count_active_agents() -> int:
    """Count active subagent sessions, excluding infrastructure roles."""
    registry = get_registry()
    excluded_session_names = _excluded_session_names()
    return sum(
        1 for entry in registry.list_active()
        if not is_excluded_from_concurrency(entry, excluded_session_names)
    )


def check_concurrency(cap: int) -> tuple[bool, int]:
    """Check whether a new agent launch would exceed the concurrency cap.

    Returns (allowed, current_count). When allowed is False, the caller
    should queue or block until a slot opens.
    """
    current = count_active_agents()
    return current < cap, current


def wait_for_slot(cap: int, timeout: float) -> bool:
    """Block until a concurrency slot opens, or timeout expires.

    Returns True if a slot opened, False if the timeout was exhausted.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        allowed, current = check_concurrency(cap)
        if allowed:
            return True
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        sleep_time = min(_POLL_INTERVAL, remaining)
        log.info(
            "Concurrency cap reached (%d/%d active agents) — "
            "queued, retrying in %.0fs",
            current, cap, sleep_time,
        )
        time.sleep(sleep_time)
    return False


def emit_concurrency_cap_alert(count: int, cap: int) -> None:
    """Emit a system/concurrency.cap.queued event. Best-effort."""
    try:
        from bobi.events.publish import post_event
        post_event("system/concurrency.cap.queued", {
            "count": count,
            "cap": cap,
            "text": (
                f"Concurrency semaphore: {count} agents running "
                f"(cap: {cap}). New launch queued."
            ),
        })
    except Exception:
        log.warning("Failed to emit concurrency cap alert", exc_info=True)
