"""Unit tests for the load-evidence sampler (MOD-364).

The gate derives "legitimately busy" from the live process table - a synthetic
/proc tree under tmp_path stands in for the real one, so stat parsing, the
descendant walk, the tick delta, and the fail-closed reads are exercised
without real processes. The real-process leg lives in the acceptance test in
test_supervision_restart.py.
"""

import shutil
from pathlib import Path

from bobi.supervisor.load import (
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
        busy, sample = _descendant_cpu(100, tmp_path, None)
        assert busy == 0  # no baseline yet
        assert set(sample) == {101, 102}

        _write_proc(tmp_path, {
            100: (1, 12, 10),
            101: (100, 17, 0),    # +7 ticks
            102: (101, 0, 0),
            200: (1, 199, 99),
            300: (1, 199, 99),
        })
        busy, sample2 = _descendant_cpu(100, tmp_path, sample)
        assert busy == 1          # only 101 grew; outside trees don't count
        assert set(sample2) == {101, 102}

    def test_manager_itself_never_counts(self, tmp_path):
        # A busy manager with an idle worker tree is NOT evidence of
        # sanctioned heavy work - only descendants count.
        _write_proc(tmp_path, {100: (1, 10, 10), 101: (100, 1, 1)})
        _, sample = _descendant_cpu(100, tmp_path, None)
        _write_proc(tmp_path, {100: (1, 99, 99), 101: (100, 1, 1)})
        busy, _ = _descendant_cpu(100, tmp_path, sample)
        assert busy == 0

    def test_vanished_pid_is_skipped(self, tmp_path):
        _write_proc(tmp_path, {100: (1, 1, 1), 101: (100, 5, 0)})
        _, sample = _descendant_cpu(100, tmp_path, None)
        # The busy child vanishes between samples: its /proc/<pid> dir is gone,
        # so it must not linger in the new sample (or be counted as busy).
        shutil.rmtree(tmp_path / "101")
        _write_proc(tmp_path, {100: (1, 2, 2)})
        busy, sample2 = _descendant_cpu(100, tmp_path, sample)
        assert busy == 0
        assert sample2 == {}

    def test_manager_absent_fails_closed(self, tmp_path):
        _write_proc(tmp_path, {101: (1, 5, 5)})
        busy, sample = _descendant_cpu(100, tmp_path, {"101": 1})
        assert busy == 0
        assert sample == {}

    def test_missing_proc_root_fails_closed(self, tmp_path):
        busy, sample = _descendant_cpu(100, tmp_path / "nope", {"101": 1})
        assert busy == 0
        assert sample == {}


# --- host load ------------------------------------------------------------

class TestHostLoad:

    def test_reads_first_loadavg_field(self, tmp_path):
        (tmp_path / "loadavg").write_text("2.5 1.2 0.9 1/100 1234\n")
        load1, ncpu = _host_load(tmp_path)
        assert load1 == 2.5
        assert isinstance(ncpu, int) and ncpu >= 1

    def test_missing_loadavg_reads_none(self, tmp_path):
        load1, _ncpu = _host_load(tmp_path / "nope")
        assert load1 is None


# --- the combined verdict -------------------------------------------------

class TestLoadEvidence:

    def test_active_requires_pegged_and_busy(self, tmp_path):
        _write_proc(tmp_path, {100: (1, 1, 1), 101: (100, 5, 0)})
        _, baseline = _descendant_cpu(100, tmp_path, None)
        _write_proc(tmp_path, {100: (1, 2, 2), 101: (100, 12, 0)})
        ev = load_evidence(100, baseline, proc_root=tmp_path,
                           host_load=(8.0, 2))
        assert ev["pegged"] is True
        assert ev["busy_descendants"] == 1
        assert ev["active"] is True
        assert ev["sample"]  # the next poll's baseline rides along

    def test_not_active_when_host_not_pegged(self, tmp_path):
        _write_proc(tmp_path, {100: (1, 1, 1), 101: (100, 5, 0)})
        _, baseline = _descendant_cpu(100, tmp_path, None)
        _write_proc(tmp_path, {100: (1, 2, 2), 101: (100, 12, 0)})
        ev = load_evidence(100, baseline, proc_root=tmp_path,
                           host_load=(0.5, 2))
        assert ev["pegged"] is False
        assert ev["active"] is False

    def test_not_active_when_no_busy_descendants(self, tmp_path):
        _write_proc(tmp_path, {100: (1, 1, 1), 101: (100, 5, 0)})
        _, baseline = _descendant_cpu(100, tmp_path, None)
        # Same ticks: nobody worked between samples.
        ev = load_evidence(100, baseline, proc_root=tmp_path,
                           host_load=(8.0, 2))
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
        _, baseline = _descendant_cpu(100, tmp_path, None)
        _write_proc(tmp_path, {100: (1, 2, 2), 101: (100, 12, 0)})
        ev = load_evidence(100, baseline, proc_root=tmp_path,
                           host_load=(2.0, 2), pegged_ratio=1.0)
        assert ev["pegged"] is True

    def test_unreadable_loadavg_fails_closed(self, tmp_path):
        _write_proc(tmp_path, {100: (1, 1, 1), 101: (100, 5, 0)})
        _, baseline = _descendant_cpu(100, tmp_path, None)
        _write_proc(tmp_path, {100: (1, 2, 2), 101: (100, 12, 0)})
        # No loadavg file: pegging is unknown, and uncertainty never defers.
        ev = load_evidence(100, baseline, proc_root=tmp_path)
        assert ev["load1"] is None
        assert ev["pegged"] is False
        assert ev["active"] is False
