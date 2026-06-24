"""The `modastack deploy` / `modastack destroy` engine — the one-instance
deployment primitive (DEPLOY_INTERFACE.md).

Layering (applied recursively so the engine stays operator-agnostic):

    Layer 3  orchestration : GitHub Action | Terraform | SaaS plane | a for-loop
                                └─ loops/diffs/decides, calls ↓
    Layer 2  the primitive : modastack deploy <name> / destroy <name>   ← ONE instance
                                └─ uses ↓
    Layer 1  mechanics     : provision-instance.sh · fleet.sh · install · fly

`deploy` provisions OR updates ONE instance idempotently; anything that loops or
diffs across instances is orchestration and lives on top. The primitive merges
its own config (flags › deployments/<name>.yaml › deployments/defaults.yaml ›
built-ins) so it works standalone, with no pre-merge by the caller.

Two delivery modes, selected by which team source the config carries:
  * team:     <name> → a LOCAL package → ssh-push (provision blank, push the
                       built tarball in over `fly ssh`, the waiting entrypoint
                       installs it and starts). The "I built it, ship it" path.
  * team-url: <url>  → a PUBLISHED tarball → HTTPS-fetch at first boot (the dark
                       instance pulls it). The enterprise/CI path.

The heavy Fly mechanics stay in scripts/provision-instance.sh + scripts/fleet.sh;
this module resolves config, validates secrets, selects the delivery mode, and
drives those scripts so the same engine backs the CLI, CI, and any future plane.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from modastack.config import _ENV_VAR_RE, parse_env_file, write_env_file

log = logging.getLogger(__name__)

# Mirror provision-instance.sh's default so a standalone deploy and the script
# agree on where instances phone home when no event server is configured.
DEFAULT_EVENT_SERVER = "https://modastack-events.modalabs.workers.dev"

# Built-in defaults — the lowest-precedence layer. Operators override via
# deployments/defaults.yaml (shared values) or per-instance deployments/<name>.yaml.
BUILTIN_DEFAULTS: dict = {
    "region": "iad",
    "memory": "4gb",
    "cpus": 2,
    "volume_size": 15,
    "auth": "api_key",
    "event_server": DEFAULT_EVENT_SERVER,
}

# Config keys the engine understands (after `-`→`_` normalization). `secrets` is
# a nested mapping handled separately.
_SCALAR_KEYS = {
    "team", "team_url", "fleet", "tenant", "region", "memory", "cpus",
    "volume_size", "auth", "event_server", "login_channel", "claude_version",
    "org", "volume_name", "image",
}


class DeployError(RuntimeError):
    """A deployment could not proceed (bad config, missing secret, Fly failure)."""


@dataclass
class DeployConfig:
    """Fully-resolved config for ONE instance, after the precedence merge."""

    name: str
    team: str = ""
    team_url: str = ""
    fleet: str = ""
    # Tenant = the GitHub Environment the GitOps Action binds for this deployment's
    # per-key secrets (key convention `<TEAM>__<KEY>`). The engine itself never
    # uses it — secrets are per-app — but it's a first-class config value the
    # orchestration layer resolves (defaults: prod = `modalabs`; canary overrides).
    tenant: str = ""
    region: str = "iad"
    memory: str = "4gb"
    cpus: int = 2
    volume_size: int = 15
    auth: str = "api_key"
    event_server: str = DEFAULT_EVENT_SERVER
    login_channel: str = ""
    claude_version: str = ""
    org: str = ""
    volume_name: str = "data"
    # A prebuilt team-flavored image ref (C24). When set, deploy by ref instead
    # of building from the Dockerfile — the provisioner gets --image and the
    # binary build context is not assembled.
    image: str = ""
    # Secret source: a local env-file path (self-service) and/or a named source
    # (a GitHub Environment, etc. — a hint the orchestration layer materializes).
    secrets_env: str = ""
    secrets_env_file: str = ""

    @property
    def delivery(self) -> str:
        """'ssh-push' for a local `team:`, 'team-url' for a published URL."""
        return "ssh-push" if self.team else "team-url"

    @property
    def team_name(self) -> str:
        """The team name with any `@version` stripped (D-6). Empty for team-url."""
        from modastack.registry import split_team_ref
        return split_team_ref(self.team)[0] if self.team else ""

    @property
    def team_version(self) -> str | None:
        """The pinned version from a `team: <name>@<version>`, else None (D-6)."""
        from modastack.registry import split_team_ref
        return split_team_ref(self.team)[1] if self.team else None

    @property
    def app_name(self) -> str:
        """Fly app name: `<fleet>-<name>`, or bare `<name>` with no fleet."""
        return f"{self.fleet}-{self.name}" if self.fleet else self.name

    @property
    def fleet_stamp(self) -> str:
        """MODASTACK_FLEET value — the fleet if set, else the name (single-instance)."""
        return self.fleet or self.name

    def validate(self) -> None:
        sources = [bool(self.team), bool(self.team_url)]
        if sum(sources) == 0:
            raise DeployError(
                f"deployment '{self.name}' declares no team source — set `team:` "
                "(a local package, ssh-push) or `team-url:` (a published tarball)."
            )
        if sum(sources) > 1:
            raise DeployError(
                f"deployment '{self.name}' sets both `team:` and `team-url:` — "
                "pick exactly one delivery mode."
            )
        if self.auth not in ("api_key", "subscription"):
            raise DeployError(
                f"deployment '{self.name}' has auth='{self.auth}' "
                "(expected api_key or subscription)."
            )


# --- config loading + precedence merge --------------------------------------

def deployments_dir(project_path: Path) -> Path:
    return project_path / "deployments"


def _normalize(raw: dict) -> dict:
    """Lower-noise a raw YAML dict: `-`→`_` keys, pull `secrets:` up to flat
    `secrets_env` / `secrets_env_file`, and drop unknown keys (with a warning)."""
    out: dict = {}
    for key, value in (raw or {}).items():
        nkey = str(key).replace("-", "_")
        if nkey == "secrets":
            sec = value or {}
            if not isinstance(sec, dict):
                raise DeployError("`secrets:` must be a mapping (env / env-file).")
            if sec.get("env"):
                out["secrets_env"] = str(sec["env"])
            if sec.get("env-file") or sec.get("env_file"):
                out["secrets_env_file"] = str(sec.get("env-file") or sec.get("env_file"))
        elif nkey in _SCALAR_KEYS:
            out[nkey] = value
        else:
            log.warning("ignoring unknown deployment config key '%s'", key)
    return out


def _load_yaml_dict(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise DeployError(f"{path} must be a YAML mapping.")
    return data


def load_deploy_config(project_path: Path, name: str,
                       overrides: dict | None = None) -> DeployConfig:
    """Resolve one deployment's config by the precedence chain:

        flags (overrides)  ›  deployments/<name>.yaml  ›  deployments/defaults.yaml
                           ›  built-in defaults

    The merge happens here, in the primitive, so `deploy` is self-contained: a
    caller never has to pre-merge. A bare `<name>` with no file resolves to the
    local package `agents/<name>` (ssh-push) plus defaults.
    """
    ddir = deployments_dir(project_path)
    merged: dict = dict(BUILTIN_DEFAULTS)
    merged.update(_normalize(_load_yaml_dict(ddir / "defaults.yaml")))

    name_file = ddir / f"{name}.yaml"
    if name_file.exists():
        merged.update(_normalize(_load_yaml_dict(name_file)))
    elif not (overrides and (overrides.get("team") or overrides.get("team_url"))):
        # No deployments/<name>.yaml and no team override: fall back to the local
        # package agents/<name> (the minimal dev path → ssh-push).
        if (project_path / "agents" / name / "agent.yaml").exists():
            merged.setdefault("team", name)

    if overrides:
        merged.update({k: v for k, v in overrides.items() if v is not None})

    cfg = DeployConfig(
        name=name,
        team=str(merged.get("team", "") or ""),
        team_url=str(merged.get("team_url", "") or ""),
        fleet=str(merged.get("fleet", "") or ""),
        tenant=str(merged.get("tenant", "") or ""),
        region=str(merged.get("region")),
        memory=str(merged.get("memory")),
        cpus=int(merged.get("cpus")),
        volume_size=int(merged.get("volume_size")),
        auth=str(merged.get("auth")),
        event_server=str(merged.get("event_server")),
        login_channel=str(merged.get("login_channel", "") or ""),
        claude_version=str(merged.get("claude_version", "") or ""),
        org=str(merged.get("org", "") or ""),
        volume_name=str(merged.get("volume_name", "data") or "data"),
        image=str(merged.get("image", "") or ""),
        secrets_env=str(merged.get("secrets_env", "") or ""),
        secrets_env_file=str(merged.get("secrets_env_file", "") or ""),
    )
    cfg.validate()
    return cfg


# --- repo + package resolution ----------------------------------------------

def find_repo_root(start: Path | None = None) -> Path:
    """Locate the modastack source root (the dir holding scripts/ + Dockerfile).

    `deploy` builds the instance image from the Dockerfile (the image is generic;
    identity lives in the volume + env), so it must run from a modastack checkout
    — exactly as the GitHub Action does. Walk up from `start` until both
    scripts/provision-instance.sh and Dockerfile are found.
    """
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        if (d / "scripts" / "provision-instance.sh").exists() and (d / "Dockerfile").exists():
            return d
    raise DeployError("not a modastack checkout")


@dataclass
class DeployAssets:
    """Where deploy finds its mechanics + how the instance image is built.

    Two modes, so the binary deploys with OR without a repo:
      * **source** — a modastack checkout: build the image from local source
        (Dockerfile, `COPY . .`). Used in dev and the modastack repo's own CI.
      * **binary** — no checkout: the scripts + a PyPI-install Dockerfile ship in
        the wheel (`modastack/_deploy`), so `uv tool install modastack` is enough
        to deploy. The image installs `modastack==<this version>` from PyPI.
    """

    mode: str
    provision_sh: Path
    destroy_sh: Path
    build_context: Path | None
    dockerfile: Path | None
    build_args: dict
    run_cwd: Path | None


def _packaged_deploy_dir() -> Path | None:
    """The bundled deploy assets (`modastack/_deploy`) in an installed wheel, or
    None in an editable/source checkout (where source mode is used instead)."""
    try:
        import importlib.resources as ir
        root = ir.files("modastack") / "_deploy"
        if root.is_dir():
            return Path(str(root))
    except (ModuleNotFoundError, FileNotFoundError, AttributeError, TypeError):
        pass
    return None


def _modastack_version() -> str:
    """The installed modastack version — pinned into the PyPI instance image so
    the deployed instance runs the same code as the binary that deployed it."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("modastack")
    except PackageNotFoundError as e:
        raise DeployError(
            "cannot determine the installed modastack version to build the "
            "instance image. Install modastack from PyPI (`uv tool install "
            "modastack`)."
        ) from e


