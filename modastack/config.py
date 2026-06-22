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


def write_env_file(path: Path, values: dict[str, str]) -> None:
    """Write a .env file in the one format parse_env_file reads.

    The single serializer for .env — install and setup both write
    through here so the round-trip rules can never diverge.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"{k}={v}" for k, v in sorted(values.items())) + "\n")


def load_dotenv(project_path: Path) -> None:
    """Load .modastack/.env into os.environ (existing vars take precedence)."""
    from modastack import paths
    for key, value in parse_env_file(paths.modastack_dir(project_path) / ".env").items():
        if key not in os.environ:
            os.environ[key] = value


def find_required_env_vars(project_path: Path) -> list[str]:
    """Scan .modastack/agent.yaml for ${VAR} references and return var names."""
    from modastack import paths
    agent_yaml = paths.agent_yaml_path(project_path)
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
    from modastack import paths
    return paths.agent_yaml_path(project_path)


def _parse_channels(value) -> list[str]:
    """Normalize a `channels:` field to a list of non-empty strings.

    Accepts a list, or a comma-separated string (so it can come from a
    `${SLACK_CHANNELS}` env var that resolves to "C1,C2"). Empty/None -> [].
    """
    if not value:
        return []
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        return []
    return [str(c).strip() for c in items if str(c).strip()]


@dataclass
class RequiresEntry:
    """One host-level dependency declared in agent.yaml."""

    name: str
    check: str
    why: str = ""
    fix: str = ""


def run_requires_checks(
    requires: list[RequiresEntry],
    timeout: float = 10,
) -> list[tuple[RequiresEntry, bool, str]]:
    """Run each requires check command and return (entry, passed, detail).

    Shared runner used by both doctor and dispatch-time gate.
    """
    import subprocess

    results: list[tuple[RequiresEntry, bool, str]] = []
    for entry in requires:
        try:
            proc = subprocess.run(
                entry.check, shell=True, timeout=timeout,
                capture_output=True, text=True,
            )
            if proc.returncode == 0:
                results.append((entry, True, "healthy"))
            else:
                detail = proc.stderr.strip()[:200] or f"exit code {proc.returncode}"
                results.append((entry, False, detail))
        except subprocess.TimeoutExpired:
            results.append((entry, False, f"check timed out ({timeout}s)"))
        except OSError as exc:
            results.append((entry, False, f"check command failed: {exc}"))
    return results


@dataclass
class BuildSpec:
    """A team's container build declaration (C24 team-flavored images).

    Optional `build:` block in agent.yaml. Renders to a shell hook script
    (see modastack/build_render.py) run as one stable Docker layer BELOW the
    volatile framework-wheel copy, so a framework release rebuilds only the
    wheel, not the team's tools. `apt`/`npm`/`run_root` install system-wide as
    root (`run_root` is the escape hatch for root steps apt can't express, e.g.
    `npx playwright install-deps chromium`); `run` steps execute as the
    `modastack` user into the image HOME (/home/modastack) — the same path the
    agent runs with, so ~-relative tools like gstack's skills are baked in place
    and read directly at runtime (the entrypoint redirects only Claude's durable
    state to the volume via CLAUDE_CONFIG_DIR; no tool copy). `verify_requires`
    runs the team's requires[].check as the final hook step, against that same
    HOME, failing CI on a miss.

    `dockerfile` is the escape hatch: when a raw `Dockerfile` sits beside
    agent.yaml it wins, and the renderer is bypassed (the framework only asserts
    its `FROM …modastack-base…`). Set by the loader when that file exists.
    """

    base: str = ""
    apt: list[str] = field(default_factory=list)
    npm: list[str] = field(default_factory=list)
    run_root: list[str] = field(default_factory=list)
    run: list[str] = field(default_factory=list)
    verify_requires: bool = False
    dockerfile: str = ""

    @property
    def is_empty(self) -> bool:
        """True when nothing would be baked (no layers, no escape-hatch file)."""
        return not (self.apt or self.npm or self.run_root or self.run
                    or self.dockerfile)


@dataclass
class ServiceConfig:
    """One service declaration from agent.yaml."""

    name: str
    events: bool = False
    # When True, a failed preflight check for this service blocks `modastack
    # start`. When False (the default), the failure is surfaced as a warning
    # and the agent starts degraded — the service's events just don't arrive
    # until it's configured. Pack authors mark genuinely-essential services
    # `required: true`.
    required: bool = False
    credentials: dict[str, str] = field(default_factory=dict)
    # Optional event-scoping keys (e.g. Slack channel IDs). When set, the
    # service subscribes only to these channels rather than the whole
    # workspace — lets multiple teams share one bot, split by channel.
    channels: list[str] = field(default_factory=list)


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

    default_role: str = ""

    mcp_servers: dict[str, dict] = field(default_factory=dict)
    monitors: list[dict] = field(default_factory=list)
    auto_dispatch: list[dict] = field(default_factory=list)
    requires: list[RequiresEntry] = field(default_factory=list)
    build: "BuildSpec | None" = None  # C24 team image build spec; None = generic base
    spend_cap: int = 0  # max agent invocations per rolling hour; 0 = use default
    max_concurrent_agents: int = 0  # max simultaneous subagents; 0 = use default (2)

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
        # Preserve monitor commands and requires check/fix commands
        # verbatim — they may contain ${VAR} or ~ intended for shell
        # expansion, not config interpolation.
        monitors_raw = raw_uninterpolated.get("monitors", [])
        requires_raw = raw_uninterpolated.get("requires", [])
        # build steps are shell commands run at image-build time; preserve them
        # verbatim (they may carry ~ or literal $VAR for the build shell).
        build_raw = raw_uninterpolated.get("build", None)
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
                    required=bool(s.get("required", False)),
                    credentials={k: str(v) for k, v in creds.items()},
                    channels=_parse_channels(s.get("channels")),
                ))

        requires = []
        for r in requires_raw:
            if not isinstance(r, dict):
                continue
            name = r.get("name", "")
            check = r.get("check", "")
            if not name or not check:
                log.warning("requires entry missing name or check, skipping: %s", r)
                continue
            requires.append(RequiresEntry(
                name=name, check=check,
                why=r.get("why", ""), fix=r.get("fix", ""),
            ))

        build = cls._parse_build(build_raw, path)

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
            default_role=raw.get("defaults", {}).get("role", "") if isinstance(raw.get("defaults"), dict) else "",
            venn_api_key=raw.get("venn_api_key", ""),
            mcp_servers=raw.get("mcp_servers", {}),
            monitors=raw.get("monitors", []),
            auto_dispatch=raw.get("auto_dispatch", []),
            requires=requires,
            build=build,
            spend_cap=int(raw.get("spend_cap", 0)),
            max_concurrent_agents=int(raw.get("max_concurrent_agents", 0)),
        )

    @staticmethod
    def _parse_build(build_raw, agent_yaml_path: Path) -> "BuildSpec | None":
        """Parse the `build:` block + detect a sibling Dockerfile escape hatch.

        Returns None when the team declares no build (deploys on the generic
        base). A raw `Dockerfile` next to agent.yaml counts as a build even with
        no `build:` block — it's the long-tail escape hatch.
        """
        def _str_list(value) -> list[str]:
            if value is None:
                return []
            if isinstance(value, str):
                return [value]
            if isinstance(value, (list, tuple)):
                return [str(v) for v in value if str(v).strip()]
            return []

        sibling = agent_yaml_path.parent / "Dockerfile"
        dockerfile = str(sibling) if sibling.exists() else ""

        if not isinstance(build_raw, dict):
            # No build: block. Still a build if a raw Dockerfile is present.
            if dockerfile:
                return BuildSpec(dockerfile=dockerfile)
            return None

        verify = str(build_raw.get("verify", "")).strip().lower() == "requires"
        spec = BuildSpec(
            base=str(build_raw.get("base", "")),
            apt=_str_list(build_raw.get("apt")),
            npm=_str_list(build_raw.get("npm")),
            run_root=_str_list(build_raw.get("run_root")),
            run=_str_list(build_raw.get("run")),
            verify_requires=verify,
            dockerfile=dockerfile,
        )
        return None if spec.is_empty and not verify else spec

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
#
# One deployment per SESSION, never shared. When sessions shared one
# deployment (a single deployment.json per project), every agent's
# subscriptions were unioned onto it and the event server fanned every
# matching event out to every connected agent — project leads received
# the user's Slack DMs to the director and replied to them.


def _safe_session(session: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]", "_", session) or "_"


def deployment_state_path(project_path: Path, session: str) -> Path:
    from modastack import paths
    return (paths.state_path(project_path) / "deployments"
            / f"{_safe_session(session)}.json")


def session_cursor_path(project_path: Path, session: str) -> Path:
    """Per-session event cursor. Seq numbers are per-deployment, so a shared
    cursor file would corrupt replay positions across sessions."""
    from modastack import paths
    return (paths.state_path(project_path) / "cursors"
            / f"{_safe_session(session)}.json")


def load_deployment_state(project_path: Path, session: str) -> dict:
    """Load a session's event server deployment_id + api_key."""
    import json
    state_file = deployment_state_path(project_path, session)
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_deployment_state(project_path: Path, session: str,
                          deployment_id: str, api_key: str) -> None:
    """Save a session's event server deployment_id + api_key."""
    import json
    state_file = deployment_state_path(project_path, session)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({
        "deployment_id": deployment_id,
        "api_key": api_key,
    }))


