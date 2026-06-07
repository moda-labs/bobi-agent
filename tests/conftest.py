"""Shared test fixtures.

The `modastack_install` fixture creates a fully isolated modastack
installation in a temp directory so tests never touch production
config, Slack channels, event servers, or session state.
"""

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

TEST_AGENT_NAME = "test-agent"


def _create_test_agent(agents_dir: Path) -> Path:
    """Create a minimal self-contained agent pack for testing."""
    pack = agents_dir / TEST_AGENT_NAME
    (pack / "roles").mkdir(parents=True)
    (pack / "workflows").mkdir()
    (pack / "monitors").mkdir()

    (pack / "defaults.yaml").write_text(yaml.dump({
        "version": "0.0.1",
        "role": "director",
        "event_sources": ["slack"],
    }))

    (pack / "agent.md").write_text("# Test Agent\nMinimal agent for testing.")

    (pack / "roles" / "director.md").write_text(
        "# Engineering Director\n\n"
        "You are a director of engineering managing multiple software projects."
    )
    (pack / "roles" / "project_lead.md").write_text(
        "# Project Lead\n\n"
        "You are a project lead managing a single software project."
    )
    (pack / "roles" / "engineer.md").write_text(
        "# Engineer Agent\n\n"
        "You are a staff engineer who ships production-quality code."
    )

    (pack / "workflows" / "adhoc.yaml").write_text(yaml.dump({
        "name": "adhoc",
        "trigger": "For any ad-hoc task.",
        "description": "Open-ended task.",
        "steps": [{"name": "task", "prompt": "${{input.task}}"}],
    }))

    (pack / "monitors" / "defaults.yaml").write_text(yaml.dump({
        "monitors": [{
            "name": "test-check",
            "description": "Test monitor",
            "interval": "15m",
            "event": "monitor/test",
            "check": "pr_conflicts",
        }],
    }))

    (pack / "monitors" / "github_checks.py").write_text(
        (Path(__file__).parent.parent / "tests" / "_test_checks_stub.py").read_text()
        if (Path(__file__).parent.parent / "tests" / "_test_checks_stub.py").exists()
        else _CHECKS_STUB
    )

    return pack


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

    Sets sdk._project_root so all per-project path resolution points at tmp_path.
    No global ~/.modastack directory is created or referenced.

    Creates a self-contained test agent pack so tests never depend on
    remote-fetched packs or the user's cache.
    """
    repo_path = tmp_path / "repo"

    config_dir = repo_path / ".modastack"
    state_dir = config_dir / "state"
    sessions_dir = config_dir / "sessions"
    agents_dir = tmp_path / "agents_cache"

    for d in [config_dir, state_dir, sessions_dir, config_dir / "workflows"]:
        d.mkdir(parents=True)

    _create_test_agent(agents_dir)

    (config_dir / "agent.yaml").write_text(yaml.dump({
        "agent": TEST_AGENT_NAME,
        "role": "director",
    }))

    machine_config = tmp_path / "machine_config.yaml"
    machine_config.write_text("{}")

    creds_path = tmp_path / "credentials.yaml"
    creds_path.write_text("{}")

    monkeypatch.setattr("modastack.sdk._project_root", repo_path)
    monkeypatch.setattr("modastack.config._machine_config_path", lambda: machine_config)
    monkeypatch.setattr("modastack.config._credentials_path", lambda: creds_path)
    monkeypatch.setattr("modastack.prompts.AGENTS_CACHE_DIR", agents_dir)
    monkeypatch.setattr("modastack.prompts.resolver.AGENTS_CACHE_DIR", agents_dir)

    return ModastackInstall(
        repo_path=repo_path,
        state_dir=state_dir,
        sessions_dir=sessions_dir,
        agents_dir=agents_dir,
        agent_name=TEST_AGENT_NAME,
    )
