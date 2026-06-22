# Spec: MCP stdio preflight — poll race (MDS-63) + env mismatch (MDS-64)

- **Tickets:** [MDS-63](https://linear.app/moda-labs/issue/MDS-63), [MDS-64](https://linear.app/moda-labs/issue/MDS-64) (sibling bugs, same files)
- **Type:** Bug fix (×2) · **Priority:** 2 (medium) · **Status:** spec — HELD for approval
- **Files:** `modastack/validate.py` (`_check_mcp_servers`, `_probe_mcp_server`, `_async_probe_mcp`), `modastack/subagent.py` (agent-spawn env), plus tests.

> Two independent bugs in the **stdio** MCP preflight path, shipped as **one branch / one PR**
> because they touch the same functions and would collide otherwise. HTTP/SSE MCP servers
> (Venn, posthog) are unaffected by both — only command-based stdio servers hit these.

---

## 1. Problem

`modastack start` / `restart` / `doctor` run a preflight that connects to each configured MCP
server and reports its health. For **stdio** servers, the preflight is wrong in two distinct ways:

### MDS-63 — single-poll race: healthy stdio servers false-fail as `pending`

`_async_probe_mcp` calls `client.get_mcp_status()` **exactly once**, immediately after
`client.connect()`. A stdio server has to spawn a subprocess and run its import + MCP
`initialize` handshake (~1–2s), so the single poll catches it mid-spawn in the `pending`
window and treats that as failure:

```
[poll 0] substack -> status: pending
[poll 1] substack -> status: connected, 11 tools   # ~1.5s later
```

Result: `✗ substack    mcp — pending` → `Startup blocked — fix the issues above.` (exit 1),
with **no bypass flag**. A correctly-configured, working stdio MCP cannot be brought online
without a code change. Affects **any** stdio MCP, including built-ins injected by
`modastack/mcp/inject.py` (e.g. `modastack-codex`).

### MDS-64 — env mismatch: bare commands pass preflight, fail at agent spawn (false-green)

A stdio server whose `command` is a **bare executable name** (PATH-resolved, e.g.
`substack-mcp` installed by `uv tool install` into `~/.local/bin`) can pass preflight **and**
`doctor` yet have **no tools at all** in actually-spawned agent sessions — because the two run
in **different environments**:

- **Preflight** runs in the **foreground CLI process** → rich interactive `PATH` incl. `~/.local/bin`. Bare name resolves. ✅
- **Agents** are spawned by the long-running **manager daemon** via `subagent.py` →
  `_launch_detached(..., env={**os.environ, ...})` (`subagent.py:821`) inherits the **daemon's**
  `PATH`, which does **not** include `~/.local/bin`. Bare name fails to resolve. ❌

Proven under a daemon-like stripped `PATH` (`/usr/bin:/bin:/usr/sbin:/sbin`):

```
[BARE NAME] command=substack-mcp                       -> failed     Executable not found in $PATH
[ABSOLUTE ] command=/Users/.../.local/bin/substack-mcp -> connected   tools=11
```

Symptom: `doctor`/preflight show `✓ substack — mcp, 11 tools`, but the agent's transcript says
`Substack MCP server isn't connected this session` and it silently degrades (here: scraped
Substack over the web instead of using the live `notes_feed` tool). The failure is invisible
unless you read the agent transcript.

### Why fix together

Both live in the same two functions of the stdio probe path and both are about *preflight not
reflecting reality for stdio servers* — one fails a healthy server, the other passes a doomed
one. Fixing them on separate branches guarantees a merge conflict in `validate.py`. One PR,
one set of regression tests, one review.

---

## 2. Scope

**In scope**
- Poll-with-timeout in the stdio MCP probe so a server that is still `pending` is re-polled
  until it settles (`connected` / `failed` / `needs-auth`) or a timeout elapses (MDS-63).
- Make preflight reflect the environment agents actually run in, so a bare-name stdio command
  cannot be green at preflight and broken at spawn (MDS-64).
- Failing-first regression tests for both (per CLAUDE.md: production bug ⇒ integration-test gap).

**Out of scope** (note, don't build)
- A `--skip-preflight` / `--force` escape hatch (MDS-63 "Notes"). Useful but orthogonal — track
  as its own ticket; see Decision D-63c.
- Reworking HTTP/SSE probing — unaffected by both bugs.
- Broader MCP config schema changes.

---

## 3. Technical approach

### 3.1 MDS-63 — poll until settled (low-risk, design largely locked)

Replace the single `get_mcp_status()` read in `_async_probe_mcp` with a bounded poll loop that
returns as soon as the target server leaves `pending`:

```python
status = await client.get_mcp_status()
deadline_polls = MCP_PROBE_MAX_POLLS          # ~20
for _ in range(deadline_polls):
    srv = next((s for s in status.get("mcpServers", []) if s.get("name") == name), None)
    if srv is not None and srv.get("status") != "pending":
        break
    await asyncio.sleep(MCP_PROBE_POLL_INTERVAL)   # ~0.5s
    status = await client.get_mcp_status()
# …then judge srv["status"] exactly as today (connected / needs-auth / failed / other)
```

Semantics preserved: a server that genuinely `failed` / `needs-auth` still reports that on the
first non-`pending` poll (no added latency for real failures); only the `pending` window is
waited out, bounded by the timeout. A server stuck `pending` past the timeout reports
`mcp — pending` exactly as today — so the worst case is no worse than current behavior.

**Open knobs:** see D-63a (timeout/interval) and D-63b (probe-all-in-one-connect).

### 3.2 MDS-64 — make preflight env == agent-spawn env

The root cause is an **environment divergence**, not a probe-logic bug: preflight and the daemon
build `PATH` differently. The four upstream options from the ticket, with the lead's read:

| Opt | What | Pro | Con |
|----|------|-----|-----|
| **1** | **Probe in the agent's real env** — run the preflight probe under the same `PATH`/env the manager uses to spawn agents | Preflight can never be green when runtime will fail (closes the gap by construction) | Preflight currently runs in the foreground CLI; needs a shared "spawn env" helper to be meaningful |
| **2** | **Auto-resolve bare commands** to absolute paths via `shutil.which(command)` at config-load/install, warn if unresolvable | Config becomes env-independent; deterministic | `which` must run in an env that *has* the dir (i.e. the rich one), else resolves to `None`; mutating-config variant edits user's `agent.yaml` |
| **3** | **Warn on bare-name** stdio commands in `_check_mcp_servers` | Cheap; surfaces the footgun | Doesn't fix anything on its own — still green/broken without operator action |
| **4** | **Normalize the daemon's `PATH`** to include common user-bin dirs (`~/.local/bin`, etc.) for agent spawn | Fixes the actual root cause at the source; bare names then resolve at runtime too | Must choose the dir set; slight risk of shadowing if a user-bin dir has a same-named binary |

**Lead recommendation — unify around a single spawn-env helper (Opt 4 + Opt 1), add Opt 3 as
cheap defense-in-depth:**

1. Introduce one helper, e.g. `modastack/env.py:agent_spawn_env()`, that returns the env used to
   spawn agents — `{**os.environ}` with `PATH` normalized to **prepend** the standard user-bin
   dirs (`~/.local/bin`, and `$XDG_BIN_HOME` / `/opt/homebrew/bin` etc. as chosen in D-64b).
2. Use it in **both** places: `subagent.py` `_launch_detached` `child_env` (so runtime resolves
   bare names) **and** `validate.py` `_async_probe_mcp` (so preflight probes the *same* env).
   This makes Opt 1 true by construction — preflight and runtime can no longer diverge.
3. Add a non-blocking **warning** (Opt 3) in `_check_mcp_servers` when a stdio `command` is a
   bare name, pointing at PATH-in-daemon as the historical failure mode (uses the existing
   `required=False` warning path → `⚠`, agent still starts).

This keeps user `agent.yaml` untouched (no config rewrite), fixes both the false-green *and* the
underlying runtime breakage, and is the smallest change that makes "preflight green ⇒ agent
works" actually hold. **Opt 2 (which-resolve) is offered as an alternative/addition** in D-64a
for reviewers who prefer absolute paths baked into config over PATH normalization.

> **Decision D-64a is the load-bearing one** — it picks which of the above ships. Everything in
> §3.2 is the *recommendation*, not locked. Implementation is HELD until Zach picks.

---

## 4. Decisions for review

### MDS-63
- **D-63a — timeout / interval.** Recommend `MCP_PROBE_POLL_INTERVAL = 0.5s`, `MCP_PROBE_MAX_POLLS = 20`
  (≈10s ceiling), matching the proven local patch. Alternative: make the ceiling configurable via
  env/`agent.yaml` for slow servers. *Recommendation: hard-coded 10s ceiling; revisit if a real
  server needs longer.*
- **D-63b — probe all servers in one connect (efficiency).** Today `_probe_mcp_server` is called
  **per server** and each call spins up a full SDK client that connects **all** servers, then
  filters for one name — so N stdio servers ⇒ N full connects. Layering the poll loop on top
  multiplies wall-clock preflight time. *Recommendation: collapse to one `connect()` + one poll
  loop that judges every server, bounding preflight latency. Slightly larger diff; clearly
  correct. Alternative: keep per-server (smaller diff) and accept the latency.*
- **D-63c — escape hatch.** `--skip-preflight` / `--force`: in this PR or a separate ticket?
  *Recommendation: separate ticket — out of scope here.*

### MDS-64
- **D-64a — which fix ships (load-bearing).** Pick: **(A)** shared spawn-env helper = Opt 4 + Opt 1
  *(recommended)*; **(B)** which-resolve bare commands to absolute paths (Opt 2), in-memory at load
  vs. rewriting `agent.yaml`; **(C)** A **and** B (normalize PATH *and* which-resolve, belt-and-
  suspenders); **(D)** warn-only (Opt 3) and document the absolute-path workaround. *Recommendation: A,
  plus the Opt 3 warning regardless.*
- **D-64b — user-bin dir set** (only if A/normalize chosen). Which dirs to prepend to the daemon
  `PATH`? Recommend `~/.local/bin` + `$XDG_BIN_HOME` (default `~/.local/bin`); consider
  `/opt/homebrew/bin`, `/usr/local/bin` for mac dev. *Recommendation: `~/.local/bin` (+
  `$XDG_BIN_HOME`) — the documented `uv tool install` target; keep the set minimal.*
- **D-64c — warning blocking?** The bare-name warning: non-blocking `⚠` (agent starts) vs hard
  block. *Recommendation: non-blocking `⚠` — once A ships, bare names work at runtime, so blocking
  would be a false alarm; the warning is just guidance.*

---

## 5. Verification plan

Per CLAUDE.md (**production bug = integration-test gap**): each fix gets a regression test that
**fails on `main` and passes after the fix**.

- **MDS-63 (poll race).** Unit test with a fake SDK client whose `get_mcp_status()` returns
  `pending` on the first call and `connected` (with tools) thereafter. Assert `_async_probe_mcp`
  returns `ok=True` with the tool count. Fails today (single poll sees `pending` → `ok=False`),
  passes after the loop. Add a timeout test: a client stuck `pending` forever returns
  `mcp — pending` within the bounded poll budget (no hang).
- **MDS-64 (env mismatch).** Test the spawn-env helper directly: under a monkeypatched stripped
  `PATH` (`/usr/bin:/bin`), `agent_spawn_env()["PATH"]` includes the user-bin dir, and a bare
  command placed there resolves (`shutil.which(cmd, path=env["PATH"])` is not `None`). Assert
  `validate.py`'s probe and `subagent.py`'s `child_env` use the **same** helper (so they can't
  diverge). If the Opt 3 warning ships: a bare-name stdio command yields a non-blocking `⚠`
  `CheckResult` (`required=False`).
- **Full suite:** `pytest tests/ --ignore=tests/integration/` (unit, ~30s) before push; integration
  tests before merge.
- **Manual smoke:** with a bare-name stdio MCP installed in `~/.local/bin`, `modastack restart`
  reports it healthy *and* a real agent run actually sees its tools (the MDS-64 repro, now green
  for the right reason); a slow-spawning stdio server no longer false-fails as `pending` (MDS-63
  repro).

---

## 6. Implementation plan (HELD — do not start until approved)

1. **Tests first** (failing): poll-race test + env-mismatch test as above.
2. **MDS-63:** poll loop in `_async_probe_mcp`; constants per D-63a; optional one-connect refactor
   per D-63b.
3. **MDS-64:** `agent_spawn_env()` helper per D-64a/D-64b; wire into `subagent.py` `_launch_detached`
   and `validate.py` probe; bare-name `⚠` warning in `_check_mcp_servers` per D-64c.
4. `/review`; run unit + integration suites; manual smoke of both repros.
5. One PR, base `main`, **closes MDS-63 and MDS-64**. No `VERSION` / `CHANGELOG.md` / `pyproject.toml`
   version bump (feature/fix PRs don't bump — release-time only).
