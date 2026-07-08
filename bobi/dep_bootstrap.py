"""Bootstrap-agent harness for the unified dependency model (#428 Stage 2).

Stage 1 (`bobi/tool_library.py`) gave every dependency a `name`, a required
`success` contract, and the optional fields (`install`/`guide`/`host`/`mcp`).
This module is the **cold path**: on a fresh base image that already has the
brain CLI installed, make each dependency's `success` true and verify it, so
Stage 3 can freeze the result into a snapshot.

Two things happen per dependency:

- **Materialize** — produce a *resolved recipe* (declarative apt/npm/run steps
  that reproduce the install without an agent):
    - a dependency with an explicit ``install`` is already pinned and is baked by
      the existing `build:` layer (`bobi/build_render.py`); its recipe is that
      ``install`` verbatim, no agent runs.
    - a dependency with only a ``guide`` is materialized by a **bootstrap agent**
      (reusing `subagent.py` / the brain abstraction): it reads the guide,
      installs the dependency with pinned versions, and reports the exact steps
      it ran as a machine-readable recipe. That recorded recipe is what Stage 3
      freezes (OQ6: image-layer-via-build, agent-resolved recipe).

- **Agentic preflight** — verify the `success` contract, per target brain, in
  the *build* tier (`BOBI_VERIFY_PHASE=build`). A shell `success` is run
  directly (the build-success gate, OQ2); it runs once per brain because a
  contract can branch on the active brain (the migrated `codex` entry does).
  The snapshot is trusted only when every declared dependency is materialized
  and passes preflight under every target brain.

The harness is pure over injected runners (`agent_runner` / `shell_runner`) so
the orchestration is unit-testable without a real brain or a container; the
defaults wire the real subagent loop and a subprocess shell. Actually running
the recipes into an image, generalizing `host:`, and rendering `mcp:` per brain
are Stage 3/4 and live elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from bobi.brain import DEFAULT_BRAIN
from bobi.tool_library import Dependency

log = logging.getLogger(__name__)

# A shell `success` runs quickly at build tier (no network round-trips in the
# common case); keep the gate snappy so a hung check can't wedge the bootstrap.
PREFLIGHT_TIMEOUT = 30
# A bootstrap agent installs from a guide — allow real work, but cap it so a
# confused agent can't run unbounded.
BOOTSTRAP_TIMEOUT = 1800
BOOTSTRAP_MAX_TURNS = 60

# (prompt, brain) -> the agent's final text. Injected so tests never spawn a
# real brain; the default reuses the supervised subagent loop.
AgentRunner = Callable[[str, str], str]
# (command, env, timeout) -> (returncode, stdout, stderr).
ShellRunner = Callable[[str, dict, float], "tuple[int, str, str]"]


@dataclass
class ResolvedRecipe:
    """Declarative steps that reproduce a materialized dependency (Stage 3 input).

    The same shape as a `build:` spec (`bobi/config.py:BuildSpec`) so a resolved
    recipe feeds straight back into the one build renderer without a second code
    path. ``run_root`` is root; ``run`` drops to the app user.
    """

    apt: list[str] = field(default_factory=list)
    npm: list[str] = field(default_factory=list)
    run_root: list[str] = field(default_factory=list)
    run: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.apt or self.npm or self.run_root or self.run)

    def to_dict(self) -> dict:
        return {"apt": self.apt, "npm": self.npm,
                "run_root": self.run_root, "run": self.run}

    @classmethod
    def from_install(cls, install: dict) -> "ResolvedRecipe":
        """Adopt a dependency's pinned ``install`` verbatim (already declarative)."""
        return cls._coerce(install or {})

    @classmethod
    def from_agent(cls, recipe: dict) -> "ResolvedRecipe":
        """Adopt the recipe a bootstrap agent reported. Unknown keys are ignored;
        each field is coerced to a list of strings so a stray scalar can't poison
        the Stage-3 build."""
        return cls._coerce(recipe or {})

    @staticmethod
    def _coerce(data: dict) -> "ResolvedRecipe":
        def _steps(key: str) -> list[str]:
            val = data.get(key)
            if isinstance(val, str):
                return [val] if val.strip() else []
            if isinstance(val, (list, tuple)):
                return [str(s) for s in val if str(s).strip()]
            return []

        return ResolvedRecipe(
            apt=_steps("apt"), npm=_steps("npm"),
            run_root=_steps("run_root"), run=_steps("run"),
        )


