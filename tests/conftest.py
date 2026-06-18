"""Shared test fixtures.

The `modastack_install` fixture creates a fully isolated modastack
installation in a temp directory so tests never touch production
config, Slack channels, event servers, or session state.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# --- Worktree safety (must run before any `import modastack`) --------------
# Pin BOTH this test process and every modastack subprocess it spawns to the
# checkout these tests live in. The editable install's .pth points at the
# primary checkout, and `python -m pytest` puts the launch cwd on sys.path —
# so from a git worktree, `import modastack` (or a spawned
# `python -m modastack.cli`) can silently resolve to the WRONG checkout. That
# tests the wrong code, or pairs a worktree manager with a primary-checkout
# CLI over the event server. Anchored on this file's own location so it always
# matches the tests actually being run, in a worktree or the primary checkout.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path[:1]:
    sys.path.insert(0, _REPO_ROOT)
_existing_pythonpath = os.environ.get("PYTHONPATH", "")
if _REPO_ROOT not in _existing_pythonpath.split(os.pathsep):
    os.environ["PYTHONPATH"] = (
        _REPO_ROOT + (os.pathsep + _existing_pythonpath if _existing_pythonpath else "")
    )
# Stop a spawned `python -m modastack.cli` / `python -c` from prepending its
# cwd to sys.path ahead of PYTHONPATH (Python 3.11+). The harness runs
# subprocesses from the temp project dir — harmless there — but if cwd ever is
# a modastack checkout, cwd would shadow our pinned PYTHONPATH and resolve the
# wrong code. This makes worktree pinning hold regardless of subprocess cwd.
os.environ["PYTHONSAFEPATH"] = "1"

import asyncio

import pytest


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

    Must also clear MODASTACK_ROOT from os.environ since bind_root() now
    propagates it (#249) — a stale env var causes resolve_root() to
    short-circuit to a prior test's root instead of walking from cwd.
    """
    from modastack import paths
    before = paths._root
    env_before = os.environ.get("MODASTACK_ROOT")
    yield
    paths._root = before
    if env_before is None:
        os.environ.pop("MODASTACK_ROOT", None)
    else:
        os.environ["MODASTACK_ROOT"] = env_before


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
        "modastack.subagent._start_event_subscription",
        lambda *a, **k: None,
    )
    yield

import yaml

TEST_AGENT_NAME = "test-agent"


def _install_test_agent(config_dir: Path) -> None:
    """Create installed agent state in .modastack/ (simulates `modastack install`)."""
    for subdir in ["roles/director", "roles/project_lead", "roles/engineer",
                    "workflows", "monitors"]:
        (config_dir / subdir).mkdir(parents=True, exist_ok=True)

    (config_dir / "agent.md").write_text("# Test Agent\nMinimal agent for testing.")

    (config_dir / "roles" / "director" / "ROLE.md").write_text(
        "# Engineering Director\n\n"
        "You are a director of engineering managing multiple software projects."
    )
    (config_dir / "roles" / "project_lead" / "ROLE.md").write_text(
        "# Project Lead\n\n"
        "You are a project lead managing a single software project."
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
class ModastackInstall:
    """Paths and ports for an isolated modastack installation."""
    repo_path: Path
    state_dir: Path
    sessions_dir: Path
    agents_dir: Path
    agent_name: str


@pytest.fixture
def modastack_install(tmp_path, monkeypatch):
    """Create a fully isolated modastack installation in a temp directory.

    Binds the paths root so all per-project path resolution points at tmp_path.
    No global ~/.modastack directory is created or referenced.

    Creates a self-contained test agent team so tests never depend on
    remote-fetched packs or the user's cache.
    """
    repo_path = tmp_path / "repo"

    config_dir = repo_path / ".modastack"
    state_dir = config_dir / "state"
    sessions_dir = config_dir / "sessions"
    agents_dir = config_dir / "agents"

    for d in [config_dir, state_dir, sessions_dir, agents_dir]:
        d.mkdir(parents=True)

    _install_test_agent(config_dir)

    (config_dir / "agent.yaml").write_text(yaml.dump({
        "version": "0.0.1",
        "agent": TEST_AGENT_NAME,
        "entry_point": "director",
        "services": [
            {"name": "slack", "events": True},
        ],
    }))

    monkeypatch.setattr("modastack.paths._root", repo_path)

    return ModastackInstall(
        repo_path=repo_path,
        state_dir=state_dir,
        sessions_dir=sessions_dir,
        agents_dir=agents_dir,
        agent_name=TEST_AGENT_NAME,
    )
