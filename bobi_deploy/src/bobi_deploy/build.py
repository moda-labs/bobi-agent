"""`bobi build` - render an agent team into a ready-to-run image (#610).

One command from team declaration to tagged, pushable Docker image:

    bobi build <team> --tag ghcr.io/acme/my-team:1 --push

The image engine lives in this package (#707): everything that assembles a
docker build context, runs `docker build`, or ships the deploy assets is
private-side, behind the `bobi.commands` plugin seam. The public package keeps
the dual-use seams this engine composes: `bobi.build.resolve_team_dir` (path /
`name@version` registry pin / `from:` flattening), `bobi.build_render` (the
team-deps script renderer) and `bobi.dep_bootstrap.render_team_deps` (the ONE
deps-render seam) - both of which must remain importable from the public wheel
alone, because the guide-dep bootstrap runs `python -m bobi.dep_bootstrap`
INSIDE the built container, whose installed bobi is the public PyPI package.

Guide-only dependencies are materialized by the bootstrap agent inside a fresh
base image (OQ6: the resolved recipe is faithful to the image, not the host).
The team dir handed to that container is always chain-free: `resolve_team_dir`
flattens any `from:` chain on the host, and the tool-library catalog is bobi
package data, so the container needs only the team dir and an output dir.

Registry-agnostic: `--push` is a plain `docker push` through the local docker
credential helpers (GHCR/GAR/ECR/...). Fly's app-scoped registry dance stays in
`scripts/build-team-images.sh`, the thin CI wrapper.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from bobi.build import BuildError, resolve_team_dir

log = logging.getLogger(__name__)

# Repo name of the throwaway base image guide-dep bootstraps run in. The tag
# is derived from the build inputs (see _bootstrap_base_tag), so concurrent
# builds with different modes/versions never race on one mutable tag; never
# pushed.
BOOTSTRAP_BASE_REPO = "bobi-bootstrap-base"
# In-container mount points for the bootstrap run. The team dir is read-only;
# the rendered team-deps.sh comes back through the out mount.
_TEAM_MOUNT = "/bobi-team"
_OUT_MOUNT = "/bobi-out"


class GuideDepsError(BuildError):
    """The team declares guide-only deps that need the bootstrap agent.

    Raised by `stage_team_deps(allow_agent=False)` - the deploy path, which
    never runs an agent - so the caller can say what to do instead.
    """

    def __init__(self, team: str, deps: list[str]):
        self.team = team
        self.deps = deps
        super().__init__(
            f"team '{team}' declares guide-only dependencies "
            f"({', '.join(deps)}) that a bootstrap agent must resolve at "
            f"build time")


@dataclass
class BuildResult:
    tags: list[str]
    team_dir: Path
    mode: str  # BOBI_BUILD mode the image was built with
    team_deps: str | None  # TEAM_DEPS build-arg used, None = generic image


# --- repo + build-asset resolution -------------------------------------------
# Shared by `bobi build` and the deploy engine: both build the same instance
# image from the same assets, so the resolution lives here, next to the
# bundled assets this package ships (`bobi_deploy/_deploy`).

def find_repo_root(start: Path | None = None) -> Path:
    """Locate the bobi source root (the dir holding scripts/ + Dockerfile).

    The instance image builds from the Dockerfile (the image is generic;
    identity lives in the volume + env), so a source-mode build must run from a
    bobi checkout — exactly as the GitHub Action does. Walk up from `start`
    until both scripts/provision-instance.sh and Dockerfile are found.
    """
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        if (d / "scripts" / "provision-instance.sh").exists() and (d / "Dockerfile").exists():
            return d
    raise BuildError("not a bobi checkout")


@dataclass
class BuildAssets:
    """Where a build finds its mechanics + how the instance image is built.

    Two modes, so the binary builds with OR without a repo:
      * **source** — a bobi checkout: build the image from local source
        (Dockerfile, `COPY . .`). Used in dev and the bobi repo's own CI.
      * **binary** — no checkout: the scripts + a PyPI-install Dockerfile ship
        in this package (`bobi_deploy/_deploy`), so installing bobi + the
        bobi-deploy plugin is enough to build or deploy. The image installs
        `bobi==<this version>` from PyPI.
    """

    mode: str
    provision_sh: Path
    destroy_sh: Path
    build_context: Path | None
    dockerfile: Path | None
    build_args: dict
    run_cwd: Path | None


def _packaged_deploy_dir() -> Path | None:
    """The bundled deploy assets (`bobi_deploy/_deploy`) in an installed wheel,
    or None in an editable/source checkout (where source mode is used instead)."""
    try:
        import importlib.resources as ir
        root = ir.files("bobi_deploy") / "_deploy"
        if root.is_dir():
            return Path(str(root))
    except (ModuleNotFoundError, FileNotFoundError, AttributeError, TypeError):
        pass
    return None


def installed_bobi_version() -> str:
    """The installed bobi version — pinned into the PyPI instance image so
    the built instance runs the same code as the binary that built it."""
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("bobi")
    except PackageNotFoundError as e:
        raise BuildError(
            "cannot determine the installed bobi version to build the "
            "instance image. Install bobi from PyPI (`uv tool install "
            "bobi`)."
        ) from e


def resolve_assets(project_path: Path, staging: Path | None = None) -> BuildAssets:
    """Resolve build mechanics, preferring a source checkout, else the bundled
    wheel assets. When `staging` is given (a build that produces an image), the
    binary-mode build context is assembled there (Dockerfile.pypi + docker/)."""
    try:
        repo = find_repo_root(project_path)
    except BuildError:
        repo = None
    if repo is not None:
        return BuildAssets(
            mode="source",
            provision_sh=repo / "scripts" / "provision-instance.sh",
            destroy_sh=repo / "scripts" / "destroy-instance.sh",
            build_context=repo,
            dockerfile=repo / "Dockerfile",
            build_args={},
            run_cwd=repo,
        )

    pkg = _packaged_deploy_dir()
    if pkg is None:
        raise BuildError(
            "no build assets found — not in a bobi checkout, and the "
            "installed bobi-deploy package has no bundled deploy assets. "
            "Reinstall the bobi-deploy package."
        )

    ctx = dockerfile = None
    if staging is not None:
        ctx = staging / "build-context"
        ctx.mkdir(parents=True, exist_ok=True)
        shutil.copy(pkg / "Dockerfile", ctx / "Dockerfile")
        shutil.copytree(pkg / "docker", ctx / "docker", dirs_exist_ok=True)
        dockerfile = ctx / "Dockerfile"
    return BuildAssets(
        mode="binary",
        provision_sh=pkg / "scripts" / "provision-instance.sh",
        destroy_sh=pkg / "scripts" / "destroy-instance.sh",
        build_context=ctx,
        dockerfile=dockerfile,
        # `pypi` builder + the version to install (the source builder is the
        # Dockerfile's default, used in a checkout).
        build_args={"BOBI_BUILD": "pypi",
                    "BOBI_VERSION": installed_bobi_version()},
        run_cwd=None,
    )


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Thin subprocess shell - module-level so tests monkeypatch it."""
    log.info("$ %s", " ".join(cmd))
    return subprocess.run(cmd, check=True)