@dataclass
class MaterializeResult:
    """Outcome of making one dependency present (before verification)."""

    dep: str
    recipe: ResolvedRecipe
    agent_used: bool
    ok: bool
    notes: str = ""
    error: str = ""


@dataclass
class PreflightResult:
    """One brain's verdict on a dependency's `success` contract (build tier)."""

    dep: str
    brain: str
    ok: bool
    detail: str = ""


@dataclass
class DependencyOutcome:
    """Materialization + per-brain preflight for one dependency."""

    dep: str
    materialize: MaterializeResult
    preflight: list[PreflightResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Satisfied only when it materialized AND every brain's preflight passed.

        Empty preflight (materialization failed, so verification was skipped) is a
        failure, never a vacuous pass — the snapshot must not trust an unverified
        dependency."""
        return (self.materialize.ok and bool(self.preflight)
                and all(p.ok for p in self.preflight))


@dataclass
class BootstrapReport:
    """Whole-bootstrap result: the gate Stage 3 trusts before snapshotting."""

    outcomes: list[DependencyOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(o.ok for o in self.outcomes)

    def failures(self) -> list[DependencyOutcome]:
        return [o for o in self.outcomes if not o.ok]

    def summary(self) -> str:
        total = len(self.outcomes)
        passed = sum(1 for o in self.outcomes if o.ok)
        return f"{passed}/{total} dependencies bootstrapped"


# ---------------------------------------------------------------------------
# Bootstrap agent prompt (guide path)
# ---------------------------------------------------------------------------


def build_bootstrap_prompt(dep: Dependency, brains: list[str]) -> str:
    """Instruction for a bootstrap agent materializing `dep` from its guide.

    The agent must PIN versions and report the exact steps it ran as a
    single-line JSON recipe, so the materialization is reproducible without an
    agent (OQ6 faithfulness: the recipe is what gets frozen). It must not touch
    credentials — the build has none; only the baked artifact is materialized
    here, runtime wiring (`host`/`mcp`) is a later stage.
    """
    brain_list = ", ".join(brains) if brains else DEFAULT_BRAIN
    return "\n\n".join([
        "You are a bootstrap agent materializing a software dependency on a "
        "fresh base image. The agent brain CLI is already installed; your job "
        "is to install and configure ONLY this dependency so its success "
        "condition holds, then report the exact pinned steps you ran so they "
        "can be frozen into an image build layer and reproduced without an agent.",
        f"Dependency name: {dep.name}",
        f"Success condition (must hold when you are done, under every target "
        f"brain: {brain_list}):\n{dep.success}",
        f"Guide (how to materialize and use it):\n{dep.guide}",
        "Rules:\n"
        "- Install system-wide with PINNED versions (`@<sha>` or `==x.y.z`); "
        "never leave a floating/`latest`/unpinned reference.\n"
        "- Prefer apt / npm / pip; use root where the install needs it.\n"
        "- The exact success command above must pass in a fresh non-login shell "
        "after your recipe is replayed. If a package installs a binary outside "
        "the default PATH, add a stable symlink or wrapper under /usr/local/bin "
        "instead of relying on interactive shell profile changes.\n"
        "- Do the minimum to make the success condition true, then verify it "
        "yourself before reporting.\n"
        "- Do NOT configure secrets or credentials and do NOT emit host/MCP "
        "runtime wiring; the build has none. Materialize only the baked artifact.",
        "When finished, output as the VERY LAST line a single line of JSON, with "
        "nothing after it, in exactly this form:\n"
        '{"ok": true, "recipe": {"apt": [], "npm": [], "run_root": [], "run": []}, '
        '"notes": "<what you did / any caveat>"}\n'
        "Every command that materializes the dependency must appear in `recipe` "
        "so the build reproduces it without an agent (`run_root` runs as root, "
        "`run` as the app user). Keep all four keys even when a list is empty. "
        'Use "ok": false if you could not make the success condition true.',
    ])


def _parse_recipe_verdict(text: str) -> dict | None:
    """Return the trailing JSON recipe verdict a bootstrap agent emitted, or None.

    None means no parseable verdict (a malformed or truncated run) — distinct
    from an explicit ``{"ok": false}``. Reuses subagent's brace-balanced object
    scanner so a nested `recipe` object stays intact.
    """
    if not text:
        return None
    from bobi.subagent import _extract_json_objects

    for chunk in reversed(_extract_json_objects(text)):
        try:
            parsed = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and "ok" in parsed:
            return parsed
    return None


# ---------------------------------------------------------------------------
# Materialize
# ---------------------------------------------------------------------------


def materialize(dep: Dependency, *, agent_runner: AgentRunner,
                brains: list[str]) -> MaterializeResult:
    """Make `dep` present and capture the recipe that reproduces it.

    A dependency with a pinned ``install`` is baked by the existing build layer;
    its recipe is that install verbatim and no agent runs. A guide-only
    dependency is handed to a bootstrap agent, which materializes it and reports
    the pinned steps it ran.
    """
    if dep.install:
        return MaterializeResult(
            dep=dep.name, recipe=ResolvedRecipe.from_install(dep.install),
            agent_used=False, ok=True, notes="pinned install recipe (no agent)",
        )

    if not dep.guide.strip():
        return MaterializeResult(
            dep=dep.name, recipe=ResolvedRecipe(), agent_used=False, ok=False,
            error=("dependency has neither 'install' nor 'guide' — nothing to "
                   "materialize from"),
        )

    prompt = build_bootstrap_prompt(dep, brains)
    # Materialize once (baked artifacts are brain-independent); verification is
    # per-brain. Run under the primary target brain so the agent's own checks use
    # a real brain when the guide involves one.
    brain = brains[0] if brains else DEFAULT_BRAIN
    try:
        text = agent_runner(prompt, brain)
    except Exception as exc:  # a crashed bootstrap agent is a failed materialize
        log.warning("Bootstrap agent for %s crashed: %s", dep.name, exc)
        return MaterializeResult(
            dep=dep.name, recipe=ResolvedRecipe(), agent_used=True, ok=False,
            error=f"bootstrap agent crashed: {exc}",
        )

    verdict = _parse_recipe_verdict(text)
    if verdict is None:
        return MaterializeResult(
            dep=dep.name, recipe=ResolvedRecipe(), agent_used=True, ok=False,
            error=("bootstrap agent produced no parseable recipe (malformed or "
                   "truncated output)"),
        )

    recipe = ResolvedRecipe.from_agent(verdict.get("recipe") or {})
    ok = bool(verdict.get("ok"))
    notes = str(verdict.get("notes", "") or "")
    if ok and recipe.is_empty:
        # An agent claiming success with no recorded steps can't be frozen; a
        # snapshot built from an empty recipe would silently lose the dependency.
        return MaterializeResult(
            dep=dep.name, recipe=recipe, agent_used=True, ok=False, notes=notes,
            error="bootstrap agent reported success but emitted an empty recipe",
        )
    return MaterializeResult(
        dep=dep.name, recipe=recipe, agent_used=True, ok=ok, notes=notes,
        error="" if ok else "bootstrap agent could not satisfy the success condition",
    )


# ---------------------------------------------------------------------------
# Agentic preflight (build tier)
# ---------------------------------------------------------------------------


def preflight(dep: Dependency, *, brains: list[str], shell_runner: ShellRunner,
              base_env: dict | None = None,
              timeout: float = PREFLIGHT_TIMEOUT,
              phase: str = "build") -> list[PreflightResult]:
    """Verify `dep.success` in a given verify tier, once per target brain.

    Runs with ``BOBI_VERIFY_PHASE=<phase>`` and ``BOBI_BRAIN`` set per brain,
    because a `success` contract can branch on the active brain. A dependency is
    satisfied only when every brain passes.

    ``phase`` defaults to ``build`` — the weak/no-auth form that gates the
    snapshot (OQ2) on the container cold path. Local materialization (#428 Stage
    5) passes ``phase="runtime"`` so a contract that gates on the phase (the
    migrated ``codex`` entry) verifies against a real, credentialed check.
    """
    env_base = dict(os.environ if base_env is None else base_env)
    env_base["BOBI_VERIFY_PHASE"] = phase
    from bobi.brain import BRAIN_ENV

    results: list[PreflightResult] = []
    for brain in brains:
        env = dict(env_base)
        env[BRAIN_ENV] = brain
        try:
            rc, out, err = shell_runner(dep.success, env, timeout)
        except Exception as exc:  # a runner blowup is a failed check, not a crash
            results.append(PreflightResult(
                dep.name, brain, ok=False, detail=f"preflight runner error: {exc}"))
            continue
        ok = rc == 0
        detail = "healthy" if ok else (
            (err or "").strip()[:200] or (out or "").strip()[:200]
            or f"exit code {rc}")
        results.append(PreflightResult(dep.name, brain, ok=ok, detail=detail))
    return results


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def bootstrap(deps: list[Dependency], *, brains: list[str],
              agent_runner: AgentRunner | None = None,
              shell_runner: ShellRunner | None = None,
              base_env: dict | None = None) -> BootstrapReport:
    """Materialize + preflight every dependency; gate the snapshot on the result.

    Each dependency is materialized (install verbatim, or agent-from-guide) and
    then verified against its `success` under every target brain. Preflight is
    skipped for a dependency that failed to materialize (there is nothing to
    verify) and that dependency is reported as failed, so the overall gate is
    honest.
    """
    if not brains:
        raise ValueError("bootstrap needs at least one target brain")
    agent_runner = agent_runner or default_agent_runner
    shell_runner = shell_runner or default_shell_runner

    outcomes: list[DependencyOutcome] = []
    for dep in deps:
        m = materialize(dep, agent_runner=agent_runner, brains=brains)
        if not m.ok:
            log.warning("Bootstrap materialize failed for %s: %s", dep.name, m.error)
            outcomes.append(DependencyOutcome(dep.name, m, preflight=[]))
            continue
        pf = preflight(dep, brains=brains, shell_runner=shell_runner,
                       base_env=base_env)
        outcomes.append(DependencyOutcome(dep.name, m, preflight=pf))
    return BootstrapReport(outcomes=outcomes)


class BootstrapError(RuntimeError):
    """A guide-dependency bootstrap failed its agentic preflight gate.

    Raised by `render_team_deps` so the build/CI caller fails loudly instead of
    freezing an unverified snapshot. Carries the report for a detailed message.
    """

    def __init__(self, report: BootstrapReport):
        self.report = report
        fails = ", ".join(o.dep for o in report.failures()) or "(none)"
        super().__init__(
            f"bootstrap gate failed ({report.summary()}); unsatisfied: {fails}")


def _agent_needed(dep: Dependency) -> bool:
    """A dependency the bootstrap agent must materialize from its guide.

    A pinned `install` is baked deterministically by the existing build layer (via
    compose → `cfg.build`), so it needs no agent. Only a guide-only dependency
    (guide, no install) is resolved by an agent into a recipe here."""
    return not dep.install and bool(dep.guide.strip())


def team_has_bake(team_dir: "Path", project_path: "Path | None" = None) -> bool:
    """True if a team bakes anything into an image — a declarative `build:` OR a
    guide-only dependency the bootstrap agent must resolve.

    The agent-free gate `build-team-images.sh --check` uses to decide whether to
    build a team-flavored image. Unlike `build_render --check` (declarative build
    only), this also catches a team whose ONLY baked content is a guide dependency
    — which carries no `cfg.build` yet still needs an image layer.
    """
    from pathlib import Path

    from bobi.build_render import _workspace_root, load_composed_team_config
    from bobi.tool_library import resolve_team_dependencies

    team_dir = Path(team_dir)
    project_path = project_path or _workspace_root(team_dir)
    spec = load_composed_team_config(team_dir, project_path).build
    declarative = spec is not None and bool(
        spec.apt or spec.npm or spec.run_root or spec.run or spec.verify_requires)
    if declarative:
        return True
    deps = resolve_team_dependencies(team_dir, project_path)
    return any(_agent_needed(d) for d in deps)


def render_team_deps(team_dir: "Path", project_path: "Path | None" = None, *,
                     brains: list[str] | None = None,
                     agent_runner: AgentRunner | None = None,
                     shell_runner: ShellRunner | None = None,
                     cfg=None, deps: list[Dependency] | None = None) -> str | None:
    """Bootstrap a team's guide-only deps and render its team-deps.sh (#428 Stage 3).

    The single seam the image build calls (`build-team-images.sh`, the release
    rollout). It composes the team, resolves its full declared dependency set,
    runs the bootstrap agent for any guide-only dependency (the CI cold path),
    and feeds the resolved recipes back through the ONE renderer
    (`build_render.render_team_deps_script`) alongside the pinned-install steps
    compose already merged — no second install code path. The declared-set hash is
    stamped so a later deploy/boot can detect drift and re-bootstrap.

    Returns None for a generic team (nothing to bake). Raises `BootstrapError` if
    a guide-dep fails its agentic preflight, so an unverified snapshot is never
    frozen.

    `cfg`/`deps` let a caller that already composed the team and resolved its
    dependency set (the deploy plugin's stage_team_deps) pass them in, avoiding a second
    chain walk + registry fetch; omitted, they are computed here as before.
    """
    from pathlib import Path

    from bobi.brain import _BRAINS
    from bobi.build_render import (
        _workspace_root,
        load_composed_team_config,
        render_team_deps_script,
    )
    from bobi.tool_library import dependency_list_hash, resolve_team_dependencies

    team_dir = Path(team_dir)
    project_path = project_path or _workspace_root(team_dir)
    cfg = cfg or load_composed_team_config(team_dir, project_path)
    deps = deps if deps is not None else resolve_team_dependencies(
        team_dir, project_path)

    spec = cfg.build
    declarative = spec is not None and bool(
        spec.apt or spec.npm or spec.run_root or spec.run or spec.verify_requires)
    guide_deps = [d for d in deps if _agent_needed(d)]
    if not declarative and not guide_deps:
        return None  # generic team — deploys on the shared base image

    extra_recipes: list[dict] = []
    if guide_deps:
        brains = brains or sorted(_BRAINS)
        report = bootstrap(guide_deps, brains=brains, agent_runner=agent_runner,
                           shell_runner=shell_runner)
        if not report.ok:
            raise BootstrapError(report)
        extra_recipes = [o.materialize.recipe.to_dict() for o in report.outcomes]

    # Only stamp a dep-list hash when the team actually declares dependencies —
    # a plain inline `build:` team has none, and an empty-set hash is meaningless
    # noise deploy would ignore anyway (its local hash is "" too).
    dep_hash = dependency_list_hash(deps) if deps else ""
    return render_team_deps_script(
        cfg, extra_recipes=extra_recipes, dep_list_hash=dep_hash)


# ---------------------------------------------------------------------------
# Default runners (real brain + real shell)
# ---------------------------------------------------------------------------


def default_shell_runner(command: str, env: dict,
                         timeout: float) -> "tuple[int, str, str]":
    """Run a `success` command in a shell. Never raises: a timeout / OS error is
    a failed check (non-zero rc), so preflight treats it as unsatisfied rather
    than crashing the bootstrap."""
    try:
        proc = subprocess.run(
            command, shell=True, env=env, timeout=timeout,
            capture_output=True, text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"check timed out ({timeout:.0f}s)"
    except OSError as exc:
        return 127, "", f"check command failed: {exc}"


def _ensure_bootstrap_runtime() -> str:
    """Bind a throwaway Bobi runtime root so the subagent machinery has one.

    The agent session path (`subagent._run_agent_supervised` → the brain session)
    resolves session storage / workspace paths from a *bound* runtime root
    (`paths.bound_root`). A bootstrap has no installed team runtime — it runs on a
    bare base image — so stand up a minimal canonical layout in a temp dir and
    bind it once. Returns the runtime's workspace dir (a writable agent cwd).
    Idempotent: a real spawner-bound root (or an earlier bootstrap dep) wins.
    """
    import tempfile
    from pathlib import Path

    from bobi import paths

    existing = paths.bound_root()
    if existing is not None:
        return str(paths.workspace_dir(existing))
    rt = Path(tempfile.mkdtemp(prefix="bobi-bootstrap-runtime-"))
    (rt / "package").mkdir(parents=True, exist_ok=True)
    (rt / "package" / "agent.yaml").write_text("agent: bootstrap\n")
    (rt / "state").mkdir(exist_ok=True)
    (rt / "workspace").mkdir(exist_ok=True)
    paths.bind_root(rt)
    return str(rt / "workspace")


def default_agent_runner(prompt: str, brain: str, *, cwd: str | None = None,
                         timeout: int = BOOTSTRAP_TIMEOUT,
                         max_turns: int = BOOTSTRAP_MAX_TURNS) -> str:
    """Run a bootstrap agent through the supervised subagent loop.

    Reuses `subagent._run_agent_supervised` (the short-lived, out-of-band,
    non-addressable agent path — the same one monitor checks use) under the
    requested brain. Returns the agent's final text (the recipe verdict is the
    last line); an errored run still returns its text so `materialize` can parse
    an explicit ``{"ok": false}``.

    Binds a throwaway runtime root when none is bound (the bare-base-image case),
    and runs the agent in that runtime's workspace unless `cwd` is given.
    """
    import asyncio

    from bobi.brain import BRAIN_ENV
    from bobi.subagent import _run_agent_supervised

    if cwd is None:
        cwd = _ensure_bootstrap_runtime()

    run_key = f"boot-{hashlib.sha256(prompt.encode()).hexdigest()[:8]}"
    saved = os.environ.get(BRAIN_ENV)
    if brain:
        os.environ[BRAIN_ENV] = brain
    try:
        result = asyncio.run(
            asyncio.wait_for(
                _run_agent_supervised(
                    prompt, cwd, run_key, "bootstrap", timeout,
                    role="bootstrap", max_turns=max_turns,
                ),
                timeout=timeout,
            )
        )
        # Surface the cold-path cost so a CI build log shows what a bootstrap
        # actually spent (the warm path is free — no agent runs). Cost is
        # brain-reported; some brains report $0, so log tokens/duration too.
        log.info(
            "Bootstrap agent (%s%s) finished: cost $%.4f, %.1fs",
            brain or DEFAULT_BRAIN,
            f", model={result.model}" if result.model else "",
            result.total_cost_usd or 0.0, (result.duration_ms or 0) / 1000.0)
        return result.final_text or ""
    except asyncio.TimeoutError:
        log.warning("Bootstrap agent timed out after %ss", timeout)
        return ""
    finally:
        if saved is None:
            os.environ.pop(BRAIN_ENV, None)
        else:
            os.environ[BRAIN_ENV] = saved


# ---------------------------------------------------------------------------
# CLI — resolve a team's dependencies and bootstrap them (Stage 3 wires this
# into the build; here it is a manual entry point).
# ---------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    import argparse

    # Surface this module's INFO logs (the per-bootstrap cost line) when run as a
    # CLI — Python defaults to WARNING, so the cost would otherwise be swallowed.
    # Scoped to `bobi.dep_bootstrap` so the subagent loop's own logs stay quiet.
    if not log.handlers:
        _h = logging.StreamHandler()
        _h.setLevel(logging.INFO)
        _h.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(_h)
        log.setLevel(logging.INFO)
        log.propagate = False

    from bobi.brain import _BRAINS
    from bobi.build_render import _workspace_root
    from bobi.tool_library import (
        dependency_list_hash,
        resolve_team_dependencies,
    )

    ap = argparse.ArgumentParser(
        description="Bootstrap a team's declared dependencies on a base image "
                    "(#428): materialize each, verify its success, and (Stage 3) "
                    "render the resolved team-deps hook for the image build.")
    ap.add_argument("team_dir", type=str, help="team source dir (holds agent.yaml)")
    ap.add_argument(
        "--brains", default=",".join(sorted(_BRAINS)),
        help="comma-separated target brains to verify under "
             "(default: all registered brains)")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    ap.add_argument(
        "--check", action="store_true",
        help="exit 0 if the team bakes anything (declarative build or a "
             "guide-only dependency), 2 otherwise; no bootstrap, no output")
    ap.add_argument(
        "--needs-agent", action="store_true",
        help="exit 0 if the team has a guide-only dependency the bootstrap agent "
             "must resolve (so --render must run inside the base image), 2 "
             "otherwise; no bootstrap, no output")
    ap.add_argument(
        "--render", metavar="OUT",
        help="bootstrap guide-only deps and render the team-deps hook to OUT "
             "(the image-build seam); exits 0 with no file for a generic team")
    ap.add_argument(
        "--print-dep-hash", action="store_true",
        help="print the declared dependency-set hash (the re-bootstrap key) "
             "and exit; no bootstrap")
    args = ap.parse_args(argv)

    from pathlib import Path
    team_dir = Path(args.team_dir)
    project_path = _workspace_root(team_dir)
    brains = [b.strip() for b in args.brains.split(",") if b.strip()]

    # Agent-free modes the image build uses to gate/identify a team before the
    # (expensive) bootstrap agent runs.
    if args.check:
        return 0 if team_has_bake(team_dir, project_path) else 2
    if args.needs_agent:
        deps = resolve_team_dependencies(team_dir, project_path)
        return 0 if any(_agent_needed(d) for d in deps) else 2
    if args.print_dep_hash:
        deps = resolve_team_dependencies(team_dir, project_path)
        print(dependency_list_hash(deps))
        return 0
    if args.render:
        try:
            script = render_team_deps(team_dir, project_path, brains=brains)
        except BootstrapError as exc:
            print(str(exc))
            return 1
        if script is None:
            return 0  # generic team — nothing to bake
        out = Path(args.render)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(script)
        out.chmod(0o755)
        return 0

    deps = resolve_team_dependencies(team_dir, project_path)
    report = bootstrap(deps, brains=brains)

    if args.json:
        print(json.dumps({
            "ok": report.ok,
            "outcomes": [
                {
                    "dep": o.dep,
                    "ok": o.ok,
                    "agent_used": o.materialize.agent_used,
                    "recipe": o.materialize.recipe.to_dict(),
                    "error": o.materialize.error,
                    "preflight": [
                        {"brain": p.brain, "ok": p.ok, "detail": p.detail}
                        for p in o.preflight
                    ],
                }
                for o in report.outcomes
            ],
        }, indent=2))
    else:
        print(report.summary())
        for o in report.outcomes:
            glyph = "ok" if o.ok else "FAIL"
            src = "agent" if o.materialize.agent_used else "install"
            print(f"  [{glyph}] {o.dep} ({src})")
            if not o.materialize.ok:
                print(f"        materialize: {o.materialize.error}")
            for p in o.preflight:
                if not p.ok:
                    print(f"        preflight[{p.brain}]: {p.detail}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
