"""Tests for session registry — file-per-worker model."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from bobi import paths
from bobi.sdk import SessionEntry, SessionRegistry


@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    paths.bind_root(None)
    paths.package_dir(tmp_path).mkdir(parents=True)
    paths.agent_yaml_path(tmp_path).write_text("agent: test\n")
    paths.bind_root(tmp_path)
    yield SessionRegistry()
    paths.bind_root(None)


class TestSessionEntry:
    def test_defaults(self):
        e = SessionEntry(name="test")
        assert e.role == ""
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
        entry = SessionEntry(name="agent-42", run_key="42", phase="pickup")
        tmp_registry.register(entry)
        got = tmp_registry.get("agent-42")
        assert got is not None
        assert got.run_key == "42"

    def test_update(self, tmp_registry):
        tmp_registry.register(SessionEntry(name="agent-1", status="starting"))
        tmp_registry.update("agent-1", status="running")
        got = tmp_registry.get("agent-1")
        assert got.status == "running"

    def test_update_nonexistent_is_noop(self, tmp_registry):
        tmp_registry.update("does-not-exist", status="running")

    def test_mark_done_keeps_entry(self, tmp_registry):
        tmp_registry.register(SessionEntry(name="agent-1", status="running"))
        tmp_registry.mark_done("agent-1")
        assert tmp_registry.get("agent-1") is not None
        assert tmp_registry.get("agent-1").status == "done"

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
        tmp_registry.register(SessionEntry(name="agent-1", role="engineer"))
        tmp_registry.register(SessionEntry(name="agent-2", role="engineer"))
        engineers = tmp_registry.get_by_role("engineer")
        assert len(engineers) == 2

    def test_dir_per_session(self, tmp_registry, tmp_path):
        """Each session gets its own directory."""
        tmp_registry.register(SessionEntry(name="agent-42", run_key="42"))
        assert (tmp_path / "state" / "sessions" / "agent-42" / "state.json").exists()

    def test_mark_done_clears_pid(self, tmp_registry):
        tmp_registry.register(SessionEntry(name="agent-1", pid=12345, status="running"))
        tmp_registry.mark_done("agent-1")
        got = tmp_registry.get("agent-1")
        assert got.status == "done"
        assert got.pid == 0

    def test_handoff_path(self, tmp_path, monkeypatch):
        paths.bind_root(tmp_path)
        path = SessionRegistry.handoff_path("agent-42", "setup")
        assert path == tmp_path / "state" / "sessions" / "agent-42" / "handoff-setup.yaml"

    def test_log_path(self, tmp_path, monkeypatch):
        paths.bind_root(tmp_path)
        path = SessionRegistry.log_path("agent-42")
        assert path == tmp_path / "state" / "sessions" / "agent-42" / "log.jsonl"

    def test_cross_process_visibility(self, tmp_path, monkeypatch):
        """Two registry instances see each other's entries."""
        paths.bind_root(tmp_path)

        r1 = SessionRegistry()
        r1.register(SessionEntry(name="agent-42", run_key="42", status="running"))

        r2 = SessionRegistry()
        got = r2.get("agent-42")
        assert got is not None
        assert got.run_key == "42"

    def test_completed_session_stays_for_history(self, tmp_path, monkeypatch):
        paths.bind_root(tmp_path)

        r = SessionRegistry()
        r.register(SessionEntry(name="agent-42", status="running"))
        r.mark_done("agent-42")

        assert r.get("agent-42") is not None
        assert r.get("agent-42").status == "done"
        assert len(r.list_active()) == 0
        assert len(r.list_all()) == 1
