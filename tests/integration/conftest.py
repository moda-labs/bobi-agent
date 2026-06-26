"""Integration test fixtures — fully isolated bobi installation.

Every integration test runs against a temporary bobi installation
in tmp_path. Nothing touches the real repo's .bobi/ directory
or any production state.
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

PACKAGE_ROOT = Path(__file__).parent.parent.parent


@dataclass
class BobiEnv:
    """Paths for an isolated bobi installation."""
    project_path: Path
    state_dir: Path
    sessions_dir: Path
    workflows_dir: Path


@pytest.fixture(scope="session")
def bobi_env(tmp_path_factory):
    """Create a fully isolated bobi installation in a temp directory.

    Session-scoped: created once, shared across all integration tests.
    Includes a real git repo (for worktree support), config files,
    workflows, and empty credentials.
    """
    base = tmp_path_factory.mktemp("bobi")
    project_path = base / "test-repo"

    config_dir = project_path / ".bobi"
    state_dir = config_dir / "state"
    sessions_dir = config_dir / "sessions"
    workflows_dir = config_dir / "workflows"

    for d in [config_dir, state_dir, sessions_dir, workflows_dir,
              state_dir / "workflow" / "runs", state_dir / "logs"]:
        d.mkdir(parents=True)

    # Build a local agent team, then install it via bobi install
    pack_dir = base / "software_team"
    pack_dir.mkdir()
    (pack_dir / "agent.yaml").write_text(yaml.dump({
        "version": "1.0.0",
        "agent": "software_team",
        "entry_point": "manager",
        "services": [
            {"name": "github", "events": True},
        ],
    }))
    for role_name, content in [
        ("manager", "# Manager\n\nYou are a test manager agent.\n"),
        ("engineer", "# Engineer\n\nYou are a test engineer agent. Complete tasks quickly.\n"),
        ("project_lead", "# Project Lead\n\nYou are a test project lead agent.\n"),
    ]:
        (pack_dir / "roles" / role_name).mkdir(parents=True)
        (pack_dir / "roles" / role_name / "ROLE.md").write_text(content)

    result = subprocess.run(
        [sys.executable, "-m", "bobi.cli", "install", str(pack_dir)],
        capture_output=True, text=True, timeout=30,
        cwd=str(project_path),
    )
    assert result.returncode == 0, f"install failed: {result.stderr}"

    subprocess.run(
        ["git", "init"], cwd=str(project_path),
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=str(project_path), capture_output=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test.com",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test.com"},
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/test-org/test-repo.git"],
        cwd=str(project_path), capture_output=True, check=True,
    )

    (workflows_dir / "adhoc.yaml").write_text(
        "name: adhoc\n"
        "trigger: >\n"
        "  For any ad-hoc task.\n"
        "description: >\n"
        "  Open-ended task.\n"
        "steps:\n"
        "  - name: task\n"
        '    prompt: "${{input.task}}"\n'
    )

    (workflows_dir / "two-step.yaml").write_text(
        "name: two-step\n"
        "trigger: >\n"
        "  For integration testing — two quick steps.\n"
        "steps:\n"
        "  - name: analyze\n"
        '    prompt: "Say STEP1_DONE and nothing else."\n'
        "    timeout: 60\n"
        "    handoff:\n"
        "      required: []\n"
        "  - name: summarize\n"
        '    prompt: "Say STEP2_DONE and nothing else."\n'
        "    timeout: 60\n"
    )

    from bobi.sdk import set_project_root
    set_project_root(project_path)

    yield BobiEnv(
        project_path=project_path,
        state_dir=state_dir,
        sessions_dir=sessions_dir,
        workflows_dir=workflows_dir,
    )

    set_project_root(None)


requires_claude = pytest.mark.skipif(
    not shutil.which("claude"),
    reason="claude CLI not installed",
)


@pytest.fixture
def cli_run(bobi_env):
    """Run bobi CLI commands against the isolated install."""
    def _run(*args, timeout=10):
        return subprocess.run(
            [sys.executable, "-m", "bobi.cli", *args],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(bobi_env.project_path),
        )
    return _run


@pytest.fixture
def clean_session(bobi_env):
    """Clean up a named session from the registry."""
    names = []

    def _register(name):
        names.append(name)
        from bobi.sdk import get_registry, SessionRegistry
        registry = get_registry()
        registry.mark_done(name)
        session_dir = SessionRegistry.session_dir(name)
        if session_dir.exists():
            shutil.rmtree(session_dir)

    yield _register

    for name in names:
        from bobi.sdk import get_registry, SessionRegistry
        registry = get_registry()
        registry.mark_done(name)
        session_dir = SessionRegistry.session_dir(name)
        if session_dir.exists():
            shutil.rmtree(session_dir)