def resolve_assets(project_path: Path, staging: Path | None = None) -> DeployAssets:
    """Resolve deploy mechanics, preferring a source checkout, else the bundled
    wheel assets. When `staging` is given (a deploy that builds an image), the
    binary-mode build context is assembled there (Dockerfile.pypi + docker/)."""
    try:
        repo = find_repo_root(project_path)
        return DeployAssets(
            mode="source",
            provision_sh=repo / "scripts" / "provision-instance.sh",
            destroy_sh=repo / "scripts" / "destroy-instance.sh",
            build_context=repo,
            dockerfile=repo / "Dockerfile",
            build_args={},
            run_cwd=repo,
        )
    except DeployError:
        pass

    pkg = _packaged_deploy_dir()
    if pkg is None:
        raise DeployError(
            "no deploy assets found — not in a modastack checkout, and the "
            "installed package has no bundled deploy assets. Reinstall with "
            "`uv tool install modastack`."
        )

    ctx = dockerfile = None
    if staging is not None:
        ctx = staging / "build-context"
        ctx.mkdir(parents=True, exist_ok=True)
        shutil.copy(pkg / "Dockerfile", ctx / "Dockerfile")
        shutil.copytree(pkg / "docker", ctx / "docker", dirs_exist_ok=True)
        dockerfile = ctx / "Dockerfile"
    return DeployAssets(
        mode="binary",
        provision_sh=pkg / "scripts" / "provision-instance.sh",
        destroy_sh=pkg / "scripts" / "destroy-instance.sh",
        build_context=ctx,
        dockerfile=dockerfile,
        # `pypi` builder + the version to install (the source builder is the
        # Dockerfile's default, used in a checkout).
        build_args={"MODASTACK_BUILD": "pypi",
                    "MODASTACK_VERSION": _modastack_version()},
        run_cwd=None,
    )