def _docker_build(ctx: Path, dockerfile: Path, build_args: dict[str, str],
                  tags: list[str], team_deps: str | None = None) -> None:
    """The ONE `docker build` assembly - shared by the bootstrap base and the
    final image so the two can never drift (OQ6: the recipe must be resolved
    against the same base the shipped image is built from)."""
    cmd = ["docker", "build"]
    for k, v in build_args.items():
        cmd += ["--build-arg", f"{k}={v}"]
    if team_deps:
        cmd += ["--build-arg", f"TEAM_DEPS={team_deps}"]
    for t in tags:
        cmd += ["-t", t]
    cmd += ["-f", str(dockerfile), str(ctx)]
    _run(cmd)


def stage_team_deps(team_dir: Path, project_path: Path, *,
                    ctx: Path | None, dockerfile: Path | None = None,
                    build_args: dict[str, str] | None = None,
                    allow_agent: bool = False,
                    brains: list[str] | None = None) -> str | None:
    """Render a team's deps hook into the build context; return the TEAM_DEPS arg.

    The shared staging seam for `bobi build` (allow_agent=True: guide-only deps
    bootstrap inside a fresh base image) and `bobi deploy` (allow_agent=False:
    deploy never runs an agent, so a guide-dep team raises `GuideDepsError`).

    Returns None for a generic team (nothing to bake - it builds/deploys on
    the plain image with the noop deps hook). The guide-dep gate and the
    generic-team return run before `ctx` is needed; a team that DOES bake
    raises a clean BuildError when ctx is None rather than staging nowhere.
    """
    from bobi.build_render import load_composed_team_config
    from bobi.dep_bootstrap import _agent_needed, render_team_deps
    from bobi.tool_library import resolve_team_dependencies

    team_dir = Path(team_dir)
    # Compose + resolve ONCE; render_team_deps reuses both (it would otherwise
    # recompose the chain and re-resolve the set - repeated registry fetches
    # for chained teams).
    cfg = load_composed_team_config(team_dir, project_path)
    deps = resolve_team_dependencies(team_dir, project_path)
    guide_deps = [d for d in deps if _agent_needed(d)]
    if guide_deps and not allow_agent:
        raise GuideDepsError(team_dir.name, [d.name for d in guide_deps])

    if guide_deps:
        if ctx is None or dockerfile is None:
            raise BuildError(
                "guide-only dependency bootstrap needs a docker build context")
        script = _bootstrap_in_container(
            team_dir, ctx=ctx, dockerfile=dockerfile,
            build_args=build_args or {}, brains=brains)
    else:
        # No agent needed: the ONE renderer, host-side (byte-identical to the
        # pre-#610 deploy inline render - extra_recipes is empty).
        script = render_team_deps(team_dir, project_path, cfg=cfg, deps=deps)
        if script is None:
            # The escape hatch is a raw Dockerfile SIBLING of agent.yaml
            # (config.py); compose drops it, so check the team dir itself.
            if allow_agent and (team_dir / "Dockerfile").exists():
                # A raw-Dockerfile escape-hatch team bakes via its OWN
                # Dockerfile; silently tagging the generic image under the
                # team's name would ship an image missing its toolchain.
                # (deploy keeps returning None here - its generic-image path
                # is the long-standing contract for that surface.)
                raise BuildError(
                    f"team '{team_dir.name}' uses a raw Dockerfile escape "
                    f"hatch; build that Dockerfile directly instead of "
                    f"`bobi build`")
            return None
        if ctx is None:
            raise BuildError(
                f"team '{team_dir.name}' bakes dependencies but no build "
                f"context was provided to stage them into")

    rel = Path("dist") / "team-deps" / f"{team_dir.name}.sh"
    out = Path(ctx) / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(script)
    out.chmod(0o755)
    log.info("rendered team-deps hook for '%s' -> %s", team_dir.name, rel)
    return str(rel)


