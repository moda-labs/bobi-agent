#!/usr/bin/env python3
"""Render a deployment's team-deps hook for the release rollout (C24).

Given a deployment NAME, if its team declares a `build:` spec, render the
team-deps.sh into dist/team-deps/<team>.sh and print its repo-relative path (the
value to pass as the Dockerfile's TEAM_DEPS build-arg). Print NOTHING (and exit 0)
for a generic deployment — no `build:` spec, no local team package (a `team-url:`
deployment whose package CI can't see), or no deployment file at all.

Used by .github/workflows/release.yml so a framework release rebuilds each
team-flavored instance's OWN image (its baked host tools on the new framework
wheel) instead of rolling the shared generic image onto it — which would strip a
team's tools and break its dispatch gate.
"""
from __future__ import annotations

import pathlib
import sys

from bobi.dep_bootstrap import BootstrapError, render_team_deps
from bobi.deploy import DeployError, load_deploy_config


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: render-team-deps.py <deployment-name>", file=sys.stderr)
        return 2
    name = argv[1]
    root = pathlib.Path(".")
    try:
        cfg = load_deploy_config(root, name)
    except DeployError:
        return 0  # no deployment file → not our instance / generic
    if not cfg.team:
        return 0  # team-url: the package isn't local, so we can't rebuild it here
    team_dir = root / "agents" / cfg.team
    if not (team_dir / "agent.yaml").exists():
        return 0
    # Route through the bootstrap→render seam (#428 Stage 3): composes the from:
    # chain + tool_library entries, runs the bootstrap agent for any guide-only
    # dependency (the CI cold path), and freezes the resolved recipe through the
    # ONE renderer — so a team that bakes its CLI via `tool_library:` rebuilds its
    # own image on a framework release instead of being rolled the generic image.
    try:
        script = render_team_deps(team_dir, root)
    except BootstrapError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if script is None:
        return 0  # generic — deploys on the shared base image
    out = root / "dist" / "team-deps" / f"{cfg.team}.sh"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(script)
    print(out.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
