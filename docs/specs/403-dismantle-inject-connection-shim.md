# Spec — #403: Completely dismantle the `inject.py` / `ConnectionEntry` connection-kind shim

**Status:** Draft — held at spec gate, pending Zach's formal GitHub approval.
**Issue:** moda-labs/modastack#403
**Type:** Removal / cleanup (pure subtraction). No new behavior.
**Preconditions (both merged):** #397 (image → CLI, removed `image_server.py` + the
`kind: image` inject branch) and #285 (codex → `codex exec` CLI shell-out). Nothing in
any shipped pack consumes the shim anymore.

> This spec is a **superset** of the #403 issue body: it re-verifies every claim against
> the current tree (line numbers re-checked off `origin/main` @ `ba834c7`, which have
> drifted from the issue's numbers), formalizes the exact deletions, and records the
> verification findings + one open decision point.

---

## 1. Problem & Solution

### Problem
modastack historically routed two "built-in capabilities" (image generation, codex
delegation) through an **MCP-injection middle layer**:

- `agent.yaml` declares a `connections:` block, each entry carrying a `kind`.
- `config.py` parses those into `ConnectionEntry` objects on `Config.connections`.
- `mcp/inject.py::inject_builtin_mcp_servers()` inspects the connections and, for
  `kind: codex`, splices a modastack-shipped stdio MCP server (`mcp/codex_server.py`)
  into the `mcp_servers` dict handed to the SDK.

Both consumers have since moved to **baked CLIs** (image via direct Images API `curl`,
codex via `codex exec`). The shim is now dead weight: an indirection layer with no
production caller, plus test-only `Config` helpers and a doc section describing a
pattern we've explicitly stopped using.

### Solution
Delete the layer **entirely**, leaving two clean capability layers (the #396 end-state):

1. **Baked CLIs** — modastack-shipped binaries + `tools/*.md` guides (`aichat`, `codex`,
   image-gen `curl`, `gh`, `venn`). Axis-2 capabilities ship CLI-first.
2. **Team-brought third-party MCP servers** — the `mcp_servers:` path (#398), threaded
   straight through to the SDK with no modastack injection.

After this change, the call sites pass `mcp_servers` straight through; there is no
"built-in MCP" concept left in the codebase.

---

## 2. Scope

### In scope (exact deletions — verified against `origin/main` @ `ba834c7`)

| # | Target | Location (current) | Action |
|---|--------|--------------------|--------|
| 1 | `inject_builtin_mcp_servers()` + module | `modastack/mcp/inject.py` (whole file, 61 lines) | **Delete file** |
| 2 | codex MCP server | `modastack/mcp/codex_server.py` (whole file) | **Delete file** |
| 3 | `ConnectionEntry` dataclass | `modastack/config.py:110–119` | **Delete** |
| 4 | `connections` field | `modastack/config.py:234` | **Delete** |
| 5 | `Config.connection()` | `modastack/config.py:250–255` | **Delete** |
| 6 | `Config.connections_by_kind()` | `modastack/config.py:257–259` | **Delete** |
| 7 | `connections:` parse block | `modastack/config.py:316–333` | **Delete** (and drop `connections=` from the `cls(...)` constructor call) |
| 8 | Call site A | `modastack/subagent.py:362–371` | **Simplify** (see §4) |
| 9 | Call site B | `modastack/subagent.py:495–506` | **Simplify** (see §4) |
| 10 | `connections:` schema + injection prose | `docs/design/MULTI_MODEL.md` (lines 38–44, 62–67) | **Edit** (see §4) |
| 11 | Shim tests | `tests/test_connections.py`, `tests/test_codex_server.py` (whole files) | **Delete** |

`image_server.py` and `tests/test_image_server.py` are **already gone** (#397) — confirmed
absent. The `kind: image` inject branch is already gone; only the `kind: codex` branch and
its `codex_server.py` spawn remain to remove.

### Out of scope (explicitly untouched)

- **`modastack/setup/` "connections"** — this is the **#398 third-party `mcp_servers:`
  path**, a *separate* layer. Verified: `grep -rn 'ConnectionEntry|connections_by_kind|inject_builtin|cfg.connections' modastack/setup/`
  returns **nothing**. `setup/authoring.py` builds the `mcp_servers:` block (`build_mcp_servers`),
  never `ConnectionEntry`. The word "connections" in `setup/webui/static/app.js:550` is a
  UI progress-phase label, unrelated. **No coupling.**
- **Cost attribution** (`SessionEntry`, `modastack costs`, `test_costs.py`,
  `test_cost_recording.py`) — independent of the shim; untouched.
- **`VERSION`, `pyproject.toml` `version`, `CHANGELOG.md`** — feature/cleanup PRs must not
  touch these (per CLAUDE.md). Untouched.
- The `aichat` CLI / `gateway`,`chat`,`embedding` retired kinds — already gone (#396); no
  action.

---

## 3. Breaking change (intended)

These public surfaces **go away** and do not come back:

- The **`codex_exec` MCP tool** (and, already removed in #397, `generate_image`).
- The **`kind: image` / `kind: codex` connections**.
- The **`connections:` block** in `agent.yaml` — no longer a recognized schema key.

Any internal (modalabs-only) consumer migrates to the CLI form (`codex exec`,
image-gen `curl`). No shipped `agent.yaml` declares a `connections:` block today
(verified — see §6), so there is **no production consumer to break**.

A stray `connections:` block left in an `agent.yaml` after this change is simply
**ignored** (the parser no longer reads the key — YAML load tolerates unknown
top-level keys; nothing errors, nothing injects). See Decision Point D1 for whether
that silent-ignore is acceptable or should warn.

---

## 4. Technical approach (call-site & doc detail)

### Call sites — `subagent.py` (verification finding: `Config.load` must stay)

Both sites currently load `Config` **only** to reach `mcp_servers` and `connections`,
then wrap through `inject_builtin_mcp_servers`. The issue says "pass `mcp_servers`
straight through" — precisely, that means **drop the inject import + the `connections`
argument, but keep `Config.load`** (still needed for the `mcp_servers` field/fallback):

**Site A** — `subagent.py:362–371` (result consumed at `:384` as `_mcp`):
```python
# before
from modastack.mcp.inject import inject_builtin_mcp_servers
try:
    _cfg = _Config.load(_mr())
    _mcp = inject_builtin_mcp_servers(_cfg.mcp_servers, _cfg.connections)
except Exception:
    _mcp = None
# after
try:
    _cfg = _Config.load(_mr())
    _mcp = _cfg.mcp_servers
except Exception:
    _mcp = None
```

**Site B** — `subagent.py:495–506` (result consumed at `:519` as `merged_mcp`):
```python
# before
from modastack.mcp.inject import inject_builtin_mcp_servers
try:
    _cfg = _Config.load(_mr())
    merged_mcp = inject_builtin_mcp_servers(
        mcp_servers or _cfg.mcp_servers, _cfg.connections)
except Exception:
    merged_mcp = mcp_servers
# after
try:
    _cfg = _Config.load(_mr())
    merged_mcp = mcp_servers or _cfg.mcp_servers
except Exception:
    merged_mcp = mcp_servers
```

The surrounding comments ("Inject built-in MCP servers…") should be reworded to
"Thread the team's `mcp_servers` through to the SDK" since nothing is injected anymore.

### Docs — `docs/design/MULTI_MODEL.md`

- Lines **38–44** ("Legacy: connection-kind + built-in MCP injection") — replace the
  description of the live shim with a one-line **"Removed (#403)"** note: the
  connection-kind + `mcp/inject.py` injection layer was deleted; built-in capabilities
  are baked CLIs, team capabilities are `mcp_servers:`.
- Lines **62–67** ("Status") — update **Shipped** (drop "`image` MCP server" / "connections
  registry" as a live pattern) to reflect that the legacy Axis-2 pattern is now fully
  retired, leaving CLI-first + team `mcp_servers:`.
- `agents/eng-team/tools/codex.md:13` already states "There is no `connections:` block and
  no MCP" — **no change needed**; it confirms the end-state.

---

## 5. Verification plan / Acceptance criteria

1. **Marker grep is empty:**
   ```bash
   grep -rn 'inject_builtin_mcp_servers\|ConnectionEntry\|connections_by_kind' modastack/
   ```
   returns nothing. (Baseline today: **10** hits across `inject.py`, `config.py`,
   `subagent.py` — all removed.)
2. **No dangling references:** `grep -rn 'mcp.inject\|codex_server\|\.connection(\|cfg.connections\|_cfg.connections' modastack/ tests/`
   returns nothing.
3. **Subagent spawn** passes `mcp_servers` directly at both sites (§4); the framework
   still threads team `mcp_servers:` through to the SDK unchanged.
4. **Unit suite green:** `pytest tests/ --ignore=tests/integration/`. The two deleted test
   files (`test_connections.py`, `test_codex_server.py`) are removed; no other test imports
   `ConnectionEntry` / `codex_server` (verified — they don't).
5. **Stray `connections:` block** in an `agent.yaml` no longer parses into config and
   causes no crash (resolution of D1 may add a one-line warning).
6. **Per CLAUDE.md (deletion discipline):** this is pure subtraction with no behavior to
   preserve; if review surfaces any behavior that *would* regress, add a failing test
   first, then keep that behavior. None is expected.

---

## 6. Re-verified safety (against `origin/main` @ `ba834c7`)

- ✅ **No shipped `agent.yaml` declares a `connections:` block** — `inject_builtin_mcp_servers`
  never injects in production; this is dead code.
- ✅ **`Config.connection()` / `connections_by_kind()` are test-only** — sole non-test
  callers were the (now-CLI) image/codex paths. Only `tests/test_connections.py` calls them.
- ✅ **`codex_server.py` is referenced only by `inject.py`** (the `-m modastack.mcp.codex_server`
  spawn) **+ `tests/test_codex_server.py` + its own `__main__`** — no pack, no other module.
- ✅ **`setup/` "connections" ≠ this layer** — it is the #398 `mcp_servers:` path; zero
  coupling to `ConnectionEntry`/`inject` (grep-confirmed).
- ✅ **`test_connections.py` is 100% shim coverage** — despite its module docstring
  ("…registry **and cost attribution**"), every test class/method targets `ConnectionEntry`
  parsing / `connection()` / `connections_by_kind()`. The actual cost-attribution tests live
  in `test_costs.py` and `test_cost_recording.py`. → safe to delete the whole file; **no
  cost coverage is lost.** (This is the one place the issue's "just delete it" could have
  dropped coverage — it does not.)

### Risks / flags
- **Low — silent-ignore of `connections:`** (D1 below). The only judgment call.
- **None — call sites:** the `Config.load` retention (§4) is the subtle bit; getting it
  wrong (deleting the whole try-block) would drop team `mcp_servers:`. The spec pins the
  exact after-shape to prevent that.

---

## 7. Open decision point (for the reviewer)

**D1 — stray `connections:` block: silent-ignore vs. warn?**
After removal, an `agent.yaml` that still carries a `connections:` block is silently
ignored by the loader. Options:
- **(a) Silent ignore (recommended, smallest diff).** Consistent with how the YAML loader
  already tolerates unknown top-level keys; the breaking change is documented; no shipped
  pack uses it.
- **(b) Emit a one-line deprecation `log.warning("connections: is no longer supported; …")`**
  in `Config._parse` when the key is present. Friendlier to any straggler internal pack, at
  the cost of a few retained lines that reference the very concept we're deleting.

Acceptance criterion #5 is phrased to accept either; defaulting to **(a)** unless Zach
prefers the warning.

---

## 8. Implementation plan (executed only after formal approval)

1. Delete `modastack/mcp/inject.py` and `modastack/mcp/codex_server.py`.
2. Edit `modastack/config.py`: remove `ConnectionEntry`, the `connections` field, both
   methods, the parse block, and the `connections=` constructor arg.
3. Simplify both `subagent.py` call sites per §4 (keep `Config.load`; reword comments).
4. Edit `docs/design/MULTI_MODEL.md` per §4.
5. Delete `tests/test_connections.py` and `tests/test_codex_server.py`.
6. (If D1 = b) add the one-line deprecation warning.
7. Run acceptance grep (§5.1–5.2) + `pytest tests/ --ignore=tests/integration/`.
8. `/review`, then unified spec+impl PR (impl pushed onto this branch; impl PR carries
   `Fixes #403`).

---

## 9. End state

Two clean capability layers, zero middle indirection:

```
Built-in capabilities  →  baked CLIs        (aichat, codex, image-gen curl, gh, venn)
Team capabilities      →  mcp_servers:       (#398 third-party MCP, threaded to SDK)
```

The connection-kind registry + built-in MCP injection — the legacy Axis-2 pattern from
#396 — is **fully retired**. This completes the #396 CLI-first direction
(`gateway`/`chat`/`embedding` retired earlier; `image` via #397; `codex` via #285; the
shim itself here).