def _bootstrap_base_tag(build_args: dict[str, str], dockerfile: Path) -> str:
    """Base-image tag derived from the build inputs.

    Concurrent builds with different modes/versions/checkouts get different
    tags, so one run can never `docker run` a base another run just retagged
    (the OQ6 fidelity guarantee). Identical inputs share a tag - a benign
    retag to the same content, and the docker layer cache makes the rebuild
    cheap while keeping a source-mode base honest about checkout changes.
    """
    ident = hashlib.sha256(json.dumps(
        {"args": build_args, "dockerfile": str(dockerfile)},
        sort_keys=True).encode()).hexdigest()[:12]
    return f"{BOOTSTRAP_BASE_REPO}:{ident}"


def _bootstrap_in_container(team_dir: Path, *, ctx: Path,
                            dockerfile: Path,
                            build_args: dict[str, str],
                            brains: list[str] | None) -> str:
    """Run the guide-dep bootstrap inside a fresh base image, return the script.

    Builds the base (same Dockerfile/build-args, no TEAM_DEPS -> noop hook),
    then runs `python -m bobi.dep_bootstrap --render` inside it. Only the
    flattened team dir (ro) and an output dir are mounted: the from: chain was
    flattened on the host and the tool-library catalog ships as package data,
    so the container's own installed bobi resolves everything.
    """
    brains = brains or ["claude"]
    base_tag = _bootstrap_base_tag(build_args, dockerfile)
    _docker_build(Path(ctx), dockerfile, build_args, [base_tag])

    with tempfile.TemporaryDirectory(prefix="bobi-deps-out-") as tmp:
        out_dir = Path(tmp)
        # IS_SANDBOX=1 lets the brain CLI run bypassPermissions as root in this
        # throwaway container. cwd=/tmp so the IMAGE's installed bobi runs,
        # never anything mounted in.
        cmd = ["docker", "run", "--rm", "-e", "IS_SANDBOX=1"]
        for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            if os.environ.get(key):
                cmd += ["-e", key]
        cmd += [
            "-v", f"{team_dir}:{_TEAM_MOUNT}:ro",
            "-v", f"{out_dir}:{_OUT_MOUNT}",
            "-w", "/tmp",
            "--entrypoint", "python",
            base_tag,
            "-m", "bobi.dep_bootstrap", _TEAM_MOUNT,
            "--render", f"{_OUT_MOUNT}/team-deps.sh",
            "--brains", ",".join(brains),
        ]
        _run(cmd)
        rendered = out_dir / "team-deps.sh"
        if not rendered.exists():
            raise BuildError(
                f"in-image dependency bootstrap for '{team_dir.name}' "
                f"produced no team-deps.sh - see the bootstrap output above")
        return rendered.read_text()


