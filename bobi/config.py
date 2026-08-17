"""Runtime configuration from a Bobi Agent package.

Each selected Bobi Agent runtime has ``run/package/agent.yaml`` plus
``run/.env``. Machine-wide ``<home>/config.yaml`` is deliberately limited to
path/source defaults and is not parsed here.
"""

import logging
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from bobi.fsutil import atomic_write_json

log = logging.getLogger(__name__)


def _launch_admission_defaults() -> dict:
    """The one authority for the launch-admission tunables (D088/Q124).

    Imported lazily ON PURPOSE. `bobi.launch_admission` reaches `bobi.sdk` and
    `bobi.concurrency_semaphore`; this module is imported almost everywhere and
    deliberately keeps `fsutil` as its only module-level `bobi` dependency, so a
    top-level import here would quadruple what `import bobi.config` costs. Both
    readers below run at call time, so the lazy import is free.
    """
    from bobi.launch_admission import LAUNCH_ADMISSION_DEFAULTS
    return dict(LAUNCH_ADMISSION_DEFAULTS)


# `${{...}}` is workflow template syntax, not an env reference. Excluding
# `${{` keeps templates intact during both scanning and interpolation.
_ENV_VAR_RE = re.compile(r"\$\{(?!\{)([^}]+)\}")
_DOTENV_LOADED: dict[str, str] = {}

# The shared moda-hosted event server. Mirrors provision-instance.sh's default
# so every surface (setup, Slack manifest, deploy) agrees on where instances
# phone home when no event server is configured.
DEFAULT_EVENT_SERVER = "https://bobi-events.modalabs.workers.dev"


@dataclass(frozen=True)
class EnvVarRef:
    """One ${VAR} reference in agent.yaml.

    A bare ``${VAR}`` is a required secret; ``${VAR:-default}`` carries its
    own fallback and is optional.

    ``build_only`` marks a name referenced ONLY under the top-level ``build:``
    block — apt/npm/run_root/run steps that bake an image layer. Those run at
    image-build time and nothing reads them to RUN an agent, so requiring them
    at install or startup blocks a runtime that needs nothing. A name used
    both under ``build:`` and anywhere else is not build_only: the runtime use
    is real and wins.
    """

    name: str
    default: str = ""
    required: bool = True
    build_only: bool = False


def parse_env_ref(token: str) -> EnvVarRef:
    """Parse the inside of a ``${...}`` reference into an EnvVarRef."""
    if ":" not in token:
        return EnvVarRef(name=token)
    name, sep, default = token.partition(":-")
    if sep:
        return EnvVarRef(name=name, default=default, required=False)
    # Any other ':' form is treated as optional with no fallback.
    return EnvVarRef(name=token.split(":", 1)[0], required=False)


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
    """Load the selected runtime's .env into os.environ."""
    from bobi import paths
    for key, value in parse_env_file(paths.env_path(project_path)).items():
        if key not in os.environ:
            os.environ[key] = value
            _DOTENV_LOADED[key] = value


def project_env(project_path: Path) -> dict[str, str]:
    """Return process env overlaid onto this runtime's .env.

    Values previously injected into ``os.environ`` by ``load_dotenv`` are not
    treated as real process env here, which keeps one runtime's .env from
    satisfying another runtime's explicit interpolation.
    """
    from bobi import paths
    env = parse_env_file(paths.env_path(project_path))
    for key, value in os.environ.items():
        if _DOTENV_LOADED.get(key) == value:
            continue
        env[key] = value
    return env


def find_env_var_refs(project_path: Path) -> list[EnvVarRef]:
    """Scan package/agent.yaml for ${VAR} references.

    De-duped by name, order preserved; a required reference wins over an
    optional one to the same name. Each ref carries ``build_only`` (see
    EnvVarRef) so callers gating a RUNTIME can skip build-time-only names.
    """
    from bobi import paths
    agent_yaml = paths.agent_yaml_path(project_path)
    build_only_names = _build_only_names(agent_yaml)
    refs: dict[str, EnvVarRef] = {}
    for ref in _scan_env_refs(agent_yaml):
        prior = refs.get(ref.name)
        if prior is None or (ref.required and not prior.required):
            refs[ref.name] = replace(
                ref, build_only=ref.name in build_only_names)
    return list(refs.values())


