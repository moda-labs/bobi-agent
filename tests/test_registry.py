"""Tests for session registry persistence and subagent loop management."""

import asyncio
import json
import os
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


class TestReapDead:
    """reap_dead reconciles 'running' rows whose process has exited.

    This is the stale-status-registry fix: a detached engineer that completed
    or crashed without writing a terminal status must not linger as "running".
    """

    def test_reaps_running_with_dead_pid(self, tmp_registry):
        # PID 999999 is overwhelmingly unlikely to be a live process.
        tmp_registry.register(SessionEntry(name="eng-1", status="running", pid=999999))
        reaped = tmp_registry.reap_dead()
        assert reaped == ["eng-1"]
        assert tmp_registry.get("eng-1").status == "stale"

    def test_keeps_running_with_live_pid(self, tmp_registry):
        tmp_registry.register(SessionEntry(name="eng-1", status="running", pid=os.getpid()))
        assert tmp_registry.reap_dead() == []
        assert tmp_registry.get("eng-1").status == "running"

    def test_ignores_rows_without_pid(self, tmp_registry):
        """Legacy/in-process rows (pid 0) can't be probed — leave them be."""
        tmp_registry.register(SessionEntry(name="eng-1", status="running", pid=0))
        assert tmp_registry.reap_dead() == []
        assert tmp_registry.get("eng-1").status == "running"

    def test_does_not_touch_terminal_or_waiting(self, tmp_registry):
        tmp_registry.register(SessionEntry(name="done", status="done", pid=999999))
        tmp_registry.register(SessionEntry(name="wait", status="waiting", pid=999999))
        assert tmp_registry.reap_dead() == []
        assert tmp_registry.get("done").status == "done"
        assert tmp_registry.get("wait").status == "waiting"

    def test_pid_roundtrips(self, tmp_registry):
        tmp_registry.register(SessionEntry(name="eng-1", pid=12345))
        fresh = type(tmp_registry)()
        assert fresh.get("eng-1").pid == 12345

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

    def test_save_merges_disk_entries_from_other_processes(self, tmp_path, monkeypatch):
        """When a subprocess writes an entry to disk, the manager's save
        must not clobber it — save should read-merge-write."""
        registry_path = tmp_path / "registry.json"
        monkeypatch.setattr("modastack.sdk.REGISTRY_PATH", registry_path)
        monkeypatch.setattr("modastack.sdk.SESSION_DIR", tmp_path)

        # Manager creates its registry with one entry
        manager_reg = SessionRegistry()
        manager_reg.register(SessionEntry(name="moda-manager", role="manager", status="idle"))

        # Subprocess writes a second entry directly to disk (simulating
        # a subprocess that loaded, registered, and saved independently)
        disk = json.loads(registry_path.read_text())
        disk["eng-70"] = {
            "name": "eng-70", "role": "engineer", "issue_id": "70",
            "phase": "adhoc", "status": "running", "session_id": "",
            "title": "Fix conflicts", "repo": "moda-labs/memorize",
            "cwd": "/tmp", "started_at": 0, "last_activity": 0,
            "requested_by": {},
        }
        registry_path.write_text(json.dumps(disk))

        # Manager saves again (e.g. drain complete → registry.update)
        manager_reg.update("moda-manager", status="idle")

        # eng-70 must survive the manager's save
        final = json.loads(registry_path.read_text())
        assert "eng-70" in final, "Subprocess entry was clobbered by manager save"
        assert final["eng-70"]["status"] == "running"
        assert "moda-manager" in final

    def test_save_prefers_in_memory_over_disk_for_same_key(self, tmp_path, monkeypatch):
        """If both in-memory and disk have the same key, in-memory wins."""
        registry_path = tmp_path / "registry.json"
        monkeypatch.setattr("modastack.sdk.REGISTRY_PATH", registry_path)
        monkeypatch.setattr("modastack.sdk.SESSION_DIR", tmp_path)

        reg = SessionRegistry()
        reg.register(SessionEntry(name="eng-1", status="running"))

        # Someone else writes a stale status to disk
        disk = json.loads(registry_path.read_text())
        disk["eng-1"]["status"] = "starting"
        registry_path.write_text(json.dumps(disk))

        # Our save should keep "running", not revert to "starting"
        reg.update("eng-1", status="done")
        final = json.loads(registry_path.read_text())
        assert final["eng-1"]["status"] == "done"


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
