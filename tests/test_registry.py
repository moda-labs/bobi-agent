"""Tests for session registry persistence and subagent loop management."""

import asyncio
import json
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from modastack.sdk import SessionEntry, SessionRegistry, REGISTRY_PATH


@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr("modastack.sdk.REGISTRY_PATH", registry_path)
    monkeypatch.setattr("modastack.sdk.SESSION_DIR", tmp_path)
    return SessionRegistry()


class TestSessionEntry:
    def test_defaults(self):
        e = SessionEntry(name="test")
        assert e.role == "engineer"
        assert e.status == "starting"
        assert e.session_id == ""
        assert e.issue_id == ""

    def test_manager_role(self):
        e = SessionEntry(name="moda-manager", role="manager")
        assert e.role == "manager"

    def test_requested_by_defaults_empty(self):
        e = SessionEntry(name="test")
        assert e.requested_by == {}

    def test_requested_by_roundtrips(self, tmp_registry):
        requester = {"from": "Alice", "user_id": "U1", "channel": "C1",
                     "thread_ts": "171.42"}
        tmp_registry.register(SessionEntry(name="adhoc-x", requested_by=requester))
        # Force a reload from disk to prove it survives serialization.
        fresh = type(tmp_registry)()
        got = fresh.get("adhoc-x")
        assert got.requested_by == requester


class TestSessionRegistry:
    def test_register_and_get(self, tmp_registry):
        entry = SessionEntry(name="eng-42", issue_id="42", phase="pickup")
        tmp_registry.register(entry)
        got = tmp_registry.get("eng-42")
        assert got is not None
        assert got.issue_id == "42"
        assert got.phase == "pickup"

    def test_update(self, tmp_registry):
        tmp_registry.register(SessionEntry(name="eng-1", status="starting"))
        tmp_registry.update("eng-1", status="running", session_id="abc123")
        got = tmp_registry.get("eng-1")
        assert got.status == "running"
        assert got.session_id == "abc123"

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

    def test_persistence_across_instances(self, tmp_path, monkeypatch):
        registry_path = tmp_path / "registry.json"
        monkeypatch.setattr("modastack.sdk.REGISTRY_PATH", registry_path)
        monkeypatch.setattr("modastack.sdk.SESSION_DIR", tmp_path)

        r1 = SessionRegistry()
        r1.register(SessionEntry(name="eng-42", issue_id="42", status="running"))

        r2 = SessionRegistry()
        got = r2.get("eng-42")
        assert got is not None
        assert got.issue_id == "42"
        assert got.status == "running"

    def test_corrupt_registry_starts_fresh(self, tmp_path, monkeypatch):
        registry_path = tmp_path / "registry.json"
        registry_path.write_text("not valid json {{{")
        monkeypatch.setattr("modastack.sdk.REGISTRY_PATH", registry_path)
        monkeypatch.setattr("modastack.sdk.SESSION_DIR", tmp_path)

        r = SessionRegistry()
        assert len(r.list_all()) == 0


class TestSubagentLoop:
    def test_ensure_loop_creates_running_loop(self):
        from modastack.subagent import _ensure_loop
        loop = _ensure_loop()
        assert loop is not None
        assert loop.is_running()

    def test_ensure_loop_actually_executes_coroutines(self):
        from modastack.subagent import _ensure_loop
        loop = _ensure_loop()

        async def _add(a, b):
            return a + b

        future = asyncio.run_coroutine_threadsafe(_add(2, 3), loop)
        assert future.result(timeout=5) == 5

    def test_ensure_loop_is_reentrant(self):
        from modastack.subagent import _ensure_loop
        loop1 = _ensure_loop()
        loop2 = _ensure_loop()
        assert loop1 is loop2

    def test_run_phase_registers_in_registry(self, tmp_path, monkeypatch):
        """run_phase should eagerly register the session in the registry."""
        registry_path = tmp_path / "registry.json"
        monkeypatch.setattr("modastack.sdk.REGISTRY_PATH", registry_path)
        monkeypatch.setattr("modastack.sdk.SESSION_DIR", tmp_path)

        import modastack.sdk
        monkeypatch.setattr(modastack.sdk, "_registry", None)

        from modastack.subagent import run_phase, _running

        with patch("modastack.subagent._ensure_loop") as mock_loop:
            loop = MagicMock()
            future = MagicMock()
            future.done.return_value = False
            mock_loop.return_value = loop

            with patch("asyncio.run_coroutine_threadsafe", return_value=future):
                run_phase("TEST-99", "pickup", str(tmp_path))

        from modastack.sdk import get_registry
        r = get_registry()
        entry = r.get("eng-test-99-pickup")
        assert entry is not None
        assert entry.role == "engineer"
        assert entry.issue_id == "TEST-99"
        assert entry.phase == "pickup"
        assert entry.status == "starting"

        if "test-99" in _running:
            del _running["test-99"]
