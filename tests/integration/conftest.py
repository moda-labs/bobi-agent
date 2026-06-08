"""Integration test fixtures — fully isolated modastack installation.

Every integration test runs against a temporary modastack installation
in tmp_path. Nothing touches the real repo's .modastack/ directory
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
class ModastackEnv:
    """Paths for an isolated modastack installation."""
    project_path: Path
    state_dir: Path
    sessions_dir: Path
    workflows_dir: Path


@pytest.fixture(scope="session")
def modastack_env(tmp_path_factory):
    """Create a fully isolated modastack installation in a temp directory.

    Session-scoped: created once, shared across all integration tests.
    Includes a real git repo (for worktree support), config files,
    workflows, and empty credentials.
    """
    base = tmp_path_factory.mktemp("modastack")
    project_path = base / "test-repo"

    config_dir = project_path / ".modastack"
    state_dir = config_dir / "state"
    sessions_dir = config_dir / "sessions"
    workflows_dir = config_dir / "workflows"

    for d in [config_dir, state_dir, sessions_dir, workflows_dir,
              state_dir / "workflow" / "runs", state_dir / "logs"]:
        d.mkdir(parents=True)

    (config_dir / "agent.yaml").write_text(yaml.dump({
        "agent": "software_team",
        "role": "manager",
    }))

    (config_dir / "config.yaml").write_text("{}")

    # Create a minimal software_team agent pack in the project
    pack_dir = config_dir / "agents" / "software_team"
    for role_name in ["manager", "engineer", "project_lead"]:
        (pack_dir / "roles" / role_name).mkdir(parents=True)
    (pack_dir / "defaults.yaml").write_text(
        "version: \"1.0.0\"\nrole: manager\nevent_sources:\n  - github\n"
    )
    (pack_dir / "roles" / "manager" / "ROLE.md").write_text(
        "# Manager\n\nYou are a test manager agent.\n"
    )
    (pack_dir / "roles" / "engineer" / "ROLE.md").write_text(
        "# Engineer\n\nYou are a test engineer agent. Complete tasks quickly.\n"
    )
    (pack_dir / "roles" / "project_lead" / "ROLE.md").write_text(
        "# Project Lead\n\nYou are a test project lead agent.\n"
    )

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

    adhoc_src = PACKAGE_ROOT / "modastack" / "workflow" / "adhoc.yaml"
    if adhoc_src.exists():
        shutil.copy2(adhoc_src, workflows_dir / "adhoc.yaml")

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

    from modastack.sdk import set_project_root
    set_project_root(project_path)

    yield ModastackEnv(
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
def cli_run(modastack_env):
    """Run modastack CLI commands against the isolated install."""
    def _run(*args, timeout=10):
        return subprocess.run(
            [sys.executable, "-m", "modastack.cli", *args],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(modastack_env.project_path),
        )
    return _run


@pytest.fixture
def clean_session(modastack_env):
    """Clean up a named session from the registry."""
    names = []

    def _register(name):
        names.append(name)
        from modastack.sdk import get_registry, SessionRegistry
        registry = get_registry()
        registry.mark_done(name)
        session_dir = SessionRegistry.session_dir(name)
        if session_dir.exists():
            shutil.rmtree(session_dir)

    yield _register

    for name in names:
        from modastack.sdk import get_registry, SessionRegistry
        registry = get_registry()
        registry.mark_done(name)
        session_dir = SessionRegistry.session_dir(name)
        if session_dir.exists():
            shutil.rmtree(session_dir)
