"""Shared test fixtures.

The `modastack_install` fixture creates a fully isolated modastack
installation in a temp directory so tests never touch production
config, Slack channels, event servers, or session state.
"""

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


@pytest.fixture
def modastack_install(tmp_path, monkeypatch):
    """Create a fully isolated modastack installation in a temp directory.

    Sets sdk._project_root so all per-project path resolution points at tmp_path.
    No global ~/.modastack directory is created or referenced.
    """
    repo_path = tmp_path / "repo"

    config_dir = repo_path / ".modastack"
    state_dir = config_dir / "state"
    sessions_dir = config_dir / "sessions"

    for d in [config_dir, state_dir, sessions_dir, config_dir / "workflows"]:
        d.mkdir(parents=True)

    (config_dir / "config.yaml").write_text(yaml.dump({
        "task_tracking": {"system": "github-issues", "project": "TEST"},
        "agent": {"max_parallel": 1},
        "verify": {"test_command": "echo pass"},
    }))

    machine_config = tmp_path / "machine_config.yaml"
    machine_config.write_text("{}")

    creds_path = tmp_path / "credentials.yaml"
    creds_path.write_text("{}")

    monkeypatch.setattr("modastack.sdk._project_root", repo_path)
    monkeypatch.setattr("modastack.config._machine_config_path", lambda: machine_config)
    monkeypatch.setattr("modastack.config._credentials_path", lambda: creds_path)

    return ModastackInstall(
        repo_path=repo_path,
        state_dir=state_dir,
        sessions_dir=sessions_dir,
    )
