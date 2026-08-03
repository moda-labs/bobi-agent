"""Agent state: the one question the page answers first.

The dashboard card and the old health fold both answered "is there a live
pid?", which is not the same question as "is this agent working". A manager
whose process is alive but whose health server stopped answering — wedged on a
turn, SIGSTOPped, out of file descriptors — passed both checks and rendered as
healthy. That is the single worst failure this page can have: the state line is
the first thing read, and a green one is taken at face value.

So state is a tri-state, and the third value costs a probe:

- ``running`` — pid alive and the manager's own health endpoint answers.
- ``stopped`` — no live pid.
- ``not_responding`` — pid alive, probe does not answer. A human's problem.

Alongside it, the strip's telemetry ``segments``. They are best-effort by
design: a segment whose data this machine cannot produce is **omitted, never
faked**, so the strip shows fewer readings rather than confident wrong ones.

Values go out raw (epochs, seconds, counts) with a ``kind`` saying how to read
them. Formatting is the browser's job — it knows the viewer's timezone and the
server may not share it.
"""

from __future__ import annotations

import time
from pathlib import Path

RUNNING = "running"
STOPPED = "stopped"
NOT_RESPONDING = "not_responding"

# How long to wait for the manager's health endpoint. A wedged manager is the
# case that costs the full budget: a stopped process refuses the connection
# instantly, while a SIGSTOPped one lets the kernel accept and then answers
# nothing. The page polls this, so the ceiling is a UI budget, not a network
# one — long enough not to call a busy manager dead, short enough that the
# strip never feels hung.
PROBE_TIMEOUT_SECONDS = 1.5

# Segment value kinds, for the renderer:
#   duration — seconds elapsed ("2d 4h")
#   time     — epoch seconds at a point in time ("14:21", "today 10:12")
#   count    — an integer
#   text     — already words; show verbatim
DURATION = "duration"
TIME = "time"
COUNT = "count"
TEXT = "text"


def _segment(key: str, label: str, kind: str, value, note: str = "") -> dict:
    return {"key": key, "label": label, "kind": kind, "value": value,
            "note": note}


# --- the probe ---------------------------------------------------------------

def probe_port(root: Path) -> int:
    """The port the manager's health server wrote, or 0 when there is none.

    Every read takes an explicit root: one webapp process serves every team on
    the machine and binds none of them.
    """
    from bobi import paths

    try:
        return int((paths.state_path(root) / "manager-health.port")
                   .read_text().strip())
    except (OSError, ValueError):
        return 0


def probe(root: Path, *, timeout: float | None = None):
    """Ask the manager how it is.

    Three outcomes, and the third is the reason this function exists:
    ``(True, payload)`` it answered, ``(False, None)`` it did not, and
    ``(None, None)`` there was nothing to ask — no port file, so this manager
    has no health server (an older build, or one that failed to start it).

    "No probe" must never read as "failed probe": that would flip every such
    manager to NOT RESPONDING on the strength of a missing file. The caller
    falls back to the pidfile view instead.
    """
    # Read at call time, not bound as a default, so the ceiling stays one
    # patchable knob rather than a value frozen at import.
    timeout = PROBE_TIMEOUT_SECONDS if timeout is None else timeout
    port = probe_port(root)
    if not port:
        return None, None
    from bobi import manager_health

    payload = manager_health.health(f"http://127.0.0.1:{port}", timeout=timeout)
    if payload is None:
        return False, None
    return True, payload


# --- segments ----------------------------------------------------------------

def _running_segments(*, pid: int, manager_entry, live_runs: int,
                      last_activity: float, now: float) -> list[dict]:
    segments = []
    started = getattr(manager_entry, "started_at", 0.0) or 0.0
    if started and now >= started:
        segments.append(_segment("uptime", "Uptime", DURATION, now - started))
    if pid:
        segments.append(_segment("manager_pid", "Manager pid", TEXT, str(pid)))
    segments.append(_segment("live_runs", "Live runs", COUNT, live_runs))
    if last_activity:
        segments.append(_segment("last_activity", "Last activity", TIME,
                                 last_activity))
    return segments


def _not_responding_segments(*, pid: int, port: int,
                             last_activity: float) -> list[dict]:
    """The diagnosis. Note what is NOT here: the prototype's `failing 12m`.

    A failure duration needs a memory of when the probe first failed, and this
    process has none — it answers each request from disk and holds nothing
    between them. The honest reading of "how long has this been wedged" is
    LAST ACTIVITY, which is a real recorded fact, so the probe segment stays
    qualitative rather than inventing a stopwatch that started when the
    browser happened to poll.
    """
    segments = [_segment("manager_pid", "Manager pid", TEXT, str(pid),
                         note="alive")]
    segments.append(_segment(
        "health_probe", "Health probe", TEXT,
        f"no answer on :{port}" if port else "no answer"))
    if last_activity:
        segments.append(_segment("last_activity", "Last activity", TIME,
                                 last_activity))
    return segments


