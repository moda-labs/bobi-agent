"""Tests for cross-project agent discovery (GitHub issue #145).

Covers:
- Walk-up runtime root resolution (find_runtime_root)
- _sessions_dir using the runtime root when a manager is running
- _sessions_dir falling back to local project root with no manager
- list_agents merging in-memory and registry sources
- Nested runtime prevention in the start command
- Message routing across project boundaries
"""

from __future__ import annotations

import json
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modastack.sdk import (
    SessionEntry,
    SessionRegistry,
    _pid_alive,
    _pid_file_alive,
    _sessions_dir,
    find_runtime_root,
    get_registry,
    set_project_root,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tree(tmp_path):
    """Create a directory tree simulating a director + child repo layout.

    ~/dev/                  ← director (parent)
    ~/dev/.modastack/
    ~/dev/.modastack/state/
    ~/dev/jobtack/          ← child repo
    ~/dev/jobtack/.modastack/
    ~/dev/jobtack/.modastack/state/
    """
    parent = tmp_path / "dev"
    parent.mkdir()
    (parent / ".modastack" / "state").mkdir(parents=True)
    (parent / ".modastack" / "sessions").mkdir(parents=True)

    child = parent / "jobtack"
    child.mkdir()
    (child / ".modastack" / "state").mkdir(parents=True)

    return parent, child


@pytest.fixture(autouse=True)
def reset_project_root():
    """Reset the global project root after each test."""
    import modastack.sdk as sdk
    old = sdk._project_root
    sdk._registry = None
    yield
    sdk._project_root = old
    sdk._registry = None


# ---------------------------------------------------------------------------
# Tests: _pid_file_alive
# ---------------------------------------------------------------------------

class TestPidFileAlive:
    def test_no_file(self, tmp_path):
        assert _pid_file_alive(tmp_path / "nonexistent.pid") is False

    def test_invalid_content(self, tmp_path):
        pid_file = tmp_path / "manager.pid"
        pid_file.write_text("not-a-number")
        assert _pid_file_alive(pid_file) is False

    def test_dead_pid(self, tmp_path):
        pid_file = tmp_path / "manager.pid"
        # PID 99999999 almost certainly doesn't exist
        pid_file.write_text("99999999")
        assert _pid_file_alive(pid_file) is False

    def test_alive_pid(self, tmp_path):
        pid_file = tmp_path / "manager.pid"
        pid_file.write_text(str(os.getpid()))
        assert _pid_file_alive(pid_file) is True


# ---------------------------------------------------------------------------
# Tests: find_runtime_root
# ---------------------------------------------------------------------------

class TestFindRuntimeRoot:
    def test_finds_parent_with_live_manager(self, tree):
        parent, child = tree
        # Simulate a live manager in parent
        pid_file = parent / ".modastack" / "state" / "manager.pid"
        pid_file.write_text(str(os.getpid()))

        result = find_runtime_root(child)
        assert result == parent

    def test_finds_self_with_live_manager(self, tree):
        parent, child = tree
        pid_file = parent / ".modastack" / "state" / "manager.pid"
        pid_file.write_text(str(os.getpid()))

        result = find_runtime_root(parent)
        assert result == parent

    def test_returns_none_when_no_manager(self, tree):
        _, child = tree
        result = find_runtime_root(child)
        assert result is None

    def test_returns_none_with_stale_pid(self, tree):
        parent, child = tree
        pid_file = parent / ".modastack" / "state" / "manager.pid"
        pid_file.write_text("99999999")  # dead PID

        result = find_runtime_root(child)
        assert result is None

    def test_returns_none_when_start_is_none(self):
        import modastack.sdk as sdk
        old = sdk._project_root
        sdk._project_root = None
        try:
            assert find_runtime_root(None) is None
        finally:
            sdk._project_root = old

    def test_uses_project_root_as_default(self, tree):
        parent, _ = tree
        pid_file = parent / ".modastack" / "state" / "manager.pid"
        pid_file.write_text(str(os.getpid()))

        set_project_root(parent)
        result = find_runtime_root()  # no explicit start
        assert result == parent

    def test_prefers_closest_ancestor(self, tmp_path):
        """When multiple ancestors have live managers, returns the closest."""
        grandparent = tmp_path / "a"
        parent = grandparent / "b"
        child = parent / "c"
        for d in (grandparent, parent, child):
            (d / ".modastack" / "state").mkdir(parents=True)

        # Both grandparent and parent have live managers
        (grandparent / ".modastack" / "state" / "manager.pid").write_text(str(os.getpid()))
        (parent / ".modastack" / "state" / "manager.pid").write_text(str(os.getpid()))

        # From child, should find parent (closest)
        result = find_runtime_root(child)
        assert result == parent


# ---------------------------------------------------------------------------
# Tests: _sessions_dir with walk-up
# ---------------------------------------------------------------------------

class TestSessionsDirWalkUp:
    def test_uses_runtime_root_when_manager_running(self, tree):
        parent, child = tree
        # Manager running at parent
        (parent / ".modastack" / "state" / "manager.pid").write_text(str(os.getpid()))

        set_project_root(child)
        sd = _sessions_dir()
        assert sd == parent / ".modastack" / "sessions"

    def test_falls_back_to_project_root_without_manager(self, tree):
        _, child = tree
        set_project_root(child)
        sd = _sessions_dir()
        assert sd == child / ".modastack" / "sessions"

    def test_uses_local_when_manager_is_self(self, tree):
        parent, _ = tree
        # Manager running at same level
        (parent / ".modastack" / "state" / "manager.pid").write_text(str(os.getpid()))

        set_project_root(parent)
        sd = _sessions_dir()
        assert sd == parent / ".modastack" / "sessions"

    def test_raises_without_project_root(self):
        set_project_root(None)
        with pytest.raises(RuntimeError, match="project root not set"):
            _sessions_dir()


# ---------------------------------------------------------------------------
# Tests: SessionRegistry cross-project visibility
# ---------------------------------------------------------------------------

class TestRegistryCrossProject:
    def test_agent_registered_in_child_visible_from_parent(self, tree):
        parent, child = tree
        # Manager at parent
        (parent / ".modastack" / "state" / "manager.pid").write_text(str(os.getpid()))

        # Sub-agent sets its project root to child
        set_project_root(child)
        registry = SessionRegistry()

        entry = SessionEntry(
            name="eng-42-implement",
            session_id="sess-abc",
            role="engineer",
            issue_id="42",
            phase="implement",
            project="jobtack",
            cwd=str(child),
            status="running",
            pid=os.getpid(),
        )
        registry.register(entry)

        # Verify it was written to parent's sessions dir
        assert (parent / ".modastack" / "sessions" / "eng-42-implement" / "state.json").exists()
        # NOT in child's sessions dir
        assert not (child / ".modastack" / "sessions" / "eng-42-implement" / "state.json").exists()

        # Now switch perspective: director sets project root to parent
        set_project_root(parent)
        import modastack.sdk as sdk
        sdk._registry = None  # reset singleton
        director_registry = SessionRegistry()
        active = director_registry.list_active()
        names = [e.name for e in active]
        assert "eng-42-implement" in names

    def test_agent_registered_locally_without_manager(self, tree):
        _, child = tree
        # No manager running anywhere

        set_project_root(child)
        registry = SessionRegistry()

        entry = SessionEntry(
            name="eng-99-spec",
            session_id="",
            role="engineer",
            status="running",
            pid=os.getpid(),
        )
        registry.register(entry)

        # Should be in child's own sessions dir
        assert (child / ".modastack" / "sessions" / "eng-99-spec" / "state.json").exists()


# ---------------------------------------------------------------------------
# Tests: list_agents merging in-memory + registry
# ---------------------------------------------------------------------------

class TestListAgentsMerge:
    def test_includes_registry_agents(self, tree):
        parent, child = tree
        (parent / ".modastack" / "state" / "manager.pid").write_text(str(os.getpid()))

        set_project_root(parent)

        # Manually create a session entry on disk (simulating a detached agent)
        session_dir = parent / ".modastack" / "sessions" / "eng-42-implement"
        session_dir.mkdir(parents=True)
        entry = SessionEntry(
            name="eng-42-implement",
            session_id="sess-x",
            role="engineer",
            issue_id="42",
            phase="implement",
            project="jobtack",
            cwd=str(child),
            status="running",
            pid=os.getpid(),
        )
        from dataclasses import asdict
        (session_dir / "state.json").write_text(json.dumps(asdict(entry)))

        from modastack.subagent import list_agents, _running
        _running.clear()
        try:
            agents = list_agents()
            assert len(agents) >= 1
            names = [a.get("name") for a in agents]
            assert "eng-42-implement" in names
            # Should be from registry source
            registry_agent = next(a for a in agents if a["name"] == "eng-42-implement")
            assert registry_agent["source"] == "registry"
        finally:
            _running.clear()

    def test_deduplicates_in_memory_and_registry(self, tree):
        """If an agent appears in both _running and registry, list it once."""
        import asyncio
        parent, _ = tree
        (parent / ".modastack" / "state" / "manager.pid").write_text(str(os.getpid()))

        set_project_root(parent)

        # Create on-disk entry
        session_dir = parent / ".modastack" / "sessions" / "eng-dup-1-implement"
        session_dir.mkdir(parents=True)
        entry = SessionEntry(
            name="eng-dup-1-implement",
            session_id="sess-dup",
            role="engineer",
            issue_id="DUP-1",
            phase="implement",
            status="running",
            pid=os.getpid(),
        )
        from dataclasses import asdict
        (session_dir / "state.json").write_text(json.dumps(asdict(entry)))

        # Also add to in-memory _running
        from modastack.subagent import _running, RunningAgent, list_agents
        loop = asyncio.new_event_loop()
        future = loop.create_future()
        task = asyncio.ensure_future(future, loop=loop)
        _running["dup-1"] = RunningAgent(
            issue_id="DUP-1",
            phase="implement",
            session_id="sess-dup",
            task=task,
            cwd="/tmp",
        )

        try:
            agents = list_agents()
            matching = [a for a in agents if a["issue_id"] == "DUP-1"]
            assert len(matching) == 1
            assert matching[0]["source"] == "in-process"
        finally:
            task.cancel()
            loop.close()
            _running.clear()

    def test_excludes_managers_from_registry(self, tree):
        parent, _ = tree
        (parent / ".modastack" / "state" / "manager.pid").write_text(str(os.getpid()))

        set_project_root(parent)

        # Create a manager session entry on disk
        session_dir = parent / ".modastack" / "sessions" / "moda-manager-dev"
        session_dir.mkdir(parents=True)
        entry = SessionEntry(
            name="moda-manager-dev",
            role="manager",
            status="running",
            pid=os.getpid(),
        )
        from dataclasses import asdict
        (session_dir / "state.json").write_text(json.dumps(asdict(entry)))

        from modastack.subagent import list_agents, _running
        _running.clear()
        try:
            agents = list_agents()
            names = [a.get("name") for a in agents]
            assert "moda-manager-dev" not in names
        finally:
            _running.clear()


# ---------------------------------------------------------------------------
# Tests: Nested runtime prevention
# ---------------------------------------------------------------------------

class TestNestedRuntimePrevention:
    def test_start_rejects_when_ancestor_has_manager(self, tree):
        parent, child = tree
        # Manager running at parent
        (parent / ".modastack" / "state" / "manager.pid").write_text(str(os.getpid()))

        # Create agent.yaml in child so start doesn't fail for missing agent
        (child / ".modastack" / "agent.yaml").write_text("agent: software_team\n")

        from click.testing import CliRunner
        from modastack.cli import main

        runner = CliRunner()
        with patch("modastack.cli._detect_project_root", return_value=child):
            result = runner.invoke(main, ["start"])

        assert result.exit_code == 1
        assert "already running" in result.output.lower() or "already running" in (result.output + (result.stderr_bytes or b'').decode()).lower()

    def test_start_allows_when_no_ancestor_manager(self, tree):
        """start should proceed normally when no ancestor has a running manager."""
        _, child = tree
        # No manager running at parent

        # We just verify the nested-runtime check passes (not the full start flow)
        from modastack.sdk import find_runtime_root
        ancestor = find_runtime_root(child.parent)
        assert ancestor is None  # no blocking ancestor


# ---------------------------------------------------------------------------
# Tests: Message routing across projects
# ---------------------------------------------------------------------------

class TestMessageRoutingCrossProject:
    def test_resolve_address_finds_agent_in_runtime_root(self, tree):
        parent, child = tree
        (parent / ".modastack" / "state" / "manager.pid").write_text(str(os.getpid()))

        # Register an agent in parent's sessions dir
        session_dir = parent / ".modastack" / "sessions" / "eng-42-implement"
        session_dir.mkdir(parents=True)
        entry = SessionEntry(
            name="eng-42-implement",
            session_id="sess-abc",
            role="engineer",
            issue_id="42",
            phase="implement",
            status="running",
            pid=os.getpid(),
            inbox_port=12345,
        )
        from dataclasses import asdict
        (session_dir / "state.json").write_text(json.dumps(asdict(entry)))

        # Simulate CLI running from child project
        set_project_root(child)
        import modastack.sdk as sdk
        sdk._registry = None

        registry = get_registry()
        found = registry.get("eng-42-implement")
        assert found is not None
        assert found.inbox_port == 12345
