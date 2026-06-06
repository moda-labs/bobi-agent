"""Integration test fixtures — fully isolated modastack installation.

Every integration test runs against a temporary modastack installation
in tmp_path. Nothing touches the real repo's .modastack/ directory,
the user's ~/.modastack, or any production state.

Two fixture scopes:
  - modastack_env (session): isolated repo dir with config, git init,
    workflows, and all path constants redirected
  - manager_session (class): live Claude Code session for tests that
    inject events and check responses
"""

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
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
    dashboard_port: int


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def modastack_env(tmp_path_factory):
    """Create a fully isolated modastack installation in a temp directory.

    Session-scoped: created once, shared across all integration tests.
    Includes a real git repo (for worktree support), config files,
    workflows, and empty credentials.
    """
    base = tmp_path_factory.mktemp("modastack")
    project_path = base / "repo"

    config_dir = project_path / ".modastack"
    state_dir = config_dir / "state"
    sessions_dir = config_dir / "sessions"
    workflows_dir = config_dir / "workflows"

    for d in [config_dir, state_dir, sessions_dir, workflows_dir,
              state_dir / "workflow" / "runs", state_dir / "logs"]:
        d.mkdir(parents=True)

    dashboard_port = _free_port()

    # Repo config
    (config_dir / "config.yaml").write_text(yaml.dump({
        "task_tracking": {"system": "github-issues", "project": "TEST"},
        "github": {"repo": "test-org/test-repo"},
        "agent": {"max_parallel": 2},
        "verify": {"test_command": "echo pass"},
    }))

    # Local config — empty creds prevent production connections
    (config_dir / "local.yaml").write_text(yaml.dump({
        "operator": {"name": "test", "email": "test@test.com"},
        "slack": {"bot_token": "", "dm_channel": ""},
        "event_server": {"url": "", "deployment_id": "", "api_key": ""},
        "dashboard_port": dashboard_port,
    }))

    # Credentials
    creds_dir = base / "config" / "modastack"
    creds_dir.mkdir(parents=True)
    (creds_dir / "credentials.yaml").write_text("{}")

    # Initialize a real git repo (needed for worktree-based workflows)
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

    # Copy the adhoc workflow (built-in, needed by agent spawn tests)
    adhoc_src = PACKAGE_ROOT / "modastack" / "workflow" / "adhoc.yaml"
    if adhoc_src.exists():
        shutil.copy2(adhoc_src, workflows_dir / "adhoc.yaml")

    # Short 2-step test workflow for integration tests
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

    # Set repo root so all per-repo path resolution works
    from modastack.sdk import set_project_root
    set_project_root(project_path)

    # Redirect credentials path
    from modastack import config as _cfg
    _orig_creds = _cfg._credentials_path
    _cfg._credentials_path = lambda: creds_dir / "credentials.yaml"

    yield ModastackEnv(
        project_path=project_path,
        state_dir=state_dir,
        sessions_dir=sessions_dir,
        workflows_dir=workflows_dir,
        dashboard_port=dashboard_port,
    )

    # Teardown: restore
    _cfg._credentials_path = _orig_creds
    set_project_root(None)


requires_claude = pytest.mark.skipif(
    not shutil.which("claude"),
    reason="claude CLI not installed",
)