def local_package_dir(base: Path, team: str) -> Path:
    """Resolve a local team package (the ssh-push source). `team` may be a path
    to a team dir, or a name found under `base/agents/<team>` or `base/<team>`."""
    for cand in (Path(team), base / "agents" / team, base / team):
        if (cand / "agent.yaml").exists():
            return cand.resolve()
    raise DeployError(
        f"local team '{team}' not found — looked for agent.yaml in '{team}', "
        f"'{base}/agents/{team}', and '{base}/{team}'. For ssh-push delivery, "
        "point `team:` at a local package directory holding agent.yaml."
    )


def resolve_team_dir(project_path: Path, team: str) -> Path:
    """Resolve a deploy `team:` ref to a **flat, ready-to-ship** package dir.

    The single seam every deploy consumer routes through (D-2) — secret-prune
    scan, deps-render, deps-hash, AND the ssh-push — so they all see the same
    package. A team that declares `from:` is **composed (flattened) here** (#446/
    #451): the base is resolved on the host (which has registry access), the
    chain is merged into a staging dir with no `from:`, and every downstream
    consumer sees the merged build/secrets. The pushed tarball is already flat,
    so the instance never resolves a chain at first boot.
    """
    src = _resolve_team_package(project_path, team)
    return _flatten_if_chained(project_path, src)


def _flatten_if_chained(project_path: Path, team_dir: Path) -> Path:
    """Compose a `from:` chain into a flat staging dir; pass through otherwise.

    A team with no `from:` is returned unchanged (today's behavior, byte-for-byte).
    Composition is deterministic, so the repeated `resolve_team_dir` calls across
    one deploy each produce the same staged image."""
    from modastack import compose, paths
    try:
        has_from = bool((compose._read_agent_yaml(team_dir)).get("from"))
    except compose.ComposeError:
        return team_dir
    if not has_from:
        return team_dir
    chain = compose.resolve_chain(team_dir, project_path)
    staged = paths.modastack_dir(project_path) / "build" / f"composed-{team_dir.name}"
    if staged.exists():
        shutil.rmtree(staged)
    compose.compose(chain, staged)
    # Preserve the leaf's directory name so the app/tarball naming is unchanged.
    cfg = compose._read_agent_yaml(staged)
    cfg.setdefault("agent", team_dir.name)
    (staged / "agent.yaml").write_text(
        yaml.dump(cfg, default_flow_style=False, sort_keys=False))
    final = staged.parent / team_dir.name
    if final.exists():
        shutil.rmtree(final)
    staged.rename(final)
    return final


def _resolve_team_package(project_path: Path, team: str) -> Path:
    """Resolve a deploy `team:` ref (optionally `name@version`) to a package dir.

    After resolution the package is on disk, so the existing local-team
    build/prune/push path runs unchanged. Resolution order:

      1. explicit `@version` → fetch the immutable per-team asset into the
         **shared** install/deploy cache (D-3). A pin **never** falls back to a
         local dir: a stale `agents/<name>` must not silently shadow the pin, and
         a missing asset is a hard error (surfaced by registry.fetch).
      2. bare name with a local `agents/<name>` / `<name>` dir → use it (today's
         behavior, byte-for-byte unchanged — local dev keeps working).
      3. bare name, no local dir → fetch latest into the shared cache.
    """
    from modastack import registry
    # A `team:` that is itself a path to a package dir wins literally — mirrors
    # local_package_dir's first candidate (`Path(team)`) and avoids mis-splitting
    # a filesystem path that happens to contain '@' (e.g. `/work@v2/eng-team`).
    if (Path(team) / "agent.yaml").exists():
        return Path(team).resolve()
    name, version = registry.split_team_ref(team)
    if version:
        # Reuse an already-cached pin with no second download (§3.4); the
        # immutable asset makes the cached copy authoritative.
        if (registry.cached_version(project_path, name) == version
                and registry.is_cached(project_path, name)):
            return registry.cache_path(project_path, name)
        try:
            return registry.fetch(project_path, name, version=version)
        except Exception as e:
            raise DeployError(
                f"could not resolve pinned team '{name}@{version}': {e}"
            ) from e
    # Bare name: a local checkout wins (unchanged), exactly like local_package_dir.
    for cand in (project_path / "agents" / name, project_path / name):
        if (cand / "agent.yaml").exists():
            return cand.resolve()
    try:
        return registry.fetch(project_path, name)
    except Exception as e:
        raise DeployError(
            f"local team '{name}' not found and could not fetch it from the "
            f"registry: {e}"
        ) from e


def scan_required_vars(agent_yaml: Path) -> list[str]:
    """Return the bare ${VAR} secret names a package's agent.yaml requires.

    Like config.find_required_env_vars but for an arbitrary (not-yet-installed)
    package file. A bare ${VAR} is required; ${VAR:-default} carries its own
    fallback (a ':' in the captured name) and is optional, so it's excluded.
    """
    if not agent_yaml.exists():
        return []
    return [v for v in _ENV_VAR_RE.findall(agent_yaml.read_text()) if ":" not in v]


