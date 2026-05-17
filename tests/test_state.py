"""Tests for state store."""

import time

from dispatch.state import StateStore, Status


def test_dispatch_and_track(tmp_path):
    store = StateStore(path=tmp_path / "state.json")

    assert not store.is_tracked("PROJ-1")

    result = store.dispatch("PROJ-1", "/tmp/repo", "Fix the bug", agent_pid=1234)
    assert result is True
    assert store.is_tracked("PROJ-1")


def test_cas_prevents_double_dispatch(tmp_path):
    store = StateStore(path=tmp_path / "state.json")

    store.dispatch("PROJ-1", "/tmp/repo", "Fix the bug")
    result = store.dispatch("PROJ-1", "/tmp/repo", "Fix the bug again")

    assert result is False


def test_status_transitions(tmp_path):
    store = StateStore(path=tmp_path / "state.json")
    store.dispatch("PROJ-1", "/tmp/repo", "Build feature")

    store.update_status("PROJ-1", Status.WORKING)
    items = store.get_in_flight()
    assert items[0].status == Status.WORKING

    store.update_status("PROJ-1", Status.AUDITING)
    items = store.get_in_flight()
    assert items[0].status == Status.AUDITING

    store.mark_done("PROJ-1", pr_url="https://github.com/org/repo/pull/42")
    items = store.get_in_flight()
    assert len(items) == 0


def test_mark_failed(tmp_path):
    store = StateStore(path=tmp_path / "state.json")
    store.dispatch("PROJ-2", "/tmp/repo", "Broken thing")

    store.mark_failed("PROJ-2", error="Tests failed")
    items = store.get_in_flight()
    assert len(items) == 0


def test_get_by_repo(tmp_path):
    store = StateStore(path=tmp_path / "state.json")
    store.dispatch("PROJ-1", "/tmp/repo-a", "Task A")
    store.dispatch("PROJ-2", "/tmp/repo-b", "Task B")
    store.dispatch("PROJ-3", "/tmp/repo-a", "Task C")

    repo_a_items = store.get_by_repo("/tmp/repo-a")
    assert len(repo_a_items) == 2

    repo_b_items = store.get_by_repo("/tmp/repo-b")
    assert len(repo_b_items) == 1


def test_cleanup_old(tmp_path):
    store = StateStore(path=tmp_path / "state.json")
    store.dispatch("PROJ-old", "/tmp/repo", "Old task")
    store.mark_done("PROJ-old")

    # Manually backdate
    store._items["PROJ-old"].dispatched_at = time.time() - (100 * 3600)
    store._save()

    store.cleanup_old(max_age_hours=72)

    # Should be cleaned up
    assert "PROJ-old" not in store._items


def test_persistence_across_loads(tmp_path):
    state_path = tmp_path / "state.json"

    store1 = StateStore(path=state_path)
    store1.dispatch("PROJ-1", "/tmp/repo", "Persistent task", agent_pid=999)

    # Load fresh from disk
    store2 = StateStore(path=state_path)
    assert store2.is_tracked("PROJ-1")
    items = store2.get_in_flight()
    assert items[0].agent_pid == 999
