"""Install a team pack into a runtime's frozen package image.

Extracted from the CLI (#525) so every install caller — `bobi agents
install`, the setup web UI, and the unified webapp — shares one code path.
The CLI re-exports these under their old private names for back-compat.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import yaml

from bobi import paths


def install_pack(pack_dir: Path, project_path: Path,
                 local_source: bool = True, *, pinned: bool = False) -> None:
    """Compose the pack's `from:` chain into run/package/ for runtime use.

    The installed copy is a frozen runtime image: install regenerates it
    verbatim from the pack source every time, including agent.yaml.
    Machine- and runtime-specific variance enters through ${VAR}
    references resolved from run/.env, never by editing the
    installed copy.

    A team may declare `from: <base-team>`; compose then walks the chain
    (base → … → leaf) and merges the layers into one flat image (#446/#451).
    A team with no `from:` composes to a single-layer image identical in
    content to the team itself — the common case is unchanged. ``pinned``
    resolves the chain registry-only at locked versions (CI/deploy).
    """
    from bobi import compose as _compose
    from bobi.runtime_guard import with_mutable_runtime_package

    dest = paths.package_dir(project_path)
    dest.mkdir(parents=True, exist_ok=True)

    with with_mutable_runtime_package(project_path):
        locked = read_compose_lock(dest) if pinned else None
        chain = _compose.resolve_chain(pack_dir, project_path, pinned=pinned,
                                       locked=locked)

        # Clear the previously frozen copy of each surface the composed chain
        # contributes, so a re-install drops stale files (e.g. a tool the new chain
        # no longer ships). A surface NO layer contributes is left untouched — the
        # pre-compose install semantics — so package-added files (e.g. an extra
        # `package/workflows/*.yaml`) survive a reinstall of the same team.
        contributed = {sub for layer in chain
                       for sub in ["roles", "tools", "workflows", "monitors", "context"]
                       if (layer.dir / sub).is_dir()}
        for sub in contributed:
            d = dest / sub
            if d.exists():
                shutil.rmtree(d)

        prov = _compose.compose(chain, dest)

        # Seed workspace leaf → base (seed-if-absent), so an overlay's own template
        # wins and the base only fills files the overlay doesn't supply. (Mirrors the
        # deploy flatten's leaf-wins merge_workspace; user edits already on disk are
        # never overwritten regardless of order.)
        for layer in reversed(chain):
            seed_workspace(layer.dir, project_path)

        # The leaf's directory name is the installed agent name (the team a user
        # named on the CLI), regardless of how deep its `from:` chain runs.
        cfg = _read_yaml(dest / "agent.yaml")
        cfg.setdefault("agent", pack_dir.name)
        _write_yaml(dest / "agent.yaml", cfg)

        write_compose_lock(dest, chain, prov)
        write_install_manifest(dest, pack_dir, local_source)


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


def read_compose_lock(dest: Path) -> dict[str, str]:
    """The {team_name: version} map recorded by the last compose, used by
    `install --pinned` to pin otherwise-floating `latest` refs reproducibly."""
    f = dest / "compose-lock.json"
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError):
        return {}
    locked = {}
    for layer in data.get("chain", []):
        ref, ver = layer.get("ref"), layer.get("version")
        if ref and ver:
            name = ref.split("@", 1)[0]
            locked[name] = ver
    return locked


def write_compose_lock(dest: Path, chain, prov) -> None:
    """Record the resolved `from:` chain + provenance so a deploy/outside-org
    install is reproducible and `doctor` can flag a drifted local sibling."""
    (dest / "compose-lock.json").write_text(json.dumps({
        "chain": prov.chain,
        "provenance": prov.items,
        "warnings": prov.warnings,
    }, indent=1))


def seed_workspace(pack_dir: Path, project_path: Path) -> None:
    """Seed <run>/workspace/ from the pack's workspace/ templates.

    Workspace files are user-owned domain content (context the user fills
    in, directories agents write into). Unlike the frozen image, each file
    is copied only if absent — reinstall never overwrites user edits.
    """
    src = pack_dir / "workspace"
    if not src.is_dir():
        return
    dest = paths.workspace_dir(project_path)
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src)
        target = dest / rel
        if f.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)


def write_install_manifest(dest: Path, pack_dir: Path,
                           local_source: bool) -> None:
    """Record a hash of every installed file so doctor can flag drift.

    Edits to a frozen image are lost on the next install; the manifest
    lets `bobi agent <name> doctor` warn before that happens.
    """
    files = {}
    for subdir in ["roles", "tools", "workflows", "monitors", "context"]:
        d = dest / subdir
        if d.is_dir():
            for f in sorted(d.rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts:
                    files[f.relative_to(dest).as_posix()] = \
                        hashlib.sha256(f.read_bytes()).hexdigest()
    for name in ["agent.yaml", "agent.md", "AGENTS.md"]:
        f = dest / name
        if f.exists():
            files[name] = hashlib.sha256(f.read_bytes()).hexdigest()

    (dest / "install-manifest.json").write_text(json.dumps({
        "agent": pack_dir.name,
        "source": str(pack_dir),
        "frozen": local_source,
        "files": files,
    }, indent=1))


def write_install_gitignore(project_path: Path, local_source: bool) -> None:
    """Write package/.gitignore based on which install path was taken.

    Runtime state is always ignored. When the team source lives in the
    repo (local source of truth), the installed copies are build artifacts
    and get ignored too. A downloaded team's installed copy IS the source
    of truth, so it stays check-in-able. Install owns this file and
    rewrites it each run so switching paths doesn't leave stale entries.
    """
    entries = [".gitignore", "install-manifest.json", "compose-lock.json"]
    if local_source:
        entries += ["roles/", "tools/", "workflows/", "monitors/", "context/",
                    "agent.md", "agent.yaml", "AGENTS.md"]
    from bobi.runtime_guard import with_mutable_runtime_package

    with with_mutable_runtime_package(project_path):
        gitignore = paths.package_dir(project_path) / ".gitignore"
        gitignore.write_text("\n".join(entries) + "\n")
