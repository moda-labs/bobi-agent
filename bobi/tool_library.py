"""Reusable dependency library (#428, absorbing #416/#398) — resolve
`tool_library:` entries into the agent.yaml surfaces a dependency contributes,
then let the existing compose merge run.

This is the **hub** of the dependency track. There is **one concept: a
dependency** — a CLI tool, a skill library, a font, or an MCP server are all the
same thing, differing only by which optional fields they carry. There is no
`kind` axis (the discriminator #416 shipped is gone): a dependency is identified
structurally by its fields, not by an author-chosen type.

A dependency has:

- `name`
- `success` (**required**) — the contract a preflight verifies. Prose or shell.
  Run with `BOBI_VERIFY_PHASE=build` it is the *build-success* that gates the
  snapshot; run plain it is the *runtime-success* a warm-boot doctor checks. The
  field is one; the two tiers are phases of evaluating it (the convention the
  migrated `codex` entry already encodes inside its check).
- `guide` (optional) — how to materialize / use it (link or text); becomes
  `tools/<name>.md`.
- `install` (optional) — explicit pinned steps ("do exactly this"), reusing the
  `build:` shape (apt/npm/run_root/run) so the #416 migration is mechanical.
- `host` (optional) — runtime wiring the snapshot cannot hold: host capabilities
  an in-container agent cannot grant itself (a sysctl, a device). Rendered to
  deploy/doctor in Stage 3, never materialized at compose.
- `mcp` (optional) — runtime wiring: an MCP server's connection spec, rendered
  per brain in Stage 4.
- `why` / `fix` (optional) — documentation and a runtime repair hint carried
  through to the legacy `requires:` doctor surface. They keep the migrated #416
  entries byte-identical (the #452 bar); they move to the runtime-success doctor
  in a later stage.

This module does **not** carry its own merge/precedence engine: it splices a
dependency's surfaces into the merged agent.yaml dict (+ `tools/` dir) and the
existing `_merge_keyed_list` / `_merge_build` / leaf-wins-file rules do the rest.
The pin-duplication problem dissolves — the pin is written once per catalog entry
and `_merge_build`'s de-dupe collapses any repeat across `from:` layers.

A `tool_library:` item is either a **string** (a reference to a named catalog
entry under `tool_library/<name>/`) or an **inline mapping** (a dependency
declared directly on the team). The catalog lives as framework package data:
`tool.yaml` (the fields) + `guide.md` (becomes `tools/<name>.md`). Note this data
directory sits beside *this module file of the same stem*: a real
`tool_library.py` module shadows the `tool_library/` namespace-package directory
for imports, while `CATALOG_DIR` points at the directory on disk for data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from bobi import fsutil
from bobi.compose import ComposeError, _merge_build

# Catalog root — one directory per entry, the directory name IS the entry id.
CATALOG_DIR = Path(__file__).parent / "tool_library"

# Written into a composed image: the `tools/<name>.md` guides THIS module
# generated there, by digest. It is what lets a re-compose into a reused dest
# tell its own previous output from a team-shipped file (see
# `_materialize_guides`); absent (a fresh dest), every guide is written.
GUIDE_RECORD = "tool-library-guides.json"


@dataclass
class Dependency:
    """One declared dependency in the unified model (#428).

    `name` + required `success`; everything else optional. `install` reuses the
    `build:` shape (apt/npm/run_root/run). `host`/`mcp` are runtime wiring carried
    through compose (and the dependency-list hash) but rendered by later stages,
    not materialized here. `why`/`fix` feed the legacy `requires:` doctor surface.
    """

    name: str
    success: str
    guide: str = ""
    install: dict = field(default_factory=dict)
    host: list = field(default_factory=list)
    mcp: dict = field(default_factory=dict)
    why: str = ""
    fix: str = ""


def available_entries() -> list[str]:
    """Sorted list of catalog entry ids (directories holding a tool.yaml)."""
    if not CATALOG_DIR.is_dir():
        return []
    return sorted(
        p.name for p in CATALOG_DIR.iterdir()
        if p.is_dir() and (p / "tool.yaml").is_file()
    )


def _build_dependency(data: dict, *, name: str, guide: str,
                      source: str) -> Dependency:
    """Validate a dependency mapping and build a Dependency.

    `success` is required: without it an agent (or a build verify) can declare
    victory on a half-install. `source` names the origin in the error so an author
    can self-correct (a catalog path or an inline declaration)."""
    success = data.get("success")
    if not success or not str(success).strip():
        raise ComposeError(
            f"dependency '{name}' is missing required 'success' ({source})")
    return Dependency(
        name=name,
        success=str(success),
        guide=guide,
        install=dict(data.get("install") or {}),
        host=list(data.get("host") or []),
        mcp=dict(data.get("mcp") or {}),
        why=str(data.get("why") or ""),
        fix=str(data.get("fix") or ""),
    )


def load_entry(name: str) -> Dependency:
    """Load + validate a named catalog dependency. Raises ComposeError
    (human-facing) on an unknown name (listing available entries), a malformed
    tool.yaml, or a missing `success`."""
    entry_dir = CATALOG_DIR / name
    tool_yaml = entry_dir / "tool.yaml"
    if not tool_yaml.is_file():
        avail = ", ".join(available_entries()) or "(none)"
        raise ComposeError(
            f"unknown tool_library entry '{name}' — available: {avail}")
    try:
        data = yaml.safe_load(tool_yaml.read_text()) or {}
    except yaml.YAMLError as e:
        raise ComposeError(
            f"could not parse tool_library entry '{name}': {e}") from e
    if not isinstance(data, dict):
        raise ComposeError(f"tool_library entry '{name}' is not a mapping")
    # The guide is the entry's guide.md text by default; an explicit `guide:` in
    # tool.yaml (a link or inline text) overrides it.
    guide = data.get("guide")
    if guide is None:
        guide_file = entry_dir / "guide.md"
        guide = guide_file.read_text() if guide_file.is_file() else ""
    return _build_dependency(
        data, name=name, guide=str(guide), source=f"tool_library/{name}")


def resolve_dependencies(merged_yaml: dict) -> list[Dependency]:
    """Resolve `merged_yaml`'s declared dependencies into Dependency objects.

    Each `tool_library:` item is a string (catalog ref) or an inline mapping.
    De-duped by name, LAST occurrence wins: compose unions `tool_library:` with
    the overlaying layer's entries appended after the base's, so the last one is
    the leaf's - keeping the first would invert compose's leaf-always-wins rule
    and silently bake a base layer's pin over the overlay's. A repeat across
    `from:` layers still collapses to one; an override replaces the inherited
    dependency wholesale (never field-merged, like the `requires:`/`mcp:` escape
    hatches), so an overlay that re-pins a tool restates the `guide:` it wants.
    Position follows the first occurrence, so an override does not reshuffle the
    accreted build/requires order. NB: `brain: codex` no longer implies a codex
    dependency - the Codex CLI ships in the base image (#428), so a codex-brained
    team bakes nothing extra."""
    resolved: dict[str, Dependency] = {}
    for item in (merged_yaml.get("tool_library") or []):
        if isinstance(item, str):
            dep = load_entry(item)
        elif isinstance(item, dict):
            name = item.get("name")
            if not name:
                raise ComposeError(
                    "inline tool_library dependency is missing required 'name'")
            dep = _build_dependency(
                item, name=str(name), guide=str(item.get("guide") or ""),
                source="inline tool_library")
        else:
            raise ComposeError(
                f"tool_library entry must be a name or mapping, "
                f"got {type(item).__name__}")
        resolved[dep.name] = dep  # last (leaf) wins, first position kept
    return list(resolved.values())


def resolve_team_dependencies(team_dir: Path, project_path: Path) -> list[Dependency]:
    """Resolve a team's full declared dependency set (from-chain + inline).

    The bootstrap harness (#428 Stage 2) needs the same dependency list the
    deploy image bakes: the `from:` chain merged, `tool_library:` unioned across
    layers, before `expand()` consumes the key. Reuses compose's own chain
    resolution + agent.yaml merge (the single merge path) so the harness sees
    exactly what `bobi deploy` composes — a team declaring `tool_library: [venn]`
    two layers up is resolved, not missed.
    """
    # Local import: compose imports this module's merge helpers, so a module-level
    # import would cycle (mirrors build_render.load_composed_team_config).
    from bobi.compose import Provenance, _compose_agent_yaml, resolve_chain

    chain = resolve_chain(team_dir, project_path)
    merged_yaml = _compose_agent_yaml(chain, Provenance())
    return resolve_dependencies(merged_yaml)


def _guide_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_guide_record(dest: Path) -> dict[str, str]:
    """The `tools/<file> → digest` map a previous expansion into `dest` wrote."""
    try:
        data = json.loads((dest / GUIDE_RECORD).read_text())
    except (OSError, ValueError):
        return {}
    guides = data.get("guides") if isinstance(data, dict) else None
    if not isinstance(guides, dict):
        return {}
    return {str(k): str(v) for k, v in guides.items()}


def _write_guide_record(dest: Path, record: dict[str, str]) -> None:
    """Publish the ownership record atomically - a torn one is unrecoverable.

    `_read_guide_record` maps unparseable JSON to `{}`, which reclassifies every
    generated guide as team-shipped: the next compose skips them all, then
    `_write_guide_record(dest, {})` unlinks the record and the loss is permanent.
    """
    path = dest / GUIDE_RECORD
    if not record:
        path.unlink(missing_ok=True)
        return
    fsutil.atomic_write_text(
        path,
        json.dumps({"guides": dict(sorted(record.items()))}, indent=2) + "\n")


def _materialize_guides(deps: list[Dependency], dest: Path) -> None:
    """Write each dependency's `tools/<name>.md` and reconcile the last run's.

    A team-shipped guide always wins (the leaf-wins file rule, applied after the
    structured tools/ merge), so only a file this module generated is refreshed
    or removed - identified by the digest recorded in `GUIDE_RECORD`, since mere
    existence cannot tell a team file from our own leftover. `dest` is REUSED
    across installs and install.py clears only the surfaces some layer
    contributes, so without that record a team shipping no `tools/` dir would
    freeze the first install's guide forever and orphan the guide of a dependency
    it later drops.
    """
    owned = _read_guide_record(dest)
    written: dict[str, str] = {}
    for dep in deps:
        if not dep.guide:
            continue
        guide_path = dest / "tools" / f"{dep.name}.md"
        if guide_path.exists() and _guide_digest(
                guide_path.read_bytes()) != owned.get(guide_path.name):
            continue  # team-shipped (or locally edited) - never clobbered
        guide_path.parent.mkdir(parents=True, exist_ok=True)
        body = dep.guide.encode()
        guide_path.write_bytes(body)
        written[guide_path.name] = _guide_digest(body)

    for name, digest in owned.items():
        if name in written:
            continue
        stale = dest / "tools" / name
        if stale.is_file() and _guide_digest(stale.read_bytes()) == digest:
            stale.unlink()  # ours, and no longer declared
    _write_guide_record(dest, written)


def _expand_dependency(dep: Dependency, merged_yaml: dict) -> None:
    """Splice one dependency's agent.yaml surfaces into the merged config.

    Reuses compose's own merge rules so a dependency behaves exactly like the
    inline surfaces it replaces (the #452 regression bar). Each surface honours an
    escape hatch: an explicit team declaration wins. The `tools/<name>.md` guide
    is materialized separately by `_materialize_guides`.
    """
    # requires: add a {name, why, check, fix} entry only if the team hasn't
    # already declared this name. An explicit team `requires:` (already merged as
    # the leaf) therefore wins wholesale — a deliberate override, never silently
    # field-merged. Key order matches the hand-inline form for byte-identity.
    existing = merged_yaml.get("requires") or []
    present = {r.get("name") for r in existing if isinstance(r, dict)}
    if dep.name not in present:
        entry: dict = {"name": dep.name}
        if dep.why:
            entry["why"] = dep.why
        entry["check"] = dep.success
        if dep.fix:
            entry["fix"] = dep.fix
        merged_yaml["requires"] = list(existing) + [entry]

    # install: accrete + de-dupe via the SAME _merge_build compose uses — identical
    # pins across dependencies/layers collapse to one string. This is the core fix.
    if dep.install:
        merged_yaml["build"] = _merge_build(merged_yaml.get("build"), dep.install)

    # host: runtime wiring surfaced to deploy/doctor (#428 Stage 3). Emitted into
    # the composed agent.yaml as a top-level `host:` list — accreted, de-duped by
    # the entry's `key=value` spec, and skipped when the team already declares the
    # same capability inline (leaf wins). Carried into the frozen config so the
    # runtime doctor and deploy can verify/provision it. NOT materialized into the
    # image (a snapshot cannot hold a host capability); it is provisioned per host.
    if dep.host:
        def _sysctl_key(entry) -> str | None:
            # The dedup identity is the sysctl KEY, not the whole key=value: two
            # entries setting the same knob to different values are a conflict, not
            # two capabilities, so only one may win (leaf/first). Non-sysctl kinds
            # have no key to dedup on and pass through untouched.
            if isinstance(entry, dict) and "sysctl" in entry:
                return str(entry["sysctl"]).split("=", 1)[0].strip()
            return None

        existing_host = list(merged_yaml.get("host") or [])
        seen_keys = {k for k in map(_sysctl_key, existing_host) if k}
        additions: list = []
        for entry in dep.host:
            key = _sysctl_key(entry)
            if key is None:
                additions.append(entry)  # unknown kind — keep as-is for Stage-N
                continue
            if key in seen_keys:
                continue  # already set (inline team or an earlier dep) — leaf wins
            seen_keys.add(key)
            additions.append(entry)
        if additions:
            merged_yaml["host"] = existing_host + additions

    # mcp: an MCP server's connection spec (#428 Stage 4). Emitted into the
    # composed agent.yaml as a top-level `mcp_servers:` dict (the SDK-native
    # `{name: spec}` shape). Merged per-server-name, leaf-wins: an explicit team
    # `mcp_servers.<name>` (already merged as the leaf, last-wins) overrides a
    # dependency's `mcp:` for that name wholesale — a deliberate override, never
    # field-merged. Two dependencies contributing the same name → first wins
    # (mirrors the requires/host escape hatches above). The existing subagent.py
    # pass-through carries it to Claude unchanged; the codex brain renders it into
    # `~/.codex/config.toml` at runtime.
    if dep.mcp:
        existing_mcp = dict(merged_yaml.get("mcp_servers") or {})
        for server_name, spec in dep.mcp.items():
            if server_name in existing_mcp:
                continue  # team or an earlier dep already declared it — leaf wins
            existing_mcp[server_name] = spec
        merged_yaml["mcp_servers"] = existing_mcp


def expand(merged_yaml: dict, dest: Path) -> None:
    """Expand `merged_yaml['tool_library']` in place, then drop the key.

    Resolves each entry (catalog ref or inline mapping) to a Dependency and
    splices its surfaces in. Idempotent and pure over inputs: an empty/absent
    `tool_library` is a no-op, and re-expanding into a dest a previous run wrote
    converges on the same tools/ (see `_materialize_guides`). `tool_library` is
    consumed at compose, never emitted (like `from`/`prune`).
    """
    deps = resolve_dependencies(merged_yaml)
    for dep in deps:
        _expand_dependency(dep, merged_yaml)
    _materialize_guides(deps, dest)
    merged_yaml.pop("tool_library", None)


def dependency_list_hash(deps: list[Dependency]) -> str:
    """Stable short hash of the declared dependency set — the re-bootstrap key.

    Mirrors `build_render.team_deps_hash`: keyed on what actually changes what a
    bootstrap would materialize and verify (name/success/guide/install) and how it
    is wired at runtime (host/mcp), so a warm boot whose hash matches the snapshot
    skips bootstrap and a changed set triggers re-bootstrap. Order-independent
    (sorted by name): the set's identity, not its declaration order. `why`/`fix`
    are documentation and excluded — they do not change materialization.
    """
    payload = json.dumps(
        sorted(
            (
                {
                    "name": d.name,
                    "success": d.success,
                    "guide": d.guide,
                    "install": d.install,
                    "host": d.host,
                    "mcp": d.mcp,
                }
                for d in deps
            ),
            key=lambda d: d["name"],
        ),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]
