#!/usr/bin/env python3
"""Reject a team that cannot be published, before it is packaged (#446 §7.1).

A team's `from:` may be a registry ref (`name` / `name@version`) or a path
override (`../eng-team`, `/abs`, `~/x`). A path override is **local-only** —
a consumer's checkout has no such path, so a published tarball carrying one is
broken on arrival. Mirroring Go `replace` / Cargo `[patch]` (which never leak
into published artifacts), packaging must reject it.

Exits non-zero (aborting the build under `set -e`) if the team's agent.yaml
declares a path-based `from:`. A registry ref or no `from:` passes silently.

Kept dependency-free (yaml only, no `bobi` import) so it runs in a bare CI
packaging step; the rule mirrors `bobi.compose._is_path_ref` /
`reject_path_from`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml


def _is_path_ref(ref: str) -> bool:
    return ref.startswith((".", "/", "~"))


def check(team_dir: Path) -> int:
    # A missing or unparseable agent.yaml is NOT this guard's concern — the
    # downstream version helper degrades those to rolling-only, and a multi-team
    # build must survive one bad team. We only ever hard-fail on a confirmed
    # path-based `from:`, the one thing that would publish broken.
    agent_yaml = team_dir / "agent.yaml"
    if not agent_yaml.exists():
        return 0
    try:
        data = yaml.safe_load(agent_yaml.read_text()) or {}
    except yaml.YAMLError:
        return 0
    if not isinstance(data, dict):
        return 0
    ref = data.get("from")
    if isinstance(ref, str) and _is_path_ref(ref):
        print(
            f"check-publishable: {team_dir.name}/agent.yaml declares `from: {ref}`, "
            "a path override that cannot be published — a consumer has no such "
            "path. Change it to a `name@version` (or `name`) registry ref before "
            "packaging.", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: check-publishable.py TEAM_DIR", file=sys.stderr)
        return 2
    return check(Path(argv[0]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
