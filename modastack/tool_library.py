"""Reusable tool library (#416) — resolve `tool_library: [names]` into the
agent.yaml surfaces a tool contributes, then let the existing compose merge run.

This is the **hub** of the capability-library track. A catalog entry is a bundle
of three surfaces — `requires:`, `build:`, and a `tools/<name>.md` guide — that
already merge in `compose.py`. So this module does **not** carry its own
merge/precedence engine: it splices an entry's surfaces into the merged agent.yaml
dict (+ `tools/` dir) and the existing `_merge_keyed_list` / `_merge_build` /
leaf-wins-file rules do the rest. The pin-duplication problem dissolves — the pin
is written once per catalog entry and `_merge_build`'s de-dupe collapses any
repeat across `from:` layers.

`expand()` is **kind-dispatched** via the `EXPANDERS` table (D-5), not
cli-hardcoded. `kind: cli` is the only expander this PR ships; #428 registers
`_expand_skill` and #398 registers `_expand_mcp` (plus its own compose merge
branch) as **additive** changes — neither edits `expand()`. An entry whose kind
has no registered expander raises a `ComposeError` naming the kind and its owning
issue, so the seam is real from day one rather than a hardcoded cli reject.

The catalog lives as framework package data under `tool_library/<name>/` —
`tool.yaml` (the surfaces) + `guide.md` (becomes `tools/<name>.md`). Note this
data directory sits beside *this module file of the same stem*: a real
`tool_library.py` module shadows the `tool_library/` namespace-package directory
for imports, while `CATALOG_DIR` points at the directory on disk for data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from modastack.compose import ComposeError, _dedupe, _merge_build

# Catalog root — one directory per entry, the directory name IS the entry id.
CATALOG_DIR = Path(__file__).parent / "tool_library"

# Reserved kinds not yet implemented, and the issue that owns each spoke. Used to
# point an author at the right ticket when an entry's kind has no expander.
_KIND_OWNER = {
    "mcp": "#398",
    "skill": "#428",
}


@dataclass
class ToolEntry:
    name: str
    kind: str
    requires: list[dict]
    build: dict
    guide: str  # guide.md text → tools/<name>.md


def available_entries() -> list[str]:
    """Sorted list of catalog entry ids (directories holding a tool.yaml)."""
    if not CATALOG_DIR.is_dir():
        return []
    return sorted(
        p.name for p in CATALOG_DIR.iterdir()
        if p.is_dir() and (p / "tool.yaml").is_file()
    )


def load_entry(name: str) -> ToolEntry:
    """Load + validate a catalog entry. Raises ComposeError (human-facing) on an
    unknown name (listing available entries) or a malformed tool.yaml."""
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
    kind = data.get("kind")
    if not kind:
        raise ComposeError(
            f"tool_library entry '{name}' is missing required 'kind'")
    guide_file = entry_dir / "guide.md"
    guide = guide_file.read_text() if guide_file.is_file() else ""
    return ToolEntry(
        name=name,
        kind=str(kind),
        requires=list(data.get("requires") or []),
        build=dict(data.get("build") or {}),
        guide=guide,
    )


# --- per-kind expanders (the seam the spokes plug into) ----------------------


def _expand_cli(entry: ToolEntry, merged_yaml: dict, dest: Path) -> None:
    """Splice a `kind: cli` entry's surfaces into the merged agent.yaml + tools/.

    Reuses compose's own merge rules so a tool entry behaves exactly like the
    inline surfaces it replaces (the #452 regression bar). Each surface honours an
    escape hatch: an explicit team declaration wins.
    """
    # requires: add the entry only if its name isn't already present. An explicit
    # team `requires:` for the same name (already merged as the leaf) therefore
    # wins wholesale — a deliberate override, never silently field-merged.
    if entry.requires:
        existing = merged_yaml.get("requires") or []
        present = {
            r.get("name") for r in existing if isinstance(r, dict)
        }
        additions = [
            r for r in entry.requires
            if not (isinstance(r, dict) and r.get("name") in present)
        ]
        if additions:
            merged_yaml["requires"] = list(existing) + additions

    # build: accrete + de-dupe via the SAME _merge_build compose uses — identical
    # pins across entries/layers collapse to one string. This is the core fix.
    if entry.build:
        merged_yaml["build"] = _merge_build(merged_yaml.get("build"), entry.build)

    # guide: write tools/<name>.md only if the team didn't already ship one
    # (consistent with the leaf-wins file rule after the structured tools/ merge).
    if entry.guide:
        guide_path = dest / "tools" / f"{entry.name}.md"
        if not guide_path.exists():
            guide_path.parent.mkdir(parents=True, exist_ok=True)
            guide_path.write_text(entry.guide)


# New kinds register here — #428 adds "skill", #398 adds "mcp" — WITHOUT touching
# expand(). That additivity is the whole point of the kind discriminator (D-5).
EXPANDERS: dict[str, Callable[[ToolEntry, dict, Path], None]] = {
    "cli": _expand_cli,
}


def expand(merged_yaml: dict, dest: Path) -> None:
    """Expand `merged_yaml['tool_library']` in place, then drop the key.

    Dispatches each named entry to `EXPANDERS[entry.kind]`. Raises a ComposeError
    naming the unsupported kind + its owning issue when no expander exists.
    Idempotent and pure over inputs: an empty/absent `tool_library` is a no-op.
    `tool_library` is consumed at compose, never emitted (like `from`/`prune`).
    """
    names = _library_names_for_config(merged_yaml)
    for name in names:
        entry = load_entry(name)
        expander = EXPANDERS.get(entry.kind)
        if expander is None:
            owner = _KIND_OWNER.get(entry.kind)
            hint = f" (see {owner})" if owner else ""
            raise ComposeError(
                f"tool '{name}' is kind '{entry.kind}' — not yet implemented"
                f"{hint}")
        expander(entry, merged_yaml, dest)
    merged_yaml.pop("tool_library", None)


def _library_names_for_config(merged_yaml: dict) -> list[str]:
    """Return explicit tool-library names plus brain-implied entries.

    `brain.kind: codex` needs the Codex CLI available for auth bootstrap and turn
    execution, so it is equivalent to an implicit `tool_library: [codex]`.
    Explicit `tool_library: [codex]` still de-dupes through the same path. If a
    team has already declared its own Codex check/build, treat that as the local
    override and do not add the implicit catalog pin.
    """
    names = list(merged_yaml.get("tool_library") or [])
    brain = merged_yaml.get("brain") or {}
    kind = str(brain.get("kind", "") or "") if isinstance(brain, dict) else ""
    if (
        kind == "codex"
        and "codex" not in names
        and not _has_codex_override(merged_yaml)
    ):
        names.append("codex")
    return _dedupe(names)


def _has_codex_override(merged_yaml: dict) -> bool:
    requires = merged_yaml.get("requires") or []
    if any(isinstance(r, dict) and r.get("name") == "codex" for r in requires):
        return True
    build = merged_yaml.get("build") or {}
    if not isinstance(build, dict):
        return False
    values = []
    for key in ("npm", "run_root", "run"):
        raw = build.get(key) or []
        values.extend(raw if isinstance(raw, list) else [raw])
    return any("codex" in str(v) for v in values)
