"""Load-evidence sampling for the load-grace verdict gate (MOD-364).

Sanctioned heavy work (a full test suite, a long gate) saturates a small host
while remaining productive. Under that pressure the manager's liveness
signals - probe responses, active-turn progress, its own health thread -
miss for *starvation* reasons, not failure reasons, and the supervisor's
restart budget treats both identically (issue #903: budget exhaustion
restarted a healthy instance).

This module derives "legitimately busy" from the live process table, so the
supervisor needs no declaration, no flag, and no persisted lease:

- the host is saturated: ``load1 >= ratio * ncpu`` (from ``/proc/loadavg``), and
- the manager's own worker tree is the thing working: some process in the
  manager's descendant tree consumed CPU time (``utime + stime`` delta across
  two polls, from ``/proc/<pid>/stat``).

Evidence is re-derived every poll. The exemption therefore cannot outlive the
busy processes themselves - when they finish or die, the gate reopens - and
nothing is persisted, so an abrupt supervisor or host death leaves no stale
exemption behind. Attribution is by *ancestry from the managed pid*, not by
registration, so it stays inside the "no arbitrary worker PID attribution"
bound: only the supervised manager's own tree can count.

Unreadable evidence fails CLOSED on the exemption (``active`` stays False):
uncertainty must never defer a restart, mirroring the fail-open ethos of the
wedge discriminator. On hosts without ``/proc`` (macOS, Windows) every read is
unreadable, so the gate is inert there: the supervisor behaves exactly as it
did before this feature.

TODO(macos): make the gate portable behind the same seam (``load_fn`` in
supervision.py) once a real dev-machine false-kill under saturation is
reported - nothing else needs to change:

- load1: ``os.getloadavg()[0]``, already the house pattern (see
  ``launch_admission.read_host_metrics``);
- the tree: ``ps -axo pid,ppid,time`` (cumulative CPU time, diffable across
  polls like the /proc tick delta);
- keep it honest: a macOS CI leg exercising the real ``ps`` output, not only
  fixtures. Until then macOS fails closed to the pre-feature behavior on
  purpose. Windows stays out of scope (no load-average concept).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _host_load(proc_root: Path) -> tuple[float | None, int | None]:
    """1-minute load average and CPU count, best-effort (None on failure)."""
    load1: float | None = None
    try:
        raw = (proc_root / "loadavg").read_text().split()[0]
        load1 = float(raw)
    except (OSError, ValueError, IndexError):
        pass
    return load1, os.cpu_count()


def _parse_stat(raw: str) -> tuple[int, int, int] | None:
    """``(pid, ppid, cpu_ticks)`` from one ``/proc/<pid>/stat`` line.

    The ``comm`` field (in parens) may itself contain spaces and parens, so
    the line is split at the LAST ``)``: everything after it is the fixed-
    order field tail (state, ppid, ..., utime, stime).
    """
    _, _, tail = raw.rpartition(")")
    fields = tail.split()
    if len(fields) < 13:
        return None
    left, _, _comm = raw.partition(" (")
    try:
        pid = int(left)
        ppid = int(fields[1])    # field 4
        utime = int(fields[11])  # field 14
        stime = int(fields[12])  # field 15
    except ValueError:
        return None
    return pid, ppid, utime + stime


def _collect_descendants(children: dict[int, list[int]], root: int) -> set[int]:
    """All descendants of ``root`` via a breadth-first walk of the ppid map."""
    descendants: set[int] = set()
    frontier = [root]
    while frontier:
        level = frontier
        frontier = []
        for pid in level:
            for child in children.get(pid, ()):
                if child not in descendants:
                    descendants.add(child)
                    frontier.append(child)
    return descendants


def _descendant_cpu(manager_pid: int, proc_root: Path,
                    previous: dict[int, int] | None) -> tuple[int, dict[int, int]]:
    """Count of manager descendants consuming CPU since the last sample.

    Returns ``(busy_count, sample)`` where ``sample`` maps descendant pid ->
    cpu ticks and is the baseline to hand back on the next poll. A pid with no
    prior sample does not count as busy yet (conservative: the next poll, one
    interval later, establishes the delta).
    """
    entries: dict[int, tuple[int, int]] = {}
    try:
        names = list(proc_root.iterdir())
    except OSError:
        return 0, {}
    for entry in names:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text()
        except OSError:
            continue  # vanished mid-scan
        parsed = _parse_stat(raw)
        if parsed is not None:
            pid, ppid, ticks = parsed
            entries[pid] = (ppid, ticks)

    if manager_pid not in entries:
        return 0, {}
    children: dict[int, list[int]] = {}
    for pid, (ppid, _ticks) in entries.items():
        children.setdefault(ppid, []).append(pid)
    descendants = _collect_descendants(children, manager_pid)

    sample = {pid: entries[pid][1] for pid in descendants}
    prev = previous or {}
    busy = sum(1 for pid in descendants
               if pid in prev and sample[pid] > prev[pid])
    return busy, sample


def load_evidence(manager_pid: int, previous: dict[int, int] | None, *,
                  proc_root: Path = Path("/proc"),
                  host_load: tuple[float, int] | None = None,
                  pegged_ratio: float = 1.0) -> dict:
    """One poll's worth of legitimately-busy evidence.

    ``host_load`` overrides the ``/proc/loadavg`` read (tests inject it; the
    descendant walk stays real). The returned ``sample`` is the baseline the
    caller should pass back as ``previous`` on the next poll.
    """
    if host_load is not None:
        load1, ncpu = host_load
    else:
        load1, ncpu = _host_load(proc_root)
    busy, sample = _descendant_cpu(manager_pid, proc_root, previous)
    pegged = bool(load1 is not None and ncpu and load1 >= pegged_ratio * ncpu)
    return {
        "active": pegged and busy > 0,
        "load1": load1,
        "ncpu": ncpu,
        "pegged": pegged,
        "busy_descendants": busy,
        "sample": sample,
    }
