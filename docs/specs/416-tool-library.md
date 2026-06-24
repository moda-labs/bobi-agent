# Spec: Reusable tool library — opt-in catalog of baked CLI tools (#416)

- **Ticket:** [#416](https://github.com/moda-labs/modastack/issues/416) · **Type:** Enhancement · **Priority:** 2 (medium) · **Status:** approved — implementing
- **Track:** the **hub** of the Tool/Capability library track. Sibling spokes: [#428](https://github.com/moda-labs/modastack/issues/428) (`kind: skill`), [#398](https://github.com/moda-labs/modastack/issues/398) (`kind: mcp`). This issue ships `kind: cli` and the catalog/resolver foundation the spokes build on.
- **Files:** `modastack/tool_library.py` (new), `modastack/tool_library/<name>/` catalog (new package data), `modastack/compose.py` (one merge branch + one expand call), tests. **No** changes to `subagent.py`/runtime — this is build/config-time sugar.

---

## 1. Problem

Baking a CLI tool into a team today requires **three hand-coordinated places that drift by design**, in each team's `agent.yaml`:

- a `requires:` entry (the `doctor`/dispatch check + a `fix:` install line),
- a `build:` fragment (the pinned image bake), and
- a `tools/<name>.md` guide (how the agent uses it).

The pin lives in **two** of them and must be bumped in lockstep. Concretely, in the `moda-eng-team` overlay:

- **codex** — `@openai/codex@<ver>` appears in both `requires.fix` (the `npm install -g …` line) and `build.npm`.
- **venn** — `venn-cli==<ver>` appears in both `requires.fix` and the `build.run_root` venv install (`agents/personal-assistant-core/agent.yaml`).

Two strings, one truth, no enforcement that they agree. The same tool re-declared in another team (venn across `support-manager` / `personal-assistant-core`; the #397 image CLI) repeats the whole three-place dance from scratch. (gstack repeats it too — a commit SHA in both `requires.fix` and `build.run` — but it's the `kind: skill` case handled by #428.)

A library entry collapses this to **one pinned definition + one guide, opted into by name** — reusable across teams, with the pin written exactly once.

---

## 2. Key insight — ride the existing compose pipeline (#457)

#416 was filed **before** `from:` composition (#451/#457) landed. That changes the design: `modastack/compose.py` already merges precisely the three surfaces a tool entry contributes —

| Surface | Existing merge in `compose.py` |
|---|---|
| `requires:` | `_merge_keyed_list(..., "name")` — de-dupe by name, deep-merge fields, `remove: true` tombstone |
| `build:` `apt`/`npm`/`run`/`run_root` | `_merge_build` → `_dedupe` — append + de-dupe across layers (`BUILD_LIST_KEYS`) |
| `tools/<name>.md` | `_compose_structured_dir("tools")` — copy, **last layer (leaf) wins** by path |

So a tool-library entry is **a bundle of those three surfaces**, and `tool_library:` does **not** need its own merge/precedence engine. It **expands into the agent.yaml dict + `tools/` dir, then the existing compose merge/de-dupe runs**. The pin-duplication problem dissolves: the pin is written once in the catalog entry, and `_dedupe` collapses any repeat across layers for free.

`compose()` is the **single chokepoint** — `cli.py` (install) and `deploy.py` (deploy) both route through it, and a team with **no** `from:` still composes (single-layer chain). Expanding inside `compose()` therefore covers every path with one insertion.

---

## 3. Scope

**In scope**
- Catalog format + location (framework package data) with a `kind:` discriminator; **`kind: cli` only** this PR.
- A resolver (`tool_library.py`) that expands `tool_library: [names]` into `requires` + `build` + a `tools/<name>.md` guide, integrated as one step in `compose()`.
- The one merge branch that makes `tool_library:` **union** across `from:` layers.
- Catalog entries authored for **codex, venn, openai** — pinned to current last-stable (`@openai/codex@0.142.0`, `venn-cli==0.2.0`, `openai==2.43.0`); versions are deliberately bumpable later. **gstack is excluded** — it is primarily a Claude Code skill library, so it lands as the canonical `kind: skill` entry under #428, not here.
- Failing-first tests (per CLAUDE.md: production-pattern ⇒ test) — the headline one proves the cross-layer pin **de-dupes to a single string**.

**Out of scope** (note, don't build)
- `kind: mcp` (#398) and `kind: skill` (#428) — schema-forward only: the `kind` field is parsed, and the **expander dispatch table** (§4.2, D-5) is the seam they plug into. This PR registers only `cli`; an entry of any other kind raises a `ComposeError` naming the unsupported kind + its owning issue. The spokes are *additive* (register an expander; #398 also adds an `mcp_servers` merge branch) — they do not edit `expand()`.
- **aichat is NOT migrated** — it is universal framework infra, already baked into the base `Dockerfile` and documented in `prompts/base.md` for every agent. It stays in base; the catalog is for genuinely opt-in, per-team tools.
- Flipping the teams to `tool_library:` refs — that is the **migration**, sequenced as follow-up PRs (§7), and the `moda-eng-team` half lands in the private `moda-agent-teams` repo.
- Any runtime / `subagent.py` change. Agents still shell out to the bare CLI; no MCP indirection is reintroduced.

---

## 4. Technical approach

### 4.1 Catalog format & location

Framework package data, one directory per entry:

```
modastack/tool_library/
  codex/   { tool.yaml, guide.md }
  venn/    { tool.yaml, guide.md }
  openai/  { tool.yaml, guide.md }
```

The **directory name is the entry id** referenced in `tool_library: [codex]`; `guide.md` becomes `tools/<id>.md`. `tool.yaml` mirrors the agent.yaml surfaces verbatim, so expansion is a literal splice:

```yaml
# modastack/tool_library/codex/tool.yaml
kind: cli                       # only 'cli' now; mcp/skill reserved (#398/#428)
requires:                       # spliced into agent.yaml `requires:` (list-merge by name)
  - name: codex
    why: "Delegate a coding sub-task to the Codex CLI (tools/codex.md)."
    check: "command -v codex >/dev/null 2>&1 && { test -n \"${OPENAI_API_KEY:-}\" || codex --version >/dev/null 2>&1; }"
    fix: "npm install -g @openai/codex@0.142.0 && (codex auth login || echo 'Set OPENAI_API_KEY in .modastack/.env')"
build:                          # spliced into agent.yaml `build:` (list-accrete + de-dupe)
  npm: ["@openai/codex@0.142.0"]
```

Entries carry **whatever build surface the tool needs** — not just `npm`. The three first-pass entries cover all of them:

| Entry | id | Pin (last-stable) | `build` surface |
|---|---|---|---|
| Codex CLI | `codex` | `@openai/codex@0.142.0` | `npm:` |
| OpenAI image CLI | `openai` | `openai==2.43.0` | `apt: [python3-venv]` + `run_root:` pip into an isolated venv + symlink onto PATH |
| Venn CLI | `venn` | `venn-cli==0.2.0` | `apt: [python3-venv]` + `run_root:` venv install + symlink |

> Pins are **last-stable as of authoring** and deliberately bumpable later — a stale pin is fine; the value is single-source. All three resolve from **public** registries (npm/PyPI), so the first PR needs nothing from the private `moda-agent-teams` overlay.

The pin appears once per entry — but still in two *fields* (`requires.fix` and `build`) **within the single entry file**. A test (§6) asserts the entry's `requires.fix` install ref and its `build` pin agree, so the one remaining co-location is guarded rather than scattered across teams.

### 4.2 Resolver — `modastack/tool_library.py`

```python
CATALOG_DIR = Path(__file__).parent / "tool_library"

@dataclass
class ToolEntry:
    name: str
    kind: str
    requires: list[dict]
    build: dict
    guide: str            # guide.md text

def load_entry(name: str) -> ToolEntry:
    """Load + validate a catalog entry. Raises ComposeError (human-facing) on
    unknown name (listing available entries)."""

# --- per-kind expanders (the seam the spokes plug into) ----------------------
EXPANDERS: dict[str, Callable[[ToolEntry, dict, Path], None]] = {
    "cli": _expand_cli,
}

def expand(merged_yaml: dict, dest: Path) -> None:
    """Expand merged_yaml['tool_library'] in place, then drop the key.
    Dispatches each entry to EXPANDERS[entry.kind]; raises ComposeError naming
    the unsupported kind + pointing at the owning issue when no expander exists."""
```

**`expand()` is kind-dispatched, not cli-hardcoded** (D-5). It resolves each name, looks up `EXPANDERS[entry.kind]`, and calls it. The cli logic lives in `_expand_cli`; #428 registers `_expand_skill` and #398 registers `_expand_mcp` (plus its own compose merge branch) as **additive** changes — no edit to `expand()` itself. An entry whose `kind` has no registered expander raises a `ComposeError` like `tool 'foo' is kind 'mcp' — not yet implemented (see #398)`.

`expand()` (idempotent, pure over inputs):

1. `names = merged_yaml.get("tool_library", [])` — already the **de-duped union** across layers (see §4.3). Empty/absent → no-op.
2. For each `name`: `entry = load_entry(name)`; `EXPANDERS.get(entry.kind)` — missing → `ComposeError`.
3. `merged_yaml.pop("tool_library", None)` — consumed by compose, **never emitted** (exactly like `from`/`prune`). Prevents double-expansion and signals build-time sugar.

**`_expand_cli(entry, merged_yaml, dest)`** — the only expander this PR ships:

- `requires`: append `entry.requires` **only if that name isn't already present**. An explicit team `requires:` for the same name (already merged into `merged_yaml` as the leaf) therefore **wins wholesale** (intentional override / escape hatch).
- `build`: merge `entry.build` via the **same `_merge_build`** — `apt`/`npm`/`run`/`run_root` accrete + `_dedupe`. Identical pins across entries/layers collapse to one string. **This is the core fix.**
- `guide`: write `entry.guide` to `dest/tools/<name>.md` **only if that file does not already exist** after the structured `tools/` merge — so a team that ships its own `tools/<name>.md` **wins**.

### 4.3 `compose.py` integration (two edits)

**a) Union `tool_library` across layers** — in `_merge_agent_yaml`, add a branch beside `requires`/`build`:

```python
elif key == "tool_library":
    out[key] = _dedupe(list(out.get(key) or []) + list(val or []))
```

Without this, the top-level list would hit the `else: last-wins` branch and an overlay's `tool_library:` would *replace* the base's instead of adding to it.

**b) Call `expand()`** in `compose()` — after the structured `tools/` merge and the agent.yaml merge, before prune/write:

```python
    merged_yaml = _compose_agent_yaml(chain, prov)

    from modastack import tool_library
    tool_library.expand(merged_yaml, dest)      # splice requires/build, write guides, pop key

    _apply_prune(chain, dest, merged_yaml, prov)
```

Ordering matters: guides are written **after** `_compose_structured_dir("tools")` (so the local-wins check sees team files) and **before** prune (so prune can still drop a tool guide if a layer's `prune:` names it). The import is local to avoid a module-level import cycle (`tool_library` imports compose's merge helpers).

### 4.4 Pinning (#380) — single source of truth

The pin lives once, in the catalog entry. Cross-layer `_dedupe` guarantees a repeat reference can't double-bake. A test (§6) extends the #380 reproducibility convention to the catalog: **no floating refs** (`@latest`, bare `HEAD`, unpinned git clone) in any `tool.yaml`. Build hashing (`build_render.py` deps-stamp) is unaffected — it hashes the *composed* `build:`, which is what the entry expanded into.

---

## 5. Decisions (resolved with Zach)

- **D-1 — Architecture: thin resolver → existing compose surfaces** (vs modeling tools as `from:`-able micro-teams). Chosen: thin resolver. A catalog is a flat **pick-N-from-a-set**; `from:` is a leaf-wins **chain**, and a tool is not a team. Reusing the merge semantics #457 proved, without bending `from:`, is the minimal correct design.
- **D-2 — aichat stays in base** (vs migrate to catalog). aichat is universal; pulling it into an opt-in entry adds indirection for zero benefit. First-pass catalog = **codex, venn, openai** (gstack → #428).
- **D-3 — `kind` discriminator shipped now, cli-only.** Schema-forward so #398/#428 slot in; non-`cli` rejected with a pointer to those issues.
- **D-4 — `tool_library` is consumed at compose (dropped from emitted agent.yaml)**, like `from`/`prune`. Build-time sugar, not a runtime field.
- **D-5 — `expand()` is kind-dispatched via an `EXPANDERS` table, not cli-hardcoded.** Adding a spoke is registering an expander, not refactoring `expand()`. This is the seam that keeps the spokes cheap.

---

## 6. Verification plan

Per CLAUDE.md (**production pattern ⇒ test gap**): tests-first, each failing on `main`.

- **Pin de-dup (headline).** A team with `from: base` where **both** base and leaf reference `tool_library: [codex]` composes to an agent.yaml whose `build.npm` contains the codex pin **exactly once** and `requires` has the codex entry **exactly once**.
- **Expansion basics.** `tool_library: [codex]` → composed agent.yaml has the codex `requires` + `build.npm`; `dest/tools/codex.md` equals the entry guide.
- **Local-wins guide.** Team ships its own `tools/codex.md` → catalog guide does **not** overwrite it.
- **Explicit-wins requires.** Team has an explicit `requires: [{name: codex, ...}]` → the catalog entry does not duplicate or clobber it (leaf field wins).
- **Union across layers.** base `tool_library: [venn]` + leaf `tool_library: [codex]` → composed set is `{venn, codex}`.
- **Consumed key.** Emitted agent.yaml has **no** `tool_library` key.
- **Unknown entry.** `tool_library: [nope]` → `ComposeError` naming `nope` and listing available entries.
- **Unsupported kind (the seam).** An entry with `kind: mcp` → `ComposeError` naming the kind + owning issue, raised because `EXPANDERS` has no `mcp` key.
- **Pin lint.** No `tool.yaml` uses a floating ref; each entry's `requires.fix` pin agrees with its `build` pin.
- **Regression bar (#452-style).** A composed team using `tool_library: [codex, venn]` produces an agent.yaml + `tools/` **byte-identical** to the same team with those three surfaces hand-written inline — the migration is provably zero-behavior-change.
- **Suite:** `pytest tests/ --ignore=tests/integration/` before push; integration before merge.

---

## 7. Implementation plan

1. **Tests first** (failing): the §6 set, headline = pin de-dup.
2. **Catalog**: `modastack/tool_library/{codex,venn,openai}/{tool.yaml,guide.md}` at the pins in §4.1 (all public registries). Hatchling ships non-`.py` files under `modastack/` as package data automatically (same path `modastack/skills/*.md` uses) — no `pyproject.toml` change needed.
3. **Resolver**: `modastack/tool_library.py` — `load_entry` + kind-dispatched `expand` (the `EXPANDERS` table) + `_expand_cli` (reusing `compose._merge_build` / `_dedupe`).
4. **compose.py**: `tool_library` union branch in `_merge_agent_yaml`; `expand()` call in `compose()`.
5. **Pin lint** test extending the #380 convention to the catalog.
6. **Docs**: `skills/create-agent.md` + `CLAUDE.md` (agent-teams section) document `tool_library:`.
7. `/review`; unit + integration; **no** `VERSION`/`CHANGELOG.md`/`pyproject.toml` version bump (release-time only).
8. **Follow-ups (separate tickets/PRs):**
   - This repo: flip `personal-assistant-core` (and `support-manager`) from inline venn `requires`/`build` to `tool_library: [venn]`. Zero-rework — the §6 regression bar proves it.
   - `moda-agent-teams` (private): flip `moda-eng-team` overlay's codex `requires`/`build` to `tool_library: [codex]`.
   - **gstack** lands as the canonical `kind: skill` entry under **#428**.
   - #428/#398 consume this catalog shape for `kind: skill` / `kind: mcp`.

---

## 8. The spokes (informational — how `kind` pays off)

This is the **hub** of the capability-library track. The `kind` field answers one question — *which agent.yaml surfaces does this entry expand into?* — and the `EXPANDERS` dispatch table (§4.2, D-5) is where each spoke plugs in.

| `kind` | Expands into | New compose machinery | Effort |
|---|---|---|---|
| **cli** (#416, this PR) | `requires` + `build` (apt/npm/run) + `tools/<name>.md` | none — every surface already merges | — |
| **skill** ([#428](https://github.com/moda-labs/modastack/issues/428)) | `build.run` (clone `<repo>@<SHA>` → install skill `.md`s) + optional `requires`/guide | **none** — still a build-time bake | **thin** — register `_expand_skill` + SHA-pin/supply-chain lint. gstack becomes the canonical `skill` entry. |
| **mcp** ([#398](https://github.com/moda-labs/modastack/issues/398)) | `mcp_servers:` (dict) + optional `build` + optional `requires` + guide | **yes** — `mcp_servers` is a dict `compose.py` does **not** deep-merge today; #398 adds that merge branch. | **heavy** — register `_expand_mcp`, add the merge branch, runtime-coupled, gated behind PR #435. |

**Sequencing:** #416 (this) → #428 fast-follow (cheap, reuses everything here) → #398 later, gated behind #435. Nothing for the spokes is built in this PR; D-5 is the only thing that keeps them additive.
