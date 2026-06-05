"""Per-repo and per-operator configuration.

Per-repo config (.modastack/config.yaml): shared repo settings (checked in)
  - Task tracking system, project prefix, trigger labels
  - Slack workspace ID, shared channel
  - Test command, review policy
  - Repo-specific context for agents

Per-operator config (.modastack/local.yaml): operator-specific (gitignored)
  - Operator identity (name, email, slack_user_id)
  - Slack bot token, DM channel
  - Event server deployment_id + api_key
  - API keys (Linear, etc.)
"""

import logging
import shutil
import warnings
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
    """Per-repo, per-operator config from .modastack/local.yaml (gitignored)."""

    operator_name: str = ""
    operator_email: str = ""
    operator_slack_user_id: str = ""

    slack_bot_token: str = ""
    slack_dm_channel: str = ""

    event_server_url: str = ""
    event_server_deployment_id: str = ""
    event_server_api_key: str = ""

    credentials: dict[str, str] = field(default_factory=dict)
    dashboard_port: int = 8095

    @classmethod
    def load(cls, repo_path: Path) -> "LocalConfig":
        local_path = repo_path / ".modastack" / "local.yaml"
        if not local_path.exists():
            return cls._from_global_fallback(repo_path)
        raw = yaml.safe_load(local_path.read_text()) or {}
        operator = raw.get("operator", {})
        slack = raw.get("slack", {})
        event_server = raw.get("event_server", {})
        return cls(
            operator_name=operator.get("name", ""),
            operator_email=operator.get("email", ""),
            operator_slack_user_id=operator.get("slack_user_id", ""),
            slack_bot_token=slack.get("bot_token", ""),
            slack_dm_channel=slack.get("dm_channel", ""),
            event_server_url=event_server.get("url", ""),
            event_server_deployment_id=event_server.get("deployment_id", ""),
            event_server_api_key=event_server.get("api_key", ""),
            credentials=raw.get("credentials", {}),
            dashboard_port=raw.get("dashboard_port", 8095),
        )

    @classmethod
    def _from_global_fallback(cls, repo_path: Path) -> "LocalConfig":
        """Return empty config when no local.yaml exists."""
        return cls()

    def save(self, repo_path: Path) -> None:
        local_path = repo_path / ".modastack" / "local.yaml"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if self.operator_name or self.operator_email or self.operator_slack_user_id:
            data["operator"] = {}
            if self.operator_name:
                data["operator"]["name"] = self.operator_name
            if self.operator_email:
                data["operator"]["email"] = self.operator_email
            if self.operator_slack_user_id:
                data["operator"]["slack_user_id"] = self.operator_slack_user_id
        slack: dict = {}
        if self.slack_bot_token:
            slack["bot_token"] = self.slack_bot_token
        if self.slack_dm_channel:
            slack["dm_channel"] = self.slack_dm_channel
        if slack:
            data["slack"] = slack
        if self.event_server_url or self.event_server_deployment_id:
            data["event_server"] = {
                "url": self.event_server_url,
                "deployment_id": self.event_server_deployment_id,
                "api_key": self.event_server_api_key,
            }
        if self.credentials:
            data["credentials"] = self.credentials
        if self.dashboard_port != 8095:
            data["dashboard_port"] = self.dashboard_port
        local_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    def slack_token_for(self, workspace_id: str = "") -> str:
        return self.slack_bot_token



def _resolve_repo_config_path(repo_path: Path) -> Path:
    """Find the repo config file, preferring .modastack/config.yaml."""
    new_path = repo_path / ".modastack" / "config.yaml"
    if new_path.exists():
        return new_path
    legacy_path = repo_path / ".modastack.yaml"
    if legacy_path.exists():
        warnings.warn(
            f"Using deprecated .modastack.yaml in {repo_path}; "
            "migrate to .modastack/config.yaml",
            DeprecationWarning,
            stacklevel=3,
        )
        return legacy_path
    raise FileNotFoundError(
        f"No .modastack/config.yaml or .modastack.yaml in {repo_path}"
    )


@dataclass
class RepoConfig:
    """Per-repo config from .modastack/config.yaml."""

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

    @classmethod
    def from_file(cls, repo_path: Path) -> "RepoConfig":
        config_path = _resolve_repo_config_path(repo_path)

        raw = yaml.safe_load(config_path.read_text()) or {}
        github = raw.get("github", {})
        linear = raw.get("linear", {})
        slack = raw.get("slack", {})
        agent = raw.get("agent", {})
        verify = raw.get("verify", {})

        return cls(
            path=repo_path,
            task_tracking=raw.get("task_tracking", {}).get("system", "github-issues"),
            max_parallel=agent.get("max_parallel", 2),
            test_command=verify.get("test_command", ""),
            context=raw.get("context", {}),
            github_repo=github.get("repo", ""),
            linear_team=linear.get("team", ""),
            linear_project=linear.get("project", ""),
            slack_workspace_id=slack.get("workspace_id", ""),
            slack_channel=slack.get("channel", ""),
        )