def _stopped_segments(manager_entry) -> list[dict]:
    """SINCE · EXIT · WAS UP, from the manager's last terminal record.

    An agent that has never run has no such record, and all three segments are
    simply absent — a fresh agent's strip says STOPPED and nothing else, which
    is the whole truth about it.
    """
    if manager_entry is None:
        return []
    segments = []
    terminal_at = getattr(manager_entry, "terminal_at", 0.0) or 0.0
    started = getattr(manager_entry, "started_at", 0.0) or 0.0
    if terminal_at:
        segments.append(_segment("since", "Since", TIME, terminal_at))
    from bobi.sdk import TERMINAL_STATUSES

    error = getattr(manager_entry, "error", "") or ""
    status = getattr(manager_entry, "status", "") or ""
    if error:
        segments.append(_segment("exit", "Exit", TEXT, error))
    elif status in TERMINAL_STATUSES:
        # "completed" is a clean exit in the strip's words; anything else
        # (crashed, failed) is reported as the registry recorded it.
        segments.append(_segment("exit", "Exit", TEXT,
                                 "clean" if status in ("completed", "done")
                                 else status))
    # A NON-terminal status here (a manager whose process is gone but whose
    # record was never closed) reports no exit at all. It is a live-sounding
    # word — "idle" — for a process that is not running, and rendering it as
    # `Exit: idle` is precisely the confident wrong reading these segments
    # exist to avoid.
    if terminal_at and started and terminal_at >= started:
        segments.append(_segment("was_up", "Was up", DURATION,
                                 terminal_at - started))
    return segments


# --- the fold ----------------------------------------------------------------

def build_state(*, running: bool, pid: int, root: Path, manager_entry,
                live_runs: int, last_activity: float,
                now: float | None = None) -> dict:
    """State, its one-line detail, and the strip's segments.

    ``detail`` is prose for a human: on NOT RESPONDING it says what was tried
    and failed, which is the difference between a state word and a diagnosis.
    """
    now = time.time() if now is None else now

    if not running:
        return {
            "state": STOPPED,
            "detail": "the manager process is not running",
            "segments": _stopped_segments(manager_entry),
            "probe_ok": None,
            "idle_seconds": None,
        }

    answered, payload = probe(root)
    port = probe_port(root)

    if answered is False:
        return {
            "state": NOT_RESPONDING,
            "detail": (f"manager pid {pid} is alive but its health endpoint "
                       f"on port {port} did not answer within "
                       f"{PROBE_TIMEOUT_SECONDS:g}s"),
            "segments": _not_responding_segments(
                pid=pid, port=port, last_activity=last_activity),
            "probe_ok": False,
            "idle_seconds": None,
        }

    # Answered, or there was no endpoint to ask. Both are RUNNING — see
    # probe() on why a missing port file is not evidence of anything.
    detail = ("the manager is running and its health endpoint answers"
              if answered else
              "the manager process is running; it publishes no health "
              "endpoint, so this is the pidfile view")
    idle_seconds = None
    if payload:
        manager_block = payload.get("manager") or {}
        raw = manager_block.get("idle_seconds")
        if isinstance(raw, (int, float)):
            idle_seconds = float(raw)
    return {
        "state": RUNNING,
        "detail": detail,
        "segments": _running_segments(
            pid=pid, manager_entry=manager_entry, live_runs=live_runs,
            last_activity=last_activity, now=now),
        # Tri-state, deliberately: True answered, False failed, None never
        # asked. Collapsing None into False would make every manager without
        # a health endpoint report unhealthy.
        "probe_ok": answered,
        "idle_seconds": idle_seconds,
    }


def normalize(payload: dict) -> dict:
    """Fill the state keys on any runtime's health payload.

    ``health_summary`` is implemented out of tree as well as here, and a
    runtime that predates these keys returns the older shape. Completing it at
    the response boundary keeps the module's own rule — render code branches on
    value, never on key presence — true for every runtime, without requiring
    the out-of-tree one to ship first.
    """
    payload = dict(payload or {})
    manager = payload.get("manager") or {}
    if "state" not in payload:
        payload["state"] = RUNNING if manager.get("running") else STOPPED
    payload.setdefault("detail", "")
    payload.setdefault("segments", [])
    return payload