@pytest.fixture(scope="class")
def manager_session(modastack_env):
    """Start a lightweight Claude Code session for inject/response testing.

    Class-scoped: one session per test class, torn down after.
    """
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from modastack.sdk import get_cli_path
    from modastack.manager.session import ManagerSession, set_default_session

    s = ManagerSession(project_path=modastack_env.project_path)
    set_default_session(s)

    s._client = None
    s._loop = None
    s._state = "stopped"
    s._last_response = ""

    loop = asyncio.new_event_loop()
    s._loop = loop

    async def _run():
        options = ClaudeAgentOptions(
            cwd=str(modastack_env.project_path),
            permission_mode="bypassPermissions",
            cli_path=get_cli_path(),
            system_prompt="You are a test agent. Reply concisely. No tools.",
        )
        client = ClaudeSDKClient(options)
        s._client = client
        await client.connect("You are online. Reply with just: READY")
        await s._drain_turn()
        keep_alive = asyncio.Event()
        await keep_alive.wait()

    def _thread():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run())
        except Exception:
            pass
        finally:
            loop.close()
            s._loop = None

    t = threading.Thread(target=_thread, daemon=True, name="test-session")
    t.start()
    s._thread = t

    for _ in range(60):
        if s._state == "waiting_input":
            break
        time.sleep(1)
    else:
        raise RuntimeError("Test session failed to start within 60s")

    yield s

    # Teardown
    if s._client and s._loop:
        async def _disconnect():
            await s._client.disconnect()
        try:
            fut = asyncio.run_coroutine_threadsafe(_disconnect(), s._loop)
            fut.result(timeout=5)
        except Exception:
            pass
    s._client = None
    s._state = "stopped"
    set_default_session(None)


@pytest.fixture(scope="class")
def manager_session_with_prompt(modastack_env):
    """Start a Claude Code session with a custom manager prompt.

    Used by lifecycle tests that need the manager to behave like production.
    """
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from modastack.sdk import get_cli_path
    from modastack.manager.session import ManagerSession, set_default_session

    prompt = (
        "You are a test manager. You receive events and take action.\n\n"
        "When you receive a task.assigned event, run this command:\n"
        "```bash\necho \"SPAWN:$REPO:$ISSUE\"\n```\n"
        "Replace $REPO with the repo name and $ISSUE with the issue ID.\n\n"
        "When you receive a slack.dm event, reply to the human using:\n"
        "```bash\nmodastack slack-reply -w $WORKSPACE -c $CHANNEL \"$YOUR_RESPONSE\"\n```\n"
        "Replace the variables from the event. Keep your response under 50 words.\n\n"
        "When you receive a task.closed or pr.closed event, just say NOTED.\n\n"
        "Always take action — never just describe what you would do."
    )

    s = ManagerSession(project_path=modastack_env.project_path)
    set_default_session(s)

    s._client = None
    s._loop = None
    s._state = "stopped"
    s._last_response = ""

    loop = asyncio.new_event_loop()
    s._loop = loop

    async def _run():
        options = ClaudeAgentOptions(
            cwd=str(modastack_env.project_path),
            permission_mode="bypassPermissions",
            cli_path=get_cli_path(),
            system_prompt=prompt,
        )
        client = ClaudeSDKClient(options)
        s._client = client
        await client.connect("You are online. Reply: READY")
        await s._drain_turn()
        keep_alive = asyncio.Event()
        await keep_alive.wait()

    def _thread():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run())
        except Exception:
            pass
        finally:
            loop.close()
            s._loop = None

    t = threading.Thread(target=_thread, daemon=True, name="test-manager")
    t.start()
    s._thread = t

    for _ in range(60):
        if s._state == "waiting_input":
            break
        time.sleep(1)
    else:
        raise RuntimeError("Test manager session failed to start within 60s")

    yield s

    if s._client and s._loop:
        async def _disconnect():
            await s._client.disconnect()
        try:
            fut = asyncio.run_coroutine_threadsafe(_disconnect(), s._loop)
            fut.result(timeout=5)
        except Exception:
            pass
    s._client = None
    s._state = "stopped"
    set_default_session(None)


@pytest.fixture
def cli_run(modastack_env):
    """Helper to run modastack CLI commands against the isolated install.

    Returns a function that runs the CLI subprocess with the correct
    cwd and environment so _detect_project_root() finds the temp repo.
    """
    def _run(*args, timeout=10):
        return subprocess.run(
            [sys.executable, "-m", "modastack.cli", *args],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(modastack_env.project_path),
        )
    return _run


@pytest.fixture
def clean_session(modastack_env):
    """Helper to clean up a named session from the registry."""
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
