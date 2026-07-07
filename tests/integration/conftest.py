"""Integration test fixtures — fully isolated Bobi Agent installation.

Every integration test runs against a temporary BOBI_HOME. Nothing touches the
real ~/.bobi directory or any production state.
"""

import os
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

PACKAGE_ROOT = Path(__file__).parent.parent.parent
TEST_GRANTS_SECRET = "bobi-integration-test-grants"


@dataclass
class BobiEnv:
    """Paths for an isolated Bobi home."""
    home_dir: Path
    agent_name: str
    project_path: Path
    package_dir: Path
    state_dir: Path
    sessions_dir: Path
    workflows_dir: Path
    event_server_url: str
    env: dict[str, str]


@pytest.fixture(scope="session")
def bobi_env(tmp_path_factory):
    """Create a fully isolated Bobi home in a temp directory.

    Session-scoped: created once, shared across all integration tests.
    Includes a real git repo at the selected run root (for worktree support),
    config files, workflows, and empty credentials.
    """
    base = tmp_path_factory.mktemp("bobi")
    home_dir = base / "home"
    agent_name = "test-repo"
    project_path = home_dir / "agents" / agent_name / "run"
    package_dir = project_path / "package"
    state_dir = project_path / "state"
    sessions_dir = state_dir / "sessions"
    workflows_dir = package_dir / "workflows"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        event_server_port = sock.getsockname()[1]
    event_server_url = f"http://localhost:{event_server_port}"

    for d in [home_dir, package_dir, state_dir, sessions_dir, workflows_dir,
              state_dir / "workflow" / "runs", state_dir / "logs"]:
        d.mkdir(parents=True, exist_ok=True)

    old_home = os.environ.get("BOBI_HOME")
    old_root = os.environ.get("BOBI_ROOT")
    os.environ["BOBI_HOME"] = str(home_dir)
    os.environ.pop("BOBI_ROOT", None)

    # Build a local agent team, then install it via the machine-scoped CLI.
    pack_dir = base / "software_team"
    pack_dir.mkdir()
    (pack_dir / "agent.yaml").write_text(yaml.dump({
        "version": "1.0.0",
        "agent": "software_team",
        "entry_point": "manager",
        "event_server": {"url": event_server_url},
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

    (pack_dir / "workflows").mkdir()
    (pack_dir / "workflows" / "adhoc.yaml").write_text(
        "name: adhoc\n"
        "trigger: >\n"
        "  For any ad-hoc task.\n"
        "description: >\n"
        "  Open-ended task.\n"
        "steps:\n"
        "  - name: task\n"
        '    prompt: "${{input.task}}"\n'
    )

    (pack_dir / "workflows" / "two-step.yaml").write_text(
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

    env = {
        **os.environ,
        "BOBI_HOME": str(home_dir),
        "BOBI_EVENT_SERVER": event_server_url,
        "BOBI_ES_TEST_GRANTS_SECRET": TEST_GRANTS_SECRET,
    }

    result = subprocess.run(
        [
            sys.executable, "-m", "bobi.cli",
            "agents", "install", str(pack_dir),
            "--name", agent_name, "--non-interactive",
        ],
        capture_output=True, text=True, timeout=30,
        cwd=str(base), env=env,
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

    try:
        yield BobiEnv(
            home_dir=home_dir,
            agent_name=agent_name,
            project_path=project_path,
            package_dir=package_dir,
            state_dir=state_dir,
            sessions_dir=sessions_dir,
            workflows_dir=workflows_dir,
            event_server_url=event_server_url,
            env=env,
        )
    finally:
        if old_home is None:
            os.environ.pop("BOBI_HOME", None)
        else:
            os.environ["BOBI_HOME"] = old_home
        if old_root is None:
            os.environ.pop("BOBI_ROOT", None)
        else:
            os.environ["BOBI_ROOT"] = old_root


@pytest.fixture(autouse=True)
def _bind_bobi_env_for_test(request):
    """Bind the shared integration Bobi Agent only for tests that use it."""
    if "bobi_env" not in request.fixturenames:
        yield
        return

    from bobi import paths
    from bobi.sdk import set_project_root

    bobi_env = request.getfixturevalue("bobi_env")
    paths.bind_root(bobi_env.project_path)
    set_project_root(bobi_env.project_path)
    try:
        yield
    finally:
        set_project_root(None)
        paths.bind_root(None)


requires_claude = pytest.mark.skipif(
    not shutil.which("claude"),
    reason="claude CLI not installed",
)


@pytest.fixture
def cli_run(bobi_env):
    """Run bobi CLI commands against the isolated install."""
    def _run(*args, timeout=10):
        explicit_top_level = {
            "agent", "agents", "deploy", "destroy", "supervise", "version",
            "create-slack-bot", "skill", "login-bootstrap",
            "reply", "read-conversation",
        }
        argv = list(args)
        if argv and argv[0] not in explicit_top_level:
            argv = ["agent", bobi_env.agent_name, *argv]
        env = {
            **os.environ,
            "BOBI_HOME": str(bobi_env.home_dir),
            "BOBI_EVENT_SERVER": bobi_env.event_server_url,
            "BOBI_ES_TEST_GRANTS_SECRET": TEST_GRANTS_SECRET,
        }
        return subprocess.run(
            [sys.executable, "-m", "bobi.cli", *argv],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(bobi_env.project_path), env=env,
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
