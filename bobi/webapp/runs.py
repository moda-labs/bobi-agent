"""The unified runs read model — everything an agent did, as one list.

The agent page asks two questions in order: is the agent running, and what
failed. The second is answered by a single table, so the three places an
agent's work is recorded have to become one shape:

- **sessions** (`SessionRegistry`) — the manager and every subagent it ran
- **workflow runs** (`run/state/workflow/runs/*.json`) — including the ones
  suspended waiting for an event that never came
- **monitor run records** (`run/state/monitor_runs/*.json`) — one per firing

Deliberately NOT here: decisions and raw event deliveries. They are log lines,
not runs — no status, no cost, nothing to open (`bobi agent events` still
serves them).

Every read takes an explicit root: one webapp process serves every team on the
machine and binds none of them.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

# Status vocabulary. `stalled` is the one derived state: a workflow run
# suspended long enough that it is waiting on something that is not coming,
# which is a human's problem and so belongs under the Failed tab.
RUNNING = "running"
IDLE = "idle"
DONE = "done"
FAILED = "failed"
CRASHED = "crashed"
STALLED = "stalled"

# What the FAILED tab holds: everything that needs a human.
FAILED_STATUSES = (FAILED, CRASHED, STALLED)
# What the RUNNING tab holds. `idle` is a live-but-waiting manager or a
# freshly suspended workflow — present, not working.
LIVE_STATUSES = (RUNNING,)

# How long a workflow run may sit suspended before it reads as stalled rather
# than waiting. A constant, not config: the threshold is a judgement about
# human attention ("nobody is coming back to this today"), not about any
# particular workflow. Revisit if a real workflow legitimately waits longer.
STALLED_AFTER_SECONDS = 24 * 3600

# Rows returned in one response. The fold reads everything on disk and counts
# it all; only the payload is capped, so the tab counts stay true even when
# the list is cut. Real pagination waits for a home that outgrows this.
DEFAULT_LIMIT = 200


def _iso(epoch: float | None) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _epoch(iso: str) -> float:
    """Epoch seconds from an ISO timestamp, 0.0 when unparseable.

    Workflow runs record naive local timestamps (`time.strftime`) and monitor
    records record aware UTC ones; a naive value is read as local time, which
    is what its writer meant.
    """
    if not iso:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def _duration(start: float, end: float) -> float | None:
    if not start or not end or end < start:
        return None
    return round(end - start, 3)


def _row(*, run_kind: str, key: str, status: str, title: str, origin: str,
         started_at: float, duration: float | None, error: str = "",
         session_id: str = "", run_id: str = "", tokens: int = 0,
         cost_usd: float = 0.0, est_cost_usd: float = 0.0,
         detail: dict | None = None) -> dict:
    """One row. Fields a source cannot know are empty/zero, never omitted, so
    render code branches on value and never on key presence."""
    return {
        "kind": run_kind,
        "key": key,
        "status": status,
        "title": title,
        "origin": origin,
        "started_at": _iso(started_at),
        "duration_seconds": duration,
        "tokens": tokens,
        "cost_usd": round(cost_usd, 6),
        "est_cost_usd": round(est_cost_usd, 6),
        "error": error,
        "session_id": session_id,
        "run_id": run_id,
        "detail": detail or {},
        # Sort key only — stripped before the response goes out.
        "_started": started_at,
    }


# --- sessions ---------------------------------------------------------------

def _session_status(entry) -> str:
    from bobi.sdk import (
        ACTIVE_STATUSES,
        TERMINAL_CRASHED,
        TERMINAL_FAILED,
    )

    if entry.status == "idle":
        return IDLE
    if entry.status in ACTIVE_STATUSES:
        return RUNNING
    if entry.status in (TERMINAL_FAILED, "error"):
        return FAILED
    if entry.status == TERMINAL_CRASHED:
        return CRASHED
    # completed/done, plus anything outside the vocabulary a writer recorded
    # (stopped, cancelled): over, and not a failure we can claim.
    return DONE


def _session_origin(entry, *, is_manager: bool) -> str:
    """What kicked this session off, for the row's faint sub-line."""
    if is_manager:
        return "manager · entry point"
    if entry.role == "monitor":
        return f"monitor · {entry.run_key}" if entry.run_key else "monitor"
    requester = entry.requested_by or {}
    by = requester.get("session") or requester.get("role") or ""
    role = entry.role or "agent"
    if by:
        return f"{role} · dispatched by {by}"
    return f"{role} · dispatched by manager"