# --- bubble (trust-domain) state -------------------------------------------
# One bubble per running instance. Minted once (lazily, lock-protected) and
# shared by every session of the instance via the local filesystem. The bubble
# key signs publishes + join registrations; it is a private local secret stored
# OUTSIDE .env (which is template-expanded into agent configs). See
# modastack/events/server.py:ensure_bubble.


def bubble_state_path(project_path: Path) -> Path:
    from modastack import paths
    return paths.state_path(project_path) / "bubble.json"


def load_bubble_state(project_path: Path) -> dict:
    """Load the instance's bubble_id + bubble_key, or {} if not yet minted."""
    import json
    state_file = bubble_state_path(project_path)
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_bubble_state(project_path: Path, bubble_id: str, bubble_key: str) -> None:
    """Persist the bubble credential at mode 0600 (it is a signing secret)."""
    import json
    import os
    state_file = bubble_state_path(project_path)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 so the key is never group/world-readable.
    fd = os.open(str(state_file), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps({
            "bubble_id": bubble_id,
            "bubble_key": bubble_key,
        }).encode())
    finally:
        os.close(fd)
    try:
        os.chmod(state_file, 0o600)
    except OSError:
        pass


def clear_bubble_state(project_path: Path) -> None:
    """Drop the bubble credential — a subsequent start mints a fresh bubble."""
    bubble_state_path(project_path).unlink(missing_ok=True)


