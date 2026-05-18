"""Tests for state store."""

import time

from dispatch.state import StateStore


def test_track_and_check(tmp_path):
    store = StateStore(path=tmp_path / "state.json")
    assert not store.is_tracked("AGD-1")

    store.track("AGD-1", pid=1234, repo_path="/tmp/repo", title="Test",
                worktree="/tmp/worktree")
    assert store.is_tracked("AGD-1")


def test_remove(tmp_path):
    store = StateStore(path=tmp_path / "state.json")
    store.track("AGD-1", pid=1234, repo_path="/tmp/repo", title="Test",
                worktree="/tmp/worktree")
    store.remove("AGD-1")
    assert not store.is_tracked("AGD-1")


def test_agents_for_repo(tmp_path):
    store = StateStore(path=tmp_path / "state.json")
    store.track("AGD-1", pid=1, repo_path="/tmp/repo-a", title="A", worktree="/tmp/wt-a")
    store.track("AGD-2", pid=2, repo_path="/tmp/repo-b", title="B", worktree="/tmp/wt-b")
    store.track("AGD-3", pid=3, repo_path="/tmp/repo-a", title="C", worktree="/tmp/wt-c")

    assert len(store.agents_for_repo("/tmp/repo-a")) == 2
    assert len(store.agents_for_repo("/tmp/repo-b")) == 1


def test_touch_updates_activity(tmp_path):
    store = StateStore(path=tmp_path / "state.json")
    store.track("AGD-1", pid=1234, repo_path="/tmp", title="Test", worktree="/tmp/wt")

    before = store.get("AGD-1").last_activity_at
    time.sleep(0.01)
    store.touch("AGD-1")
    after = store.get("AGD-1").last_activity_at
    assert after > before


def test_attempts_increment(tmp_path):
    store = StateStore(path=tmp_path / "state.json")
    store.track("AGD-1", pid=1, repo_path="/tmp", title="Test", worktree="/tmp/wt")
    assert store.get("AGD-1").attempts == 1

    store.track("AGD-1", pid=2, repo_path="/tmp", title="Test", worktree="/tmp/wt")
    assert store.get("AGD-1").attempts == 2


def test_persistence(tmp_path):
    state_path = tmp_path / "state.json"
    store1 = StateStore(path=state_path)
    store1.track("AGD-1", pid=999, repo_path="/tmp", title="Test",
                 worktree="/tmp/wt", linear_issue_id="abc")

    store2 = StateStore(path=state_path)
    assert store2.is_tracked("AGD-1")
    agent = store2.get("AGD-1")
    assert agent.pid == 999
    assert agent.linear_issue_id == "abc"
