"""Unit tests for the load-evidence sampler (#903).

The gate derives "legitimately busy" from the live process table - a synthetic
/proc tree under tmp_path stands in for the real one, so stat parsing, the
descendant walk, the tick delta, and the fail-closed reads are exercised
without real processes. The real-process leg lives in the acceptance test in
test_supervision_restart.py.
"""

import os
import shutil
from pathlib import Path

from bobi.supervisor.config import SupervisorConfig
from bobi.supervisor.load import (
    _cpu_capacity,
    _descendant_cpu,
    _host_load,
    _parse_stat,
    load_evidence,
)


def _stat_line(pid, ppid, utime, stime, comm="worker"):
    """A minimal but well-formed /proc/<pid>/stat line.

    Only the fields the parser reads need to be real: pid, comm, state,
    ppid (field 4), utime (field 14), stime (field 15). The rest are
    zero-padded placeholders.
    """
    fields = ["S", str(ppid)] + ["0"] * 9 + [str(utime), str(stime)]
    return f"{pid} ({comm}) " + " ".join(fields)


def _write_proc(proc_root: Path, pids: dict[int, tuple[int, int, int]]):
    """Write /proc/<pid>/stat files. ``pids`` maps pid -> (ppid, utime, stime)."""
    proc_root.mkdir(parents=True, exist_ok=True)
    for pid, (ppid, utime, stime) in pids.items():
        (proc_root / str(pid)).mkdir(exist_ok=True)
        (proc_root / str(pid) / "stat").write_text(
            _stat_line(pid, ppid, utime, stime))


def test_config_reads_tree_cpu_ratio(monkeypatch):
    monkeypatch.setenv("WATCHDOG_LOAD_TREE_CPU_RATIO", "0.65")
    assert SupervisorConfig.from_env().load_tree_cpu_ratio == 0.65


# --- stat parsing ---------------------------------------------------------

class TestParseStat:

    def test_parses_plain_line(self):
        assert _parse_stat(_stat_line(100, 1, 10, 20)) == (100, 1, 30)

    def test_comm_with_spaces_and_parens(self):
        # The comm field may itself contain spaces and parens; the parser must
        # split at the LAST ')' and not be fooled by the inner one.
        assert _parse_stat(_stat_line(100, 1, 5, 7, comm="(bash) worker 2")) \
            == (100, 1, 12)

    def test_short_tail_rejected(self):
        assert _parse_stat("100 (worker) S 1 0") is None

    def test_non_numeric_fields_rejected(self):
        line = _stat_line(100, 1, 5, 7).replace(" 5 7", " x 7")
        assert _parse_stat(line) is None


# --- the descendant cpu walk ----------------------------------------------

class TestDescendantCpu:

    def test_counts_busy_descendants_not_siblings(self, tmp_path):
        # Manager 100 -> child 101 -> grandchild 102. 200 and 300 sit outside
        # the tree (reparented orphans / other tenants). Only 101/102 belong
        # to the manager's descendants.
        _write_proc(tmp_path, {
            100: (1, 10, 10),     # the manager itself
            101: (100, 10, 0),    # child, busy in sample 2
            102: (101, 0, 0),     # grandchild, idle
            200: (1, 99, 99),     # outside the tree, busy - must NOT count
            300: (1, 99, 99),     # outside the tree, busy - must NOT count
        })
        busy, tick_delta, sample = _descendant_cpu(100, tmp_path, None)
        assert busy == 0  # no baseline yet
        assert tick_delta == 0
        assert set(sample) == {101, 102}

        _write_proc(tmp_path, {
            100: (1, 12, 10),
            101: (100, 17, 0),    # +7 ticks
            102: (101, 0, 0),
            200: (1, 199, 99),
            300: (1, 199, 99),
        })
        busy, tick_delta, sample2 = _descendant_cpu(100, tmp_path, sample)
        assert busy == 1          # only 101 grew; outside trees don't count
        assert tick_delta == 7
        assert set(sample2) == {101, 102}

    def test_manager_itself_never_counts(self, tmp_path):
        # A busy manager with an idle worker tree is NOT evidence of
        # sanctioned heavy work - only descendants count.
        _write_proc(tmp_path, {100: (1, 10, 10), 101: (100, 1, 1)})
        _, _, sample = _descendant_cpu(100, tmp_path, None)
        _write_proc(tmp_path, {100: (1, 99, 99), 101: (100, 1, 1)})
        busy, tick_delta, _ = _descendant_cpu(100, tmp_path, sample)
        assert busy == 0
        assert tick_delta == 0

    def test_vanished_pid_is_skipped(self, tmp_path):
        _write_proc(tmp_path, {100: (1, 1, 1), 101: (100, 5, 0)})
        _, _, sample = _descendant_cpu(100, tmp_path, None)
        # The busy child vanishes between samples: its /proc/<pid> dir is gone,
        # so it must not linger in the new sample (or be counted as busy).
        shutil.rmtree(tmp_path / "101")
        _write_proc(tmp_path, {100: (1, 2, 2)})
        busy, tick_delta, sample2 = _descendant_cpu(100, tmp_path, sample)
        assert busy == 0
        assert tick_delta == 0
        assert sample2 == {}

    def test_manager_absent_fails_closed(self, tmp_path):
        _write_proc(tmp_path, {101: (1, 5, 5)})
        busy, tick_delta, sample = _descendant_cpu(100, tmp_path, {"101": 1})
        assert busy == 0
        assert tick_delta == 0
        assert sample == {}

    def test_missing_proc_root_fails_closed(self, tmp_path):
        busy, tick_delta, sample = _descendant_cpu(
            100, tmp_path / "nope", {"101": 1})
        assert busy == 0
        assert tick_delta == 0
        assert sample == {}