def _session_rows(entries, *, manager_name: str,
                  usage_by_session: dict) -> list[dict]:
    rows = []
    for entry in entries:
        is_manager = bool(manager_name) and entry.name == manager_name
        usage = usage_by_session[entry.name]
        status = _session_status(entry)
        end = entry.terminal_at or (entry.last_activity
                                    if status not in LIVE_STATUSES else 0.0)
        rows.append(_row(
            run_kind="session",
            key=f"session:{entry.name}",
            status=status,
            title=entry.title or entry.run_key or entry.name,
            origin=_session_origin(entry, is_manager=is_manager),
            started_at=entry.started_at or 0.0,
            duration=_duration(entry.started_at or 0.0, end or 0.0),
            error=entry.error,
            session_id=entry.name,
            tokens=usage["total_tokens"],
            cost_usd=usage["cost_usd"],
            est_cost_usd=usage["estimated_cost_usd"],
            detail={"role": entry.role, "phase": entry.phase,
                    "model": entry.model, "provider": entry.provider,
                    "is_manager": is_manager},
        ))
    return rows


# --- workflow runs ----------------------------------------------------------

def _workflow_status(run, *, now: float) -> str:
    if run.status == "completed":
        return DONE
    if run.status in ("failed", "error"):
        return FAILED
    if run.status in ("waiting", "suspended"):
        # Suspended is normal; suspended for a day is a human's problem.
        started = _epoch(run.resumed_at or run.started_at)
        if started and (now - started) >= STALLED_AFTER_SECONDS:
            return STALLED
        return IDLE
    if run.status == "running":
        return RUNNING
    return DONE


def _workflow_rows(root: Path, *, now: float,
                   usage_by_session: dict) -> list[dict]:
    from bobi.workflow.state import WorkflowRun

    rows = []
    for run in WorkflowRun.list_runs(root=root):
        status = _workflow_status(run, now=now)
        trigger = (run.trigger_event or {}).get("type", "")
        started = _epoch(run.started_at)
        usage = usage_by_session.get(run.session_name) or {}
        error = ""
        if status == STALLED:
            error = (f"suspended at step {run.suspended_at_step} "
                     f"awaiting {run.await_event}" if run.await_event
                     else f"suspended at step {run.suspended_at_step}")
        rows.append(_row(
            run_kind="workflow",
            key=f"workflow:{run.run_id}",
            status=status,
            title=run.workflow_name,
            origin=(f"workflow · on {trigger}" if trigger else "workflow"),
            started_at=started,
            duration=_duration(started, _epoch(run.completed_at)),
            error=error,
            session_id=run.session_name,
            run_id=run.run_id,
            tokens=usage.get("total_tokens", 0),
            cost_usd=usage.get("cost_usd", 0.0),
            est_cost_usd=usage.get("estimated_cost_usd", 0.0),
            detail={"await_event": run.await_event,
                    "suspended_at_step": run.suspended_at_step,
                    "run_key": run.run_key, "repo": run.repo,
                    "resumable": status == STALLED or run.status == "waiting"},
        ))
    return rows


# --- monitor runs -----------------------------------------------------------