def scan_declared_vars(agent_yaml: Path) -> list[str]:
    """All ${VAR} secret names a package references — required AND optional.

    Unlike `scan_required_vars`, this keeps ${VAR:-default} refs (stripping the
    `:-default` suffix). An optional ref is still DECLARED: it may legitimately be
    set, and must never be pruned. This is the team's complete secret surface, so
    it doubles as the prune authority and the env-file filter. De-duped, order
    preserved.
    """
    if not agent_yaml.exists():
        return []
    seen: dict[str, None] = {}
    for v in _ENV_VAR_RE.findall(agent_yaml.read_text()):
        seen.setdefault(v.split(":", 1)[0], None)  # ${VAR:-x} -> VAR
    return list(seen)


def _secret_sets(cfg: DeployConfig,
                 project_path: Path) -> tuple[list[str], set[str] | None]:
    """Compute (required, declared) secret keys for one deployment.

    required  — must be present (supplied OR already a live Fly secret) or the
                deploy fails: the bare ${VAR} refs, plus ANTHROPIC_API_KEY in
                api_key mode (the auth overlay from the deployment config).
    declared  — the full surface the team may set AND the prune authority: every
                ${VAR} ref (incl. ${VAR:-default}) plus the auth overlay. None for
                a `team-url:` package — it isn't on disk, so we can't see its refs;
                without the declared set we don't filter or prune (defer to boot).

    MODASTACK_* refs are instance identity the provisioner stamps into [env] from
    flags — never secrets — so they're excluded from both sets.
    """
    auth_req = ["ANTHROPIC_API_KEY"] if cfg.auth == "api_key" else []
    if not cfg.team:
        return (auth_req, None)  # team-url: package not local
    y = resolve_team_dir(project_path, cfg.team) / "agent.yaml"
    keep = lambda vs: [v for v in vs if not v.startswith("MODASTACK_")]
    required = keep(scan_required_vars(y)) + auth_req
    declared = set(keep(scan_declared_vars(y))) | set(auth_req)
    return (required, declared)


# --- secret resolution -------------------------------------------------------

def resolve_env_file(cfg: DeployConfig, project_path: Path,
                     out_dir: Path, *, live: set[str] | None = None) -> Path:
    """Materialize the resolved secrets into the KEY=VALUE env-file that
    provision-instance.sh consumes (mode 0600). Thin wrapper over
    resolve_secret_values — see it for sourcing, the declared-set filter, and the
    live-aware required check."""
    values = resolve_secret_values(cfg, project_path, live=live)
    out = out_dir / "instance.env"
    write_env_file(out, values)
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    return out


def resolve_secret_values(cfg: DeployConfig, project_path: Path,
                          *, live: set[str] | None = None) -> dict[str, str]:
    """Resolve the secret KEY=VALUE map to apply to this instance.

    Sources: cfg.secrets_env_file (a local path), backfilled from the process
    environment (the CI seam — the Action materializes the team's secrets into the
    job env and runs `modastack deploy`). The result is FILTERED to the team's
    DECLARED set, so only secrets the team actually references reach Fly.

    `live` is the set of secret names already on the instance (fly_secrets_list).
    When given (the update/reconcile path), an already-live secret SATISFIES the
    required check — an update needn't re-supply what Fly already holds. None (the
    provision path) means nothing is live yet, so every required key must be here.

    Raises DeployError on a subscription/ANTHROPIC conflict, or (for a local team)
    a missing required secret. A team-url package isn't on disk, so its refs are
    invisible: no filter, and presence validation defers to the instance's boot.
    """
    if cfg.secrets_env_file:
        src = Path(cfg.secrets_env_file)
        if not src.is_absolute():
            # Relative to the deployments/ owner (the project), not cwd-of-script.
            src = (project_path / src).resolve()
        if not src.exists():
            raise DeployError(f"secrets env-file not found: {src}")
        values = parse_env_file(src)
    else:
        values = {}

    required, declared = _secret_sets(cfg, project_path)

    # Backfill declared keys (or, for a team-url, the auth overlay) from the
    # process env — the CI seam. Only known keys, never the whole environment.
    for var in (declared if declared is not None else set(required)):
        if var not in values and var in os.environ:
            values[var] = os.environ[var]

    # Subscription mode: ANTHROPIC_API_KEY silently outranks the OAuth creds and
    # bills the API instead — it must be entirely absent (§6.1).
    if cfg.auth == "subscription" and values.get("ANTHROPIC_API_KEY"):
        raise DeployError(
            f"deployment '{cfg.name}' is auth=subscription but ANTHROPIC_API_KEY "
            "is present in its secrets — remove it (it overrides subscription auth)."
        )

    # Filter to the declared set: only secrets the team references reach Fly. An
    # undeclared key (a CI dump's FLY_API_TOKEN, or a typo'd name) is dropped with
    # a warning rather than silently provisioned. team-url skips this (no refs
    # visible). MODASTACK_* identity is already excluded from `declared`.
    if declared is not None:
        for key in [k for k in values if k not in declared]:
            log.warning("dropping undeclared secret '%s' for '%s' (not referenced "
                        "in the team's agent.yaml — typo?)", key, cfg.name)
            values.pop(key)

    # Presence check. A var must be DECLARED present (may be intentionally empty —
    # optional scoping knobs like `channels: ${SLACK_CHANNELS}`). For a LOCAL team
    # we fail loud rather than boot broken; a team-url defers to its boot install.
    if declared is not None:
        missing = [v for v in required
                   if v not in values and (live is None or v not in live)]
        if missing:
            raise DeployError(
                f"deployment '{cfg.name}' is missing required secret(s): "
                f"{', '.join(missing)}. Provide them via the env-file "
                f"(secrets.env-file), the process environment, or as live Fly "
                f"secrets (fly secrets set … -a {cfg.app_name})."
            )
    return values


# --- Fly mechanics (thin shells; monkeypatchable in tests) ------------------

