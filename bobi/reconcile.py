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
  3. timed out - past its deadline (+ grace) with the pid still alive → mark
     ``failed``, stop its whole process tree, emit ``agent/session.failed``. The
     deadline is the later of the run-level ``started_at + timeout`` and the
     ``deadline_at`` a workflow runner arms when a prompt step starts, from that
     step's own budget (#1124), so a manager restart never closes a run that is
     still inside the budget of its current prompt step.

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
from dataclasses import replace

from bobi.sdk import (
    ACTIVE_STATUSES,
    DIED_WITHOUT_TERMINAL,
    FAILED_STATUSES,
    TERMINAL_COMPLETED,
    TERMINAL_CRASHED,
    TERMINAL_FAILED,
    get_registry,
    pid_alive,
)

log = logging.getLogger(__name__)

# Grace period past a run's deadline before the dead-man closes it - absorbs
# clock skew and a slow-but-still-alive final turn so we don't kill an
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
        from bobi.events.publish import post_event
        return post_event(
            event_type, {k: v for k, v in data.items() if v not in (None, "")})
    except Exception as e:
        log.debug("Reconcile emit %s not posted: %s", event_type, e)
        return False


def _default_cancel(name: str, error: str) -> bool:
    """Stop a past-deadline run, and close its entry before signalling it.

    Closing first means a manager stopped during the wait still leaves a
    `failed` entry the next sweep can announce. Returns False when the run was
    left alone, so the caller must neither close it nor announce it: marking a
    run we did not stop `failed` would leave it alive and untracked.
    """
    from bobi.subagent import CancelIncompleteError, CancelRefusedError, cancel_agent

    try:
        return cancel_agent(name, terminal=(TERMINAL_FAILED, error))
    except CancelIncompleteError as e:
        log.warning("%s", e)
        return True
    except CancelRefusedError as e:
        log.warning("Reconcile left %s running: %s", name, e)
        return False
    except Exception:
        log.warning("Reconcile could not cancel %s", name, exc_info=True)
        # The entry is written before any signal, so it says how far this got.
        current = get_registry().get(name)
        return current is not None and current.status == TERMINAL_FAILED


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


def run_deadline(entry) -> float:
    """The epoch a live run is allowed to run until; 0.0 when none is declared.

    Never earlier than the run-level ``started_at + timeout``: ``deadline_at``
    can only extend it. That keeps a stale ``deadline_at`` - left behind when
    an await suspension is resumed and re-stamped (#826) - from closing the
    resumed run early, and leaves runs that never arm one judged as before.
    A run declared with no timeout has no deadline at all, whatever its
    runner armed.
    """
    if not entry.timeout:
        return 0.0
    base = entry.started_at + entry.timeout if entry.started_at else 0.0
    return max(base, entry.deadline_at or 0.0)


def is_dead_run(entry) -> bool:
    """An entry claiming to be running whose process is gone."""
    return (entry.status in _RECONCILABLE_ACTIVE
            and bool(entry.pid) and not pid_alive(entry.pid))


def close_dead_run(entry, registry=None, *, emit=None):
    """Crash-close one dead run and emit for it.

    Returns ``(closed_entry, emit_landed)``.

    Factored out of :func:`reconcile_sessions` because launch admission needs
    the same close: since #850 an un-keyed relaunch lands on its predecessor's
    session name, so admission is the first thing to notice a run that died
    without reporting a terminal status, and it must clear that corpse or the
    run refuses its own relaunch until the next manager start.

    Emitting here rather than leaving it to the next sweep is the point.
    Admission calls this immediately before ``register()`` replaces the entry
    wholesale - the crash record, and the un-emitted ``emit_confirmed=False``
    the sweep keys off, would be gone. Nobody would ever learn the run died.
    """
    registry = registry or get_registry()
    emit = emit or _default_emit
    # The read that produced this entry can predate a cancel, which writes the
    # entry before it signals the process. The pid is dead now, so a fresh read
    # sees that write (the same rule as SessionRegistry._reap_if_dead).
    current = registry.get(entry.name)
    if current and (current.status not in _RECONCILABLE_ACTIVE
                    or current.pid != entry.pid):
        return current, False
    error = entry.error or DIED_WITHOUT_TERMINAL
    registry.mark_terminal(entry.name, TERMINAL_CRASHED, error=error,
                           reconciled=True)
    # The crashed view is synthesized in memory, NOT re-read from state.json -
    # the same rule SessionRegistry._reap_if_dead documents. mark_terminal
    # no-ops on a missing or torn file, so a re-read can hand back the stale
    # ACTIVE entry, and admission decides whether to refuse from this value.
    closed = replace(entry, status=TERMINAL_CRASHED, pid=0, error=error,
                     terminal_at=time.time())
    landed = emit("agent/session.failed", _failed_payload(closed, "crashed"))
    if landed:
        registry.update(entry.name, emit_confirmed=True)
        closed = replace(closed, emit_confirmed=True)
    return closed, landed


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
    POST and ``cancel_agent``. ``cancel(name, error)`` closes the run with that
    error, and returning False means it was left alone. Pure over its inputs
    otherwise.
    """
    registry = registry or get_registry()
    now = now if now is not None else time.time()
    emit = emit or _default_emit
    cancel = cancel or _default_cancel
    exclude_names = exclude_names or set()

    actions: list[dict] = []
    for name in sorted(e.name for e in registry.list_all()):
        if name in exclude_names:
            continue
        # Read again, not from the list above: stopping a run earlier in this
        # sweep also stops the sub-runs under it, and a stale read would judge
        # each of them, or a relaunch on the name, as it was.
        entry = registry.get(name)
        if entry is None:
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
        if is_dead_run(entry):
            closed, landed = close_dead_run(entry, registry, emit=emit)
            if closed.status == TERMINAL_CRASHED:
                actions.append({"name": entry.name, "action": "crashed",
                                "emitted": landed})
            continue

        # (3) Past its deadline, pid still alive → failed + stop + emit.
        deadline = run_deadline(entry)
        if deadline and deadline + RECONCILE_GRACE < now:
            error = (f"agent ran past its deadline in phase {entry.phase} "
                     f"({int(now - deadline)}s over) without reporting a "
                     "terminal status")
            if cancel(entry.name, error) is False:
                continue
            # The wait can last 15s. Past it the entry may be gone, or `failed` may
            # have let a relaunch take the name. Either way the record is no longer
            # this run's to touch, so the run is announced from what was read, as
            # launch admission does for a dead one.
            current = registry.get(entry.name)
            still_this_run = current is not None and current.started_at == entry.started_at
            if still_this_run:
                if current.status in _RECONCILABLE_ACTIVE:
                    registry.mark_terminal(entry.name, TERMINAL_FAILED,
                                           error=error, reconciled=True)
                elif current.status == TERMINAL_FAILED:
                    error = current.error or error  # cancel's, which names any survivors
                else:
                    continue  # it finished by itself, and the next sweep re-emits that
            closed = replace(entry, status=TERMINAL_FAILED, pid=0, error=error)
            landed = emit("agent/session.failed", _failed_payload(closed, "timed out"))
            if landed and still_this_run:
                registry.update(entry.name, emit_confirmed=True)
            actions.append({"name": entry.name, "action": "timeout",
                            "emitted": landed})

    return actions
