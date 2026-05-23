"""Global configuration.

Global config (~/.modastack/config.yaml): instance-level settings
  - Slack tokens (one bot per modabot instance)
  - Webhook server config
  - GitHub accounts
  - Registered repos (with Linear project, credentials, labels)

Credentials (~/.modastack/credentials.yaml): Linear API keys per workspace
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

GLOBAL_CONFIG_DIR = Path.home() / ".modastack"
GLOBAL_CONFIG_PATH = GLOBAL_CONFIG_DIR / "config.yaml"
STATE_PATH = GLOBAL_CONFIG_DIR / "state.json"
CREDENTIALS_PATH = GLOBAL_CONFIG_DIR / "credentials.yaml"
LOG_DIR = GLOBAL_CONFIG_DIR / "logs"


@dataclass
class Credentials:
    """Linear API keys per workspace."""

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

    def add(self, name: str, linear_api_key: str = "") -> None:
        self.entries.setdefault(name, {})
        if linear_api_key:
            self.entries[name]["linear_api_key"] = linear_api_key
        self.save()

    def list_names(self) -> list[str]:
        return list(self.entries.keys())


@dataclass
class RepoEntry:
    """A registered repo with its settings."""

    path: Path
    remote: str = ""
    linear_project: str = ""
    credentials: str = "default"
    trigger_labels: list[str] = field(default_factory=lambda: ["agent"])
    skip_labels: list[str] = field(default_factory=lambda: ["blocked", "human-only"])

    def get_credentials(self) -> dict[str, str]:
        creds = Credentials.load()
        return creds.get(self.credentials)


@dataclass
class GlobalConfig:
    """Instance-level config from ~/.modastack/config.yaml."""

    repos: list[RepoEntry] = field(default_factory=list)

    # Slack — one bot per modabot instance
    slack_bot_token: str = ""
    slack_app_token: str = ""

    # Webhook server
    webhook_port: int = 8080
    public_url: str = ""

    # GitHub accounts
    github_default_account: str = ""
    github_accounts: dict[str, str] = field(default_factory=dict)

    @property
    def repo_paths(self) -> list[Path]:
        return [e.path for e in self.repos]

    def get_repo(self, path: Path) -> RepoEntry | None:
        resolved = path.resolve()
        for entry in self.repos:
            if entry.path.resolve() == resolved:
                return entry
        return None

    @classmethod
    def load(cls) -> "GlobalConfig":
        if not GLOBAL_CONFIG_PATH.exists():
            return cls()

        raw = yaml.safe_load(GLOBAL_CONFIG_PATH.read_text()) or {}

        repos = []
        for entry in raw.get("repos", []):
            if isinstance(entry, dict):
                repos.append(RepoEntry(
                    path=Path(entry["path"]).expanduser(),
                    remote=entry.get("remote", ""),
                    linear_project=entry.get("linear_project", ""),
                    credentials=entry.get("credentials", "default"),
                    trigger_labels=entry.get("trigger_labels", ["agent"]),
                    skip_labels=entry.get("skip_labels", ["blocked", "human-only"]),
                ))
            elif isinstance(entry, str):
                repos.append(RepoEntry(path=Path(entry).expanduser()))

        slack = raw.get("slack", {})
        webhooks = raw.get("webhooks", {})
        github = raw.get("github", {})

        return cls(
            repos=repos,
            slack_bot_token=slack.get("bot_token", "") or raw.get("slack_bot_token", ""),
            slack_app_token=slack.get("app_token", ""),
            webhook_port=webhooks.get("port", 8080),
            public_url=webhooks.get("public_url", ""),
            github_default_account=github.get("default_account", ""),
            github_accounts=github.get("accounts", {}),
        )

    def save(self) -> None:
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        repo_dicts = []
        for entry in self.repos:
            d: dict = {"path": str(entry.path)}
            if entry.remote:
                d["remote"] = entry.remote
            if entry.linear_project:
                d["linear_project"] = entry.linear_project
            if entry.credentials != "default":
                d["credentials"] = entry.credentials
            if entry.trigger_labels != ["agent"]:
                d["trigger_labels"] = entry.trigger_labels
            if entry.skip_labels != ["blocked", "human-only"]:
                d["skip_labels"] = entry.skip_labels
            repo_dicts.append(d)

        data = {
            "slack": {
                "bot_token": self.slack_bot_token,
                "app_token": self.slack_app_token,
            },
            "webhooks": {
                "port": self.webhook_port,
            },
            "github": {
                "default_account": self.github_default_account,
                "accounts": self.github_accounts,
            },
            "repos": repo_dicts,
        }
        if self.public_url:
            data["webhooks"]["public_url"] = self.public_url
        GLOBAL_CONFIG_PATH.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
