"""Lint guard: eng-team's `build:` deps stay pinned for reproducible rebuilds (#380).

Floating deps make two builds of the same commit diverge and let an upstream
breaking change land with no spec diff to point at — and `team_deps_hash` keys on
the spec TEXT, so an unpinned bump doesn't even move the image identity. This
fails loudly if a pin is dropped (re-floated), so a reviewer must re-pin instead.
Refresh pins with `npm view <pkg> version` / `git ls-remote .../gstack HEAD`.
"""

import re
from pathlib import Path

from modastack.build_render import load_team_config

REPO_ROOT = Path(__file__).resolve().parent.parent
ENG_TEAM = REPO_ROOT / "agents" / "eng-team"


def test_npm_deps_are_version_pinned():
    spec = load_team_config(ENG_TEAM).build
    assert spec and spec.npm, "eng-team should declare npm build deps"
    for pkg in spec.npm:
        # name@version — accept scoped (@scope/name@x.y.z) and bare (name@x.y.z).
        assert re.search(r".+@\d+\.\d+\.\d+", pkg), f"npm dep not pinned: {pkg!r}"


def test_gstack_clone_is_pinned_to_a_sha():
    """Every gstack clone (build `run` AND the requires.fix that mirrors it) must
    `git checkout <40-hex sha>` so the cloned tree is reproducible, not HEAD."""
    cfg = load_team_config(ENG_TEAM)
    clones = [s for s in (cfg.build.run or []) if "garrytan/gstack" in s]
    clones += [r.fix for r in cfg.requires
               if r.fix and "garrytan/gstack" in r.fix]
    assert clones, "expected at least one gstack clone to check"
    for step in clones:
        assert re.search(r"git checkout [0-9a-f]{40}", step), \
            f"gstack clone not pinned to a SHA: {step!r}"


def test_playwright_install_deps_is_pinned():
    spec = load_team_config(ENG_TEAM).build
    pw = [s for s in (spec.run_root or []) if "playwright" in s]
    assert pw, "expected a playwright install-deps step"
    for step in pw:
        assert re.search(r"playwright@\d+\.\d+\.\d+", step), \
            f"playwright not pinned: {step!r}"
