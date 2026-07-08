"""Render a team's `build:` spec into a Docker build hook (C24).

A team that declares `build:` in agent.yaml needs its host tools baked into the
container image. Rather than `FROM bobi-base` + tool layers on top — which
re-runs every team's apt/npm/run on each framework release (the FROM digest
moves) — we render the spec to a single shell script (`team-deps.sh`) and the
ONE Dockerfile runs it as a stable layer BELOW the volatile framework-wheel copy
(via the `TEAM_DEPS` build-arg hook). A code-only framework release then rebuilds
only the wheel layer; the team's tools stay cached. See
docs/CONTAINERIZED_DEPLOYMENT.md §2.6 (Team-flavored images).

The script:
  * `apt` / `npm` install system-wide as **root** (npm globals land in
    /usr/local/bin — on PATH, not under HOME).
  * `run` steps execute as the **bobi** user with HOME pinned to the
    user's real image home (/home/bobi) — the SAME path the agent runs
    with at runtime, so ~-relative tools (e.g. gstack's ~/dev/gstack) are baked
    in place and read directly at runtime; nothing is copied onto the volume.
    `env HOME=…` makes `~` expand to the home (an inline `HOME=… cmd ~/x` would
    NOT — tilde expands before the assignment). `env -u CLAUDE_CONFIG_DIR` is
    critical: the runtime sets CLAUDE_CONFIG_DIR to the volume, but at BUILD
    time tools must write to the IMAGE — so we strip it here.
  * personal skills are baked at BAKED_SKILLS (/opt/bobi/skills), OUTSIDE
    ~/.claude, via a build-time `~/.claude/skills -> BAKED_SKILLS` symlink. This
    frees ~/.claude so the entrypoint can point the whole dir at the durable
    volume config dir — making `~/.claude/{projects,settings.json,skills,…}` all
    resolve to Claude's real state at runtime (docker-entrypoint.sh §2b), so any
    skill keyed off ~/.claude or $HOME just works.
  * `verify: requires` re-runs the team's requires[].check as the final step,
    against the SAME image HOME the runtime uses — so the build's verify and
    production's dispatch gate read identical paths, closing the
    build-time-vs-runtime HOME gap that the old seed-copy model left open.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import tempfile
from pathlib import Path

from bobi.config import BuildSpec, Config

# The bobi user's real image home — where `run` steps bake ~-relative tools
# and where the agent runs at runtime (docker-entrypoint.sh keeps HOME here and
# redirects only Claude's durable state off to the volume via CLAUDE_CONFIG_DIR).
TEAM_HOME = "/home/bobi"
# Personal skills bake HERE — an image path OUTSIDE ~/.claude — so the entrypoint
# can point ~/.claude at the durable volume config dir without clobbering them.
# A build-time `~/.claude/skills -> BAKED_SKILLS` symlink lets tools that write
# to ~/.claude/skills (gstack) land here transparently. See docker-entrypoint.sh.
BAKED_SKILLS = "/opt/bobi/skills"
APP_USER = "bobi"
# The team-deps hook stamps its deps-identity hash here, in the IMAGE, so a
# running instance can report what tools it was built with. `bobi deploy`
# reads it over `fly ssh` to detect a `build:` change before taking the silent
# in-place hot-push path that would never rebuild the image (#379).
TEAM_DEPS_STAMP = "/opt/bobi/team-deps.hash"
# The DECLARED dependency-set identity (#428 Stage 3). Sibling of TEAM_DEPS_STAMP,
# but keyed on the loose declaration (name/success/guide/install/host/mcp — see
# tool_library.dependency_list_hash), NOT the resolved build. It is the
# re-bootstrap trigger: a warm boot / deploy whose declared-set hash matches the
# snapshot skips re-bootstrap; a changed set forces a rebuild + re-bootstrap.
# Kept separate from TEAM_DEPS_STAMP because a guide-only dep's *resolved* recipe
# is non-deterministic (two bootstraps can pin different upstreams), so it must
# not enter the deterministic #379 stamp deploy compares without running an agent.
DEP_LIST_STAMP = "/opt/bobi/dep-list.hash"


def team_deps_hash(spec: BuildSpec) -> str:
    """Stable short hash of the build inputs — the team-image cache key / tag.

    Keyed on what actually changes the baked tools (apt/npm/run/verify/base), so
    an unrelated agent.yaml edit doesn't churn the image tag.
    """
    payload = json.dumps(
        {
            "base": spec.base,
            "apt": spec.apt,
            "npm": spec.npm,
            "run_root": spec.run_root,
            "run": spec.run,
            "verify": spec.verify_requires,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _merge_recipes_into_spec(spec: BuildSpec, recipes: list[dict]) -> BuildSpec:
    """Fold agent-resolved recipes (#428 Stage 3) into a copy of `spec`.

    Each recipe mirrors a `build:` block (apt/npm/run_root/run). Guide-only
    dependencies carry no pinned `install`, so they never reach `cfg.build` at
    compose; the bootstrap agent resolves them to a recipe, and THIS is where that
    recipe re-enters the ONE renderer — appended + de-duped exactly like an inline
    `build:` list, so there is no second install code path. Order: declarative
    (compose) steps first, resolved recipes after, matching compose's base-first
    accretion.
    """
    from dataclasses import replace

    from bobi.compose import _dedupe

    def _accrete(field: str) -> list[str]:
        out = list(getattr(spec, field))
        for r in recipes:
            out += [str(s) for s in (r.get(field) or [])]
        return _dedupe(out)

    return replace(
        spec,
        apt=_accrete("apt"), npm=_accrete("npm"),
        run_root=_accrete("run_root"), run=_accrete("run"),
    )


def render_team_deps_script(cfg: Config, *, extra_recipes: list[dict] | None = None,
                            dep_list_hash: str = "") -> str:
    """Render the `build:` spec to the team-deps.sh hook script.

    `extra_recipes` are agent-resolved recipes for guide-only dependencies (#428
    Stage 3): they bake into the script body but NOT into the #379 deps stamp,
    which stays keyed on the deterministic declarative spec so deploy — which
    never runs the bootstrap agent — computes the same value. `dep_list_hash`, when
    given, is stamped into DEP_LIST_STAMP as the re-bootstrap trigger.

    Raises ValueError if the team has nothing to bake (no declarative build and no
    recipes, or a pure raw-Dockerfile escape hatch — that path bypasses this
    renderer).
    """
    recipes = [r for r in (extra_recipes or []) if r]
    spec = cfg.build
    if spec is None:
        # A guide-only team contributes no pinned install, so compose produced no
        # build spec; the resolved recipes ARE its whole build. Synthesize an empty
        # spec (re-verifying the team's requires, if any, as the final build step).
        if not recipes:
            raise ValueError("team has no build: spec to render")
        spec = BuildSpec(verify_requires=bool(cfg.requires))
    declarative = bool(spec.apt or spec.npm or spec.run_root or spec.run
                       or spec.verify_requires)
    if spec.dockerfile and not declarative and not recipes:
        raise ValueError(
            "team uses a raw Dockerfile escape hatch; build it directly, "
            "do not render"
        )
    if not declarative and not recipes:
        raise ValueError("team has no declarative build: spec to render")

    # The #379 stamp is the DECLARATIVE identity — computed BEFORE folding in the
    # (non-deterministic) resolved recipes — so it is agent-free and deploy's local
    # hash matches. Guide-dep drift is caught by dep_list_hash instead.
    deps_stamp = team_deps_hash(spec)
    if recipes:
        spec = _merge_recipes_into_spec(spec, recipes)

    lines: list[str] = [
        "#!/usr/bin/env bash",
        "# GENERATED by bobi.build_render — do not edit.",
        "# Runs as root, BELOW the framework-wheel layer (C24 team-deps hook).",
        "set -euo pipefail",
        "",
        # Stamp the deps identity into the image (#379): a running instance reads
        # this over `fly ssh` so deploy can detect a `build:` change and refuse to
        # silently hot-push (which never rebuilds the image). `mkdir -p` (not
        # `install -d`) keeps no-skills teams free of a seed-dir marker.
        "echo '== stamp team-deps identity (#379) =='",
        "mkdir -p /opt/bobi",
        f"printf '%s\\n' {shlex.quote(deps_stamp)} > {TEAM_DEPS_STAMP}",
        "",
    ]
    if dep_list_hash:
        # The declared dependency-set identity (#428): deploy/warm-boot compares
        # this to decide whether to re-bootstrap. Stamped only when the caller
        # resolved the full dependency set (the build/deploy path), so a bare
        # `render_team_deps_script(cfg)` stays byte-identical to pre-Stage-3.
        lines += [
            "echo '== stamp declared dependency-set identity (#428) =='",
            f"printf '%s\\n' {shlex.quote(dep_list_hash)} > {DEP_LIST_STAMP}",
            "",
        ]

    if spec.apt:
        pkgs = " ".join(shlex.quote(p) for p in spec.apt)
        lines += [
            "echo '== apt =='",
            "apt-get update",
            f"apt-get install -y --no-install-recommends {pkgs}",
            "rm -rf /var/lib/apt/lists/*",
            "",
        ]

    if spec.npm:
        pkgs = " ".join(shlex.quote(p) for p in spec.npm)
        lines += ["echo '== npm =='", f"npm install -g {pkgs}", ""]

    for step in spec.run_root:
        lines += [
            f"echo {shlex.quote('== run_root: ' + step[:60] + ' ==')}",
            step,  # already root; run as-is
            "",
        ]

    # `run` and `verify` steps drop to the bobi user with HOME pinned to the
    # image home and CLAUDE_CONFIG_DIR stripped, so ~-relative tools bake into the
    # image (read directly at runtime — no seed copy). `-u CLAUDE_CONFIG_DIR`
    # defends against the runtime ENV leaking into the build and redirecting
    # writes to the (build-time-absent) volume.
    as_user = f"gosu {APP_USER} env -u CLAUDE_CONFIG_DIR HOME={TEAM_HOME} bash -lc"

    if spec.run:
        # Bake personal skills OUTSIDE ~/.claude (at BAKED_SKILLS) via a build-time
        # symlink, so a tool writing ~/.claude/skills/X lands in the immutable
        # image path — leaving ~/.claude itself free for the entrypoint to point
        # at the durable volume config dir (full ~/.claude coincidence at runtime).
        lines += [
            "echo '== bake skills into image path =='",
            f"install -d -o {APP_USER} -g {APP_USER} {BAKED_SKILLS}",
            f"{as_user} {shlex.quote(f'mkdir -p ~/.claude && rm -rf ~/.claude/skills && ln -sfn {BAKED_SKILLS} ~/.claude/skills')}",
            "",
        ]

    for step in spec.run:
        lines += [
            f"echo {shlex.quote('== run: ' + step[:60] + ' ==')}",
            f"{as_user} {shlex.quote(step)}",
            "",
        ]

    if spec.verify_requires and cfg.requires:
        lines.append("echo '== verify requires =='")
        for entry in cfg.requires:
            lines += [
                f"echo {shlex.quote('verify ' + entry.name)}",
                f"{as_user} {shlex.quote('BOBI_VERIFY_PHASE=build; ' + entry.check)}",
            ]
        lines.append("")

    lines.append("echo '== team-deps complete =='")
    return "\n".join(lines) + "\n"


def load_team_config(team_dir: Path) -> Config:
    """Load a team's Config straight from <team_dir>/agent.yaml (source layout).

    Config.load() expects an installed package at run/package/agent.yaml; a team
    SOURCE dir holds agent.yaml at its root, so parse it directly.
    """
    agent_yaml = team_dir / "agent.yaml"
    if not agent_yaml.exists():
        raise FileNotFoundError(f"no agent.yaml in {team_dir}")
    return Config._parse(agent_yaml)


def load_composed_team_config(team_dir: Path, project_path: Path) -> Config:
    """Like `load_team_config`, but returns the COMPOSED config.

    The deploy image bakes what the team *composes to*, not what its raw leaf
    agent.yaml literally says: the `from:` chain is merged and `tool_library:`
    entries (#416) are expanded into `requires:`/`build:`. The team-deps renderer
    MUST read this — a team that declares its CLI via `tool_library: [venn]`
    carries no inline `build:` on the leaf, so a raw read would bake nothing,
    the binary would be missing on the box, and the dispatch-time `requires`
    gate would block every agent (the personal-assistant/venn deploy bug).

    Composes into a throwaway dir and parses the frozen agent.yaml.
    """
    # Local import: compose → tool_library → compose forms a cycle that only a
    # function-level import breaks (mirrors compose.py's own local import).
    from bobi.compose import compose, resolve_chain

    chain = resolve_chain(team_dir, project_path)
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "composed"
        compose(chain, dest)
        return Config._parse(dest / "agent.yaml")


def _workspace_root(team_dir: Path) -> Path:
    """The root `resolve_chain` composes a team against.

    A team lives at ``<root>/agents/<name>``, so its ``from:`` refs and catalog
    resolve under ``<root>``. When a team dir is not under ``agents/`` (ad-hoc or
    test layouts) its own parent is the root.
    """
    parent = team_dir.parent
    return parent.parent if parent.name == "agents" else parent


def _main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="Render a team's build: spec to a Docker team-deps hook."
    )
    ap.add_argument("team_dir", type=Path, help="team source dir (holds agent.yaml)")
    ap.add_argument("--out", type=Path, help="write script here (default: stdout)")
    ap.add_argument(
        "--print-hash",
        action="store_true",
        help="print the deps hash instead of the script (image tag / cache key)",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit 0 if the team declares a build, 2 otherwise (no output)",
    )
    args = ap.parse_args(argv)

    # Read the COMPOSED config (from: chain + tool_library/dependency expansion),
    # exactly as `bobi deploy` bakes it — NOT the raw leaf. A team that declares
    # its CLI via `tool_library:` (or a `brain: codex` that implies the codex
    # dependency) carries no inline `build:` on the leaf, so a raw read would skip
    # the bake and the CI verify gate would never exercise it. Composing here makes
    # the CI build gate and the deploy image agree byte-for-byte. (#428)
    cfg = load_composed_team_config(args.team_dir, _workspace_root(args.team_dir))
    if args.check:
        return 0 if cfg.build is not None else 2
    if cfg.build is None:
        ap.error(f"{args.team_dir} declares no build: spec")

    if args.print_hash:
        print(team_deps_hash(cfg.build))
        return 0

    script = render_team_deps_script(cfg)
    if args.out:
        args.out.write_text(script)
        args.out.chmod(0o755)
    else:
        print(script, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
