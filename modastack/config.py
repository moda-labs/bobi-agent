"""Machine-wide configuration from ~/.modastack/config.yaml.

Service credentials and connection URLs shared across all projects.
Not checked in — contains secrets.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


def _machine_config_path() -> Path:
    override = os.environ.get("MODASTACK_CONFIG")
    if override:
        return Path(override)
    return Path.home() / ".modastack" / "config.yaml"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


@dataclass
class Config:
    """Machine-wide config from ~/.modastack/config.yaml."""

    event_server_url: str = ""
    slack_bot_token: str = ""
    linear_api_key: str = ""
    registries: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, project_path: Path | None = None) -> "Config":
        """Load machine config. project_path is accepted for API compat but ignored."""
        raw = _load_yaml(_machine_config_path())
        slack = raw.get("slack", {})
        linear = raw.get("linear", {})
        event_server = raw.get("event_server", {})

        return cls(
            event_server_url=event_server.get("url", ""),
            slack_bot_token=slack.get("bot_token", ""),
            linear_api_key=linear.get("api_key", ""),
            registries=raw.get("registries", []),
        )

    @classmethod
    def from_file(cls, project_path: Path) -> "Config":
        return cls.load(project_path)


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


# --- Event server deployment state (ephemeral, auto-registered) ---


def load_deployment_state(project_path: Path) -> dict:
    """Load event server deployment_id + api_key from state dir."""
    import json
    state_file = project_path / ".modastack" / "state" / "deployment.json"
    if not state_file.exists():
        return {}
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
