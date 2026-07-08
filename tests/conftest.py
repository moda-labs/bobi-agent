"""Shared test fixtures.

The `bobi_install` fixture creates a fully isolated bobi
installation in a temp directory so tests never touch production
config, Slack channels, event servers, or session state.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# --- Worktree safety (must run before any `import bobi`) --------------
# Pin BOTH this test process and every bobi subprocess it spawns to the
# checkout these tests live in. The editable install's .pth points at the
# primary checkout, and `python -m pytest` puts the launch cwd on sys.path —
# so from a git worktree, `import bobi` (or a spawned
# `python -m bobi.cli`) can silently resolve to the WRONG checkout. That
# tests the wrong code, or pairs a worktree manager with a primary-checkout
# CLI over the event server. Anchored on this file's own location so it always
# matches the tests actually being run, in a worktree or the primary checkout.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
# bobi_deploy is src-layout, so the repo root alone only exposes it as a
# namespace shadow that loses to any editable install; pin its src dir
# explicitly so THIS checkout's copy wins too (tests/test_agentui_remote.py
# imports it).
_PIN_DIRS = [_REPO_ROOT]
if (Path(_REPO_ROOT) / "bobi_deploy" / "src").is_dir():
    _PIN_DIRS.append(str(Path(_REPO_ROOT) / "bobi_deploy" / "src"))
for _d in reversed(_PIN_DIRS):
    if _d not in sys.path[: len(_PIN_DIRS)]:
        sys.path.insert(0, _d)
_existing_pythonpath = os.environ.get("PYTHONPATH", "")
for _d in reversed(_PIN_DIRS):
    if _d not in _existing_pythonpath.split(os.pathsep):
        _existing_pythonpath = (
            _d + (os.pathsep + _existing_pythonpath if _existing_pythonpath else "")
        )
os.environ["PYTHONPATH"] = _existing_pythonpath
# Stop a spawned `python -m bobi.cli` / `python -c` from prepending its
# cwd to sys.path ahead of PYTHONPATH (Python 3.11+). The harness runs
# subprocesses from the temp project dir — harmless there — but if cwd ever is
# a bobi checkout, cwd would shadow our pinned PYTHONPATH and resolve the
# wrong code. This makes worktree pinning hold regardless of subprocess cwd.
os.environ["PYTHONSAFEPATH"] = "1"

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _isolate_environ():
    """No test may leak an os.environ mutation into the next.

    Several code paths write os.environ directly and by design — e.g.
    ``actions.save_credential`` does ``os.environ[var] = value`` so a live setup
    process can use a just-saved secret immediately. ``monkeypatch`` cannot undo
    a write the app makes on its own, so a leaked ``SLACK_BOT_TOKEN`` /
    ``LINEAR_API_KEY`` / ``BOBI_ROOT`` silently changes a later test's
    build/author/missing-credentials behavior (an order-dependent flake under
    pytest-randomly). Snapshot the environment before every test and restore it
    after. Defined first so it wraps every other fixture and the test body.
    """
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _clear_leaked_event_loop(request):
    """Prevent a leaked running event loop from poisoning async tests.

    The polluter is pytest-playwright's session-scoped ``playwright`` fixture:
    Playwright's sync API runs an asyncio event loop inside a greenlet on the
    main thread.  Because greenlets share the OS thread, the C-level
    ``_running_loop`` thread-local stays set even after control returns to the
    main greenlet.  pytest-asyncio's ``Runner.run()`` then sees a "running"
    loop and raises ``RuntimeError: cannot be called from a running event
    loop``.

    Clearing the thread-local before each test restores isolation.  The
    Playwright greenlet re-sets it when it resumes, so this is safe.

    Skipped for e2e tests — Playwright needs its own loop for teardown.
    """
    if "/e2e/" in str(request.fspath):
        yield
        return
    leaked = asyncio.events._get_running_loop()
    asyncio.events._set_running_loop(None)
    yield
    asyncio.events._set_running_loop(leaked)


@pytest.fixture(autouse=True)
def _reset_paths_root():
    """No test may leak a bound root into the next.

    The binding is process-global and bind_root refuses to rebind to a
    different path (a process has one identity) — without this reset,
    any test that binds via a real code path (CLI invoke, _run_agent_entry)
    poisons every later test that binds a different tmp root.

    Must also clear BOBI_ROOT from os.environ since bind_root() propagates it
    to children; a stale env var causes resolve_root() to select a prior
    test's runtime root.
    """
    from bobi import paths
    before = paths._root
    env_before = os.environ.get("BOBI_ROOT")
    yield
    paths._root = before
    if env_before is None:
        os.environ.pop("BOBI_ROOT", None)
    else:
        os.environ["BOBI_ROOT"] = env_before


@pytest.fixture(autouse=True)
def _no_event_server_io(request, monkeypatch):
    """Unit tests never touch a real event server (conftest invariant).

    ``Session.start()`` now starts an ``inbox/<self>`` subscription, which
    would register a deployment and even spawn a local Node server. Stub the
    subscription so unit tests stay hermetic. ``Session`` imports the helper
    lazily, so patching the module attribute reaches it; ``test_event_subscription``
    imports it eagerly and tests it directly, so it is unaffected. Integration
    and e2e tests drive the real transport and opt out.
    """
    p = str(request.fspath)
    if "/integration/" in p or "/e2e/" in p:
        yield
        return
    monkeypatch.setattr(
        "bobi.subagent._start_event_subscription",
        lambda *a, **k: None,
    )
    yield

import yaml

TEST_AGENT_NAME = "test-agent"


def _install_test_agent(config_dir: Path) -> None:
    """Create installed package state in run/package/."""
    for subdir in ["roles/director", "roles/engineer", "workflows", "monitors"]:
        (config_dir / subdir).mkdir(parents=True, exist_ok=True)

    (config_dir / "agent.md").write_text("# Test Agent\nMinimal agent for testing.")

    (config_dir / "roles" / "director" / "ROLE.md").write_text(
        "# Engineering Director\n\n"
        "You are a director of engineering managing multiple software projects."
    )
    (config_dir / "roles" / "engineer" / "ROLE.md").write_text(
        "# Engineer Agent\n\n"
        "You are a staff engineer who ships production-quality code."
    )

    (config_dir / "workflows" / "adhoc.yaml").write_text(yaml.dump({
        "name": "adhoc",
        "trigger": "For any ad-hoc task.",
        "description": "Open-ended task.",
        "steps": [{"name": "task", "prompt": "${{input.task}}"}],
    }))

    (config_dir / "monitors" / "defaults.yaml").write_text(yaml.dump({
        "monitors": [{
            "name": "test-check",
            "description": "Test monitor",
            "interval": "15m",
            "event": "monitor/test",
            "check": "pr_conflicts",
        }],
    }))

    (config_dir / "monitors" / "github_checks.py").write_text(
        (Path(__file__).parent.parent / "tests" / "_test_checks_stub.py").read_text()
        if (Path(__file__).parent.parent / "tests" / "_test_checks_stub.py").exists()
        else _CHECKS_STUB
    )


_CHECKS_STUB = '''"""Stub monitor checks for testing."""

from datetime import datetime, timezone, timedelta


_slug_cache: dict[str, str] = {}


def _parse_iso(s: str) -> datetime:
    s = s.rstrip("Z").split("+")[0]
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _repo_slug(cwd: str) -> str:
    if cwd in _slug_cache:
        return _slug_cache[cwd]
    return ""


def _gh_pr_list(slug: str, fields: str) -> list[dict]:
    return []


def pr_conflicts(cwd: str) -> list:
    return []


def stale_prs(cwd: str) -> list:
    return []


CHECKS = {
    "pr_conflicts": pr_conflicts,
    "stale_prs": stale_prs,
}
'''


@dataclass
class BobiInstall:
    """Paths and ports for an isolated Bobi home."""
    repo_path: Path
    state_dir: Path
    sessions_dir: Path
    agents_dir: Path
    agent_name: str


@pytest.fixture
def bobi_install(tmp_path, monkeypatch):
    """Create a fully isolated Bobi home in a temp directory.

    Binds a canonical runtime root under BOBI_HOME so tests never touch the
    user's real ~/.bobi directory.

    Creates a self-contained test agent team so tests never depend on
    remote-fetched packs or the user's cache.
    """
    home = tmp_path / "home"
    agents_dir = home / "agents"
    repo_path = agents_dir / TEST_AGENT_NAME / "run"
    config_dir = repo_path / "package"
    state_dir = repo_path / "state"
    sessions_dir = state_dir / "sessions"
    workspace_dir = repo_path / "workspace"

    monkeypatch.setenv("BOBI_HOME", str(home))

    for d in [config_dir, state_dir, sessions_dir, agents_dir, workspace_dir]:
        d.mkdir(parents=True, exist_ok=True)

    _install_test_agent(config_dir)

    (config_dir / "agent.yaml").write_text(yaml.dump({
        "version": "0.0.1",
        "agent": TEST_AGENT_NAME,
        "entry_point": "director",
        "services": [
            {"name": "slack", "events": True},
        ],
    }))

    from bobi import paths
    paths.bind_root(None)
    paths.bind_root(repo_path)

    return BobiInstall(
        repo_path=repo_path,
        state_dir=state_dir,
        sessions_dir=sessions_dir,
        agents_dir=agents_dir,
        agent_name=TEST_AGENT_NAME,
    )
