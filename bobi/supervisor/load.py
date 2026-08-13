"""Load-evidence sampling for the load-grace verdict gate (#903).

Derives "legitimately busy" from the live process table so the
supervisor can defer ambiguous liveness verdicts under saturation:

- host is pegged: ``load1 >= ratio * ncpu`` (from ``/proc/loadavg``),
  where ``ncpu`` respects process affinity and cgroup CPU quota; and
- the manager's own descendant tree materially consumed that
  capacity: aggregate ``utime + stime`` delta over the poll interval
  meets a minimum ratio (from ``/proc/<pid>/stat``).

Evidence is re-derived every poll; nothing is persisted.
On hosts without ``/proc`` (macOS, Windows) reads fail closed and the
gate is inert.  See ``docs/ADMIN_PROTOCOL.md`` § Load grace for the
full design, bounds, and operator knobs.
"""

from __future__ import annotations

import logging
import os
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


def _clock_ticks() -> float | None:
    try:
        ticks = float(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, OSError, ValueError):
        return None
    return ticks if ticks > 0 else None


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
    sample = CpuSample(ticks=ticks, sampled_at=now)

    elapsed = None if previous is None else now - previous.sampled_at
    hz = clock_ticks if clock_ticks is not None else _clock_ticks()
    tree_cpu_cores: float | None = None
    tree_ratio: float | None = None
    if (elapsed is not None and elapsed > 0 and hz and hz > 0
            and ncpu and ncpu > 0):
        tree_cpu_cores = tick_delta / hz / elapsed
        tree_ratio = tree_cpu_cores / ncpu
    pegged = bool(load1 is not None and ncpu and load1 >= pegged_ratio * ncpu)
    tree_busy = bool(tree_ratio is not None and tree_ratio >= tree_cpu_ratio)
    return {
        "active": pegged and busy > 0 and tree_busy,
        "load1": load1,
        "ncpu": ncpu,
        "pegged": pegged,
        "busy_descendants": busy,
        "tree_cpu_cores": tree_cpu_cores,
        "tree_cpu_ratio": tree_ratio,
        "sample": sample,
    }