def _run(cmd: list[str], *, cwd: Path | None = None, check: bool = True,
         input_bytes: bytes | None = None,
         extra_env: dict[str, str] | None = None,
         secret: bool = False) -> subprocess.CompletedProcess:
    # `secret=True` redacts the logged command — used for `fly secrets set`, whose
    # argv carries KEY=VALUE secret values we must never write to logs.
    log.info("$ %s", f"{cmd[0]} … ({len(cmd) - 1} redacted args)" if secret
             else " ".join(cmd))
    env = {**os.environ, **extra_env} if extra_env else None
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, check=check, input=input_bytes,
        env=env,
    )


def _fly_bin() -> str:
    return "fly" if shutil.which("fly") else "flyctl"


# --- local image build (#387) ------------------------------------------------
# Fly's remote builder is unreliable from a macOS / Docker-Desktop laptop: with
# flyctl v0.4.59 it mis-parses the daemon host ("missing hostname") and the
# heartbeat dies, regardless of how many remote builders you recreate. The
# proven path there is a local buildkit build (--local-only → gzip layers, which
# Fly's machine init can extract; never Depot's zstd) with DOCKER_HOST pointed at
# Docker Desktop's real socket. The tell for "this is that kind of host" is the
# standard /var/run/docker.sock being ABSENT — it's present on Linux (including
# GitHub Actions runners, which also run `modastack deploy`, where the remote
# builder is correct and must stay the default).

def _default_docker_socket_present() -> bool:
    """True when the flyctl-default Docker socket /var/run/docker.sock exists."""
    return Path("/var/run/docker.sock").exists()


