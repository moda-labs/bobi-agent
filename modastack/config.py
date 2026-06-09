"""Per-project configuration from agent.yaml / .modastack/config.yaml.

All config is scoped to a project directory — no global ~/.modastack/.
Service credentials, event server URLs, and registry lists live alongside
the project they belong to.

agent.yaml is the unified config format — it replaces both the agent pack's
defaults.yaml and .modastack/config.yaml. Secrets use ${ENV_VAR} references.
The legacy formats are supported as fallbacks during migration.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _interpolate_env(value):
    """Recursively resolve ${ENV_VAR} references in strings, dicts, and lists."""
    if isinstance(value, str):
        return _ENV_VAR_RE.sub(
            lambda m: os.environ.get(m.group(1), ""), value
        )
    if isinstance(value, dict):
        return {k: _interpolate_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v) for v in value]
    return value


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _project_config_path(project_path: Path) -> Path:
    return project_path / ".modastack" / "config.yaml"


@dataclass
class ServiceConfig:
    """One service declaration from agent.yaml."""

    name: str
    events: bool = False


@dataclass
class Config:
    """Per-project config, loaded from agent.yaml or legacy config files."""

    # --- pack metadata (was in defaults.yaml) ---
    version: str = ""
    entry_point: str = ""
    chat: str = ""
    services: list[ServiceConfig] = field(default_factory=list)

    # --- infrastructure ---
    event_server_url: str = ""
    registries: list[str] = field(default_factory=list)

    # --- native integration credentials ---
    slack_bot_token: str = ""
    linear_api_key: str = ""

    # --- venn ---
    venn_api_key: str = ""

    # --- monitors ---
    monitors: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, project_path: Path, agent_name: str | None = None) -> "Config":
        """Load config with fallback: agent.yaml → legacy defaults.yaml + config.yaml."""
        agent_yaml = _find_agent_yaml(project_path, agent_name)
        if agent_yaml:
            return cls._from_agent_yaml(agent_yaml)
        return cls._from_legacy(project_path, agent_name)

    @classmethod
    def _from_agent_yaml(cls, path: Path) -> "Config":
        raw = _interpolate_env(_load_yaml(path))

        services = []
        for s in raw.get("services", []):
            if isinstance(s, str):
                services.append(ServiceConfig(name=s))
            elif isinstance(s, dict):
                services.append(ServiceConfig(
                    name=s.get("name", ""),
                    events=s.get("events", False),
                ))

        slack = raw.get("slack", {})
        linear = raw.get("linear", {})
        event_server = raw.get("event_server", {})
        if isinstance(event_server, str):
            event_server_url = event_server
        else:
            event_server_url = event_server.get("url", "")

        return cls(
            version=str(raw.get("version", "")),
            entry_point=raw.get("entry_point", ""),
            chat=raw.get("chat", ""),
            services=services,
            event_server_url=raw.get("event_server_url", event_server_url),
            registries=raw.get("registries", []),
            slack_bot_token=slack.get("bot_token", "") if isinstance(slack, dict) else "",
            linear_api_key=linear.get("api_key", "") if isinstance(linear, dict) else "",
            venn_api_key=raw.get("venn_api_key", ""),
            monitors=raw.get("monitors", []),
        )

    @classmethod
    def _from_legacy(cls, project_path: Path, agent_name: str | None = None) -> "Config":
        """Load from legacy .modastack/config.yaml + defaults.yaml."""
        raw = _load_yaml(_project_config_path(project_path))
        slack = raw.get("slack", {})
        linear = raw.get("linear", {})
        event_server = raw.get("event_server", {})

        entry_point = ""
        if agent_name:
            defaults = _load_agent_defaults(project_path, agent_name)
            entry_point = defaults.get("role", "")

        return cls(
            event_server_url=event_server.get("url", ""),
            slack_bot_token=slack.get("bot_token", ""),
            linear_api_key=linear.get("api_key", ""),
            registries=raw.get("registries", []),
            entry_point=entry_point,
        )

    @classmethod
    def from_file(cls, project_path: Path) -> "Config":
        return cls.load(project_path)

    @property
    def native_services(self) -> list[str]:
        return ["github", "slack", "linear"]

    @property
    def venn_services(self) -> list[ServiceConfig]:
        """Services that require Venn (not natively supported)."""
        return [s for s in self.services if s.name not in self.native_services]

    @property
    def event_services(self) -> list[ServiceConfig]:
        """Services with events enabled."""
        return [s for s in self.services if s.events]


def _is_unified_agent_yaml(path: Path) -> bool:
    """Check if an agent.yaml uses the new unified format (has services or version)."""
    raw = _load_yaml(path)
    return bool(raw.get("services") or raw.get("entry_point"))


def _find_agent_yaml(project_path: Path, agent_name: str | None = None) -> Path | None:
    """Find a unified-format agent.yaml: project override first, then agent pack.

    Legacy .modastack/agent.yaml files (with just role/subscribe) are ignored —
    those are handled by the legacy config path.
    """
    project_override = project_path / ".modastack" / "agent.yaml"
    if project_override.exists() and _is_unified_agent_yaml(project_override):
        return project_override

    if agent_name:
        for base in [project_path / "agents", project_path / ".modastack" / "agents"]:
            candidate = base / agent_name / "agent.yaml"
            if candidate.exists() and _is_unified_agent_yaml(candidate):
                return candidate

    return None


def _load_agent_defaults(project_path: Path, agent_name: str) -> dict:
    """Load legacy defaults.yaml from an agent pack."""
    from modastack.prompts.resolver import _resolve_agent_dir
    agent_dir = _resolve_agent_dir(agent_name, project_path)
    if not agent_dir:
        return {}
    defaults = agent_dir / "defaults.yaml"
    if not defaults.exists():
        return {}
    return yaml.safe_load(defaults.read_text()) or {}


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