def find_required_env_vars(project_path: Path) -> list[str]:
    """The bare ${VAR} names an agent needs to RUN.

    ${VAR:-default} carries its own fallback and is excluded, as is a
    build-time-only name (EnvVarRef.build_only) — nothing reads those to run
    an agent, and the image build enforces them where they are actually used.
    """
    return [r.name for r in find_env_var_refs(project_path)
            if r.required and not r.build_only]


def _build_only_names(agent_yaml: Path) -> frozenset[str]:
    """Names referenced under top-level ``build:`` and nowhere else.

    Structural, not a name list: which variables are build-time is a property
    of where a package uses them, and deriving it from the document cannot
    drift the way a maintained allowlist does. (The private deploy plugin carries
    the same contract for its own deploy-side surface, but as a hardcoded
    BUILD_SECRET_NAMES tuple — a name list can't live in `bobi/`, which stays
    generic.)

    Fails SAFE: any parse problem yields the empty set, so every ref stays
    runtime-required exactly as before. A classification bug must over-require
    a secret, never quietly stop requiring one.
    """
    try:
        doc = yaml.safe_load(agent_yaml.read_text())
    except (OSError, yaml.YAMLError):
        return frozenset()
    if not isinstance(doc, dict):
        return frozenset()
    build = doc.get("build")
    if build is None:
        return frozenset()
    in_build = _refs_in(build)
    elsewhere: set[str] = set()
    for key, value in doc.items():
        if key != "build":
            elsewhere |= _refs_in(value)
    return frozenset(in_build - elsewhere)


