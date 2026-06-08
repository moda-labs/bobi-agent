"""Per-project configuration from .modastack/config.yaml.

All config is scoped to a project directory — no global ~/.modastack/.
Service credentials, event server URLs, and registry lists live alongside
the project they belong to.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _project_config_path(project_path: Path) -> Path:
    return project_path / ".modastack" / "config.yaml"


@dataclass
class Config:
    """Per-project config from .modastack/config.yaml."""

    event_server_url: str = ""
    slack_bot_token: str = ""
    linear_api_key: str = ""
    registries: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, project_path: Path) -> "Config":
        """Load config from <project>/.modastack/config.yaml."""
        raw = _load_yaml(_project_config_path(project_path))
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
