"""Shared test fixtures.

The `modastack_install` fixture creates a fully isolated modastack
installation in a temp directory so tests never touch production
config, Slack channels, event servers, or session state.
"""

import socket
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml


@dataclass
class ModastackInstall:
    """Paths and ports for an isolated modastack installation."""
    repo_path: Path
    state_dir: Path
    sessions_dir: Path
    dashboard_port: int


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def modastack_install(tmp_path, monkeypatch):
    """Create a fully isolated modastack installation in a temp directory.

    Sets sdk._repo_root so all per-repo path resolution points at tmp_path.
    No global ~/.modastack directory is created or referenced.
    """
    repo_path = tmp_path / "repo"

    config_dir = repo_path / ".modastack"
    state_dir = config_dir / "state"
    sessions_dir = config_dir / "sessions"

    for d in [config_dir, state_dir, sessions_dir, config_dir / "workflows"]:
        d.mkdir(parents=True)

    dashboard_port = _free_port()

    (config_dir / "config.yaml").write_text(yaml.dump({
        "task_tracking": {"system": "github-issues", "project": "TEST"},
        "github": {"repo": "test-org/test-repo"},
        "agent": {"max_parallel": 1},
        "verify": {"test_command": "echo pass"},
    }))

    (config_dir / "local.yaml").write_text(yaml.dump({
        "operator": {"name": "test", "email": "test@test.com"},
        "slack": {"bot_token": "", "dm_channel": ""},
        "event_server": {"url": "", "deployment_id": "", "api_key": ""},
        "dashboard_port": dashboard_port,
    }))

    creds_path = tmp_path / "credentials.yaml"
    creds_path.write_text("{}")

    monkeypatch.setattr("modastack.sdk._repo_root", repo_path)
    monkeypatch.setattr("modastack.config._credentials_path", lambda: creds_path)

    return ModastackInstall(
        repo_path=repo_path,
        state_dir=state_dir,
        sessions_dir=sessions_dir,
        dashboard_port=dashboard_port,
    )