def _pypi_version(bobi_version: str | None) -> str:
    """The version a pypi-mode image installs: an explicit pin, else the
    version of the bobi running this command (the image runs the same code as
    the CLI that built it). Warns on a version PyPI can't serve."""
    version = bobi_version or installed_bobi_version()
    if not re.fullmatch(r"\d+(\.\d+)*", version):
        log.warning(
            "pypi build mode pins bobi==%s, which does not look like a "
            "published release - the docker build will fail if PyPI has no "
            "such version (pass --bobi-version to pin an explicit one)",
            version)
    return version


def _resolve_build_mode(assets, build_mode: str | None,
                        bobi_version: str | None = None) -> tuple[str, dict]:
    """Pick the BOBI_BUILD mode + build-args for the given assets.

    `--build` overrides the mode but never the context: a checkout can build
    source (default), pypi, or wheel (prebuilt dist/*.whl); a wheel install
    always builds pypi, pinned to `bobi_version` or its own version.
    """
    if assets.mode == "source":
        mode = build_mode or "source"
        if mode == "source":
            return mode, {}
        if mode == "pypi":
            return mode, {"BOBI_BUILD": "pypi",
                          "BOBI_VERSION": _pypi_version(bobi_version)}
        wheels = list((Path(assets.build_context) / "dist").glob("*.whl"))
        if len(wheels) != 1:
            raise BuildError(
                f"--build wheel needs exactly one prebuilt wheel in dist/ "
                f"(found {len(wheels)} under "
                f"{Path(assets.build_context) / 'dist'})")
        return mode, {"BOBI_BUILD": "wheel"}

    # binary mode: the staged context (bundled Dockerfile + docker/) can only
    # install from PyPI - there is no source tree and no dist/ to copy.
    if build_mode in ("source", "wheel"):
        raise BuildError(
            f"--build {build_mode} requires a bobi checkout; this "
            f"installation only has the bundled build assets (pypi mode)")
    build_args = dict(assets.build_args)
    build_args["BOBI_VERSION"] = _pypi_version(
        bobi_version or build_args.get("BOBI_VERSION"))
    return "pypi", build_args


def build_team_image(team: str, *, tags: list[str] | None = None,
                     push: bool = False, build_mode: str | None = None,
                     project_path: Path | None = None,
                     brains: list[str] | None = None,
                     bobi_version: str | None = None) -> BuildResult:
    """Build (and optionally push) a ready-to-run image for one agent team.

    `team` is a path to a team dir, a registry `name[@version]` ref, or a bare
    name resolvable under `<project>/agents/` - exactly deploy's resolution.
    """
    if not shutil.which("docker"):
        raise BuildError("docker not found - `bobi build` needs a local "
                         "docker daemon to build the image")
    project_path = Path(project_path or Path.cwd())
    team_dir = resolve_team_dir(project_path, str(team))

    tags = list(tags or []) or [f"bobi-{team_dir.name}:latest"]

    with tempfile.TemporaryDirectory(prefix="bobi-build-") as tmp:
        assets = resolve_assets(project_path, Path(tmp))
        mode, build_args = _resolve_build_mode(assets, build_mode,
                                               bobi_version)
        ctx = Path(assets.build_context)

        team_deps = stage_team_deps(
            team_dir, project_path, ctx=ctx, dockerfile=assets.dockerfile,
            build_args=build_args, allow_agent=True, brains=brains)
        if team_deps is None:
            log.info("team '%s' bakes nothing - building the generic image",
                     team_dir.name)

        _docker_build(ctx, Path(assets.dockerfile), build_args, tags,
                      team_deps=team_deps)

    if push:
        for t in tags:
            if "/" not in t:
                log.warning("pushing unqualified tag '%s' - this targets "
                            "docker.io/library, which is probably not what "
                            "you want", t)
            _run(["docker", "push", t])

    return BuildResult(tags=tags, team_dir=team_dir, mode=mode,
                       team_deps=team_deps)