def _monitor_schedules(root: Path) -> dict:
    """monitor name -> its schedule, for the origin sub-line. Best effort: a
    registry that will not load costs the sub-line, not the row."""
    from bobi.monitors.registry import MonitorRegistry

    try:
        monitors = MonitorRegistry.load(project_path=root).all_monitors()
    except Exception:
        return {}
    schedules = {}
    for m in monitors:
        if m.at:
            times = ", ".join(f"{h:02d}:{mi:02d}" for h, mi in m.at_times)
            schedules[m.name] = f"at {times}"
        else:
            schedules[m.name] = f"every {m.interval}"
    return schedules


def _monitor_rows(root: Path, *, usage_by_session: dict) -> list[dict]:
    from bobi.monitors import run_records

    schedules = _monitor_schedules(root)
    rows = []
    for record in run_records.load_all(root):
        failed = record.outcome == run_records.FAILED
        started = _epoch(record.started_at)
        schedule = schedules.get(record.monitor, "")
        usage = usage_by_session.get(record.session_ref) or {}
        note = record.reason
        if not note and record.outcome == run_records.NOTIFIED:
            note = (f"published {record.published} event(s)"
                    if record.published else "")
        rows.append(_row(
            run_kind="monitor",
            key=f"monitor:{record.run_id}",
            status=FAILED if failed else DONE,
            title=record.monitor,
            origin=f"monitor · {schedule}" if schedule else "monitor",
            started_at=started,
            duration=_duration(started, _epoch(record.ended_at)),
            error=record.reason if failed else "",
            session_id=record.session_ref,
            run_id=record.run_id,
            tokens=usage.get("total_tokens", 0),
            cost_usd=usage.get("cost_usd", 0.0),
            est_cost_usd=usage.get("estimated_cost_usd", 0.0),
            detail={"outcome": record.outcome, "note": note,
                    "flavor": record.flavor,
                    "script_cache_mode": record.script_cache_mode,
                    "published": record.published},
        ))
    return rows


# --- the fold ---------------------------------------------------------------

def _matches(row: dict, status: str) -> bool:
    """`status=failed` is the FAILED TAB — everything needing a human, not
    just rows literally marked failed. Any other value matches exactly."""
    if status == FAILED:
        return row["status"] in FAILED_STATUSES
    return row["status"] == status


def build_runs(root: Path, *, manager_name: str = "", status: str = "",
               limit: int = DEFAULT_LIMIT, now: float | None = None) -> dict:
    """Everything this agent did, newest first, live runs at the top.

    ``status`` filters the payload (``running`` / ``failed`` are the tabs;
    ``failed`` covers crashed and stalled too). ``counts`` always describes
    the whole set, so the tab counts stay honest when ``limit`` cuts the list.
    """
    now = time.time() if now is None else now
    from bobi.costs import session_usage
    from bobi.sdk import SessionRegistry

    # Sessions are read once and indexed: a workflow run and a monitor record
    # both point at a session, and neither should re-walk the registry.
    entries = SessionRegistry(root).list_all(reap_dead=True)
    usage_by_session = {
        e.name: session_usage(e.model_usage, e.total_cost_usd) for e in entries
    }

    rows = _session_rows(entries, manager_name=manager_name,
                         usage_by_session=usage_by_session)
    rows += _workflow_rows(root, now=now, usage_by_session=usage_by_session)
    rows += _monitor_rows(root, usage_by_session=usage_by_session)

    # Live first, then newest first — a running job is what you came for.
    rows.sort(key=lambda r: (0 if r["status"] in LIVE_STATUSES else 1,
                             -r["_started"]))

    counts = {"all": len(rows), "running": 0, "failed": 0}
    for row in rows:
        if row["status"] in LIVE_STATUSES:
            counts["running"] += 1
        if row["status"] in FAILED_STATUSES:
            counts["failed"] += 1

    if status:
        rows = [r for r in rows if _matches(r, status)]
    truncated = len(rows) > limit
    rows = rows[:limit]
    for row in rows:
        row.pop("_started", None)
    return {"runs": rows, "counts": counts, "truncated": truncated}
