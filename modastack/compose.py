"""Compose a `from:` team-inheritance chain into one flat frozen image.

Implements the *compose* step of agent-team distribution:

  * #446 — resolution: given `from: <ref>` in a team's agent.yaml, find the base
    team's source, validate its version, walk the chain bottom-up, and freeze a
    reproducible ordered list of layers (local-always-wins + fail-fast).
  * #451 — merge: flatten that ordered chain (`base → … → leaf`) into one
    directory by two rules — *prose* surfaces concatenate in chain order;
    *structured* surfaces deep-merge by key. The leaf always wins.

`install` and `deploy` both call `compose()` to flatten a chain into a single
directory with no `from:`. Nothing downstream (runtime resolver, container
build) ever learns about layers — the output is one flat team, exactly as a
hand-written monolithic team would be.

See `docs/specs/team-from-resolution.md` (#446) and
`docs/specs/team-compose-merge.md` (#451).
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from modastack import registry

log = logging.getLogger(__name__)

# Surfaces composed by **prose** rule (concatenate in chain order). Everything
# else under a team dir is composed by the **structured** rule (deep-merge).
PROSE_AGENT_MD = "agent.md"

# Structured directory surfaces and their per-file merge key.
STRUCTURED_DIRS = ("tools", "workflows", "monitors", "context")

# The agent.yaml `build` block: list subkeys accrete (append + de-dupe across
# layers); every other subkey is a scalar that the last layer wins.
BUILD_LIST_KEYS = ("apt", "npm", "run_root", "run")

# Guard rails for `from:` chain recursion.
MAX_CHAIN_DEPTH = 16


class ComposeError(RuntimeError):
    """A `from:` chain could not be resolved or composed. The message is meant
    to be shown verbatim to a human (Cargo-quality), so build it with care."""


@dataclass
class ResolvedLayer:
    """One link in a resolved `from:` chain.

    `ref` is the `from:` string that produced this layer (None for the leaf —
    the team actually being installed). `source` is how it was found:
    ``leaf`` / ``path`` / ``local-source`` / ``cache`` / ``registry``.
    """

    dir: Path
    ref: str | None
    version: str | None
    source: str


# --- resolution (#446) -------------------------------------------------------


def _read_agent_yaml(team_dir: Path) -> dict:
    f = team_dir / "agent.yaml"
    if not f.is_file():
        raise ComposeError(
            f"team directory {team_dir} has no agent.yaml — not a team package."
        )
    try:
        return yaml.safe_load(f.read_text()) or {}
    except yaml.YAMLError as e:
        raise ComposeError(f"could not parse {f}: {e}") from e


def _read_version(team_dir: Path) -> str | None:
    try:
        return _read_agent_yaml(team_dir).get("version")
    except ComposeError:
        return None


def _is_path_ref(ref: str) -> bool:
    """A `from:` that points at a filesystem path (override hatch), not a
    registry name. Mirrors Cargo `[patch]` / Go `replace`: local-only, never
    published (rejected at packaging — see `reject_path_from` / §7.1)."""
    return ref.startswith((".", "/", "~"))


def resolve_team_ref(
    ref: str,
    referrer_dir: Path,
    project_path: Path,
    *,
    pinned: bool = False,
) -> ResolvedLayer:
    """Resolve a single `from:` ref to a base-team source directory.

    Policy (#446 §4–§5), layered over the landed registry primitives:

      * **path ref** (`../core`, `/abs`, `~/x`) — resolved relative to the
        *referring team's own directory* (so the team stays portable). No
        version assertion. Rejected in ``pinned`` mode (a path override must
        never bleed into a reproducible CI/deploy image).
      * **name / name@version** — local-always-wins search: checked-in
        ``agents/<name>`` source, then the install/deploy cache, then the
        registry. An exact pin that disagrees with a chosen *local* version
        **fails fast** (no silent fall-through to the registry).

    In ``pinned`` mode local sources are skipped entirely — resolution is
    registry-only at the ref's version (or latest), so a stray checked-out
    sibling cannot leak into a published image (Cargo ``--locked`` / ``npm ci``).
    """
    if _is_path_ref(ref):
        if pinned:
            raise ComposeError(
                f"`from: {ref}` is a path override, which is not allowed under "
                "`--pinned` (reproducible CI/deploy installs resolve from the "
                "registry only). Change it to a `name@version` ref, or run a "
                "normal install."
            )
        base = Path(ref).expanduser()
        if not base.is_absolute():
            base = (referrer_dir / base).resolve()
        if not (base / "agent.yaml").is_file():
            raise ComposeError(
                f"`from: {ref}` (required by {referrer_dir.name}) resolves to "
                f"{base}, which has no agent.yaml."
            )
        return ResolvedLayer(dir=base, ref=ref, version=_read_version(base),
                             source="path")

    name, version = registry.split_team_ref(ref)

    if pinned:
        # Registry-only. An explicit pin fetches that immutable asset; a "latest"
        # ref fetches the rolling asset. (A compose-lock can pin "latest" to a
        # locked version upstream — see compose()'s lock handling.)
        fetched = registry.fetch(project_path, name, version=version)
        return ResolvedLayer(dir=fetched, ref=ref,
                             version=version or registry.cached_version(project_path, name),
                             source="registry")

    # 1. Checked-in sibling source — local always wins.
    local = project_path / "agents" / name
    if (local / "agent.yaml").is_file():
        local_v = _read_version(local)
        _assert_pin(ref, name, version, local_v, local, referrer_dir)
        return ResolvedLayer(dir=local, ref=ref, version=local_v,
                             source="local-source")

    # 2. Already-fetched cache. Version comes from .meta.json, falling back to
    #    the cached agent.yaml (spec §4) for a cache stamped without one.
    if registry.is_cached(project_path, name):
        cache_dir = registry.cache_path(project_path, name)
        cached_v = registry.cached_version(project_path, name) or _read_version(cache_dir)
        _assert_pin(ref, name, version, cached_v, cache_dir, referrer_dir)
        return ResolvedLayer(dir=cache_dir, ref=ref, version=cached_v,
                             source="cache")

    # 3. Registry fetch (immutable asset for a pin; rolling for latest).
    fetched = registry.fetch(project_path, name, version=version)
    return ResolvedLayer(
        dir=fetched, ref=ref,
        version=version or registry.cached_version(project_path, name),
        source="registry")


def _assert_pin(ref: str, name: str, pin: str | None, found: str | None,
                local_dir: Path, referrer_dir: Path) -> None:
    """Fail fast when an exact pin disagrees with a chosen local source (§5).

    "latest" (pin is None) asserts nothing — local wins as-is. An exact pin that
    a local source violates is an error with **no fall-through** to the registry:
    falling through would hide a genuine inconsistency behind a surprise fetch.
    """
    if pin is None or found == pin:
        return
    raise ComposeError(
        f"cannot resolve `from: {ref}` (required by {referrer_dir.name})\n"
        f"  local source {local_dir} is version {found}, but the pin requires "
        f"exactly {pin}\n"
        f"  to proceed, either:\n"
        f"    • use a path ref to override deliberately:  from: ../{name}\n"
        f"    • change the pin to match local:             from: {name}@{found} "
        f"(or `{name}` for latest)\n"
        f"    • bump {local_dir} to version {pin}"
    )


def resolve_chain(
    leaf_dir: Path,
    project_path: Path,
    *,
    pinned: bool = False,
    locked: dict[str, str] | None = None,
) -> list[ResolvedLayer]:
    """Walk a team's `from:` chain and return it **base-first → leaf-last**.

    The leaf (the team being installed) is discovered first and the deepest base
    last, then reversed — so the returned order is the compose precedence order
    (later layers win). Each link resolves independently through
    `resolve_team_ref`. A cycle (A→B→A) or absurd depth is a hard error.

    ``locked`` (compose-lock) maps a team name to a version; in ``pinned`` mode a
    "latest" ref for a locked name is fetched at the locked version, making a
    pinned install bit-for-bit reproducible.
    """
    chain: list[ResolvedLayer] = [
        ResolvedLayer(dir=leaf_dir.resolve(), ref=None,
                      version=_read_version(leaf_dir), source="leaf")
    ]
    seen: set[Path] = {leaf_dir.resolve()}

    cur = leaf_dir
    depth = 0
    while True:
        from_ref = _read_agent_yaml(cur).get("from")
        if not from_ref:
            break
        if not isinstance(from_ref, str):
            raise ComposeError(
                f"`from:` in {cur / 'agent.yaml'} must be a string, got "
                f"{type(from_ref).__name__}."
            )
        depth += 1
        if depth > MAX_CHAIN_DEPTH:
            trail = " -> ".join(str(l.dir) for l in chain)
            raise ComposeError(
                f"`from:` chain exceeds the max depth of {MAX_CHAIN_DEPTH}: "
                f"{trail} -> {from_ref}"
            )
        ref = _apply_lock(from_ref, locked, pinned)
        layer = resolve_team_ref(ref, referrer_dir=cur, project_path=project_path,
                                 pinned=pinned)
        resolved = layer.dir.resolve()
        if resolved in seen:
            trail = " -> ".join(str(l.dir) for l in chain)
            raise ComposeError(
                f"`from:` chain has a cycle: {trail} -> {resolved}"
            )
        seen.add(resolved)
        chain.append(layer)
        cur = layer.dir

    chain.reverse()  # base-first → leaf-last (compose precedence order)
    return chain


def _apply_lock(ref: str, locked: dict[str, str] | None, pinned: bool) -> str:
    """In pinned mode, pin a "latest" ref to its compose-locked version."""
    if not (pinned and locked) or _is_path_ref(ref):
        return ref
    name, version = registry.split_team_ref(ref)
    if version is None and name in locked:
        return f"{name}@{locked[name]}"
    return ref


# --- merge (#451) ------------------------------------------------------------


@dataclass
class Provenance:
    """Records which layer each composed item came from, for `doctor` / debugging
    and so a surprising override is traceable. Also accrues non-fatal warnings
    (a `replace:` with no base, a `prune:` that matched nothing)."""

    items: dict[str, str] = field(default_factory=dict)
    chain: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def record(self, key: str, layer_label: str) -> None:
        self.items[key] = layer_label

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        log.warning("compose: %s", msg)


def _layer_label(layer: ResolvedLayer) -> str:
    v = f"@{layer.version}" if layer.version else ""
    return f"{layer.dir.name}{v}"


def compose(chain: list[ResolvedLayer], dest: Path) -> Provenance:
    """Flatten an ordered chain (base-first) into a single team image at `dest`.

    Produces the frozen surfaces install/deploy expect — agent.md, roles/,
    tools/, workflows/, monitors/, context/, agent.yaml — with no `from:`. Pure
    over (chain, layer contents): same inputs → byte-identical output, which is
    what makes the #452 regression bar a real diff. Returns a provenance map.

    `workspace/` is **not** merged here — it stays the seed-if-absent mechanism
    (handled by the caller), never frozen into the image.
    """
    prov = Provenance(chain=[
        {"ref": l.ref, "version": l.version, "source": l.source,
         "dir": str(l.dir)} for l in chain
    ])
    dest.mkdir(parents=True, exist_ok=True)

    _compose_prose_files(chain, dest, prov)
    _compose_roles(chain, dest, prov)
    for sub in STRUCTURED_DIRS:
        _compose_structured_dir(chain, dest, sub, prov)
    merged_yaml = _compose_agent_yaml(chain, prov)

    # Expand opt-in tool-library refs (#416): splice each entry's requires/build
    # into merged_yaml + write its tools/<name>.md guide, then drop the key. Runs
    # AFTER the structured tools/ merge (so the local-wins guide check sees team
    # files) and BEFORE prune (so a layer's prune: can still drop a tool guide).
    # Local import avoids a module-level import cycle (tool_library imports the
    # compose merge helpers).
    from modastack import tool_library
    tool_library.expand(merged_yaml, dest)

    # prune (§4) is applied after merge, across the frozen surfaces + agent.yaml.
    _apply_prune(chain, dest, merged_yaml, prov)

    (dest / "agent.yaml").write_text(
        yaml.dump(merged_yaml, default_flow_style=False, sort_keys=False))
    return prov


# --- prose rule (§2) ---------------------------------------------------------


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split optional leading YAML frontmatter (`---\\n…\\n---`) from a prose body.

    Returns (frontmatter_dict, body). No frontmatter → ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    # first line is `---`; find the closing fence.
    for i in range(1, len(lines)):
        if lines[i].rstrip("\n") == "---":
            raw = "".join(lines[1:i])
            body = "".join(lines[i + 1:])
            try:
                fm = yaml.safe_load(raw) or {}
            except yaml.YAMLError:
                fm = {}
            if isinstance(fm, dict):
                return fm, body.lstrip("\n")
            return {}, text
    return {}, text


def _concat_prose(contributions: list[tuple[str, str]], prov: Provenance,
                  prov_key: str) -> str | None:
    """Concatenate prose contributions in chain order (§2).

    Each contribution is (layer_label, text). A contribution whose frontmatter
    carries `replace: true` discards everything accumulated before it and owns
    the target wholesale. Returns the merged text, or None if no layer
    contributed."""
    accum: list[str] = []
    sources: list[str] = []
    saw_replace_with_base = False
    for label, text in contributions:
        fm, body = _split_frontmatter(text)
        if fm.get("replace") is True:
            if accum:
                saw_replace_with_base = True
            accum = [body]
            sources = [label]
        else:
            accum.append(body)
            sources.append(label)
    if not accum:
        return None
    # A `replace:` in the very first (base) contribution has nothing to replace.
    first_label, first_text = contributions[0]
    first_fm, _ = _split_frontmatter(first_text)
    if first_fm.get("replace") is True and not saw_replace_with_base:
        prov.warn(f"{prov_key}: `replace: true` in base layer {first_label} has "
                  "no base contribution to replace")
    prov.record(prov_key, " + ".join(sources))
    merged = "\n\n".join(c.strip("\n") for c in accum if c.strip("\n"))
    return merged + "\n"


def _compose_prose_files(chain: list[ResolvedLayer], dest: Path,
                         prov: Provenance) -> None:
    """agent.md — concatenate in chain order."""
    contributions = []
    for layer in chain:
        f = layer.dir / PROSE_AGENT_MD
        if f.is_file():
            contributions.append((_layer_label(layer), f.read_text()))
    merged = _concat_prose(contributions, prov, PROSE_AGENT_MD)
    if merged is not None:
        (dest / PROSE_AGENT_MD).write_text(merged)


def _compose_roles(chain: list[ResolvedLayer], dest: Path,
                   prov: Provenance) -> None:
    """roles/<role>/ROLE.md — concatenate per role across layers."""
    roles: dict[str, list[tuple[str, str]]] = {}
    order: list[str] = []
    for layer in chain:
        rdir = layer.dir / "roles"
        if not rdir.is_dir():
            continue
        for role_dir in sorted(rdir.iterdir()):
            role_md = role_dir / "ROLE.md"
            if role_md.is_file():
                role = role_dir.name
                if role not in roles:
                    roles[role] = []
                    order.append(role)
                roles[role].append((_layer_label(layer), role_md.read_text()))
    for role in order:
        merged = _concat_prose(roles[role], prov, f"roles/{role}/ROLE.md")
        if merged is not None:
            out = dest / "roles" / role / "ROLE.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(merged)


# --- structured rule (§3) ----------------------------------------------------


def _compose_structured_dir(chain: list[ResolvedLayer], dest: Path, sub: str,
                            prov: Provenance) -> None:
    """Deep-merge a structured directory surface across layers.

    Default rule: key by relative path, last layer wins (copy). `monitors/*.yaml`
    is special — its monitor *records* deep-merge by `name` across layers (flip
    `enabled`, tweak `interval`) rather than the whole file replacing.
    """
    out = dest / sub
    # Monitor record-level merge needs to accumulate across layers first.
    monitor_records: dict[str, dict] = {}
    monitor_order: list[str] = []
    monitor_src: dict[str, str] = {}  # record name → contributing layer label(s)
    monitor_yaml_seen = False

    # Framework-default monitors (#471) are seeded as the most-base layer, BEFORE
    # any real team layer. Two consequences this ordering buys us: (1) a team's own
    # same-named record overlays (and thus overrides) the framework default via the
    # deep-merge-by-name path below, and (2) the seed sits at the front of the
    # monitor order regardless of whether a team also declares it — which is what
    # makes removing a team's now-redundant copy a byte-identical no-op. The seed
    # is prunable like any inherited monitor (prune runs after this writes the file).
    if sub == "monitors":
        if _seed_framework_monitors(monitor_records, monitor_order, monitor_src):
            monitor_yaml_seen = True

    for layer in chain:
        src = layer.dir / sub
        if not src.is_dir():
            continue
        for f in sorted(src.rglob("*")):
            if not f.is_file() or "__pycache__" in f.parts:
                continue
            rel = f.relative_to(src)
            if sub == "monitors" and f.suffix in (".yaml", ".yml"):
                monitor_yaml_seen = True
                _accumulate_monitors(f, _layer_label(layer), monitor_records,
                                     monitor_order, monitor_src)
                continue
            target = out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            prov.record(f"{sub}/{rel.as_posix()}", _layer_label(layer))

    if monitor_yaml_seen:
        out.mkdir(parents=True, exist_ok=True)
        merged = [monitor_records[name] for name in monitor_order]
        (out / "defaults.yaml").write_text(
            yaml.dump({"monitors": merged}, default_flow_style=False,
                      sort_keys=False))
        for name in monitor_order:
            prov.record(f"monitors:{name}", monitor_src[name])


def _accumulate_monitors(f: Path, label: str, records: dict[str, dict],
                         order: list[str], src: dict[str, str]) -> None:
    """Merge one monitors yaml file's records into the accumulator by name."""
    try:
        data = yaml.safe_load(f.read_text()) or {}
    except yaml.YAMLError as e:
        raise ComposeError(f"could not parse monitor file {f}: {e}") from e
    for rec in data.get("monitors", []) or []:
        name = rec.get("name")
        if not name:
            continue
        if name in records:
            records[name] = _deep_merge_dict(records[name], rec)
            src[name] = f"{src[name]} + {label}"
        else:
            records[name] = dict(rec)
            order.append(name)
            src[name] = label


def _seed_framework_monitors(records: dict[str, dict], order: list[str],
                             src: dict[str, str]) -> bool:
    """Seed framework-default monitor records (#471) as the most-base layer.

    Loads `modastack/monitors/framework_defaults.yaml` and folds its records into
    the accumulator *before* any real team layer, labelled ``framework``. Returns
    True if anything was seeded (so the caller forces the defaults file to be
    written even for a team that declares no monitors of its own). Missing/empty
    framework defaults is a no-op, not an error.
    """
    from modastack.monitors import FRAMEWORK_DEFAULTS_PATH
    if not FRAMEWORK_DEFAULTS_PATH.is_file():
        return False
    _accumulate_monitors(FRAMEWORK_DEFAULTS_PATH, "framework", records, order, src)
    return bool(order)


# --- agent.yaml deep-merge (§3.1) --------------------------------------------


def _compose_agent_yaml(chain: list[ResolvedLayer], prov: Provenance) -> dict:
    """Deep-merge each layer's agent.yaml per §3.1 and drop `from:`."""
    merged: dict = {}
    for layer in chain:
        cfg = _read_agent_yaml(layer.dir)
        merged = _merge_agent_yaml(merged, cfg, _layer_label(layer), prov)
    merged.pop("from", None)        # consumed by compose, never emitted
    merged.pop("prune", None)       # applied separately, not part of the image
    merged.setdefault("agent", chain[-1].dir.name)
    return merged


def _merge_agent_yaml(base: dict, overlay: dict, label: str,
                      prov: Provenance) -> dict:
    out = dict(base)
    for key, val in overlay.items():
        if key in ("from", "prune"):
            out[key] = val  # carried, stripped later (prune handled by caller)
            continue
        if key == "services":
            out[key] = _merge_keyed_list(out.get("services"), val, "name")
        elif key == "requires":
            out[key] = _merge_keyed_list(out.get("requires"), val, "name")
        elif key == "build":
            out[key] = _merge_build(out.get("build"), val)
        elif key == "auto_dispatch":
            out[key] = _merge_auto_dispatch(out.get("auto_dispatch"), val)
        elif key == "tool_library":
            # Opt-in tool catalog refs (#416): union across `from:` layers so an
            # overlay's `tool_library:` ADDS to the base's instead of replacing it
            # (the `else` last-wins branch would drop the base's entries).
            # Consumed by tool_library.expand() in compose(), never emitted.
            out[key] = _dedupe(list(out.get(key) or []) + list(val or []))
        else:
            out[key] = val  # scalars + anything else: last wins
        prov.record(f"agent.yaml:{key}", label)
    return out


def _merge_keyed_list(base: list | None, overlay: list | None,
                      key: str) -> list:
    """Merge two lists of dicts keyed by `key`: same key → deep-merge fields;
    new key → append; `remove: true` on an overlay entry → drop the inherited
    one (and itself)."""
    result: list[dict] = [dict(e) for e in (base or [])]
    index = {e.get(key): i for i, e in enumerate(result) if isinstance(e, dict)}
    for entry in overlay or []:
        if not isinstance(entry, dict):
            result.append(entry)
            continue
        name = entry.get(key)
        if entry.get("remove") is True:
            if name in index:
                result[index[name]] = None  # tombstone; compacted below
            continue
        if name in index:
            result[index[name]] = _deep_merge_dict(result[index[name]], entry)
        else:
            index[name] = len(result)
            result.append(dict(entry))
    return [e for e in result if e is not None]


def _merge_build(base: dict | None, overlay: dict | None) -> dict:
    """`build`: list subkeys (apt/npm/run_root/run) append + de-dupe in chain
    order (base deps first); scalar subkeys (verify) last wins (§3.1)."""
    out = dict(base or {})
    for key, val in (overlay or {}).items():
        if key in BUILD_LIST_KEYS:
            out[key] = _dedupe(list(out.get(key, []) or []) + list(val or []))
        else:
            out[key] = val
    return out


def _merge_auto_dispatch(base: list | None, overlay: list | None) -> list:
    """`auto_dispatch`: append in chain order; an overlay rule carrying an `id`
    that matches an earlier rule's `id` replaces it in place (§3.1)."""
    result = [dict(r) if isinstance(r, dict) else r for r in (base or [])]
    id_index = {r.get("id"): i for i, r in enumerate(result)
                if isinstance(r, dict) and r.get("id") is not None}
    for rule in overlay or []:
        rid = rule.get("id") if isinstance(rule, dict) else None
        if rid is not None and rid in id_index:
            result[id_index[rid]] = dict(rule)
        else:
            if rid is not None:
                id_index[rid] = len(result)
            result.append(dict(rule) if isinstance(rule, dict) else rule)
    return result


def _deep_merge_dict(base: dict, overlay: dict) -> dict:
    """Recursively merge `overlay` onto `base`: nested dicts merge; scalars and
    lists are replaced by the overlay (the overlay wins)."""
    out = dict(base)
    for k, v in overlay.items():
        if k == "remove":
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def _dedupe(items: list) -> list:
    """Order-preserving de-dupe (first occurrence wins) for build dep lists."""
    seen = set()
    out = []
    for it in items:
        marker = it if isinstance(it, (str, int, float, bool)) else repr(it)
        if marker not in seen:
            seen.add(marker)
            out.append(it)
    return out


# --- prune (§4) --------------------------------------------------------------

_PRUNE_DIR_SURFACES = {
    "tools": "tools",
    "workflows": "workflows",
    "context": "context",
    "roles": "roles",
}


def _apply_prune(chain: list[ResolvedLayer], dest: Path, merged_yaml: dict,
                 prov: Provenance) -> None:
    """Apply every layer's `prune:` block to the frozen image (§4).

    A layer can only prune *inherited* content (keys contributed by earlier
    layers), so prune blocks are read base→leaf and applied against the already
    frozen `dest`. A prune entry that matches nothing is a warning, not an error.
    """
    for layer in chain:
        cfg = _read_agent_yaml(layer.dir)
        prune = cfg.get("prune") or {}
        if not isinstance(prune, dict):
            continue
        label = _layer_label(layer)
        for surface, names in prune.items():
            for name in names or []:
                if not _prune_one(dest, merged_yaml, surface, name):
                    prov.warn(f"{label}: prune {surface}:{name} matched nothing")


def _prune_one(dest: Path, merged_yaml: dict, surface: str, name: str) -> bool:
    """Remove one named item from a frozen surface. Returns True if something
    was removed."""
    if surface == "monitors":
        mfile = dest / "monitors" / "defaults.yaml"
        if mfile.is_file():
            data = yaml.safe_load(mfile.read_text()) or {}
            mons = data.get("monitors", []) or []
            kept = [m for m in mons if m.get("name") != name]
            if len(kept) != len(mons):
                mfile.write_text(yaml.dump({"monitors": kept},
                                           default_flow_style=False, sort_keys=False))
                return True
        return False
    if surface == "roles":
        rdir = dest / "roles" / name
        if rdir.is_dir():
            shutil.rmtree(rdir)
            return True
        return False
    if surface in _PRUNE_DIR_SURFACES:
        base = dest / surface
        # Allow either a bare stem (`codex`) or a relative path
        # (`methodology/old.md`). Try common extensions for a bare stem.
        candidates = [base / name]
        if "." not in Path(name).name:
            candidates += [base / f"{name}.md", base / f"{name}.yaml",
                           base / f"{name}.yml"]
        removed = False
        for c in candidates:
            if c.is_file():
                c.unlink()
                removed = True
            elif c.is_dir():
                shutil.rmtree(c)
                removed = True
        return removed
    return False


# --- packaging guard (§7.1) --------------------------------------------------


def reject_path_from(agent_yaml: Path) -> None:
    """Reject a path-based `from:` when packaging a team for publication (§7.1).

    A path ref is a local-only override — a consumer's checkout has no
    `../eng-team`, so a published tarball carrying one is broken. Mirrors
    Go `replace` / Cargo `[patch]` never leaking into published artifacts. The
    publishable forms are `name` / `name@version`."""
    try:
        cfg = yaml.safe_load(agent_yaml.read_text()) or {}
    except (yaml.YAMLError, OSError):
        return
    ref = cfg.get("from")
    if isinstance(ref, str) and _is_path_ref(ref):
        raise ComposeError(
            f"{agent_yaml} declares `from: {ref}`, a path override that cannot be "
            "published — a consumer has no such path. Change it to a "
            "`name@version` (or `name`) registry ref before packaging."
        )
