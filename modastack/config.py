"""Global and per-repo configuration.

Global config (~/.modastack/config.yaml): instance-level settings
  - Slack tokens (one bot per modabot instance)
  - Webhook server config
  - GitHub accounts
  - Registered repos

Credentials (~/.modastack/credentials.yaml): API keys per workspace
  - Referenced by .modastack.yaml "credentials:" field in each repo
  - Keys depend on task tracker: linear_api_key for Linear, etc.
  - GitHub Issues uses gh CLI auth (no key needed)

Per-repo config (.modastack.yaml): repo-specific settings
  - Task tracking system (github-issues or linear)
  - Project prefix, trigger labels
  - Test command, review policy
  - Repo-specific context for engineers
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

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
class GlobalConfig:
    """Instance-level config from ~/.modastack/config.yaml."""

    repos: list[Path] = field(default_factory=list)

    # Slack — one bot per modabot instance
    slack_bot_token: str = ""
    slack_app_token: str = ""

    # Webhook server
    webhook_port: int = 8080
    public_url: str = ""

    # GitHub accounts
    github_default_account: str = ""
    github_accounts: dict[str, str] = field(default_factory=dict)

    # Manager role (loads roles/manager/<role>.md)
    manager_role: str = "engineering"

    @classmethod
    def load(cls) -> "GlobalConfig":
        if not GLOBAL_CONFIG_PATH.exists():
            return cls()

        raw = yaml.safe_load(GLOBAL_CONFIG_PATH.read_text()) or {}
        repos = [Path(p).expanduser() for p in raw.get("repos", [])]
        slack = raw.get("slack", {})
        webhooks = raw.get("webhooks", {})
        github = raw.get("github", {})

        manager = raw.get("manager", {})

        return cls(
            repos=repos,
            slack_bot_token=slack.get("bot_token", "") or raw.get("slack_bot_token", ""),
            slack_app_token=slack.get("app_token", ""),
            webhook_port=webhooks.get("port", 8080),
            public_url=webhooks.get("public_url", ""),
            github_default_account=github.get("default_account", ""),
            github_accounts=github.get("accounts", {}),
            manager_role=manager.get("role", "engineering"),
        )

    def save(self) -> None:
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
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
            "repos": [str(p) for p in self.repos],
        }
        if self.public_url:
            data["webhooks"]["public_url"] = self.public_url
        GLOBAL_CONFIG_PATH.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


@dataclass
class RepoConfig:
    """Per-repo config from .modastack.yaml."""

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

    @classmethod
    def from_file(cls, repo_path: Path) -> "RepoConfig":
        config_path = repo_path / ".modastack.yaml"
        if not config_path.exists():
            raise FileNotFoundError(f"No .modastack.yaml in {repo_path}")

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
        )

    def get_credentials(self) -> dict[str, str]:
        creds = Credentials.load()
        return creds.get(self.credentials)

    @property
    def linear_project(self) -> str:
        """Backwards compat for code that still references linear_project."""
        return self.project if self.task_tracking == "linear" else ""
