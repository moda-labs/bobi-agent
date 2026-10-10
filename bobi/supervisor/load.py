"""Load-evidence sampling for the load-grace verdict gate (#903).

Derives "legitimately busy" from the live process table so the
supervisor can defer ambiguous liveness verdicts under saturation:

- host is pegged: ``load1 >= ratio * ncpu`` (from procfs or
  ``os.getloadavg()``),
  where ``ncpu`` respects process affinity and cgroup CPU quota; and
- the manager's own descendant tree materially consumed that
  capacity: aggregate ``utime + stime`` delta over the poll interval
  meets a minimum ratio (from procfs or the Darwin process table).

Evidence is re-derived every poll; nothing is persisted.
Linux reads ``/proc``; macOS reads ``ps -axo pid=,ppid=,time=``. Unsupported
platforms and unreadable evidence fail closed. See ``docs/ADMIN_PROTOCOL.md``
§ Load grace for the full design, bounds, and operator knobs.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_SELF_CGROUP = Path("/proc/self/cgroup")


@dataclass(frozen=True)
class CpuSample:
    """Descendant ticks and their monotonic sample time."""

    ticks: dict[int, int]
    sampled_at: float


def _safe_cgroup_relative(raw: str) -> Path | None:
    """Turn a `/proc/self/cgroup` membership into a safe relative path."""
    relative = Path(raw.lstrip("/"))
    if any(part in ("", ".", "..") for part in relative.parts):
        return None
    return relative


def _ancestors(member: Path, root: Path):
    """Yield member then parents up to and including its cgroup mount root."""
    current = member
    while current == root or root in current.parents:
        yield current
        if current == root:
            return
        current = current.parent


def _read_cpu_max(root: Path) -> float | None:
    try:
        quota, period = (root / "cpu.max").read_text().split()[:2]
        if quota != "max":
            quota_f, period_f = float(quota), float(period)
            if quota_f > 0 and period_f > 0:
                return quota_f / period_f
    except (OSError, ValueError, IndexError):
        pass
    return None


def _read_cpu_v1(root: Path) -> float | None:
    try:
        quota_f = float((root / "cpu.cfs_quota_us").read_text())
        period_f = float((root / "cpu.cfs_period_us").read_text())
    except (OSError, ValueError):
        return None
    if quota_f > 0 and period_f > 0:
        return quota_f / period_f
    return None


def _cgroup_cpu_quota(cgroup_root: Path, *,
                      proc_cgroup: Path = PROC_SELF_CGROUP) -> float | None:
    """Strictest CPU quota in this process's cgroup hierarchy.

    Cgroup files usually live below the mount root at the membership path from
    ``/proc/self/cgroup``. A parent may be stricter than the leaf, so every
    ancestor contributes to the effective capacity. Direct-root probes remain
    as a fallback for cgroup namespaces (whose membership is ``/``) and older
    layouts where proc membership is unavailable.
    """
    v2_path: Path | None = None
    v1_memberships: list[tuple[str, Path]] = []
    try:
        lines = proc_cgroup.read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        hierarchy, sep, tail = line.partition(":")
        if not sep:
            continue
        controllers, sep, raw_path = tail.partition(":")
        if not sep:
            continue
        relative = _safe_cgroup_relative(raw_path)
        if relative is None:
            continue
        if hierarchy == "0" and not controllers:
            v2_path = relative
        elif "cpu" in controllers.split(","):
            v1_memberships.append((controllers, relative))

    quotas: list[float] = []

    # v2 unified hierarchy.
    v2_member = cgroup_root / v2_path if v2_path is not None else cgroup_root
    for root in _ancestors(v2_member, cgroup_root):
        quota = _read_cpu_max(root)
        if quota is not None:
            quotas.append(quota)

    # v1 controller hierarchy. The mount name varies (cpu, cpu,cpuacct, or the
    # inverse), so try the proc spelling plus the common aliases.
    for controllers, relative in v1_memberships:
        mount_names = (controllers, "cpu", "cpu,cpuacct", "cpuacct,cpu")
        for name in dict.fromkeys(mount_names):
            mount = cgroup_root / name
            member = mount / relative
            for root in _ancestors(member, mount):
                quota = _read_cpu_v1(root)
                if quota is not None:
                    quotas.append(quota)

    # Direct-root fallback for callers handed the controller mount itself and
    # for fixtures/legacy layouts without readable proc membership.
    for root in (cgroup_root, cgroup_root / "cpu"):
        quota = _read_cpu_v1(root)
        if quota is not None:
            quotas.append(quota)
    return min(quotas) if quotas else None


def _cpu_capacity(cgroup_root: Path = CGROUP_ROOT, *,
                  proc_cgroup: Path = PROC_SELF_CGROUP) -> float | None:
    """CPUs usable by this supervisor, respecting affinity and cgroup quota.

    ``os.cpu_count()`` alone describes the host on common container runtimes;
    comparing a container workload against that value makes a 2-vCPU cgroup
    look idle on a large node. The minimum visible constraint is the capacity
    the manager tree can actually consume.
    """
    limits: list[float] = []
    system = os.cpu_count()
    if system and system > 0:
        limits.append(float(system))
    affinity_fn = getattr(os, "sched_getaffinity", None)
    if affinity_fn is not None:
        try:
            affinity = len(affinity_fn(0))
            if affinity > 0:
                limits.append(float(affinity))
        except OSError:
            pass
    quota = _cgroup_cpu_quota(cgroup_root, proc_cgroup=proc_cgroup)
    if quota is not None:
        limits.append(quota)
    return min(limits) if limits else None


def _host_load(proc_root: Path, *,
               cgroup_root: Path = CGROUP_ROOT) -> tuple[float | None,
                                                        float | None]:
    """1-minute load average and usable CPU capacity, best-effort."""
    load1: float | None = None
    try:
        raw = (proc_root / "loadavg").read_text().split()[0]
        load1 = float(raw)
    except (OSError, ValueError, IndexError):
        pass
    return load1, _cpu_capacity(cgroup_root)


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


def _descendant_delta(
    manager_pid: int,
    entries: dict[int, tuple[int, int]],
    previous: dict[int, int] | None,
) -> tuple[int, int, dict[int, int]]:
    if manager_pid not in entries:
        return 0, 0, {}
    children: dict[int, list[int]] = {}
    for pid, (ppid, _ticks) in entries.items():
        children.setdefault(ppid, []).append(pid)
    descendants = _collect_descendants(children, manager_pid)

    sample = {pid: entries[pid][1] for pid in descendants}
    prev = previous or {}
    deltas = [sample[pid] - prev[pid] for pid in descendants
              if pid in prev and sample[pid] > prev[pid]]
    return len(deltas), sum(deltas), sample


def _descendant_cpu(
    manager_pid: int,
    proc_root: Path,
    previous: dict[int, int] | None,
) -> tuple[int, int, dict[int, int]]:
    """Busy count, aggregate tick delta, and current descendant sample.

    A pid with no prior sample does not count as busy yet (conservative: the
    next poll, one interval later, establishes the delta).
    """
    entries: dict[int, tuple[int, int]] = {}
    try:
        names = list(proc_root.iterdir())
    except OSError:
        return 0, 0, {}
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

    return _descendant_delta(manager_pid, entries, previous)


def _parse_ps_time(raw: str) -> int | None:
    """Convert Darwin ``ps`` cumulative CPU time to centiseconds."""
    try:
        whole, dot, fraction = raw.partition(".")
        centiseconds = int((fraction + "00")[:2]) if dot else 0
        days = 0
        if "-" in whole:
            day_text, whole = whole.split("-", 1)
            days = int(day_text)
        parts = [int(part) for part in whole.split(":")]
        part_count = len(parts)
        if part_count == 2:
            hours = 0
            minutes, seconds = parts
        elif part_count == 3:
            hours, minutes, seconds = parts
        else:
            return None
        if min(days, hours, minutes, seconds, centiseconds) < 0:
            return None
        if (seconds >= 60
                or (part_count == 3 and minutes >= 60)
                or (days and hours >= 24)):
            return None
    except ValueError:
        return None
    total_seconds = ((days * 24 + hours) * 60 + minutes) * 60 + seconds
    return total_seconds * 100 + centiseconds


def _run_darwin_ps() -> str:
    return subprocess.check_output(
        ["ps", "-axo", "pid=,ppid=,time="], text=True, timeout=5)


def _darwin_descendant_cpu(
    manager_pid: int,
    previous: dict[int, int] | None,
    *,
    ps_run=_run_darwin_ps,
) -> tuple[int, int, dict[int, int]]:
    """Busy descendants from one Darwin ``ps`` process-table snapshot."""
    try:
        output = ps_run()
    except (OSError, subprocess.SubprocessError):
        return 0, 0, {}

    entries: dict[int, tuple[int, int]] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 3:
            return 0, 0, {}
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            return 0, 0, {}
        ticks = _parse_ps_time(fields[2])
        if ticks is None:
            return 0, 0, {}
        entries[pid] = (ppid, ticks)
    return _descendant_delta(manager_pid, entries, previous)


def _clock_ticks() -> float | None:
    try:
        ticks = float(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, OSError, ValueError):
        return None
    return ticks if ticks > 0 else None


def _build_evidence(
    previous: CpuSample | None,
    *,
    load1: float | None,
    ncpu: float | None,
    busy: int,
    tick_delta: int,
    ticks: dict[int, int],
    now: float,
    clock_ticks: float | None,
    pegged_ratio: float,
    tree_cpu_ratio: float,
) -> dict:
    sample = CpuSample(ticks=ticks, sampled_at=now)
    elapsed = None if previous is None else now - previous.sampled_at
    tree_cpu_cores: float | None = None
    measured_tree_ratio: float | None = None
    if (elapsed is not None and elapsed > 0 and clock_ticks
            and clock_ticks > 0 and ncpu and ncpu > 0):
        tree_cpu_cores = tick_delta / clock_ticks / elapsed
        measured_tree_ratio = tree_cpu_cores / ncpu
    pegged = bool(load1 is not None and ncpu and load1 >= pegged_ratio * ncpu)
    tree_busy = bool(
        measured_tree_ratio is not None
        and measured_tree_ratio >= tree_cpu_ratio
    )
    return {
        "active": pegged and busy > 0 and tree_busy,
        "load1": load1,
        "ncpu": ncpu,
        "pegged": pegged,
        "busy_descendants": busy,
        "tree_cpu_cores": tree_cpu_cores,
        "tree_cpu_ratio": measured_tree_ratio,
        "sample": sample,
    }


def load_evidence(manager_pid: int, previous: CpuSample | None, *,
                  proc_root: Path = Path("/proc"),
                  cgroup_root: Path = CGROUP_ROOT,
                  host_load: tuple[float, float | None] | None = None,
                  pegged_ratio: float = 1.0,
                  tree_cpu_ratio: float = 0.8,
                  now_fn=time.monotonic,
                  clock_ticks: float | None = None) -> dict:
    """One poll's worth of legitimately-busy evidence.

    ``host_load`` overrides the ``/proc/loadavg`` read (tests inject it; the
    descendant walk stays real). The returned ``sample`` is the baseline the
    caller should pass back as ``previous`` on the next poll.
    """
    if host_load is not None:
        load1, ncpu = host_load
        if ncpu is None:
            ncpu = _cpu_capacity(cgroup_root)
    else:
        load1, ncpu = _host_load(proc_root, cgroup_root=cgroup_root)
    now = now_fn()
    previous_ticks = previous.ticks if previous is not None else None
    busy, tick_delta, ticks = _descendant_cpu(
        manager_pid, proc_root, previous_ticks)
    hz = clock_ticks if clock_ticks is not None else _clock_ticks()
    return _build_evidence(
        previous,
        load1=load1,
        ncpu=ncpu,
        busy=busy,
        tick_delta=tick_delta,
        ticks=ticks,
        now=now,
        clock_ticks=hz,
        pegged_ratio=pegged_ratio,
        tree_cpu_ratio=tree_cpu_ratio,
    )


def darwin_load_evidence(
    manager_pid: int,
    previous: CpuSample | None,
    *,
    host_load: tuple[float, float | None] | None = None,
    pegged_ratio: float = 1.0,
    tree_cpu_ratio: float = 0.8,
    now_fn=time.monotonic,
    ps_run=_run_darwin_ps,
) -> dict:
    """Darwin load evidence using ``getloadavg`` and cumulative ``ps`` time."""
    if host_load is not None:
        load1, ncpu = host_load
        if ncpu is None:
            ncpu = _cpu_capacity()
    else:
        try:
            load1 = os.getloadavg()[0]
        except (AttributeError, OSError):
            load1 = None
        ncpu = _cpu_capacity()
    busy, tick_delta, ticks = _darwin_descendant_cpu(
        manager_pid,
        previous.ticks if previous is not None else None,
        ps_run=ps_run,
    )
    return _build_evidence(
        previous,
        load1=load1,
        ncpu=ncpu,
        busy=busy,
        tick_delta=tick_delta,
        ticks=ticks,
        now=now_fn(),
        clock_ticks=100.0,
        pegged_ratio=pegged_ratio,
        tree_cpu_ratio=tree_cpu_ratio,
    )


def default_load_evidence(manager_pid: int, previous: CpuSample | None, **kwargs):
    """Select the native evidence reader without changing the supervisor seam."""
    if sys.platform == "darwin":
        return darwin_load_evidence(manager_pid, previous, **kwargs)
    return load_evidence(manager_pid, previous, **kwargs)
