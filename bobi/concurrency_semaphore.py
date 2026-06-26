"""Concurrency semaphore — caps simultaneous subagent sessions.

Counts active (starting/running/idle) non-manager, non-monitor sessions
in the SessionRegistry and blocks new launches when the cap is reached.
Launches beyond the cap queue: the caller polls until a slot opens or
the timeout expires.

State is read live from the registry — no separate persistence needed.
"""

from __future__ import annotations

import logging
import time

from bobi.sdk import get_registry

log = logging.getLogger(__name__)

# Default cap when agent.yaml omits max_concurrent_agents (or sets 0).
DEFAULT_CAP = 2

# Polling interval (seconds) when waiting for a slot to open.
_POLL_INTERVAL = 5.0

# Roles excluded from the concurrency count: managers are infrastructure,
# monitors are short-lived read-only checks — neither should consume a
# concurrency slot or be blocked by the semaphore.
_EXCLUDED_ROLES = frozenset(("manager", "monitor"))


def count_active_agents() -> int:
    """Count active subagent sessions (excluding managers and monitors)."""
    registry = get_registry()
    return sum(
        1 for entry in registry.list_active()
        if entry.role not in _EXCLUDED_ROLES
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
