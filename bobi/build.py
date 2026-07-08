"""Team package resolution - the shared seam under build and deploy consumers.

`resolve_team_dir` turns a team ref (a path, a registry `name[@version]` pin,
or a bare name under `agents/`) into a **flat, ready-to-ship** package dir.
Every consumer that stages, scans, hashes, or ships a team package routes
through it (D-2), so they all see the same package: a `from:` chain is
composed (flattened) here on the host, and downstream consumers never resolve
a chain themselves.

The image engine that used to live here (`bobi build`, #610) moved behind the
deploy-plugin boundary (#707): building container images is deployment
mechanics, delivered by the separately installable deploy plugin. This module
keeps only
what the public product itself needs - resolution stays public; the plugin
imports downward (never the reverse; tests/test_import_boundaries.py).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


class BuildError(RuntimeError):
    """A team-image build failed."""


def resolve_team_dir(project_path: Path, team: str) -> Path:
    """Resolve a team ref to a **flat, ready-to-ship** package dir.

    The single seam every build/deploy consumer routes through (D-2) — secret-
    prune scan, deps-render, deps-hash, AND the ssh-push — so they all see the
    same package. A team that declares `from:` is **composed (flattened) here**
    (#446/#451): the base is resolved on the host (which has registry access),
    the chain is merged into a staging dir with no `from:`, and every downstream
    consumer sees the merged build/secrets. The pushed tarball is already flat,
    so the instance never resolves a chain at first boot.
    """
    src = _resolve_team_package(project_path, team)
    return _flatten_if_chained(project_path, src)


def _flatten_if_chained(project_path: Path, team_dir: Path) -> Path:
    """Compose a `from:` chain into a flat staging dir; pass through otherwise.

    A team with no `from:` is returned unchanged (today's behavior, byte-for-byte).
    Composition is deterministic, so the repeated `resolve_team_dir` calls across
    one deploy each produce the same staged image."""
    from bobi import compose, paths
    try:
        has_from = bool((compose._read_agent_yaml(team_dir)).get("from"))
    except compose.ComposeError:
        return team_dir
    if not has_from:
        return team_dir
    chain = compose.resolve_chain(team_dir, project_path)
    staged = paths.build_cache_dir() / "composed" / f"composed-{team_dir.name}"
    staged.parent.mkdir(parents=True, exist_ok=True)
    if staged.exists():
        shutil.rmtree(staged)
    compose.compose(chain, staged)
    # compose() doesn't freeze workspace/ (it's the seed-if-absent surface for a
    # local install), but the flat tarball IS what reaches the instance — so carry
    # the chain's merged workspace (leaf-wins) so an overlay's per-principal
    # assistant-context.md actually ships.
    compose.merge_workspace(chain, staged)
    # Preserve the leaf's directory name so the app/tarball naming is unchanged.
    cfg = compose._read_agent_yaml(staged)
    cfg.setdefault("agent", team_dir.name)
    (staged / "agent.yaml").write_text(
        yaml.dump(cfg, default_flow_style=False, sort_keys=False))
    final = staged.parent / team_dir.name
    if final.exists():
        shutil.rmtree(final)
    staged.rename(final)
    return final


def _resolve_team_package(project_path: Path, team: str) -> Path:
    """Resolve a team ref (optionally `name@version`) to a package dir.

    After resolution the package is on disk, so the existing local-team
    build/prune/push path runs unchanged. Resolution order:

      1. explicit `@version` → fetch the immutable per-team asset into the
         **shared** install/deploy cache (D-3). A pin **never** falls back to a
         local dir: a stale `agents/<name>` must not silently shadow the pin, and
         a missing asset is a hard error (surfaced by registry.fetch).
      2. bare name with a local `agents/<name>` / `<name>` dir → use it (today's
         behavior, byte-for-byte unchanged — local dev keeps working).
      3. bare name, no local dir → fetch latest into the shared cache.
    """
    from bobi import registry
    # A team ref that is itself a path to a package dir wins literally — avoids
    # mis-splitting a filesystem path that happens to contain '@'
    # (e.g. `/work@v2/eng-team`).
    if (Path(team) / "agent.yaml").exists():
        return Path(team).resolve()
    name, version = registry.split_team_ref(team)
    if version:
        # Reuse an already-cached pin with no second download (§3.4); the
        # immutable asset makes the cached copy authoritative.
        if (registry.cached_version(project_path, name) == version
                and registry.is_cached(project_path, name)):
            return registry.cache_path(project_path, name)
        try:
            return registry.fetch(project_path, name, version=version)
        except Exception as e:
            raise BuildError(
                f"could not resolve pinned team '{name}@{version}': {e}"
            ) from e
    # Bare name: a local checkout wins (unchanged).
    for cand in (project_path / "agents" / name, project_path / name):
        if (cand / "agent.yaml").exists():
            return cand.resolve()
    try:
        return registry.fetch(project_path, name)
    except Exception as e:
        raise BuildError(
            f"local team '{name}' not found and could not fetch it from the "
            f"registry: {e}"
        ) from e
