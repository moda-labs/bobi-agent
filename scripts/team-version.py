#!/usr/bin/env python3
"""Print a team's published version (its agent.yaml `version:`), or nothing.

Used by build-team-tarballs.sh to name the immutable per-team package
(`<team>-<version>.tar.gz`). YAML parsing lives here in Python rather than in
bash (brittle) so the shell script stays dumb.

This helper is the single authority on what counts as a pinnable version: it
prints a version ONLY if it is strict `MAJOR.MINOR.PATCH` semver. Anything else
(absent, prerelease `1.2.0-rc1`, partial `1.0`, a typo, unparseable YAML) prints
nothing and exits 0, so the caller publishes only the rolling `<team>.tar.gz`
(D-5: version-less / non-conforming teams are latest-only, never pinned).

Strictness matters: the publisher classifies a tarball as immutable-versioned vs
rolling by matching the `-X.Y.Z.tar.gz` suffix. If a non-`X.Y.Z` version became a
filename, the publisher would treat it as rolling and upload it WITH `--clobber`,
silently destroying the immutability guarantee. Gating here keeps the build
output and the publisher's classifier in lockstep.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def team_version(team_dir: Path) -> str:
    agent_yaml = team_dir / "agent.yaml"
    if not agent_yaml.exists():
        print(f"team-version: no agent.yaml in {team_dir}", file=sys.stderr)
        raise SystemExit(2)
    try:
        data = yaml.safe_load(agent_yaml.read_text()) or {}
    except yaml.YAMLError as exc:
        print(f"team-version: warning: {team_dir.name} has unparseable "
              f"agent.yaml ({exc.__class__.__name__}) — rolling tarball only",
              file=sys.stderr)
        return ""
    version = data.get("version")
    if version is None or str(version).strip() == "":
        return ""
    version = str(version).strip()
    if not SEMVER.match(version):
        print(f"team-version: warning: {team_dir.name} version {version!r} is "
              f"not strict MAJOR.MINOR.PATCH — rolling tarball only (not pinnable)",
              file=sys.stderr)
        return ""
    return version


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
