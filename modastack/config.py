"""Per-project and per-operator configuration.

Per-project config (.modastack/config.yaml): shared project settings (checked in)
  - Task tracking system, project prefix, trigger labels
  - Slack workspace ID, shared channel
  - Event server URL
  - Test command, review policy
  - Project-specific context for agents

Per-operator config (.modastack/local.yaml): operator-specific (gitignored)
  - Operator identity (name, email)
  - Slack bot token
  - Event server deployment_id + api_key (secrets)
  - API keys (Linear, etc.)
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


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


@dataclass
class LocalConfig:
    """Per-project, per-operator config from .modastack/local.yaml (gitignored)."""

    operator_name: str = ""
    operator_email: str = ""

    slack_bot_token: str = ""

    event_server_deployment_id: str = ""
    event_server_api_key: str = ""

    credentials: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, project_path: Path) -> "LocalConfig":
        local_path = project_path / ".modastack" / "local.yaml"
        if not local_path.exists():
            return cls()
        raw = yaml.safe_load(local_path.read_text()) or {}
        operator = raw.get("operator", {})
        slack = raw.get("slack", {})
        event_server = raw.get("event_server", {})
        return cls(
            operator_name=operator.get("name", ""),
            operator_email=operator.get("email", ""),
            slack_bot_token=slack.get("bot_token", ""),
            event_server_deployment_id=event_server.get("deployment_id", ""),
            event_server_api_key=event_server.get("api_key", ""),
            credentials=raw.get("credentials", {}),
        )

    def save(self, project_path: Path) -> None:
        local_path = project_path / ".modastack" / "local.yaml"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if self.operator_name or self.operator_email:
            data["operator"] = {}
            if self.operator_name:
                data["operator"]["name"] = self.operator_name
            if self.operator_email:
                data["operator"]["email"] = self.operator_email
        if self.slack_bot_token:
            data["slack"] = {"bot_token": self.slack_bot_token}
        if self.event_server_deployment_id:
            data["event_server"] = {
                "deployment_id": self.event_server_deployment_id,
                "api_key": self.event_server_api_key,
            }
        if self.credentials:
            data["credentials"] = self.credentials
        local_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    def slack_token_for(self, workspace_id: str = "") -> str:
        return self.slack_bot_token


@dataclass
class SlackIdentity:
    """Resolved Slack identity — looked up at runtime from bot token + email."""
    user_id: str = ""
    dm_channel: str = ""


def resolve_slack_identity(bot_token: str, email: str) -> SlackIdentity:
    """Look up Slack user ID and DM channel from bot token + email."""
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    identity = SlackIdentity()
    if not bot_token or not email:
        return identity

    try:
        url = f"https://slack.com/api/users.lookupByEmail?email={urllib.parse.quote(email)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bot_token}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("ok"):
            identity.user_id = data["user"]["id"]
        else:
            log.warning(f"Slack user lookup failed: {data.get('error')}")
            return identity
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as e:
        log.warning(f"Slack user lookup failed: {e}")
        return identity

    try:
        payload = json.dumps({"users": identity.user_id}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/conversations.open",
            data=payload,
            headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get("ok"):
            identity.dm_channel = data["channel"]["id"]
        else:
            log.warning(f"Slack DM open failed: {data.get('error')}")
    except (urllib.error.URLError, OSError, KeyError, json.JSONDecodeError) as e:
        log.warning(f"Slack DM open failed: {e}")

    return identity


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