def _refs_in(node) -> set[str]:
    """Every ${VAR} name anywhere in a parsed YAML subtree."""
    found: set[str] = set()
    if isinstance(node, str):
        found |= {parse_env_ref(t).name for t in _ENV_VAR_RE.findall(node)}
    elif isinstance(node, dict):
        for key, value in node.items():
            found |= _refs_in(key)
            found |= _refs_in(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            found |= _refs_in(item)
    return found


def _scan_env_refs(agent_yaml: Path) -> list[EnvVarRef]:
    """Every ${VAR} reference in an arbitrary (not-yet-installed) package
    file, in order, un-deduped. The one file-level scan the filters below and
    find_env_var_refs build on."""
    if not agent_yaml.exists():
        return []
    return [parse_env_ref(t) for t in _ENV_VAR_RE.findall(agent_yaml.read_text())]


def _dedup(names: list[str]) -> list[str]:
    return list(dict.fromkeys(names))


def scan_required_vars(agent_yaml: Path) -> list[str]:
    """The bare ${VAR} secret names a package's agent.yaml requires.

    Like find_required_env_vars but for a package file that isn't installed
    yet. A bare ${VAR} is required; ${VAR:-default} carries its own fallback
    (a ':' in the captured name) and is optional, so it's excluded. De-duped,
    order preserved.
    """
    return _dedup([r.name for r in _scan_env_refs(agent_yaml) if r.required])


def scan_declared_vars(agent_yaml: Path) -> list[str]:
    """All ${VAR} secret names a package references — required AND optional.

    Unlike `scan_required_vars`, this keeps ${VAR:-default} refs (stripping the
    `:-default` suffix). An optional ref is still DECLARED: it may legitimately be
    set, and must never be pruned. This is the team's complete secret surface, so
    it doubles as the prune authority and the env-file filter. De-duped, order
    preserved.
    """
    return _dedup([r.name for r in _scan_env_refs(agent_yaml)])


def _interpolate_env(value, env: dict[str, str] | None = None):
    """Recursively resolve ${VAR} / ${VAR:-default} references in strings,
    dicts, and lists. An unset (or empty) VAR resolves to its ``:-`` fallback
    when it has one, else ""."""
    lookup = os.environ if env is None else env
    if isinstance(value, str):
        def _resolve(m: "re.Match[str]") -> str:
            ref = parse_env_ref(m.group(1))
            return lookup.get(ref.name) or ref.default
        return _ENV_VAR_RE.sub(_resolve, value)
    if isinstance(value, dict):
        return {k: _interpolate_env(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env(v, env) for v in value]
    return value


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def positive_int(value: object) -> int:
    """A YAML scalar as a positive int, or 0 for absent/blank/unusable input.

    The ONE parser for "a positive integer setting", shared with the workflow
    schema (#845). Values arrive as YAML scalars OR as ``${VAR}``
    interpolations that resolved to a string (or to nothing), so a plain
    ``int()`` is not enough; and 0 must mean "unconfigured" rather than a
    literal zero, because no such setting has a valid zero.

    ``True``/``False`` (YAML ``yes``/``no``) read as unusable, not as 1/0 -
    a bool where a count belongs is a typo, and silently honoring it is the
    failure mode ``bobi/validate.py`` exists to warn about.
    """
    if isinstance(value, bool):
        return 0
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _project_config_path(project_path: Path) -> Path:
    from bobi import paths
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


def requires_detail(detail: str, limit: int = 0) -> str:
    """Render one failed check's detail as a single bounded line.

    The detail is what distinguishes a timeout from a missing command from
    the check's own stderr, so every surface that reports a failure renders
    it through here rather than deciding for itself what to drop (#771).
    `limit` truncates for display; 0 keeps the whole detail.
    """
    text = " ".join((detail or "").split()) or "no detail"
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return text


@dataclass
class BuildSpec:
    """A team's container build declaration (C24 team-flavored images).

    Optional `build:` block in agent.yaml. Renders to a shell hook script
    (see bobi/build_render.py) run as one stable Docker layer BELOW the
    volatile framework-wheel copy, so a framework release rebuilds only the
    wheel, not the team's tools. `apt`/`npm`/`run_root` install system-wide as
    root (`run_root` is the escape hatch for root steps apt can't express, e.g.
    `npx playwright install-deps chromium`); `run` steps execute as the
    `bobi` user into the image HOME (/home/bobi) — the same path the
    agent runs with, so ~-relative tools like gstack's skills are baked in place
    and read directly at runtime (the entrypoint redirects only Claude's durable
    state to the volume via CLAUDE_CONFIG_DIR; no tool copy). `verify_requires`
    runs the team's requires[].check as the final hook step, against that same
    HOME, failing CI on a miss.

    `dockerfile` is the escape hatch: when a raw `Dockerfile` sits beside
    agent.yaml it wins, and the renderer is bypassed (the framework only asserts
    its `FROM …bobi-base…`). Set by the loader when that file exists.
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
    # When True, a failed preflight check for this service blocks `bobi
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

    venn_api_key: str = ""

    mcp_servers: dict[str, dict] = field(default_factory=dict)
    monitors: list[dict] = field(default_factory=list)
    auto_dispatch: list[dict] = field(default_factory=list)
    requires: list[RequiresEntry] = field(default_factory=list)
    # Host capabilities a dependency needs but the container can't grant itself
    # (a kernel sysctl, a device) — #428 Stage 3. Raw `host:` entries
    # (`{sysctl: key=value}`); parsed into HostCap by bobi.host_caps. Emitted into
    # the composed agent.yaml from a dependency's `host:` field so deploy/doctor can
    # surface + verify it. Never materialized into the image (runtime wiring).
    host: list = field(default_factory=list)
    build: "BuildSpec | None" = None  # C24 team image build spec; None = generic base
    spend_cap: int = 0  # max agent invocations per rolling hour; 0 = use default
    max_concurrent_agents: int = 0  # max simultaneous subagents; 0 = use default (2)
    max_launch_depth: int = 0  # max launch-chain depth; 0 = use default (8)
    launch_admission: dict = field(default_factory=_launch_admission_defaults)
    # Which agent "brain" (ENGINE) drives this team's agents (#485). `{kind:
    # claude|codex, model: <optional override>, effort: <optional reasoning
    # effort>, max_turns: <optional per-session turn cap>}`. Setting
    # `base_url` points the engine at a gateway endpoint
    # (#655/#777/#789): a claude engine additionally takes `small_model`, a
    # codex engine `wire_api` (responses by default; chat remains a pass-through
    # escape hatch for pinned older Codex builds). The deprecated
    # kinds `gateway`/`gateway-openai` remain accepted aliases for
    # claude/codex-with-base_url. Empty = the framework default (claude).
    brain: dict = field(default_factory=dict)
    # Per-role settings (#617, #778, #845). `roles: {<role>: {model:
    # <override>, effort: <override>, max_turns: <override>}}`. A role's model
    # and reasoning effort are provider-native strings for the team's brain
    # (Claude aliases like `haiku`, full Claude IDs, Codex IDs; efforts like
    # `low`..`xhigh`) - never translated. `max_turns` is the role's per-session
    # turn cap: a long-running builder needs a far higher one than a
    # single-verdict monitor.
    roles: dict = field(default_factory=dict)

    @property
    def entry_role(self) -> str:
        """The team's entry-point role, defaulting to "manager" when unset.

        The one place the default lives: named start, monitor agent spawns,
        and address resolution all resolve the role through this property.
        """
        return self.entry_point or "manager"

    @property
    def brain_kind(self) -> str:
        """The configured brain kind, or "" for the framework default."""
        return str((self.brain or {}).get("kind", "") or "")

    @property
    def brain_model(self) -> str:
        """The configured brain model override, or "" for the provider default."""
        return str((self.brain or {}).get("model", "") or "")

    @property
    def brain_effort(self) -> str:
        """The configured brain reasoning effort, or "" for the provider default."""
        return str((self.brain or {}).get("effort", "") or "")

    @property
    def brain_base_url(self) -> str:
        """The configured gateway endpoint (#655/#789), or ""."""
        return str((self.brain or {}).get("base_url", "") or "")

    @property
    def brain_is_gateway(self) -> bool:
        """Whether this team DECLARES a gateway endpoint (#789).

        Presence-based, not value-based: a ``base_url`` key whose ``${VAR}``
        resolved empty still counts, so validation and the startup guard can
        act on the declaration instead of silently running the engine against
        the real vendor endpoint. The alias set is imported lazily so this
        module stays import-free of the brain package at load time while a
        future alias can never desynchronize the two.
        """
        from bobi.brain import BRAIN_KIND_ALIASES

        return (
            self.brain_kind in BRAIN_KIND_ALIASES
            or "base_url" in (self.brain or {})
        )

    @property
    def brain_small_model(self) -> str:
        """The gateway's small/fast model override (#655), or ""."""
        return str((self.brain or {}).get("small_model", "") or "")

    @property
    def brain_wire_api(self) -> str:
        """The OpenAI-compatible gateway wire API (#777), defaulting to responses."""
        return str((self.brain or {}).get("wire_api", "") or "responses")

    @property
    def brain_max_turns(self) -> int:
        """The team's default per-session turn cap (#845), or 0 when unset.

        0 means "unconfigured" and falls through to the framework default in
        ``bobi.brain.resolve_max_turns`` - the cap is a positive integer, so
        there is no valid 0 to confuse it with.
        """
        return positive_int((self.brain or {}).get("max_turns"))

    def role_model(self, role: str) -> str:
        """The model configured for *role*, or "" when unconfigured."""
        entry = (self.roles or {}).get(role)
        if isinstance(entry, dict):
            return str(entry.get("model", "") or "")
        return ""

    def role_effort(self, role: str) -> str:
        """The reasoning effort configured for *role*, or "" when unconfigured."""
        entry = (self.roles or {}).get(role)
        if isinstance(entry, dict):
            return str(entry.get("effort", "") or "")
        return ""

    def role_max_turns(self, role: str) -> int:
        """The per-session turn cap configured for *role*, or 0 when unset."""
        entry = (self.roles or {}).get(role)
        if isinstance(entry, dict):
            return positive_int(entry.get("max_turns"))
        return 0

    def credential(self, service: str, key: str) -> str:
        """Look up a credential value for a named service."""
        for svc in self.services:
            if svc.name == service:
                return svc.credentials.get(key, "")
        return ""

    @classmethod
    def load(cls, project_path: Path) -> "Config":
        """Load config from package/agent.yaml with per-project env resolution."""
        agent_yaml = _project_config_path(project_path)
        if not agent_yaml.exists():
            return cls()
        return cls._parse(agent_yaml, env=project_env(project_path))

    @classmethod
    def _parse(cls, path: Path, env: dict[str, str] | None = None) -> "Config":
        raw_uninterpolated = _load_yaml(path)
        # Preserve monitor commands and requires check/fix commands
        # verbatim — they may contain ${VAR} or ~ intended for shell
        # expansion, not config interpolation.
        monitors_raw = raw_uninterpolated.get("monitors", [])
        requires_raw = raw_uninterpolated.get("requires") or []
        # host: entries carry a sysctl `key=value` verbatim — no config
        # interpolation (mirrors requires/build).
        host_raw = raw_uninterpolated.get("host", [])
        # build steps are shell commands run at image-build time; preserve them
        # verbatim (they may carry ~ or literal $VAR for the build shell).
        build_raw = raw_uninterpolated.get("build", None)
        raw = _interpolate_env(raw_uninterpolated, env)
        raw["monitors"] = monitors_raw

        services = []
        for s in raw.get("services") or []:
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

        event_server = raw.get("event_server") or {}
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
            venn_api_key=raw.get("venn_api_key", ""),
            mcp_servers=raw.get("mcp_servers") or {},
            monitors=raw.get("monitors") or [],
            auto_dispatch=raw.get("auto_dispatch") or [],
            requires=requires,
            host=host_raw if isinstance(host_raw, list) else [],
            build=build,
            # `key:` with an empty value is YAML null, so `raw.get(key, default)`
            # returns None and the default never applies. Every container and
            # numeric field therefore reads `or <default>`, not a get-default:
            # one commented-out value in agent.yaml used to raise out of
            # Config.load and take down start/status/dispatch with a traceback
            # naming neither the key nor the file.
            spend_cap=int(raw.get("spend_cap") or 0),
            max_concurrent_agents=int(raw.get("max_concurrent_agents") or 0),
            max_launch_depth=int(raw.get("max_launch_depth") or 0),
            launch_admission=cls._parse_launch_admission(raw.get("launch_admission") or {}),
            brain=raw.get("brain", {}) if isinstance(raw.get("brain"), dict) else {},
            roles=raw.get("roles", {}) if isinstance(raw.get("roles"), dict) else {},
        )

    @staticmethod
    def _parse_launch_admission(raw: object) -> dict:
        defaults = _launch_admission_defaults()
        if not isinstance(raw, dict):
            return defaults
        # Every key is present after the merge, so the per-key `.get` fallbacks
        # this replaced were unreachable — a second copy of the defaults that
        # could only ever drift from the first (Q124).
        cfg = {**defaults, **raw}
        soft = max(0.1, float(cfg["load_per_cpu_soft_limit"]))
        hard = max(soft, float(cfg["load_per_cpu_hard_limit"]))
        return {
            "enabled": _as_bool(cfg["enabled"]),
            "max_starting_agents": max(1, int(cfg["max_starting_agents"])),
            "load_per_cpu_soft_limit": soft,
            "load_per_cpu_hard_limit": hard,
            "min_memory_available_mb": max(0, int(cfg["min_memory_available_mb"])),
            "init_failure_window_seconds": max(1, int(cfg["init_failure_window_seconds"])),
            "init_failure_backoff_threshold": max(1, int(cfg["init_failure_backoff_threshold"])),
        }

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
        from bobi.events.adapters import is_registered
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
    from bobi import paths
    return (paths.state_path(project_path) / "deployments"
            / f"{_safe_session(session)}.json")


def session_cursor_path(project_path: Path, session: str) -> Path:
    """Per-session event cursor. Seq numbers are per-deployment, so a shared
    cursor file would corrupt replay positions across sessions."""
    from bobi import paths
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
    state_file = deployment_state_path(project_path, session)
    # Atomic: a torn write here reads back as {} and the session loses the
    # credentials it needs to reach its event server.
    atomic_write_json(state_file, {
        "deployment_id": deployment_id,
        "api_key": api_key,
    }, indent=None)


# --- bubble (trust-domain) state -------------------------------------------
# One bubble per running instance. Minted once (lazily, lock-protected) and
# shared by every session of the instance via the local filesystem. The bubble
# key signs publishes + join registrations; it is a private local secret stored
# OUTSIDE .env (which is template-expanded into agent configs). See
# bobi/events/server.py:ensure_bubble.


def bubble_state_path(project_path: Path) -> Path:
    from bobi import paths
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
