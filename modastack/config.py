"""Per-project configuration from agent.yaml.

All config is scoped to a project directory — no global ~/.modastack/.
Service credentials, event server URLs, and registry lists live alongside
the project they belong to.

agent.yaml is the single config file for an agent team. It defines the
agent's roles, services, monitors, and credentials. Secrets use ${ENV_VAR}
references resolved from the environment at load time.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict (quotes stripped, comments skipped)."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            result[key] = value.strip().strip("'\"")
    return result


def load_dotenv(project_path: Path) -> None:
    """Load .modastack/.env into os.environ (existing vars take precedence)."""
    for key, value in parse_env_file(project_path / ".modastack" / ".env").items():
        if key not in os.environ:
            os.environ[key] = value


def find_required_env_vars(project_path: Path) -> list[str]:
    """Scan .modastack/agent.yaml for ${VAR} references and return var names."""
    agent_yaml = project_path / ".modastack" / "agent.yaml"
    if not agent_yaml.exists():
        return []
    content = agent_yaml.read_text()
    return _ENV_VAR_RE.findall(content)


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
    return project_path / ".modastack" / "agent.yaml"


@dataclass
class ServiceConfig:
    """One service declaration from agent.yaml."""

    name: str
    events: bool = False
    credentials: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    """Per-project config from agent.yaml."""

    agent: str = ""
    version: str = ""
    entry_point: str = ""
    chat: str = ""
    services: list[ServiceConfig] = field(default_factory=list)

    event_server_url: str = ""
    registries: list[str] = field(default_factory=list)

    venn_api_key: str = ""

    mcp_servers: dict[str, dict] = field(default_factory=dict)
    monitors: list[dict] = field(default_factory=list)

    def credential(self, service: str, key: str) -> str:
        """Look up a credential value for a named service."""
        for svc in self.services:
            if svc.name == service:
                return svc.credentials.get(key, "")
        return ""

    @classmethod
    def load(cls, project_path: Path) -> "Config":
        """Load config from .modastack/agent.yaml, resolving .env first."""
        load_dotenv(project_path)
        agent_yaml = _project_config_path(project_path)
        if not agent_yaml.exists():
            return cls()
        return cls._parse(agent_yaml)

    @classmethod
    def _parse(cls, path: Path) -> "Config":
        raw_uninterpolated = _load_yaml(path)
        # Preserve monitor commands verbatim — they may contain ${VAR}
        # intended for shell expansion, not config interpolation.
        monitors_raw = raw_uninterpolated.get("monitors", [])
        raw = _interpolate_env(raw_uninterpolated)
        raw["monitors"] = monitors_raw

        services = []
        for s in raw.get("services", []):
            if isinstance(s, str):
                services.append(ServiceConfig(name=s))
            elif isinstance(s, dict):
                creds = s.get("credentials", {})
                if not isinstance(creds, dict):
                    creds = {}
                services.append(ServiceConfig(
                    name=s.get("name", ""),
                    events=s.get("events", False),
                    credentials={k: str(v) for k, v in creds.items()},
                ))

        event_server = raw.get("event_server", {})
        if isinstance(event_server, str):
            event_server_url = event_server
        else:
            event_server_url = event_server.get("url", "")

        return cls(
            agent=raw.get("agent", ""),
            version=str(raw.get("version", "")),
            entry_point=raw.get("entry_point", ""),
            chat=raw.get("chat", ""),
            services=services,
            event_server_url=raw.get("event_server_url", event_server_url),
            registries=raw.get("registries", []),
            venn_api_key=raw.get("venn_api_key", ""),
            mcp_servers=raw.get("mcp_servers", {}),
            monitors=raw.get("monitors", []),
        )

    @property
    def venn_services(self) -> list[ServiceConfig]:
        """Services without a registered ingestion adapter (require Venn)."""
        from modastack.events.adapters import is_registered
        return [s for s in self.services if not is_registered(s.name)]

    @property
    def event_services(self) -> list[ServiceConfig]:
        """Services with events enabled."""
        return [s for s in self.services if s.events]


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
