"""Spend governor — caps agent invocations per deployment per rolling hour.

Classification-free backstop: bounds any runaway loop in invocations
rather than relying on the Phase-1 loop detector to classify it.
On breach, new agent launches are blocked and a system alert event is
emitted so monitors and operators are notified immediately.

State is persisted to disk so the cap survives process restarts within
the rolling window.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Default: 50 agent invocations per rolling hour. Override in agent.yaml
# with `spend_cap: <int>`.
DEFAULT_CAP = 50

# Rolling window size in seconds (1 hour).
WINDOW_SECONDS = 3600


def _state_path(project_path: Path) -> Path:
    """Path to the spend governor state file."""
    from modastack import paths
    return paths.state_path(project_path) / "spend_governor.json"


def _load_state(state_file: Path) -> list[float]:
    """Load invocation timestamps from disk. Returns empty list on any error."""
    if not state_file.exists():
        return []
    try:
        data = json.loads(state_file.read_text())
        if isinstance(data, dict):
            return [float(t) for t in data.get("invocations", []) if isinstance(t, (int, float))]
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    return []


def _save_state(state_file: Path, timestamps: list[float]) -> None:
    """Persist invocation timestamps to disk. Best-effort, never raises."""
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"invocations": timestamps}))
    except OSError:
        log.debug("Failed to persist spend governor state", exc_info=True)


def _prune(timestamps: list[float], now: float) -> list[float]:
    """Remove timestamps older than the rolling window."""
    cutoff = now - WINDOW_SECONDS
    return [t for t in timestamps if t > cutoff]


def check_spend_cap(project_path: Path, cap: int) -> tuple[bool, int]:
    """Check whether a new invocation would exceed the spend cap.

    Returns (allowed, current_count). When allowed is False, the caller
    must block the launch and emit an alert.
    """
    state_file = _state_path(project_path)
    now = time.time()
    timestamps = _prune(_load_state(state_file), now)
    count = len(timestamps)

    if count >= cap:
        # Persist the pruned state (removes expired entries) but do NOT
        # record a new timestamp — the launch is being blocked.
        _save_state(state_file, timestamps)
        return False, count

    return True, count


def record_invocation(project_path: Path) -> None:
    """Record a new agent invocation timestamp."""
    state_file = _state_path(project_path)
    now = time.time()
    timestamps = _prune(_load_state(state_file), now)
    timestamps.append(now)
    _save_state(state_file, timestamps)


def emit_spend_cap_alert(project_path: Path, count: int, cap: int) -> None:
    """Emit a system/spend.cap.breached event to the event bus.

    Best-effort — a failed alert must never block the governor decision.
    """
    try:
        from modastack.events.publish import post_event
        post_event("system/spend.cap.breached", {
            "count": count,
            "cap": cap,
            "window_seconds": WINDOW_SECONDS,
            "text": (
                f"Spend governor triggered: {count} agent invocations in the "
                f"last {WINDOW_SECONDS // 60} minutes (cap: {cap}). "
                f"New agent launches are blocked until invocations age out."
            ),
        }, project_path=project_path)
    except Exception:
        log.warning("Failed to emit spend cap alert", exc_info=True)
