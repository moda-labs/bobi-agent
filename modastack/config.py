"""Per-project configuration.

Per-project config (.modastack/config.yaml): shared project settings (checked in)
  - Task tracking system, project prefix, trigger labels
  - Slack workspace ID, shared channel
  - Event server URL
  - Test command, review policy
  - Project-specific context for agents

Per-project secrets (.modastack/local.yaml): gitignored
  - Event server deployment_id + api_key
  - API keys (Linear, etc.)
"""

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


def _credentials_path() -> Path:
    """XDG-standard credentials path (~/.config/modastack/credentials.yaml)."""
    xdg = Path.home() / ".config" / "modastack" / "credentials.yaml"
    if not xdg.exists():
        legacy = Path.home() / ".modastack" / "credentials.yaml"
        if legacy.exists():
            xdg.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, xdg)
    return xdg


@dataclass
class Credentials:
    """API keys per workspace (Linear, etc.). GitHub Issues needs no key."""

    entries: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Credentials":
        path = _credentials_path()
        if not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text()) or {}
        return cls(entries=raw)

    def save(self) -> None:
        path = _credentials_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.dump(self.entries, default_flow_style=False))

    def get(self, name: str) -> dict[str, str]:
        if name in self.entries:
            return self.entries[name]
        return self.entries.get("default", {})

    def add(self, name: str, **kwargs: str) -> None:
        self.entries.setdefault(name, {})
        for key, value in kwargs.items():
            if value:
                self.entries[name][key] = value
        self.save()

    def list_names(self) -> list[str]:
        return list(self.entries.keys())


@dataclass
class LocalConfig:
    """Per-project secrets from .modastack/local.yaml (gitignored)."""

    event_server_deployment_id: str = ""
    event_server_api_key: str = ""

    credentials: dict[str, str] = field(default_factory=dict)
    dashboard_port: int = 8095

    @classmethod
    def load(cls, project_path: Path) -> "LocalConfig":
        local_path = project_path / ".modastack" / "local.yaml"
        if not local_path.exists():
            return cls()
        raw = yaml.safe_load(local_path.read_text()) or {}
        event_server = raw.get("event_server", {})
        return cls(
            event_server_deployment_id=event_server.get("deployment_id", ""),
            event_server_api_key=event_server.get("api_key", ""),
            credentials=raw.get("credentials", {}),
            dashboard_port=raw.get("dashboard_port", 8095),
        )

    def save(self, project_path: Path) -> None:
        local_path = project_path / ".modastack" / "local.yaml"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if self.event_server_deployment_id:
            data["event_server"] = {
                "deployment_id": self.event_server_deployment_id,
                "api_key": self.event_server_api_key,
            }
        if self.credentials:
            data["credentials"] = self.credentials
        if self.dashboard_port != 8095:
            data["dashboard_port"] = self.dashboard_port
        local_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


def _resolve_project_config_path(project_path: Path) -> Path:
    """Find the project config file at .modastack/config.yaml."""
    path = project_path / ".modastack" / "config.yaml"
    if path.exists():
        return path
    raise FileNotFoundError(
        f"No .modastack/config.yaml in {project_path}"
    )


@dataclass
class ProjectConfig:
    """Per-project config from .modastack/config.yaml."""

    path: Path
    task_tracking: str = "github-issues"
    max_parallel: int = 2
    test_command: str = ""
    context: dict = field(default_factory=dict)

    # github:
    github_repo: str = ""

    # linear:
    linear_team: str = ""
    linear_project: str = ""

    # slack:
    slack_workspace_id: str = ""
    slack_channel: str = ""

    # event_server:
    event_server_url: str = ""

    @classmethod
    def from_file(cls, project_path: Path) -> "ProjectConfig":
        config_path = _resolve_project_config_path(project_path)

        raw = yaml.safe_load(config_path.read_text()) or {}
        github = raw.get("github", {})
        linear = raw.get("linear", {})
        slack = raw.get("slack", {})
        agent = raw.get("agent", {})
        verify = raw.get("verify", {})
        event_server = raw.get("event_server", {})

        return cls(
            path=project_path,
            task_tracking=raw.get("task_tracking", {}).get("system", "github-issues"),
            max_parallel=agent.get("max_parallel", 2),
            test_command=verify.get("test_command", ""),
            context=raw.get("context", {}),
            github_repo=github.get("repo", ""),
            linear_team=linear.get("team", ""),
            linear_project=linear.get("project", ""),
            slack_workspace_id=slack.get("workspace_id", ""),
            slack_channel=slack.get("channel", ""),
            event_server_url=event_server.get("url", ""),
        )

