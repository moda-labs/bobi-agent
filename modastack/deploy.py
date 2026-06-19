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

import logging
import os
import shutil
import subprocess
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
    "team", "team_url", "fleet", "region", "memory", "cpus", "volume_size",
    "auth", "event_server", "login_channel", "claude_version", "org",
    "volume_name",
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
    # Secret source: a local env-file path (self-service) and/or a named source
    # (a GitHub Environment, etc. — a hint the orchestration layer materializes).
    secrets_env: str = ""
    secrets_env_file: str = ""

    @property
    def delivery(self) -> str:
        """'ssh-push' for a local `team:`, 'team-url' for a published URL."""
        return "ssh-push" if self.team else "team-url"

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
    raise DeployError(
        "could not find the modastack source root (a dir with "
        "scripts/provision-instance.sh and Dockerfile). `modastack deploy` builds "
        "the instance image, so run it from a modastack checkout."
    )


def local_package_dir(repo_root: Path, team: str) -> Path:
    """Path to a local team package `agents/<team>` (the ssh-push source)."""
    pkg = repo_root / "agents" / team
    if not (pkg / "agent.yaml").exists():
        raise DeployError(
            f"local team '{team}' not found at {pkg}/agent.yaml. For ssh-push "
            "delivery the team must live under agents/ in this repo."
        )
    return pkg


def scan_required_vars(agent_yaml: Path) -> list[str]:
    """Return the bare ${VAR} secret names a package's agent.yaml requires.

    Like config.find_required_env_vars but for an arbitrary (not-yet-installed)
    package file. A bare ${VAR} is required; ${VAR:-default} carries its own
    fallback (a ':' in the captured name) and is optional, so it's excluded.
    """
    if not agent_yaml.exists():
        return []
    return [v for v in _ENV_VAR_RE.findall(agent_yaml.read_text()) if ":" not in v]


# --- secret resolution -------------------------------------------------------

def resolve_env_file(cfg: DeployConfig, repo_root: Path,
                     out_dir: Path) -> Path:
    """Produce the KEY=VALUE env-file provision-instance.sh consumes.

    Sources, in order:
      1. cfg.secrets_env_file — a local path (self-service / `env-file:`).
      2. otherwise materialize from the process environment — the package's
         required vars (+ ANTHROPIC_API_KEY in api_key mode) read from os.environ.
         This is the CI seam: the Action exports the team's secrets into the job
         env (from its GitHub Environment) and runs `modastack deploy`.

    For a LOCAL team we know the required vars and fail loudly on a gap, rather
    than booting a broken instance. For a team-url we can't see the package, so
    validation defers to the instance's `install --non-interactive` at boot.
    """
    if cfg.secrets_env_file:
        src = Path(cfg.secrets_env_file)
        if not src.is_absolute():
            # Relative to the deployments/ owner (the project), not cwd-of-script.
            src = (repo_root / src).resolve()
        if not src.exists():
            raise DeployError(f"secrets env-file not found: {src}")
        values = parse_env_file(src)
    else:
        values = {}

    required: list[str] = []
    if cfg.team:
        pkg = local_package_dir(repo_root, cfg.team)
        # MODASTACK_* references are instance identity the provisioner stamps into
        # [env] from flags (event server, fleet, …) — they are NEVER secrets, so
        # don't demand them in the env-file.
        required = [v for v in scan_required_vars(pkg / "agent.yaml")
                    if not v.startswith("MODASTACK_")]
        if cfg.auth == "api_key":
            required = [*required, "ANTHROPIC_API_KEY"]

    # Backfill anything still missing from the process environment.
    for var in required:
        if var not in values and var in os.environ:
            values[var] = os.environ[var]

    # Subscription mode: ANTHROPIC_API_KEY silently outranks the OAuth creds and
    # bills the API instead — it must be entirely absent (§6.1).
    if cfg.auth == "subscription" and values.get("ANTHROPIC_API_KEY"):
        raise DeployError(
            f"deployment '{cfg.name}' is auth=subscription but ANTHROPIC_API_KEY "
            "is present in its secrets — remove it (it overrides subscription auth)."
        )

    if cfg.team:
        missing = [v for v in required if not values.get(v)]
        if missing:
            raise DeployError(
                f"deployment '{cfg.name}' is missing required secret(s): "
                f"{', '.join(missing)}. Provide them via the env-file "
                "(secrets.env-file) or the process environment."
            )

    out = out_dir / "instance.env"
    write_env_file(out, values)
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    return out


# --- Fly mechanics (thin shells; monkeypatchable in tests) ------------------

def _run(cmd: list[str], *, cwd: Path | None = None, check: bool = True,
         input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    log.info("$ %s", " ".join(cmd))
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, check=check, input=input_bytes,
    )


def _fly_bin() -> str:
    return "fly" if shutil.which("fly") else "flyctl"


def fly_app_exists(app: str) -> bool:
    """True if the Fly app exists (and is yours) — the provision-vs-update fork."""
    return _run([_fly_bin(), "status", "-a", app], check=False).returncode == 0


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
        # Install from the pushed tarball as the volume's owner with HOME on the
        # volume (so ~/.claude + .env land there). --non-interactive => read
        # secrets from the Fly env and fail loudly on a gap, never hang on a prompt.
        _run([
            fly, "ssh", "console", "-a", app, "-C",
            "gosu modastack env HOME=/data/home bash -c "
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
        "gosu modastack env HOME=/data/home bash -c "
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


def deploy(project_path: Path, name: str, overrides: dict | None = None) -> DeployConfig:
    """Provision OR update ONE instance, idempotently.

    Resolves config + secrets, computes identity, then forks on Fly state: no app
    yet → provision (blank+ssh-push, or --team-url); app exists → in-place update.
    Returns the resolved config (for the caller to report).
    """
    cfg = load_deploy_config(project_path, name, overrides)
    repo = find_repo_root(project_path)
    provision_sh = repo / "scripts" / "provision-instance.sh"
    app = cfg.app_name

    with tempfile.TemporaryDirectory() as tmp:
        env_file = resolve_env_file(cfg, repo, Path(tmp))
        exists = fly_app_exists(app)

        if cfg.delivery == "ssh-push":
            pkg = local_package_dir(repo, cfg.team)
            if not exists:
                log.info("provisioning blank instance '%s' (ssh-push)...", app)
                _run(
                    ["bash", str(provision_sh), *_provision_args(cfg, env_file),
                     "--blank", "--yes"],
                    cwd=repo,
                )
                # Entrypoint is waiting; the push releases it (no restart needed).
                push_team(app, pkg, restart=False)
            else:
                log.info("updating instance '%s' in place (ssh-push)...", app)
                push_team(app, pkg, restart=True)
        else:  # team-url
            if not exists:
                log.info("provisioning instance '%s' (team-url)...", app)
                _run(
                    ["bash", str(provision_sh), *_provision_args(cfg, env_file),
                     "--team-url", cfg.team_url, "--yes"],
                    cwd=repo,
                )
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
    repo = find_repo_root(project_path)
    destroy_sh = repo / "scripts" / "destroy-instance.sh"

    # App name resolution mirrors deploy: <fleet>-<name>, or bare <name>. We use
    # the config when a file exists (to honor an explicit fleet); else derive.
    try:
        cfg = load_deploy_config(project_path, name, overrides)
        app = cfg.app_name
    except DeployError:
        fleet = (overrides or {}).get("fleet", "")
        app = f"{fleet}-{name}" if fleet else name

    args = ["bash", str(destroy_sh), "--app", app]
    if assume_yes:
        args.append("--yes")
    _run(args, cwd=repo)
    return app
