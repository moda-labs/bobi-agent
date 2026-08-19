"""Monitor run records — the per-firing outcome ledger.

Before this, a monitor firing left behind exactly one fact: `last_run` in
`monitor_state.json`, overwritten every time. A monitor that ran and posted,
ran and stayed quiet, or failed to run at all were indistinguishable after the
fact, and the only outcome signal (`system/monitor.error`) was an ephemeral bus
event nobody stored.

A run record is written once per firing, when the firing finishes — including
the out-of-band flavors, whose waiter thread closes the record from another
thread. Nothing is written at the start of a firing on purpose: a record that
existed only in an "open" state would strand as permanently-running whenever a
manager died mid-firing, and live monitor work is already visible through its
session, not through this ledger.

One other thing writes here, and it is not a firing: when the scheduler's
retry drain finishes recovering a park — the deliveries a failed firing owed
the bus (#1006) — it records that as its own run, so the delivery sits in the
ledger next to the firing that failed to make it.

Records are per-monitor files under `run/state/monitor_runs/`, newest first,
capped at `RETENTION_PER_MONITOR`. The cap is the whole retention policy: this
is a debugging ledger, not an audit log.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from bobi.timeutil import now_iso

log = logging.getLogger(__name__)

# Outcomes. `notified` means this firing published at least one event;
# `quiet` means it completed and had nothing new to say; `failed` means the
# firing did not produce a trustworthy answer (spawn failure, timeout,
# indeterminate verdict, a detector that errored, or a publish that did not
# land — a parked `pending_publish` payload has NOT notified anyone yet).
NOTIFIED = "notified"
QUIET = "quiet"
FAILED = "failed"

# Most records kept per monitor. A 15-minute monitor fills this in ~12 hours,
# which is the window in which anyone asks "why did that fire?".
RETENTION_PER_MONITOR = 50

# All writes go through one process-wide lock: the scheduler thread and any
# number of waiter threads close records concurrently, and a read-modify-write
# on a per-monitor file is not otherwise safe between them.
_write_lock = threading.RLock()


def _runs_dir(root: Path | None = None) -> Path:
    """The records directory for *root*, or the bound root when omitted.

    The scheduler writes from inside a bound manager process; readers (the web
    app's runs view) pass an explicit root because one webapp process serves
    every team on the machine and binds none of them.
    """
    from bobi import paths
    return paths.state_path(root) / "monitor_runs"


def _safe_name(monitor_name: str) -> str:
    """Filesystem-safe stem for a monitor name, within :func:`_runs_dir`.

    Deliberately NOT the sanitizer script_cache/tool_checks share: this is a
    whitelist (with a fallback for a name that sanitizes away to nothing),
    theirs is a two-character blacklist, and the two disagree on names
    containing e.g. a space. They may differ freely because they key artifacts
    in different directories — ``monitor_runs/`` here, ``scripts/`` there — and
    nothing reads one subsystem's stems with the other's rule.
    """
    return re.sub(r"[^A-Za-z0-9_.-]", "_", monitor_name) or "monitor"


@dataclass
class MonitorRun:
    """One monitor firing, start to finish."""

    run_id: str
    monitor: str
    started_at: str
    ended_at: str
    outcome: str
    # Why a firing failed, or what a successful one did ("published 2 events").
    # Free text for a human reading the runs table, never parsed.
    reason: str = ""
    # Which detector flavor ran: command | check | notify | sleep_cycle |
    # description (the out-of-band check agent).
    flavor: str = ""
    # script_cache runner mode for this tick (cached | first_gen |
    # fallback_regen), when the monitor is script-cache backed. Empty
    # otherwise — the field distinguishes "$0 cached tick" from "paid for an
    # agent", which is the whole point of the runner.
    script_cache_mode: str = ""
    # Registry entry name of the session this firing spawned, when it spawned
    # one. Empty means there is no transcript to open for this run.
    session_ref: str = ""
    # Events this firing published successfully.
    published: int = 0

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "MonitorRun":
        return cls(
            run_id=str(data.get("run_id", "")),
            monitor=str(data.get("monitor", "")),
            started_at=str(data.get("started_at", "")),
            ended_at=str(data.get("ended_at", "")),
            outcome=str(data.get("outcome", "")),
            reason=str(data.get("reason", "")),
            flavor=str(data.get("flavor", "")),
            script_cache_mode=str(data.get("script_cache_mode", "")),
            session_ref=str(data.get("session_ref", "")),
            published=int(data.get("published", 0) or 0),
        )


def new_run_id(monitor_name: str) -> str:
    return f"{_safe_name(monitor_name)}-{uuid.uuid4().hex[:12]}"


def record_path(monitor_name: str, root: Path | None = None) -> Path:
    return _runs_dir(root) / f"{_safe_name(monitor_name)}.json"


def record(run: MonitorRun) -> None:
    """Append one finished run, newest first, trimmed to the retention cap.

    Never raises: a monitor firing must not fail because its ledger entry
    could not be written.
    """
    try:
        with _write_lock:
            path = record_path(run.monitor)
            runs = [r.to_json() for r in load(run.monitor)]
            runs.insert(0, run.to_json())
            del runs[RETENTION_PER_MONITOR:]
            payload = json.dumps({"monitor": run.monitor, "runs": runs}, indent=2)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(payload)
            tmp.replace(path)
    except Exception:
        log.exception("Failed to record monitor run for %s", run.monitor)


def _read_file(path: Path) -> list[MonitorRun]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text()) or {}
    except (OSError, json.JSONDecodeError):
        log.warning("Corrupt monitor run records at %s — ignoring", path)
        return []
    raw = data.get("runs") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    return [MonitorRun.from_json(item) for item in raw if isinstance(item, dict)]


def load(monitor_name: str, root: Path | None = None) -> list[MonitorRun]:
    """Records for one monitor, newest first. Empty when there are none."""
    return _read_file(record_path(monitor_name, root))


def load_all(root: Path | None = None) -> list[MonitorRun]:
    """Every monitor's records, newest first across all monitors.

    The read side of this module — the runs read model folds these in
    alongside sessions and workflow runs.
    """
    runs_dir = _runs_dir(root)
    if not runs_dir.exists():
        return []
    runs: list[MonitorRun] = []
    for path in sorted(runs_dir.glob("*.json")):
        runs.extend(_read_file(path))
    runs.sort(key=lambda r: r.started_at, reverse=True)
    return runs


class RunTracker:
    """Open firing → one recorded run.

    The scheduler builds one of these when a monitor becomes due and closes it
    when the firing resolves — synchronously for the in-thread flavors, from a
    waiter thread for the out-of-band ones. Closing twice is a no-op, so a
    flavor that can resolve down two paths (a spawn that fails before it
    starts, then its callback) records exactly one run. The retry drain builds
    one too, for a recovery rather than a firing, and closes it only once the
    park it was recovering is empty.
    """

    def __init__(self, monitor_name: str, flavor: str = "", *, now=None):
        self._now = now or now_iso
        self.run = MonitorRun(
            run_id=new_run_id(monitor_name),
            monitor=monitor_name,
            started_at=self._now(),
            ended_at="",
            outcome="",
            flavor=flavor,
        )
        self._closed = False
        self._failure = ""
        self._published = 0

    @property
    def closed(self) -> bool:
        return self._closed

    def note_session(self, session_ref: str) -> None:
        if session_ref:
            self.run.session_ref = session_ref

    def note_script_cache_mode(self, mode: str) -> None:
        if mode:
            self.run.script_cache_mode = mode

    def note_failure(self, reason: str) -> None:
        """Record why this firing cannot be trusted. First reason wins — it is
        the one nearest the root cause; later ones are its consequences."""
        if reason and not self._failure:
            self._failure = reason

    def add_published(self, count: int) -> None:
        """Count events this firing actually got onto the bus."""
        if count:
            self._published += count

    def close(self, outcome: str = "", reason: str = "") -> None:
        """Record the firing's outcome. The first close wins.

        With no explicit outcome, one is derived: a noted failure makes the
        run `failed` no matter what else happened (a firing that published
        two of three findings and then broke is not a clean run), an
        otherwise-successful firing that published is `notified`, and one
        that published nothing is `quiet`.
        """
        if self._closed:
            return
        self._closed = True
        if not outcome:
            outcome = (FAILED if self._failure
                       else NOTIFIED if self._published else QUIET)
        self.run.ended_at = self._now()
        self.run.outcome = outcome
        self.run.reason = reason or self._failure
        self.run.published = self._published
        record(self.run)
