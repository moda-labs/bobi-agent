"""Dead-man reconciler — closes stranded sub-agent runs (MDS-65 §4.6).

The completion-delivery loop is best-effort at three points: the terminal bus
POST can be swallowed, the daemon thread carrying it can be killed at process
shutdown, and a hard crash never reaches the terminal emit at all. The registry
``state.json`` is the durable source of truth (written synchronously, before any
POST). This reconciler reads it and closes the gaps:

  1. terminal-but-unconfirmed — a terminal status is persisted but its emit never
     landed → re-emit the lifecycle event. Idempotency is via the persisted
     ``emit_confirmed`` flag: once an emit lands, the flag is set and the run is
     never re-emitted, so a healthy completion is delivered exactly once and only
     a genuinely-unconfirmed one is retried.
  2. crashed — a live status with a dead pid → mark ``crashed`` and emit
     ``agent/session.failed`` so the launcher's thread is closed instead of
     hanging on a never-arriving completion.
  3. timed out — past the run's declared ``timeout`` (+ grace) with the pid still
     alive → cancel it, mark ``failed``, emit ``agent/session.failed``.

It is event-driven, not a poll loop: run it on manager wake (and it composes with
the dead-pid sweep in ``SessionRegistry.list_active``). Idempotency is via the
persisted ``emit_confirmed`` flag — a confirmed terminal is never re-emitted, so
healthy completions are never double-delivered. (Re-emitting an unconfirmed one a
second time is possible if the bus is flapping; that is a deliberate
at-least-once trade-off — better a rare duplicate than a lost completion — not a
run_key dedup guarantee at the consumer.)
"""

from __future__ import annotations

import logging
import time

from modastack.sdk import (
    ACTIVE_STATUSES,
    FAILED_STATUSES,
    TERMINAL_COMPLETED,
    TERMINAL_CRASHED,
    TERMINAL_FAILED,
    get_registry,
    pid_alive,
)

log = logging.getLogger(__name__)

# Grace period past a run's declared timeout before the dead-man closes it —
# absorbs clock skew and a slow-but-still-alive final turn so we don't kill an
# agent whose completion is merely a few seconds late.
RECONCILE_GRACE = 300  # seconds

# Statuses that may still represent a live run worth reconciling. A suspended
# workflow (status "waiting") is deliberately dormant — its process has exited
# and a fresh one resumes it on the await event — so it has a dead pid by design
# and must NOT be crash-reconciled. Only genuinely-active statuses qualify.
_RECONCILABLE_ACTIVE = ACTIVE_STATUSES


def _default_emit(event_type: str, data: dict) -> bool:
    """Best-effort lifecycle POST; returns whether it landed."""
    try:
        from modastack.events.publish import post_event
        post_event(event_type, {k: v for k, v in data.items() if v not in (None, "")})
        return True
    except Exception as e:
        log.debug("Reconcile emit %s not posted: %s", event_type, e)
        return False


def _default_cancel(name: str) -> None:
    try:
        from modastack.subagent import cancel_agent
        cancel_agent(name)
    except Exception:
        log.debug("Reconcile cancel failed for %s", name, exc_info=True)


def _failed_payload(entry, reason: str) -> dict:
    error = entry.error or f"agent {reason}"
    label = entry.role or "Agent"
    return {
        "run_key": entry.run_key,
        "role": entry.role,
        "project": entry.project,
        "session_id": entry.session_id,
        "phase": entry.phase,
        "error": error,
        "requested_by": entry.requested_by or None,
        "text": f"{label} {reason} on {entry.run_key}: {error}",
    }


def _completed_payload(entry) -> dict:
    label = entry.role or "Agent"
    return {
        "run_key": entry.run_key,
        "role": entry.role,
        "project": entry.project,
        "session_id": entry.session_id,
        "phase": entry.phase,
        "requested_by": entry.requested_by or None,
        "text": f"{label} finished {entry.run_key}",
    }


def reconcile_sessions(registry=None, *, now: float | None = None,
                       emit=None, cancel=None,
                       exclude_names: set[str] | None = None) -> list[dict]:
    """Sweep the registry once, closing stranded runs. Returns the actions taken
    (one dict per closed/re-emitted run) — useful for logging and tests.

    ``exclude_names`` are skipped entirely — the caller passes the manager's own
    session name on startup so the reconciler never reports the previous
    manager's own exit as a crashed *sub-agent* (the new manager is about to
    re-claim that entry anyway).

    ``emit``/``cancel`` are injectable for testing; they default to the real bus
    POST and ``cancel_agent``. Pure over its inputs otherwise.
    """
    registry = registry or get_registry()
    now = now if now is not None else time.time()
    emit = emit or _default_emit
    cancel = cancel or _default_cancel
    exclude_names = exclude_names or set()

    actions: list[dict] = []
    for entry in registry.list_all():
        if entry.name in exclude_names:
            continue
        status = entry.status

        # (1) Terminal but un-emitted → re-emit. Gated on emit_confirmed so a
        # healthy, already-delivered completion is never re-sent.
        if status in (TERMINAL_COMPLETED,) + tuple(FAILED_STATUSES):
            if entry.emit_confirmed:
                continue
            if status == TERMINAL_COMPLETED:
                landed = emit("agent/session.completed", _completed_payload(entry))
            else:
                reason = "crashed" if status == TERMINAL_CRASHED else "failed"
                landed = emit("agent/session.failed", _failed_payload(entry, reason))
            if landed:
                registry.update(entry.name, emit_confirmed=True)
            actions.append({"name": entry.name, "action": "reemit",
                            "status": status, "emitted": landed})
            continue

        if status not in _RECONCILABLE_ACTIVE:
            continue

        # (2) Live status, dead pid → crashed + emit.
        if entry.pid and not pid_alive(entry.pid):
            registry.mark_terminal(
                entry.name, TERMINAL_CRASHED,
                error=(entry.error
                       or "agent process died without reporting a terminal status"),
                reconciled=True,
            )
            landed = emit("agent/session.failed",
                          _failed_payload(registry.get(entry.name), "crashed"))
            if landed:
                registry.update(entry.name, emit_confirmed=True)
            actions.append({"name": entry.name, "action": "crashed",
                            "emitted": landed})
            continue

        # (3) Past declared deadline, pid still alive → cancel + failed + emit.
        if entry.timeout and entry.started_at and (
            entry.started_at + entry.timeout + RECONCILE_GRACE < now
        ):
            cancel(entry.name)
            registry.mark_terminal(
                entry.name, TERMINAL_FAILED,
                error=(f"agent exceeded its {entry.timeout}s timeout "
                       "without reporting a terminal status"),
                reconciled=True,
            )
            landed = emit("agent/session.failed",
                          _failed_payload(registry.get(entry.name), "timed out"))
            if landed:
                registry.update(entry.name, emit_confirmed=True)
            actions.append({"name": entry.name, "action": "timeout",
                            "emitted": landed})

    return actions