# --- host load ------------------------------------------------------------

class TestHostLoad:

    def test_reads_first_loadavg_field(self, tmp_path):
        (tmp_path / "loadavg").write_text("2.5 1.2 0.9 1/100 1234\n")
        load1, ncpu = _host_load(tmp_path)
        assert load1 == 2.5
        assert isinstance(ncpu, (int, float)) and ncpu >= 1

    def test_missing_loadavg_reads_none(self, tmp_path):
        load1, _ncpu = _host_load(tmp_path / "nope")
        assert load1 is None


class TestCpuCapacity:

    def test_cgroup_v2_quota_limits_host_cpu_count(self, tmp_path, monkeypatch):
        (tmp_path / "cpu.max").write_text("200000 100000\n")
        monkeypatch.setattr(os, "cpu_count", lambda: 64)
        monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(64)),
                            raising=False)

        assert _cpu_capacity(tmp_path) == 2.0

    def test_cgroup_v2_uses_membership_path_and_strictest_ancestor(
            self, tmp_path, monkeypatch):
        cgroup = tmp_path / "cgroup"
        member = cgroup / "tenant" / "agent"
        member.mkdir(parents=True)
        (cgroup / "cpu.max").write_text("max 100000\n")
        (cgroup / "tenant" / "cpu.max").write_text("150000 100000\n")
        (member / "cpu.max").write_text("200000 100000\n")
        proc_cgroup = tmp_path / "self.cgroup"
        proc_cgroup.write_text("0::/tenant/agent\n")
        monkeypatch.setattr(os, "cpu_count", lambda: 64)
        monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(64)),
                            raising=False)

        assert _cpu_capacity(cgroup, proc_cgroup=proc_cgroup) == 1.5

    def test_affinity_limits_host_cpu_count_without_quota(
            self, tmp_path, monkeypatch):
        (tmp_path / "cpu.max").write_text("max 100000\n")
        monkeypatch.setattr(os, "cpu_count", lambda: 64)
        monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 1, 2, 3},
                            raising=False)

        assert _cpu_capacity(tmp_path) == 4.0

    def test_cgroup_v1_quota_is_supported(self, tmp_path, monkeypatch):
        cpu = tmp_path / "cpu"
        cpu.mkdir()
        (cpu / "cpu.cfs_quota_us").write_text("150000\n")
        (cpu / "cpu.cfs_period_us").write_text("100000\n")
        monkeypatch.setattr(os, "cpu_count", lambda: 64)
        monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(64)),
                            raising=False)

        assert _cpu_capacity(tmp_path) == 1.5

    def test_cgroup_v1_uses_cpu_controller_membership_path(
            self, tmp_path, monkeypatch):
        member = tmp_path / "cpu,cpuacct" / "docker" / "agent"
        member.mkdir(parents=True)
        (member / "cpu.cfs_quota_us").write_text("250000\n")
        (member / "cpu.cfs_period_us").write_text("100000\n")
        proc_cgroup = tmp_path / "self.cgroup"
        proc_cgroup.write_text("4:memory:/docker/agent\n"
                               "3:cpu,cpuacct:/docker/agent\n")
        monkeypatch.setattr(os, "cpu_count", lambda: 64)
        monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(64)),
                            raising=False)

        assert _cpu_capacity(tmp_path, proc_cgroup=proc_cgroup) == 2.5


# --- the combined verdict -------------------------------------------------

