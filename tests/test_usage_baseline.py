"""Per-session cumulative usage totals that a resumed session subtracts."""

import os
import time

import pytest

from bobi import paths
from bobi.brain import usage_baseline


def test_round_trip_under_the_runtime_state_dir(tmp_path):
    paths.bind_root(tmp_path)
    usage_baseline.save("claude", "sess-1", {"total_cost_usd": 0.42})
    assert usage_baseline.load("claude", "sess-1") == {"total_cost_usd": 0.42}
    assert (tmp_path / "state" / "usage_baselines" / "claude-sess-1.json").is_file()
    assert not list((tmp_path / "state" / "usage_baselines").glob("*.lock"))


@pytest.mark.parametrize("session_id", ["../escape", "a/b", "abc\n", "", ".hidden", None, 7])
def test_an_id_that_is_not_a_plain_name_is_never_a_path(tmp_path, session_id):
    paths.bind_root(tmp_path)
    usage_baseline.save("claude", session_id, {"total_cost_usd": 1.0})
    assert usage_baseline.load("claude", session_id) is None
    assert not (tmp_path / "state" / "usage_baselines").exists()


def test_nothing_is_kept_without_a_bound_runtime_root():
    usage_baseline.save("claude", "sess-1", {"total_cost_usd": 1.0})
    assert usage_baseline.load("claude", "sess-1") is None


def test_an_unreadable_file_reads_as_nothing_saved(tmp_path):
    paths.bind_root(tmp_path)
    path = tmp_path / "state" / "usage_baselines" / "codex-th-1.json"
    path.parent.mkdir(parents=True)
    path.write_text("{torn")
    assert usage_baseline.load("codex", "th-1") is None


def test_the_first_save_in_a_process_drops_month_old_files(tmp_path, monkeypatch):
    paths.bind_root(tmp_path)
    directory = tmp_path / "state" / "usage_baselines"
    directory.mkdir(parents=True)
    stale = directory / "claude-old.json"
    stale.write_text("{}")
    month_ago = time.time() - 31 * 24 * 60 * 60
    os.utime(stale, (month_ago, month_ago))
    monkeypatch.setattr(usage_baseline, "_pruned", False)

    usage_baseline.save("claude", "sess-new", {"total_cost_usd": 0.1})

    assert not stale.exists()
    assert (directory / "claude-sess-new.json").exists()


def test_a_month_old_baseline_of_a_saved_session_is_kept(tmp_path, monkeypatch):
    paths.bind_root(tmp_path)
    directory = tmp_path / "state" / "usage_baselines"
    directory.mkdir(parents=True)
    kept = directory / "claude-live-session.json"
    kept.write_text("{}")
    month_ago = time.time() - 31 * 24 * 60 * 60
    os.utime(kept, (month_ago, month_ago))
    paths.sessions_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    (paths.sessions_dir(tmp_path) / "agent.id").write_text("live-session\n")
    monkeypatch.setattr(usage_baseline, "_pruned", False)

    usage_baseline.save("claude", "sess-new", {"total_cost_usd": 0.1})

    assert kept.exists()


def test_a_failed_write_drops_the_older_baseline(tmp_path, monkeypatch):
    paths.bind_root(tmp_path)
    usage_baseline.save("claude", "sess-1", {"total_cost_usd": 0.10})

    def fail(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(usage_baseline.fsutil, "atomic_write_json", fail)
    usage_baseline.save("claude", "sess-1", {"total_cost_usd": 0.45})
    assert usage_baseline.load("claude", "sess-1") is None
