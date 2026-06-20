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
    "team", "team_url", "fleet", "region", "memory", "cpus", "volume_size",
    "auth", "event_server", "login_channel", "claude_version", "org",
    "volume_name", "image",
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

def resolve_env_file(cfg: DeployConfig, project_path: Path,
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
            src = (project_path / src).resolve()
        if not src.exists():
            raise DeployError(f"secrets env-file not found: {src}")
        values = parse_env_file(src)
    else:
        values = {}

    required: list[str] = []
    if cfg.team:
        pkg = local_package_dir(project_path, cfg.team)
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
        # A var must be DECLARED (in the env-file or process env), but may be
        # intentionally empty — some referenced vars are optional scoping knobs
        # (e.g. `channels: ${SLACK_CHANNELS}`, empty = whole workspace) that must
        # not block a deploy. Auth-critical keys are still enforced non-empty at
        # provision (provision-instance.sh) and boot (docker-entrypoint.sh §6.1).
        missing = [v for v in required if v not in values]
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
        team_dir = local_package_dir(project_path, cfg.team)
    except DeployError:
        return None
    from modastack.build_render import load_team_config, render_team_deps_script
    tcfg = load_team_config(team_dir)
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


def deploy(project_path: Path, name: str, overrides: dict | None = None) -> DeployConfig:
    """Provision OR update ONE instance, idempotently.

    Resolves config + secrets, computes identity, then forks on Fly state: no app
    yet → provision (blank+ssh-push, or --team-url); app exists → in-place update.
    Returns the resolved config (for the caller to report).
    """
    cfg = load_deploy_config(project_path, name, overrides)
    app = cfg.app_name

    with tempfile.TemporaryDirectory() as tmp:
        # In --image mode nothing is built, so the binary build context isn't
        # assembled (staging=None); we still need provision_sh from the assets.
        assets = resolve_assets(project_path, None if cfg.image else Path(tmp))
        env_file = resolve_env_file(cfg, project_path, Path(tmp))
        # Provision when there's no running instance — covers a brand-new app AND a
        # half-provisioned one (app/volume exist but the image build failed, so no
        # started machine). Only ssh-update an instance that's actually up.
        deployed = fly_app_exists(app) and fly_instance_running(app)

        # Provision flags shared by both delivery modes. Either deploy a prebuilt
        # team image by ref (C24), or pass the build context (source repo, or the
        # binary-mode PyPI context) + image build args to build one.
        base = ["bash", str(assets.provision_sh), *_provision_args(cfg, env_file)]
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

        if cfg.delivery == "ssh-push":
            pkg = local_package_dir(project_path, cfg.team)
            if not deployed:
                log.info("provisioning blank instance '%s' (ssh-push, %s mode)...",
                         app, assets.mode)
                _run([*base, "--blank", "--yes"], cwd=assets.run_cwd)
                # Entrypoint is waiting; the push releases it (no restart needed).
                push_team(app, pkg, restart=False)
            else:
                log.info("updating instance '%s' in place (ssh-push)...", app)
                push_team(app, pkg, restart=True)
        else:  # team-url
            if not deployed:
                log.info("provisioning instance '%s' (team-url, %s mode)...",
                         app, assets.mode)
                _run([*base, "--team-url", cfg.team_url, "--yes"], cwd=assets.run_cwd)
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
