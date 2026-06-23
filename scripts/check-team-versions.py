#!/usr/bin/env python3
"""CI guard: each team's agent.yaml `version:` must equal its registry.yaml entry.

`registry.yaml` is the authoritative "latest published" pointer; each team's
`agent.yaml` is the source of the version. They MUST agree, or the published
"latest" lies and an unpinned `install` resolves a version with no asset. Run on
every PR and push as a step in `.github/workflows/team-packages.yml`.

Per D-4 / spec §7, this lives in a standalone helper (invoked from the workflow),
NOT in tests/test_packaging.py — that file is touched by the open #438 and must
not collide.

Usage:  check-team-versions.py [AGENTS_DIR]   (default: <repo>/agents)
Exit 0 = all agree.  Exit 1 = drift (names the team and both values).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml


def check(agents_dir: Path) -> list[str]:
    registry = yaml.safe_load((agents_dir / "registry.yaml").read_text()) or {}
    entries = registry.get("agents") or {}
    errors: list[str] = []
    for team, meta in entries.items():
        reg_version = str((meta or {}).get("version", "")).strip()
        agent_yaml = agents_dir / team / "agent.yaml"
        if not agent_yaml.exists():
            errors.append(
                f"{team}: listed in registry.yaml but no agents/{team}/agent.yaml")
            continue
        data = yaml.safe_load(agent_yaml.read_text()) or {}
        agent_version = str(data.get("version", "")).strip()
        if not agent_version:
            errors.append(
                f"{team}: registry.yaml pins version {reg_version!r} but "
                f"agent.yaml has no version (a pinned team must declare one)")
        elif agent_version != reg_version:
            errors.append(
                f"{team}: version drift — registry.yaml={reg_version!r} "
                f"agent.yaml={agent_version!r} (bump both together)")
    return errors


def main(argv: list[str]) -> int:
    agents_dir = (
        Path(argv[0]) if argv
        else Path(__file__).resolve().parents[1] / "agents"
    )
    errors = check(agents_dir)
    if errors:
        print("Team version agreement check FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print(f"OK: every team's agent.yaml version agrees with registry.yaml ({agents_dir})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
