"""Tests for explicit Bobi Agent runtime binding.

Runtime identity no longer comes from cwd or a project-local `.bobi` walk-up.
Processes bind one selected run root, and session state lives under that root's
state/ directory.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from bobi import paths
from bobi.sdk import (
    SessionEntry,
    SessionRegistry,
    _pid_file_alive,
    _sessions_dir,
    find_runtime_root,
    get_registry,
    set_project_root,
)


@pytest.fixture
def run_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / "agents" / "eng" / "run"
    (root / "package").mkdir(parents=True)
    (root / "package" / "agent.yaml").write_text("agent: test-agent\n")
    (root / "state").mkdir()
    (root / "workspace" / "repo").mkdir(parents=True)
    monkeypatch.setenv("BOBI_HOME", str(home))
    return root


@pytest.fixture(autouse=True)
def reset_registry():
    import bobi.sdk as sdk
    old_root = paths.bound_root()
    old_registry = sdk._registry
    paths.bind_root(None)
    sdk._registry = None
    yield
    paths.bind_root(None)
    if old_root is not None:
        paths.bind_root(old_root)
    sdk._registry = old_registry


class TestPidFileAlive:
    def test_no_file(self, tmp_path):
        assert _pid_file_alive(tmp_path / "nonexistent.pid") is False

    def test_invalid_content(self, tmp_path):
        pid_file = tmp_path / "manager.pid"
        pid_file.write_text("not-a-number")
        assert _pid_file_alive(pid_file) is False

    def test_dead_pid(self, tmp_path):
        pid_file = tmp_path / "manager.pid"
        pid_file.write_text("99999999")
        assert _pid_file_alive(pid_file) is False

    def test_alive_pid(self, tmp_path):
        pid_file = tmp_path / "manager.pid"
        pid_file.write_text(str(os.getpid()))
        assert _pid_file_alive(pid_file) is True


class TestFindRuntimeRoot:
    def test_finds_live_bound_root(self, run_root):
        paths.manager_pid_path(run_root).write_text(str(os.getpid()))
        set_project_root(run_root)

        assert find_runtime_root() == run_root

    def test_finds_live_ancestor_runtime(self, run_root):
        paths.manager_pid_path(run_root).write_text(str(os.getpid()))
        deep_repo = run_root / "workspace" / "repo" / "src"
        deep_repo.mkdir()

        assert find_runtime_root(deep_repo) == run_root

    def test_returns_none_without_live_manager(self, run_root):
        assert find_runtime_root(run_root / "workspace" / "repo") is None

    def test_returns_none_when_unbound(self):
        assert find_runtime_root(None) is None


class TestSessionsDirBoundRoot:
    def test_uses_bound_run_root(self, run_root):
        set_project_root(run_root)
        assert _sessions_dir() == run_root / "state" / "sessions"

    def test_raises_without_bound_root(self):
        with pytest.raises(RuntimeError, match="not bound"):
            _sessions_dir()


class TestSessionRegistry:
    def test_agent_working_in_workspace_uses_bound_runtime_registry(self, run_root):
        work_repo = run_root / "workspace" / "repo"
        set_project_root(run_root)
        registry = SessionRegistry()

        entry = SessionEntry(
            name="agent-42-implement",
            session_id="sess-abc",
            run_key="42",
            phase="implement",
            cwd=str(work_repo),
            status="running",
            pid=os.getpid(),
        )
        registry.register(entry)

        state_file = run_root / "state" / "sessions" / entry.name / "state.json"
        assert state_file.exists()
        assert json.loads(state_file.read_text())["cwd"] == str(work_repo)

        import bobi.sdk as sdk
        sdk._registry = None
        found = get_registry().get(entry.name)
        assert found is not None
        assert found.session_id == "sess-abc"

    def test_list_agents_reads_bound_registry(self, run_root):
        set_project_root(run_root)
        session_dir = run_root / "state" / "sessions" / "agent-42-implement"
        session_dir.mkdir(parents=True)
        entry = SessionEntry(
            name="agent-42-implement",
            session_id="sess-x",
            run_key="42",
            phase="implement",
            status="running",
            pid=os.getpid(),
        )
        (session_dir / "state.json").write_text(json.dumps(asdict(entry)))

        from bobi.subagent import list_agents
        assert [a.get("name") for a in list_agents()] == ["agent-42-implement"]

    def test_managers_are_excluded_from_agent_list(self, run_root):
        set_project_root(run_root)
        session_dir = run_root / "state" / "sessions" / "bobi-manager-eng"
        session_dir.mkdir(parents=True)
        entry = SessionEntry(name="bobi-manager-eng", role="manager",
                             status="running", pid=os.getpid())
        (session_dir / "state.json").write_text(json.dumps(asdict(entry)))

        from bobi.subagent import list_agents
        assert list_agents() == []