class TestLoadEvidence:

    def test_active_requires_pegged_and_busy(self, tmp_path):
        _write_proc(tmp_path, {100: (1, 1, 1), 101: (100, 5, 0)})
        baseline = load_evidence(
            100, None, proc_root=tmp_path, host_load=(8.0, 2),
            now_fn=lambda: 10.0)["sample"]
        _write_proc(tmp_path, {100: (1, 2, 2), 101: (100, 25, 0)})
        ev = load_evidence(100, baseline, proc_root=tmp_path,
                           host_load=(8.0, 2), now_fn=lambda: 11.0,
                           clock_ticks=10)
        assert ev["pegged"] is True
        assert ev["busy_descendants"] == 1
        assert ev["active"] is True
        assert ev["tree_cpu_ratio"] == 1.0
        assert ev["sample"]  # the next poll's baseline rides along

    def test_not_active_when_host_not_pegged(self, tmp_path):
        _write_proc(tmp_path, {100: (1, 1, 1), 101: (100, 5, 0)})
        baseline = load_evidence(
            100, None, proc_root=tmp_path, host_load=(0.5, 2),
            now_fn=lambda: 10.0)["sample"]
        _write_proc(tmp_path, {100: (1, 2, 2), 101: (100, 12, 0)})
        ev = load_evidence(100, baseline, proc_root=tmp_path,
                           host_load=(0.5, 2), now_fn=lambda: 11.0,
                           clock_ticks=10, tree_cpu_ratio=0.25)
        assert ev["pegged"] is False
        assert ev["active"] is False

    def test_not_active_when_no_busy_descendants(self, tmp_path):
        _write_proc(tmp_path, {100: (1, 1, 1), 101: (100, 5, 0)})
        baseline = load_evidence(
            100, None, proc_root=tmp_path, host_load=(8.0, 2),
            now_fn=lambda: 10.0)["sample"]
        # Same ticks: nobody worked between samples.
        ev = load_evidence(100, baseline, proc_root=tmp_path,
                           host_load=(8.0, 2), now_fn=lambda: 11.0,
                           clock_ticks=10, tree_cpu_ratio=0.25)
        assert ev["busy_descendants"] == 0
        assert ev["active"] is False

    def test_first_sample_never_active(self, tmp_path):
        # No baseline => the delta cannot be established yet: fail closed.
        _write_proc(tmp_path, {100: (1, 1, 1), 101: (100, 50, 0)})
        ev = load_evidence(100, None, proc_root=tmp_path, host_load=(8.0, 2))
        assert ev["busy_descendants"] == 0
        assert ev["active"] is False

    def test_ratio_boundary_pegged_at_load1_equals_ncpu(self, tmp_path):
        _write_proc(tmp_path, {100: (1, 1, 1), 101: (100, 5, 0)})
        baseline = load_evidence(
            100, None, proc_root=tmp_path, host_load=(2.0, 2),
            now_fn=lambda: 10.0)["sample"]
        _write_proc(tmp_path, {100: (1, 2, 2), 101: (100, 12, 0)})
        ev = load_evidence(100, baseline, proc_root=tmp_path,
                           host_load=(2.0, 2), pegged_ratio=1.0,
                           now_fn=lambda: 11.0, clock_ticks=10)
        assert ev["pegged"] is True

    def test_negligible_descendant_tick_does_not_claim_host_saturation(
            self, tmp_path):
        _write_proc(tmp_path, {100: (1, 1, 1), 101: (100, 5, 0)})
        baseline = load_evidence(
            100, None, proc_root=tmp_path, host_load=(64.0, 2),
            now_fn=lambda: 10.0)["sample"]
        # One tick over a 30s poll is activity, but nowhere near enough CPU to
        # attribute a pegged host to this manager tree.
        _write_proc(tmp_path, {100: (1, 2, 2), 101: (100, 6, 0)})
        ev = load_evidence(
            100, baseline, proc_root=tmp_path, host_load=(64.0, 2),
            now_fn=lambda: 40.0, clock_ticks=100, tree_cpu_ratio=0.8)

        assert ev["busy_descendants"] == 1
        assert ev["tree_cpu_ratio"] < 0.001
        assert ev["active"] is False

    def test_cgroup_capacity_drives_pegged_threshold(self, tmp_path, monkeypatch):
        cgroup = tmp_path / "cgroup"
        cgroup.mkdir()
        (cgroup / "cpu.max").write_text("200000 100000\n")
        monkeypatch.setattr(os, "cpu_count", lambda: 64)
        monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(64)),
                            raising=False)
        proc = tmp_path / "proc"
        _write_proc(proc, {100: (1, 1, 1), 101: (100, 5, 0)})

        ev = load_evidence(100, None, proc_root=proc,
                           cgroup_root=cgroup, host_load=(2.0, None),
                           now_fn=lambda: 10.0)

        assert ev["ncpu"] == 2.0
        assert ev["pegged"] is True

    def test_unreadable_loadavg_fails_closed(self, tmp_path):
        _write_proc(tmp_path, {100: (1, 1, 1), 101: (100, 5, 0)})
        baseline = load_evidence(
            100, None, proc_root=tmp_path, host_load=(8.0, 2),
            now_fn=lambda: 10.0)["sample"]
        _write_proc(tmp_path, {100: (1, 2, 2), 101: (100, 12, 0)})
        # No loadavg file: pegging is unknown, and uncertainty never defers.
        ev = load_evidence(100, baseline, proc_root=tmp_path,
                           now_fn=lambda: 11.0, clock_ticks=10)
        assert ev["load1"] is None
        assert ev["pegged"] is False
        assert ev["active"] is False
