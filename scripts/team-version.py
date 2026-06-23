#!/usr/bin/env python3
"""Print a team's published version (its agent.yaml `version:`), or nothing.

Used by build-team-tarballs.sh to name the immutable per-team package
(`<team>-<version>.tar.gz`). YAML parsing lives here in Python rather than in
bash (brittle) so the shell script stays dumb.

A team with no `version:` prints nothing and exits 0 — the caller then publishes
only the rolling `<team>.tar.gz` (D-5: version-less teams are latest-only and
never get a pinned, immutable asset).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml


def team_version(team_dir: Path) -> str:
    agent_yaml = team_dir / "agent.yaml"
    if not agent_yaml.exists():
        print(f"team-version: no agent.yaml in {team_dir}", file=sys.stderr)
        raise SystemExit(2)
    data = yaml.safe_load(agent_yaml.read_text()) or {}
    version = data.get("version")
    if version is None or str(version).strip() == "":
        return ""
    return str(version).strip()


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: team-version.py TEAM_DIR", file=sys.stderr)
        return 2
    version = team_version(Path(argv[0]))
    if version:
        print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
