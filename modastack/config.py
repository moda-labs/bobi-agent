"""Configuration with machine → project resolution.

Machine-wide config (~/.modastack/config.yaml): service credentials (not checked in)
  - Event server URL
  - Slack bot token
  - Linear API key

Project config (.modastack/config.yaml): project settings (checked in)
  - GitHub repo, Slack channel, task tracking
  - Test command, max parallel agents
  - Overrides machine-wide values for any overlapping key
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


def _machine_config_path() -> Path:
    return Path.home() / ".modastack" / "config.yaml"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base, recursing into nested dicts."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


@dataclass
class Config:
    """Resolved config — machine defaults merged with project overrides.

    Resolution order: ~/.modastack/config.yaml → .modastack/config.yaml
    Project values override machine values at every key.
    """

    path: Path

    # task tracking
    task_tracking: str = "github-issues"
    max_parallel: int = 2
    test_command: str = ""
    context: dict = field(default_factory=dict)

    # github
    github_repo: str = ""

    # linear
    linear_team: str = ""
    linear_project: str = ""
    linear_api_key: str = ""

    # slack
    slack_workspace_id: str = ""
    slack_channel: str = ""
    slack_bot_token: str = ""

    # event server
    event_server_url: str = ""

    @classmethod
    def from_file(cls, project_path: Path) -> "Config":
        """Load config with machine → project resolution."""
        return cls.load(project_path)

    @classmethod
    def load(cls, project_path: Path) -> "Config":
        """Load config with machine → project resolution."""
        machine = _load_yaml(_machine_config_path())
        project_yaml = project_path / ".modastack" / "config.yaml"
        project = _load_yaml(project_yaml)

        raw = _deep_merge(machine, project)

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
            linear_api_key=linear.get("api_key", ""),
            slack_workspace_id=slack.get("workspace_id", ""),
            slack_channel=slack.get("channel", ""),
            slack_bot_token=slack.get("bot_token", ""),
            event_server_url=event_server.get("url", ""),
        )


# Keep these aliases so existing code doesn't break during transition
ProjectConfig = Config


def _credentials_path() -> Path:
    """XDG-standard credentials path (~/.config/modastack/credentials.yaml)."""
    return Path.home() / ".config" / "modastack" / "credentials.yaml"


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


def _resolve_project_config_path(project_path: Path) -> Path:
    """Find the project config file at .modastack/config.yaml."""
    path = project_path / ".modastack" / "config.yaml"
    if path.exists():
        return path
    raise FileNotFoundError(
        f"No .modastack/config.yaml in {project_path}"
    )


# --- Event server deployment state (ephemeral, auto-registered) ---


def load_deployment_state(project_path: Path) -> dict:
    """Load event server deployment_id + api_key from state dir."""
    state_file = project_path / ".modastack" / "state" / "deployment.json"
    if not state_file.exists():
        return {}
    import json
    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_deployment_state(project_path: Path, deployment_id: str, api_key: str) -> None:
    """Save event server deployment_id + api_key to state dir."""
    import json
    state_dir = project_path / ".modastack" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "deployment.json"
    state_file.write_text(json.dumps({
        "deployment_id": deployment_id,
        "api_key": api_key,
    }))
