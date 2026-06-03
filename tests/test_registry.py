"""Tests for session registry — file-per-worker model."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from modastack.sdk import SessionEntry, SessionRegistry, ACTIVE_DIR


@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    active_dir = tmp_path / "active"
    active_dir.mkdir()
    monkeypatch.setattr("modastack.sdk.ACTIVE_DIR", active_dir)
    monkeypatch.setattr("modastack.sdk.SESSION_DIR", tmp_path)
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

    def test_remove(self, tmp_registry):
        tmp_registry.register(SessionEntry(name="eng-1"))
        tmp_registry.remove("eng-1")
        assert tmp_registry.get("eng-1") is None

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

    def test_file_per_worker(self, tmp_registry, tmp_path):
        """Each worker gets its own file."""
        tmp_registry.register(SessionEntry(name="eng-42", issue_id="42"))
        assert (tmp_path / "active" / "eng-42.json").exists()

    def test_remove_deletes_file(self, tmp_registry, tmp_path):
        tmp_registry.register(SessionEntry(name="eng-1"))
        assert (tmp_path / "active" / "eng-1.json").exists()
        tmp_registry.remove("eng-1")
        assert not (tmp_path / "active" / "eng-1.json").exists()

    def test_cross_process_visibility(self, tmp_path, monkeypatch):
        """Two registry instances see each other's entries."""
        active_dir = tmp_path / "active"
        active_dir.mkdir()
        monkeypatch.setattr("modastack.sdk.ACTIVE_DIR", active_dir)
        monkeypatch.setattr("modastack.sdk.SESSION_DIR", tmp_path)

        r1 = SessionRegistry()
        r1.register(SessionEntry(name="eng-42", issue_id="42", status="running"))

        r2 = SessionRegistry()
        got = r2.get("eng-42")
        assert got is not None
        assert got.issue_id == "42"

    def test_remove_in_one_process_visible_to_other(self, tmp_path, monkeypatch):
        active_dir = tmp_path / "active"
        active_dir.mkdir()
        monkeypatch.setattr("modastack.sdk.ACTIVE_DIR", active_dir)
        monkeypatch.setattr("modastack.sdk.SESSION_DIR", tmp_path)

        r1 = SessionRegistry()
        r1.register(SessionEntry(name="eng-42"))
        r1.remove("eng-42")

        r2 = SessionRegistry()
        assert r2.get("eng-42") is None
        assert len(r2.list_all()) == 0
