"""Global and per-repo configuration."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

GLOBAL_CONFIG_DIR = Path.home() / ".dispatch"
GLOBAL_CONFIG_PATH = GLOBAL_CONFIG_DIR / "config.yaml"
STATE_PATH = GLOBAL_CONFIG_DIR / "state.json"
CREDENTIALS_PATH = GLOBAL_CONFIG_DIR / "credentials.yaml"
LOG_DIR = GLOBAL_CONFIG_DIR / "logs"


@dataclass
class Credentials:
    """Named credential sets for different Linear teams and Slack workspaces.

    Stored at ~/.dispatch/credentials.yaml. Each repo references a credential
    by name, so different repos can use different Linear teams and Slack workspaces.
    """

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
        """Get a credential set by name. Falls back to 'default'."""
        if name in self.entries:
            return self.entries[name]
        return self.entries.get("default", {})

    def add(self, name: str, linear_api_key: str = "", slack_bot_token: str = "") -> None:
        self.entries[name] = {
            "linear_api_key": linear_api_key,
            "slack_bot_token": slack_bot_token,
        }
        self.save()

    def list_names(self) -> list[str]:
        return list(self.entries.keys())


@dataclass
class RepoConfig:
    """Per-repo dispatch configuration loaded from .dispatch.yaml."""

    path: Path
    linear_project: str = ""
    linear_team: str = ""
    trigger_labels: list[str] = field(default_factory=lambda: ["agent"])
    skip_labels: list[str] = field(default_factory=lambda: ["blocked", "human-only"])
    complexity_rules: dict[str, str] = field(default_factory=dict)
    agent_tool: str = "claude"
    skills: list[str] = field(default_factory=list)
    max_parallel: int = 2
    test_command: str = ""
    review_required: bool = True
    auto_merge: bool = False
    slack_channel: str = ""
    credentials: str = "default"

    @classmethod
    def from_file(cls, repo_path: Path) -> "RepoConfig":
        config_path = repo_path / ".dispatch.yaml"
        if not config_path.exists():
            raise FileNotFoundError(f"No .dispatch.yaml in {repo_path}")

        raw = yaml.safe_load(config_path.read_text()) or {}
        linear = raw.get("linear", {})
        complexity = raw.get("complexity", {})
        agent = raw.get("agent", {})
        verify = raw.get("verify", {})
        notify = raw.get("notify", {})

        return cls(
            path=repo_path,
            linear_project=linear.get("project", ""),
            linear_team=linear.get("team", ""),
            trigger_labels=linear.get("trigger_labels", ["agent"]),
            skip_labels=linear.get("skip_labels", ["blocked", "human-only"]),
            complexity_rules=complexity,
            agent_tool=agent.get("tool", "claude"),
            skills=agent.get("skills", []),
            max_parallel=agent.get("max_parallel", 2),
            test_command=verify.get("test_command", ""),
            review_required=verify.get("review_required", True),
            auto_merge=verify.get("auto_merge", False),
            slack_channel=notify.get("slack_channel", ""),
            credentials=raw.get("credentials", "default"),
        )

    def get_credentials(self) -> dict[str, str]:
        """Resolve this repo's credential set."""
        creds = Credentials.load()
        return creds.get(self.credentials)


@dataclass
class GlobalConfig:
    """Global dispatch engine configuration from ~/.dispatch/config.yaml."""

    linear_api_key: str = ""
    slack_bot_token: str = ""
    repos: list[Path] = field(default_factory=list)
    poll_interval_minutes: int = 1
    default_agent: str = "claude"

    @classmethod
    def load(cls) -> "GlobalConfig":
        if not GLOBAL_CONFIG_PATH.exists():
            return cls()

        raw = yaml.safe_load(GLOBAL_CONFIG_PATH.read_text()) or {}
        repos = [Path(p).expanduser() for p in raw.get("repos", [])]

        return cls(
            linear_api_key=raw.get("linear_api_key", ""),
            slack_bot_token=raw.get("slack_bot_token", ""),
            repos=repos,
            poll_interval_minutes=raw.get("poll_interval_minutes", 5),
            default_agent=raw.get("default_agent", "claude"),
        )

    def save(self) -> None:
        GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "linear_api_key": self.linear_api_key,
            "slack_bot_token": self.slack_bot_token,
            "repos": [str(p) for p in self.repos],
            "poll_interval_minutes": self.poll_interval_minutes,
            "default_agent": self.default_agent,
        }
        GLOBAL_CONFIG_PATH.write_text(yaml.dump(data, default_flow_style=False))
