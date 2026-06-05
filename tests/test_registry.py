"""Tests for session registry — file-per-worker model."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from modastack.sdk import SessionEntry, SessionRegistry, SESSION_DIR


@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    monkeypatch.setattr("modastack.sdk._repo_root", tmp_path)
    return SessionRegistry()


class TestSessionEntry:
    def test_defaults(self):
        e = SessionEntry(name="test")
        assert e.role == "engineer"
        assert e.status == "starting"
        assert e.session_id == ""

    def test_manager_role(self):
        e = SessionEntry(name="moda-manager", role="manager")
        assert e.role == "manager"

    def test_requested_by_defaults_empty(self):
        e = SessionEntry(name="test")
        assert e.requested_by == {}

    def test_requested_by_roundtrips(self, tmp_registry):
        requester = {"from": "Alice", "user_id": "U1", "channel": "C1"}
        tmp_registry.register(SessionEntry(name="adhoc-x", requested_by=requester))
        got = tmp_registry.get("adhoc-x")
        assert got.requested_by == requester


class TestSessionRegistry:
    def test_register_and_get(self, tmp_registry):
        entry = SessionEntry(name="eng-42", issue_id="42", phase="pickup")
        tmp_registry.register(entry)
        got = tmp_registry.get("eng-42")
        assert got is not None
        assert got.issue_id == "42"

    def test_update(self, tmp_registry):
        tmp_registry.register(SessionEntry(name="eng-1", status="starting"))
        tmp_registry.update("eng-1", status="running")
        got = tmp_registry.get("eng-1")
        assert got.status == "running"

    def test_update_nonexistent_is_noop(self, tmp_registry):
        tmp_registry.update("does-not-exist", status="running")

    def test_mark_done_keeps_entry(self, tmp_registry):
        tmp_registry.register(SessionEntry(name="eng-1", status="running"))
        tmp_registry.mark_done("eng-1")
        assert tmp_registry.get("eng-1") is not None
        assert tmp_registry.get("eng-1").status == "done"

    def test_list_active(self, tmp_registry):
        tmp_registry.register(SessionEntry(name="a", status="running"))
        tmp_registry.register(SessionEntry(name="b", status="done"))
        tmp_registry.register(SessionEntry(name="c", status="idle"))
        active = tmp_registry.list_active()
        names = {e.name for e in active}
        assert names == {"a", "c"}

    def test_list_all(self, tmp_registry):
        tmp_registry.register(SessionEntry(name="a", status="running"))
        tmp_registry.register(SessionEntry(name="b", status="done"))
        assert len(tmp_registry.list_all()) == 2

    def test_get_by_role(self, tmp_registry):
        tmp_registry.register(SessionEntry(name="mgr", role="manager"))
        tmp_registry.register(SessionEntry(name="eng-1", role="engineer"))
        tmp_registry.register(SessionEntry(name="eng-2", role="engineer"))
        engineers = tmp_registry.get_by_role("engineer")
        assert len(engineers) == 2

    def test_dir_per_session(self, tmp_registry, tmp_path):
        """Each session gets its own directory."""
        tmp_registry.register(SessionEntry(name="eng-42", issue_id="42"))
        assert (tmp_path / ".modastack" / "sessions" / "eng-42" / "state.json").exists()

    def test_mark_done_clears_pid(self, tmp_registry):
        tmp_registry.register(SessionEntry(name="eng-1", pid=12345, status="running"))
        tmp_registry.mark_done("eng-1")
        got = tmp_registry.get("eng-1")
        assert got.status == "done"
        assert got.pid == 0

    def test_handoff_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.sdk._repo_root", tmp_path)
        path = SessionRegistry.handoff_path("eng-42", "setup")
        assert path == tmp_path / ".modastack" / "sessions" / "eng-42" / "handoff-setup.yaml"

    def test_log_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.sdk._repo_root", tmp_path)
        path = SessionRegistry.log_path("eng-42")
        assert path == tmp_path / ".modastack" / "sessions" / "eng-42" / "log.jsonl"

    def test_cross_process_visibility(self, tmp_path, monkeypatch):
        """Two registry instances see each other's entries."""
        monkeypatch.setattr("modastack.sdk._repo_root", tmp_path)

        r1 = SessionRegistry()
        r1.register(SessionEntry(name="eng-42", issue_id="42", status="running"))

        r2 = SessionRegistry()
        got = r2.get("eng-42")
        assert got is not None
        assert got.issue_id == "42"

    def test_completed_session_stays_for_history(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.sdk._repo_root", tmp_path)

        r = SessionRegistry()
        r.register(SessionEntry(name="eng-42", status="running"))
        r.mark_done("eng-42")

        assert r.get("eng-42") is not None
        assert r.get("eng-42").status == "done"
        assert len(r.list_active()) == 0
        assert len(r.list_all()) == 1

    def test_reaps_zombie_starting_sessions(self, tmp_path, monkeypatch):
        """Sessions stuck in 'starting' with pid=0 for >5 min are reaped."""
        import time
        monkeypatch.setattr("modastack.sdk.SESSION_DIR", tmp_path)

        r = SessionRegistry()
        r.register(SessionEntry(
            name="zombie", status="starting", pid=0,
            started_at=time.time() - 600,
        ))
        active = r.list_active()
        assert len(active) == 0
        assert r.get("zombie").status == "done"

    def test_fresh_starting_session_not_reaped(self, tmp_path, monkeypatch):
        """A recently-started session with pid=0 should NOT be reaped yet."""
        import time
        monkeypatch.setattr("modastack.sdk.SESSION_DIR", tmp_path)

        r = SessionRegistry()
        r.register(SessionEntry(
            name="fresh", status="starting", pid=0,
            started_at=time.time(),
        ))
        active = r.list_active()
        assert len(active) == 1
        assert active[0].name == "fresh"
