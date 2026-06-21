#!/usr/bin/env python3
"""Render a deployment's team-deps hook for the release rollout (C24).

Given a deployment NAME, if its team declares a `build:` spec, render the
team-deps.sh into dist/team-deps/<team>.sh and print its repo-relative path (the
value to pass as the Dockerfile's TEAM_DEPS build-arg). Print NOTHING (and exit 0)
for a generic deployment — no `build:` spec, no local team package (a `team-url:`
deployment whose package CI can't see), or no deployment file at all.

Used by .github/workflows/gitops-release.yml so a framework release rebuilds each
team-flavored instance's OWN image (its baked host tools on the new framework
wheel) instead of rolling the shared generic image onto it — which would strip a
team's tools and break its dispatch gate.
"""
from __future__ import annotations

import pathlib
import sys

from modastack.build_render import load_team_config, render_team_deps_script
from modastack.deploy import DeployError, load_deploy_config


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
    spec = load_team_config(team_dir).build
    if spec is None or not (
        spec.apt or spec.npm or spec.run_root or spec.run or spec.verify_requires
    ):
        return 0  # generic — deploys on the shared base image
    out = root / "dist" / "team-deps" / f"{cfg.team}.sh"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_team_deps_script(load_team_config(team_dir)))
    print(out.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