def _docker_context_host() -> str:
    """The active docker context's daemon endpoint (e.g.
    `unix://$HOME/.docker/run/docker.sock` for Docker Desktop), or '' if
    `docker` is unavailable. The portable way to find Docker Desktop's socket,
    which is NOT the flyctl default."""
    proc = subprocess.run(
        ["docker", "context", "inspect", "--format",
         "{{.Endpoints.docker.Host}}"],
        capture_output=True, text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _resolve_local_build() -> tuple[bool, str | None]:
    """Decide the image build mode for THIS host (#387).

    Returns ``(build_locally, docker_host)``:
    - ``(False, None)`` — the standard /var/run/docker.sock is present
      (Linux / CI): keep Fly's remote builder, the correct default.
    - ``(True, host)`` — no default socket (Docker Desktop): build locally and
      inject ``host`` (resolved from the active docker context) as DOCKER_HOST.
    - ``(True, None)`` — local build, but DOCKER_HOST is already set in the env
      (respected as-is, no overlay) or couldn't be resolved (caller warns)."""
    if _default_docker_socket_present():
        return (False, None)
    if os.environ.get("DOCKER_HOST"):
        return (True, None)  # operator already pointed Docker; subprocess inherits it
    host = _docker_context_host()
    if host.startswith("unix://") and Path(host[len("unix://"):]).exists():
        return (True, host)
    return (True, None)  # local host, socket unresolved — deploy() prints the fix


def fly_app_exists(app: str) -> bool:
    """True if the Fly app exists (and is yours) — the provision-vs-update fork."""
    return _run([_fly_bin(), "status", "-a", app], check=False).returncode == 0


def fly_secrets_list(app: str) -> set[str]:
    """The secret NAMES currently live on the app (Fly never exposes values).

    The source of truth for the reconcile: an already-live secret satisfies the
    required check, and a live name absent from the declared set is a prune
    candidate. Empty set when the app doesn't exist or has no secrets.
    """
    proc = subprocess.run(
        [_fly_bin(), "secrets", "list", "-a", app, "--json"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return set()
    try:
        return {row["Name"] for row in json.loads(proc.stdout or "[]")}
    except (ValueError, KeyError, TypeError):
        return set()


def fly_secrets_set(app: str, values: dict[str, str]) -> None:
    """Set/update secrets on an EXISTING app. Fly no-ops identical values (no
    needless restart) — only a real change triggers a release. Logging is redacted
    (the argv carries values)."""
    if not values:
        return
    _run([_fly_bin(), "secrets", "set", "-a", app,
          *(f"{k}={v}" for k, v in values.items())], secret=True)


def fly_secrets_unset(app: str, keys: list[str]) -> None:
    """Remove secrets from an app (the prune path). Keys are not sensitive."""
    if not keys:
        return
    _run([_fly_bin(), "secrets", "unset", "-a", app, *keys])


# --- Fly onboarding preflight (guide a newcomer — or an agent — to a deployable
#     Fly account before we spend minutes building an image) ------------------

def fly_preflight() -> list[str]:
    """Actionable problems blocking a Fly deploy (empty list = ready to deploy).

    Each entry is self-contained guidance with exact commands, so a human OR an
    agent can read it and get to a deployable Fly account. Checks, in order:
    flyctl installed → logged in. The high-risk-unlock (new personal orgs) can't
    be detected without attempting a create, so it's flagged as a heads-up on the
    login step rather than a hard gate."""
    fly = _fly_bin()
    if shutil.which(fly) is None:
        return [
            "flyctl (the Fly CLI) isn't installed — `modastack deploy` drives it.\n"
            "       Install it, then open a new shell so `fly` is on PATH:\n"
            "         macOS/Linux:  curl -L https://fly.io/install.sh | sh\n"
            "         Homebrew:     brew install flyctl\n"
            "         Windows:      pwsh -c \"iwr https://fly.io/install.ps1 -useb | iex\""
        ]
    whoami = subprocess.run([fly, "auth", "whoami"],
                            capture_output=True, text=True)
    if whoami.returncode != 0:
        return [
            "You're not signed in to Fly. Create an account or log in:\n"
            "         New to Fly:  fly auth signup   (sign up + add a card —\n"
            "                      Fly requires one to run machines; the canary\n"
            "                      tier is a few dollars a month)\n"
            "         Have one:    fly auth login\n"
            "       Verify with `fly auth whoami` (prints your email). A brand-new\n"
            "       personal org may be flagged high-risk — if a later deploy can't\n"
            "       create the app, unlock it once at https://fly.io/high-risk-unlock ."
        ]
    return []


def preflight_fly_or_exit() -> None:
    """Print Fly-onboarding guidance and exit(1) if the environment isn't ready;
    return quietly when it is. Called at the top of `modastack deploy`/`destroy`."""
    problems = fly_preflight()
    if not problems:
        return
    print("\nBefore deploying, finish setting up Fly:\n", file=sys.stderr)
    for i, p in enumerate(problems, 1):
        print(f"  {i}. {p}\n", file=sys.stderr)
    print("Then re-run the same `modastack` command.\n", file=sys.stderr)
    raise SystemExit(1)


def fly_instance_running(app: str) -> bool:
    """True if the app has a started machine. The provision-vs-update fork keys on
    this (not mere existence): an app that exists but has no started machine is
    HALF-PROVISIONED (a deploy that failed mid-build) and must re-provision, not
    take the ssh update path (which errors 'no started VMs'). Caught in e2e."""
    import json
    proc = subprocess.run([_fly_bin(), "machine", "list", "-a", app, "--json"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return False
    try:
        machines = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return False
    return any(m.get("state") == "started" for m in machines)


def _fly_machine_ids(app: str) -> list[str]:
    """The app's machine IDs. `fly machine restart` requires an explicit ID when
    not attached to a TTY (a bare `-a <app>` errors 'a machine ID must be
    specified' — caught in the live ssh-push e2e), so resolve them first."""
    import json
    proc = subprocess.run(
        [_fly_bin(), "machine", "list", "-a", app, "--json"],
        capture_output=True, text=True, check=True,
    )
    try:
        machines = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return [m["id"] for m in machines if m.get("id")]


def restart_app(app: str) -> None:
    """Restart every machine of an app by explicit ID (reload after a reinstall)."""
    ids = _fly_machine_ids(app)
    if not ids:
        raise DeployError(f"no machines found for '{app}' to restart.")
    for mid in ids:
        _run([_fly_bin(), "machine", "restart", mid, "-a", app])


# --- the team push (ssh-push delivery) --------------------------------------

def _build_team_tarball(pkg_dir: Path, out_dir: Path) -> Path:
    """Tar a local team package into `<out>/<team>.tar.gz`, extracting to a single
    `<team>/` dir holding agent.yaml — the shape `modastack install` expects."""
    name = pkg_dir.name
    out = out_dir / f"{name}.tar.gz"
    with tarfile.open(out, "w:gz") as t:
        t.add(pkg_dir, arcname=name)
    return out


def push_team(app: str, pkg_dir: Path, *, restart: bool) -> None:
    """Push a LOCAL team package onto a running instance over `fly ssh`.

    Builds the tarball, streams it onto the volume, and runs
    `modastack install <tarball> --non-interactive` on the instance (which reads
    secrets from the Fly-injected env). On a freshly-provisioned blank instance
    this lands .modastack/agent.yaml on the volume, releasing the entrypoint's
    wait-for-team loop; on an existing instance pass restart=True to reload.
    """
    fly = _fly_bin()
    remote = "/data/incoming-team.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        tarball = _build_team_tarball(pkg_dir, Path(tmp))
        data = tarball.read_bytes()

        # Stream the tarball onto the volume. base64 keeps the payload ASCII-safe
        # across the ssh transport; `gosu modastack` writes it as the volume owner.
        import base64
        b64 = base64.b64encode(data)
        _run(
            [fly, "ssh", "console", "-a", app, "-C",
             f"gosu modastack bash -c 'base64 -d > {remote}'"],
            input_bytes=b64,
        )
        # Install from the pushed tarball as the volume's owner, matching the
        # container's runtime env: HOME on the image (/home/modastack), Claude's
        # durable state on the volume (CLAUDE_CONFIG_DIR=/data/claude). The
        # project (.modastack/agent.yaml + .env) lands under cwd /data/project on
        # the volume. --non-interactive => read secrets from the Fly env and fail
        # loudly on a gap, never hang on a prompt.
        _run([
            fly, "ssh", "console", "-a", app, "-C",
            "gosu modastack env HOME=/home/modastack CLAUDE_CONFIG_DIR=/data/claude bash -c "
            f"'cd /data/project && modastack install {remote} --non-interactive'",
        ])
    if restart:
        restart_app(app)


def update_team_url(app: str, url: str) -> None:
    """In-place update for a team-url instance: re-pull the (refreshed) tarball
    with a workspace-safe reinstall, then restart to load the new config."""
    fly = _fly_bin()
    _run([
        fly, "ssh", "console", "-a", app, "-C",
        "gosu modastack env HOME=/home/modastack CLAUDE_CONFIG_DIR=/data/claude bash -c "
        f"'cd /data/project && modastack install \"{url}\"'",
    ])
    restart_app(app)


# --- the primitives ----------------------------------------------------------

def _provision_args(cfg: DeployConfig, env_file: Path) -> list[str]:
    """Common provision-instance.sh flags shared by both delivery modes."""
    args = [
        "--app", cfg.app_name,
        "--fleet", cfg.fleet_stamp,
        "--instance", cfg.name,
        "--env-file", str(env_file),
        "--auth", cfg.auth,
        "--event-server", cfg.event_server,
        "--region", cfg.region,
        "--memory", cfg.memory,
        "--cpus", str(cfg.cpus),
        "--volume-size", str(cfg.volume_size),
        "--volume-name", cfg.volume_name,
    ]
    if cfg.org:
        args += ["--org", cfg.org]
    if cfg.login_channel:
        args += ["--login-channel", cfg.login_channel]
    if cfg.claude_version:
        args += ["--claude-version", cfg.claude_version]
    return args


def _render_team_deps_into_context(project_path: Path, cfg: DeployConfig,
                                   assets: "DeployAssets") -> str | None:
    """Render a `build:`-declaring team's deps hook into the build context.

    Returns the TEAM_DEPS build-arg value (a path RELATIVE to the build context,
    where the Dockerfile's `COPY ${TEAM_DEPS}` resolves it), or None when the
    team has no declarative build (deploys on the generic image). Only ssh-push
    (`team:`) teams are visible locally; a `team-url:` package isn't, so its
    image must be prebuilt and passed via `image:`.
    """
    if not cfg.team:
        return None
    try:
        team_dir = resolve_team_dir(project_path, cfg.team)
    except DeployError:
        # A bare-name team with no local dir / no registry hit is legitimately
        # "not buildable here" → generic image. But a PIN that fails to resolve
        # must never be silently downgraded to a generic image — propagate it.
        if cfg.team_version:
            raise
        return None
    from modastack.build_render import (
        load_composed_team_config,
        render_team_deps_script,
    )
    # Read the COMPOSED build (from: chain + tool_library expansion), not the raw
    # leaf — a team may declare its baked CLI via `tool_library:` with no inline
    # build: (#416). Reading raw would skip the bake and leave the requires gate
    # failing on the box.
    tcfg = load_composed_team_config(team_dir, project_path)
    spec = tcfg.build
    if spec is None or not (spec.apt or spec.npm or spec.run_root or spec.run
                            or spec.verify_requires):
        return None  # no spec, or a pure raw-Dockerfile escape hatch
    ctx = Path(assets.build_context)
    rel = Path("dist") / "team-deps" / f"{team_dir.name}.sh"
    out = ctx / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_team_deps_script(tcfg))
    log.info("rendered team-deps hook for '%s' → %s", team_dir.name, rel)
    return str(rel)


def _local_team_deps_hash(project_path: Path, cfg: DeployConfig) -> str:
    """Deps identity of the team package on disk — the hash a fresh image bakes.

    Empty string => generic team (no `build:` deps that could drift). Mirrors the
    spec gate in `_render_team_deps_into_context` so the two never disagree.
    """
    if not cfg.team:
        return ""  # team-url: package isn't local
    try:
        team_dir = resolve_team_dir(project_path, cfg.team)
    except DeployError:
        if cfg.team_version:  # a pin must hard-fail, not silently hash-as-generic
            raise
        return ""
    from modastack.build_render import load_composed_team_config, team_deps_hash
    # Composed build (tool_library + from: chain) — must match the renderer above.
    spec = load_composed_team_config(team_dir, project_path).build
    if spec is None or not (spec.apt or spec.npm or spec.run_root or spec.run
                            or spec.verify_requires):
        return ""
    return team_deps_hash(spec)


def _running_team_deps_hash(app: str) -> str:
    """Deps identity baked into the RUNNING instance's image, read over `fly ssh`.

    Empty => no stamp: a generic image, or one built before the #379 guard.
    """
    from modastack.build_render import TEAM_DEPS_STAMP
    proc = subprocess.run(
        [_fly_bin(), "ssh", "console", "-a", app, "-C", f"cat {TEAM_DEPS_STAMP}"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _should_rebuild(project_path: Path, cfg: DeployConfig, app: str,
                    *, forced: bool) -> bool:
    """Decide whether an in-place ssh-push update must REBUILD the image (#379).

    A team's baked deps live in the IMAGE; the hot-push fast path re-sends the
    definition tarball + restarts but never rebuilds the image — so editing a
    team's `build:` (a new apt/npm tool, a bumped codex) on a live instance would
    silently never land. We detect the drift (the hash a fresh image would bake
    != the hash stamped in the running image) and rebuild in place instead, so a
    `deploy-agent-teams` reconcile self-heals a deps change with no manual step.

    `forced` (--rebuild) always rebuilds. A generic team (no `build:` deps) never
    rebuilds. When the running image carries no stamp (built before the #379
    stamp) we can't tell deps apart — warn and take the hot-push path; pass
    --rebuild to force it. The decision lives HERE (not in YAML diffing) so it's
    identical from a laptop and from CI.
    """
    if forced:
        return True
    local = _local_team_deps_hash(project_path, cfg)
    if not local:
        return False  # generic team — nothing baked to drift, no ssh probe
    running = _running_team_deps_hash(app)
    if not running:
        log.warning(
            "couldn't read a deps stamp from '%s' (image predates the #379 "
            "stamp?) — taking the hot-push path; pass --rebuild if you changed "
            "the team's build: deps.", app)
        return False
    if running != local:
        log.info(
            "team '%s' build: deps changed (running %s != rebuilt %s) — rebuilding "
            "the image in place instead of hot-pushing (#379).", cfg.team, running,
            local)
        return True
    return False  # deps unchanged — the hot-push fast path is correct


def reconcile_live_secrets(cfg: DeployConfig, project_path: Path, app: str,
                           values: dict[str, str], live: set[str],
                           *, prune: bool) -> tuple[list[str], list[str]]:
    """Reconcile an EXISTING app's Fly secrets to the team's declared set.

    A plain in-place update (push_team / update_team_url) never re-runs
    provision-instance.sh, so secrets are NOT touched on that path today — which is
    why a rotated/unset secret silently drifted (the eng-team outage). This closes
    it: set/update the supplied declared values directly (Fly no-ops identical
    ones), and PRUNE live, non-MODASTACK_ secrets that aren't in the declared set
    so the live store converges on what the team declares.

    Prune needs the declared set, so it only runs for a local team (team-url has no
    visible refs). Returns (set_keys, pruned_keys) for the caller to report.
    """
    _, declared = _secret_sets(cfg, project_path)
    if values:
        fly_secrets_set(app, values)
    pruned: list[str] = []
    if prune and declared is not None:
        pruned = sorted(k for k in live
                        if not k.startswith("MODASTACK_") and k not in declared)
        if pruned:
            log.info("pruning %d undeclared secret(s) on '%s': %s",
                     len(pruned), app, ", ".join(pruned))
            fly_secrets_unset(app, pruned)
    return (sorted(values), pruned)


def deploy(project_path: Path, name: str, overrides: dict | None = None) -> DeployConfig:
    """Provision OR update ONE instance, idempotently.

    Resolves config + secrets, computes identity, then forks on Fly state: no app
    yet → provision (blank+ssh-push, or --team-url); app exists → in-place update.
    Returns the resolved config (for the caller to report).
    """
    cfg = load_deploy_config(project_path, name, overrides)
    app = cfg.app_name
    # Reconcile secrets against what's LIVE on the app, not against a re-supplied
    # env-file: an existing live secret satisfies the required check, so an update
    # needn't re-declare everything (the drift the #385 outage exposed).
    app_exists = fly_app_exists(app)
    live = fly_secrets_list(app) if app_exists else None
    prune = not bool((overrides or {}).get("no_prune"))

    with tempfile.TemporaryDirectory() as tmp:
        # In --image mode nothing is built, so the binary build context isn't
        # assembled (staging=None); we still need provision_sh from the assets.
        assets = resolve_assets(project_path, None if cfg.image else Path(tmp))
        values = resolve_secret_values(cfg, project_path, live=live)
        # On an existing app, apply secret deltas + prune undeclared directly (the
        # plain-update path never re-runs the provisioner, so secrets land here).
        if app_exists:
            reconcile_live_secrets(cfg, project_path, app, values, live or set(),
                                   prune=prune)
        env_file = Path(tmp) / "instance.env"
        write_env_file(env_file, values)
        try:
            os.chmod(env_file, 0o600)
        except OSError:
            pass
        # Provision when there's no running instance — covers a brand-new app AND a
        # half-provisioned one (app/volume exist but the image build failed, so no
        # started machine). Only ssh-update an instance that's actually up.
        deployed = app_exists and fly_instance_running(app)

        # Provision flags shared by both delivery modes. Either deploy a prebuilt
        # team image by ref (C24), or pass the build context (source repo, or the
        # binary-mode PyPI context) + image build args to build one.
        base = ["bash", str(assets.provision_sh), *_provision_args(cfg, env_file)]
        build_env: dict[str, str] = {}
        if cfg.image:
            base += ["--image", cfg.image]
        else:
            base += ["--build-context", str(assets.build_context),
                     "--dockerfile", str(assets.dockerfile)]
            for k, v in assets.build_args.items():
                base += ["--build-arg", f"{k}={v}"]
            # C24: if this team bakes host tools (a `build:` spec), render its
            # team-deps hook into the build context and pass TEAM_DEPS, so the
            # team-flavored image is built on Fly's remote builder during deploy
            # (no separate registry push — Fly creates app+registry+machine
            # together). A prebuilt `image:` ref short-circuits this above.
            team_deps = _render_team_deps_into_context(project_path, cfg, assets)
            if team_deps:
                base += ["--build-arg", f"TEAM_DEPS={team_deps}"]
            # #387: a macOS/Docker-Desktop laptop can't use Fly's remote builder
            # (flyctl mis-parses the daemon host); build locally with gzip layers
            # and point DOCKER_HOST at the real socket. No-op on Linux/CI.
            build_locally, docker_host = _resolve_local_build()
            if build_locally:
                base += ["--local-build"]
                if docker_host:
                    build_env["DOCKER_HOST"] = docker_host
                    log.info("building locally (--local-only); DOCKER_HOST=%s (#387)",
                             docker_host)
                elif not os.environ.get("DOCKER_HOST"):
                    log.warning(
                        "no /var/run/docker.sock and couldn't resolve a Docker "
                        "socket from `docker context inspect` — the local build "
                        "may fail. If so, set it manually, e.g.:\n"
                        "    export DOCKER_HOST=unix://$HOME/.docker/run/docker.sock")

        if cfg.delivery == "ssh-push":
            pkg = resolve_team_dir(project_path, cfg.team)
            if not deployed:
                log.info("provisioning blank instance '%s' (ssh-push, %s mode)...",
                         app, assets.mode)
                _run([*base, "--blank", "--yes"], cwd=assets.run_cwd,
                     extra_env=build_env)
                # Entrypoint is waiting; the push releases it (no restart needed).
                push_team(app, pkg, restart=False)
            elif _should_rebuild(project_path, cfg, app,
                                 forced=bool((overrides or {}).get("rebuild"))):
                # Deps changed (or --rebuild): rebuild the image on the existing
                # app — provision-instance.sh is idempotent (skips create, just
                # re-deploys) and never touches the volume's project files — then
                # refresh the definition + reload so the new tools actually land.
                log.info("rebuilding instance '%s' image in place (ssh-push)...", app)
                _run([*base, "--blank", "--yes"], cwd=assets.run_cwd,
                     extra_env=build_env)
                push_team(app, pkg, restart=True)
            else:
                log.info("updating instance '%s' in place (ssh-push)...", app)
                push_team(app, pkg, restart=True)
        else:  # team-url
            if not deployed:
                log.info("provisioning instance '%s' (team-url, %s mode)...",
                         app, assets.mode)
                _run([*base, "--team-url", cfg.team_url, "--yes"],
                     cwd=assets.run_cwd, extra_env=build_env)
            else:
                log.info("updating instance '%s' in place (team-url)...", app)
                update_team_url(app, cfg.team_url)

    return cfg


def destroy(project_path: Path, name: str, overrides: dict | None = None,
            *, assume_yes: bool = False) -> str:
    """Tear down ONE instance — resolve <name> → <fleet>-<name>, run
    destroy-instance.sh (Fly app + volume). Returns the app name destroyed.

    The volume is the only copy of the instance's state, so destroy-instance.sh
    keeps its typed-confirmation; --yes is for automation (a human-gated
    orchestration teardown still calls through here, never silently)."""
    assets = resolve_assets(project_path)  # no build needed → no staging

    # App name resolution mirrors deploy: <fleet>-<name>, or bare <name>. We use
    # the config when a file exists (to honor an explicit fleet); else derive.
    try:
        cfg = load_deploy_config(project_path, name, overrides)
        app = cfg.app_name
    except DeployError:
        fleet = (overrides or {}).get("fleet", "")
        app = f"{fleet}-{name}" if fleet else name

    args = ["bash", str(assets.destroy_sh), "--app", app]
    if assume_yes:
        args.append("--yes")
    _run(args, cwd=assets.run_cwd)
    return app
