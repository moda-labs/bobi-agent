"""Tests for agent discovery under the single .modastack root.

Covers:
- find_runtime_root live-manager detection (nested-start guard)
- _sessions_dir always under the explicitly bound installation root
- Agents in repo checkouts sharing the registry via explicit binding
- list_agents merging in-memory and registry sources
- Nested runtime prevention in the start command
- CLI from a repo checkout resolving to the installation root
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
    (parent / ".modastack" / "agent.yaml").write_text("name: test-agent\n")

    child = parent / "jobtack"
    child.mkdir()
    (child / ".modastack" / "state").mkdir(parents=True)

    return parent, child


@pytest.fixture(autouse=True)
def reset_project_root():
    """Reset the global project root after each test."""
    import modastack.sdk as sdk
    from modastack import paths
    old = paths._root
    sdk._registry = None
    yield
    paths._root = old
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
        from modastack import paths
        old = paths._root
        paths._root = None
        try:
            assert find_runtime_root(None) is None
        finally:
            paths._root = old

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
# Tests: _sessions_dir is always the bound root
# ---------------------------------------------------------------------------

class TestSessionsDirBoundRoot:
    def test_uses_bound_root(self, tree):
        parent, _ = tree
        set_project_root(parent)
        sd = _sessions_dir()
        assert sd == parent / ".modastack" / "sessions"

    def test_no_walk_up_even_with_live_ancestor_manager(self, tree):
        """Sessions never escape the bound root. The old walk-up to a live
        manager.pid let a mis-bound agent scatter state across repo
        checkouts; binding is explicit now, so the bound root is final."""
        parent, child = tree
        (parent / ".modastack" / "state" / "manager.pid").write_text(str(os.getpid()))

        set_project_root(child)
        sd = _sessions_dir()
        assert sd == child / ".modastack" / "sessions"

    def test_raises_without_project_root(self):
        set_project_root(None)
        with pytest.raises(RuntimeError, match="not bound"):
            _sessions_dir()


# ---------------------------------------------------------------------------
# Tests: SessionRegistry cross-project visibility
# ---------------------------------------------------------------------------

class TestRegistryCrossProject:
    def test_agent_working_in_child_visible_from_parent(self, tree):
        parent, child = tree
        # Manager at parent
        (parent / ".modastack" / "state" / "manager.pid").write_text(str(os.getpid()))

        # Sub-agent works in the child repo but binds the installation
        # root its spawner passed — identity is inherited, not inferred.
        set_project_root(parent)
        registry = SessionRegistry()

        entry = SessionEntry(
            name="agent-42-implement",
            session_id="sess-abc",
            role="",
            run_key="42",
            phase="implement",
            project="jobtack",
            cwd=str(child),
            status="running",
            pid=os.getpid(),
        )
        registry.register(entry)

        # Verify it was written to parent's sessions dir
        assert (parent / ".modastack" / "sessions" / "agent-42-implement" / "state.json").exists()
        # NOT in child's sessions dir
        assert not (child / ".modastack" / "sessions" / "agent-42-implement" / "state.json").exists()

        # Now switch perspective: director sets project root to parent
        set_project_root(parent)
        import modastack.sdk as sdk
        sdk._registry = None  # reset singleton
        director_registry = SessionRegistry()
        active = director_registry.list_active()
        names = [e.name for e in active]
        assert "agent-42-implement" in names

    def test_agent_registered_locally_without_manager(self, tree):
        _, child = tree
        # No manager running anywhere

        set_project_root(child)
        registry = SessionRegistry()

        entry = SessionEntry(
            name="agent-99-spec",
            session_id="",
            role="",
            status="running",
            pid=os.getpid(),
        )
        registry.register(entry)

        # Should be in child's own sessions dir
        assert (child / ".modastack" / "sessions" / "agent-99-spec" / "state.json").exists()


# ---------------------------------------------------------------------------
# Tests: list_agents registry discovery
# ---------------------------------------------------------------------------

class TestListAgentsRegistry:
    def test_includes_registry_agents(self, tree):
        parent, child = tree
        (parent / ".modastack" / "state" / "manager.pid").write_text(str(os.getpid()))

        set_project_root(parent)

        # Manually create a session entry on disk (simulating a detached agent)
        session_dir = parent / ".modastack" / "sessions" / "agent-42-implement"
        session_dir.mkdir(parents=True)
        entry = SessionEntry(
            name="agent-42-implement",
            session_id="sess-x",
            role="",
            run_key="42",
            phase="implement",
            project="jobtack",
            cwd=str(child),
            status="running",
            pid=os.getpid(),
        )
        from dataclasses import asdict
        (session_dir / "state.json").write_text(json.dumps(asdict(entry)))

        from modastack.subagent import list_agents
        agents = list_agents()
        assert len(agents) >= 1
        names = [a.get("name") for a in agents]
        assert "agent-42-implement" in names

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

        from modastack.subagent import list_agents
        agents = list_agents()
        names = [a.get("name") for a in agents]
        assert "moda-manager-dev" not in names


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
# Tests: CLI from a repo checkout reaches the installation registry
# ---------------------------------------------------------------------------

class TestMessageRoutingCrossProject:
    def test_cli_from_child_resolves_root_and_finds_agent(self, tree):
        """A human running the CLI inside a repo checkout binds the
        installation root (agent.yaml walk-up), so the registry they see
        is the manager's — the child's stray state dir doesn't fork it."""
        parent, child = tree
        (parent / ".modastack" / "state" / "manager.pid").write_text(str(os.getpid()))

        # Register an agent in parent's sessions dir
        session_dir = parent / ".modastack" / "sessions" / "agent-42-implement"
        session_dir.mkdir(parents=True)
        entry = SessionEntry(
            name="agent-42-implement",
            session_id="sess-abc",
            role="",
            run_key="42",
            phase="implement",
            status="running",
            pid=os.getpid(),
        )
        from dataclasses import asdict
        (session_dir / "state.json").write_text(json.dumps(asdict(entry)))

        # CLI entry points bind via the agent.yaml walk-up, not raw cwd —
        # the child's .modastack here is state-only, so resolution must
        # pass over it and land on the installed root.
        from modastack.paths import resolve_root
        set_project_root(resolve_root(child))
        import modastack.sdk as sdk
        sdk._registry = None

        registry = get_registry()
        found = registry.get("agent-42-implement")
        assert found is not None
        assert found.session_id == "sess-abc"
