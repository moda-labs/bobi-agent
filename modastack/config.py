"""Global, per-repo, and per-operator configuration.

Global config (~/.modastack/config.yaml): truly global settings
  - Event server base URL (shared Cloudflare worker)
  - GitHub SSH account mappings
  - repos.json discovery cache

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

Legacy:
  - ~/.modastack/credentials.yaml — absorbed by local.yaml
  - GlobalConfig.repos, slack_*, event_server_* — migrating to per-repo
"""

import logging
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

GLOBAL_CONFIG_DIR = Path.home() / ".modastack"
GLOBAL_CONFIG_PATH = GLOBAL_CONFIG_DIR / "config.yaml"
CREDENTIALS_PATH = GLOBAL_CONFIG_DIR / "credentials.yaml"
LOG_DIR = GLOBAL_CONFIG_DIR / "logs"


@dataclass
class Credentials:
    """API keys per workspace (Linear, etc.). GitHub Issues needs no key."""

    entries: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Credentials":
        if not CREDENTIALS_PATH.exists():
            return cls()
        raw = yaml.safe_load(CREDENTIALS_PATH.read_text()) or {}
        return cls(entries=raw)

    def save(self) -> None:
        CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CREDENTIALS_PATH.write_text(yaml.dump(self.entries, default_flow_style=False))

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
        """Fall back to GlobalConfig for pre-migration setups."""
        try:
            gc = GlobalConfig.load()
        except Exception:
            return cls()
        cred_name = repo_path.name
        creds = Credentials.load().get(cred_name)
        return cls(
            slack_bot_token=gc.slack_bot_token,
            slack_dm_channel=gc.slack_dm_channel,
            event_server_url=gc.event_server_url,
            event_server_deployment_id=gc.event_server_deployment_id,
            event_server_api_key=gc.event_server_api_key,
            credentials=creds,
        )

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


@dataclass
class GlobalConfig:
    """Instance-level config from ~/.modastack/config.yaml."""

    repos: list[Path] = field(default_factory=list)

    # Slack — per-workspace bot tokens, with single-token fallback
    slack_bot_token: str = ""
    slack_dm_channel: str = ""
    slack_workspaces: dict[str, dict[str, str]] = field(default_factory=dict)

    # Webhook server
    webhook_port: int = 8080
    public_url: str = ""

    # GitHub accounts
    github_default_account: str = ""
    github_accounts: dict[str, str] = field(default_factory=dict)

    # Event server (centralized webhook relay)
    event_server_url: str = ""
    event_server_deployment_id: str = ""
    event_server_api_key: str = ""

    @classmethod
    def load(cls) -> "GlobalConfig":
        if not GLOBAL_CONFIG_PATH.exists():
            return cls()

        raw = yaml.safe_load(GLOBAL_CONFIG_PATH.read_text()) or {}
        repos = [Path(p).expanduser() for p in raw.get("repos", [])]
        slack = raw.get("slack", {})
        webhooks = raw.get("webhooks", {})
        github = raw.get("github", {})

        event_server = raw.get("event_server", {})

        return cls(
            repos=repos,
            slack_bot_token=slack.get("bot_token", "") or raw.get("slack_bot_token", ""),
            slack_dm_channel=slack.get("dm_channel", ""),
            slack_workspaces=slack.get("workspaces", {}),
            webhook_port=webhooks.get("port", 8080),
            public_url=webhooks.get("public_url", ""),
            github_default_account=github.get("default_account", ""),
            github_accounts=github.get("accounts", {}),
            event_server_url=event_server.get("url", ""),
            event_server_deployment_id=event_server.get("deployment_id", ""),
            event_server_api_key=event_server.get("api_key", ""),
        )

    def slack_token_for(self, workspace_id: str = "") -> str:
        """Look up the bot token for a workspace, falling back to the default."""
        if workspace_id and workspace_id in self.slack_workspaces:
            return self.slack_workspaces[workspace_id].get("bot_token", "")
        return self.slack_bot_token

    def save(self) -> None:
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        slack_data: dict = {
            "bot_token": self.slack_bot_token,
            "dm_channel": self.slack_dm_channel,
        }
        if self.slack_workspaces:
            slack_data["workspaces"] = self.slack_workspaces
        data = {
            "slack": slack_data,
            "webhooks": {
                "port": self.webhook_port,
            },
            "github": {
                "default_account": self.github_default_account,
                "accounts": self.github_accounts,
            },
            "repos": [str(p) for p in self.repos],
        }
        if self.public_url:
            data["webhooks"]["public_url"] = self.public_url
        if self.event_server_url:
            data["event_server"] = {
                "url": self.event_server_url,
                "deployment_id": self.event_server_deployment_id,
                "api_key": self.event_server_api_key,
            }
        GLOBAL_CONFIG_PATH.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


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
    """Per-repo config from .modastack/config.yaml (or legacy .modastack.yaml)."""

    path: Path
    task_tracking: str = "github-issues"  # "github-issues" or "linear"
    project: str = ""  # project prefix (e.g., BET, TESS) — used for both GitHub labels and Linear teams
    trigger_labels: list[str] = field(default_factory=lambda: ["agent"])
    skip_labels: list[str] = field(default_factory=lambda: ["blocked", "human-only"])
    max_parallel: int = 2
    test_command: str = ""
    review_required: bool = True
    auto_merge: bool = False
    credentials: str = "default"
    context: dict = field(default_factory=dict)
    github_repo: str = ""
    slack_workspace_id: str = ""
    slack_channel: str = ""

    @classmethod
    def from_file(cls, repo_path: Path) -> "RepoConfig":
        config_path = _resolve_repo_config_path(repo_path)

        raw = yaml.safe_load(config_path.read_text()) or {}
        task_tracking_config = raw.get("task_tracking", {})
        agent = raw.get("agent", {})
        verify = raw.get("verify", {})

        # Backwards compat: old configs use "linear:" section
        if "linear" in raw and "task_tracking" not in raw:
            linear = raw["linear"]
            return cls(
                path=repo_path,
                task_tracking="linear",
                project=linear.get("project", ""),
                trigger_labels=linear.get("trigger_labels", ["agent"]),
                skip_labels=linear.get("skip_labels", ["blocked", "human-only"]),
                max_parallel=agent.get("max_parallel", 2),
                test_command=verify.get("test_command", ""),
                review_required=verify.get("review_required", True),
                auto_merge=verify.get("auto_merge", False),
                credentials=raw.get("credentials", "default"),
                context=raw.get("context", {}),
            )

        slack = raw.get("slack", {})
        return cls(
            path=repo_path,
            task_tracking=task_tracking_config.get("system", "github-issues"),
            project=task_tracking_config.get("project", ""),
            trigger_labels=task_tracking_config.get("trigger_labels", ["agent"]),
            skip_labels=task_tracking_config.get("skip_labels", ["blocked", "human-only"]),
            max_parallel=agent.get("max_parallel", 2),
            test_command=verify.get("test_command", ""),
            review_required=verify.get("review_required", True),
            auto_merge=verify.get("auto_merge", False),
            credentials=raw.get("credentials", "default"),
            context=raw.get("context", {}),
            github_repo=raw.get("github", {}).get("repo", ""),
            slack_workspace_id=slack.get("workspace_id", ""),
            slack_channel=slack.get("channel", ""),
        )

    def get_credentials(self) -> dict[str, str]:
        creds = Credentials.load()
        return creds.get(self.credentials)

    @property
    def linear_project(self) -> str:
        """Backwards compat for code that still references linear_project."""
        return self.project if self.task_tracking == "linear" else ""
