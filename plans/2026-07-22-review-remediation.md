# Full-repo review remediation (defects + mechanical quality)

> **Status:** Approved
> **Tracking issue:** moda-labs/bobi-agent#817 · **Created:** 2026-07-22 · **Last amended:** 2026-07-29 (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

Clear the entire actionable backlog from the July 2026 two-pass full-repo review — every confirmed bug, dead-code item, doc-drift claim, and mechanical consolidation — as one checklist-driven initiative with the minimum number of PRs. The review produced 253 findings; after cross-pass dedup and deferring the 11 large structural refactors (which get their own successor plan — the deliberate scope cut, recorded in Notes), **229 items remain in scope here**. None of them need design: each is a verified defect with a known failure scenario or a behavior-preserving cleanup with the better shape already named.

## Problem

Two adversarially-verified review passes (281 agents; every finding re-verified by an independent agent, top items re-traced by hand) established the current state. Full per-item detail — failure scenario, evidence, verifier's trace — lives in the companion appendix **`plans/2026-07-22-review-remediation-findings.md`** (committed alongside this plan; IDs `D###` = defect pass, `Q###` = quality pass). The checklist lines below reference those IDs; the appendix is part of this plan's spec. Claims were verified against the working tree at `58aba2c` (review) and spot-re-verified at plan time; each builder re-verifies the cited lines before fixing (line numbers drift).

The highest-consequence clusters:

- **Session failure paths misreport dead transports** (D001/D002/D003/D021): a turn whose brain process dies mid-drain still ACKs the triggering message (lost on restart — the #688 class), is persisted `TERMINAL_COMPLETED` with `success=True`, and neither `stop()` nor `start()` can interrupt/detect the wedged state. Until this lands, a bot builder's own terminal signals can lie.
- **Suspended workflows emit `workflow.completed` and never auto-resume** (D005, D027; `try_resume_for_event` has no caller).
- **Installed agent packs have silently dead routing** (D015/D016/D017/D060/D119): unsupported `>` operator, out-of-scope variables, an `auto_dispatch` event type that is never emitted — the reason issue pickup currently needs a Slack directive.
- **Durable state written non-atomically** (D034/D038/D087) while the tmp+`os.replace` pattern is already re-implemented in five modules (D092).
- **41 dead-code items** including compat layers past their stated one-release windows; **34 doc-drift claims** where docs promise behavior the code does not deliver; **~60 mechanical consolidations** (missed reuse, copy-paste duplication, one-caller abstractions, conflicting house patterns).

## Solution

One plan, nine phases, executed as a small number of lanes (Q1). Phases are thematic checkpoints, not PR boundaries: a lane rips through its phases' checklists in one session/PR, flipping markers as it goes. The checklist line + appendix entry together are the per-item spec; the phase gate proves the batch. Builders re-verify each item's cited code before fixing; an item whose premise no longer holds (already fixed, code moved) is marked `[x]` with a one-line "already resolved / superseded" note rather than force-fixed — but never silently skipped.

Fix-shape ground rules (apply across all phases):

- **Bugs are failing-test-first** (house rule): reproduce, then fix. Behavior-preserving cleanups are proven by the existing suites staying green plus per-phase grep gates.
- **Deletions beat deprecation**: every expired compat shim in scope has a stated one-release window that is 4+ releases past, and verifiers confirmed zero importers (including the private deploy repo) — delete, don't re-deprecate (Q4).
- **Consolidations move code to the named house implementation** (the appendix names the survivor for each); never create a new third copy.
- **No version/changelog edits** in any lane (release rules).

Alternatives considered: per-finding tickets (253 issues — rejected: drowns the board, maximizes PR count, no shared context); fixing only high-severity (rejected: the long tail is exactly the mechanical work a bot rips through cheaply, and Zach asked for the full set); folding the structural refactors in (rejected: they need design and would poison a checklist run — successor plan).

## Relevant files

### Existing (verified 2026-07-22)

The authoritative per-item file inventory is the checklist below + appendix. Hot files touched by many items: `bobi/session.py`, `bobi/subagent.py`, `bobi/workflow/orchestrator.py`, `bobi/workflow/variables.py`, `bobi/workflow/state.py`, `bobi/cli.py`, `bobi/doctor.py`, `bobi/events/` (server/drain/adapters), `bobi/setup/webui/server.py`, `bobi/setup/state.py`, `bobi/webapp/` (+ static JS), `bobi/brain/` (claude/codex/codex_config), `bobi/monitors/` (scheduler/script_cache_checks), `bobi/compose.py`, `bobi/config.py`, `bobi/kb/`, `event-server/core/src/` (core/adapters/circuit-breaker), `event-server/src/local.ts`, `docs/*.md`, `skills/*.md`, `agents/*`, `tests/*`.

### New

- `bobi/fsutil.py` (name at builder's discretion, or an existing home like `bobi/paths.py`) — the single atomic-write helper (tmp sibling + `os.replace`, optional fsync) that Phase 3 introduces and all durable-state writers adopt. Must not become a second copy of an existing helper: survey the five existing implementations (D092) and hoist the best one.
- `plans/2026-07-22-review-remediation-findings.md` — the committed findings appendix (part of this plan).

## Questionables

- **Q1:** Lane granularity — how many PRs? Options: (a) **5 lanes**: A = Phases 1–4 (behavioral fixes, bobi/), B = Phases 5–6 (behavior-preserving cleanup, bobi/; depends on A — same files), C = Phase 7 (event-server + web UI; parallel with A), D = Phase 8 (docs; parallel), E = Phase 9 (tests; lands after A) / (b) 9 lanes, one per phase. Recommendation: (a) — C and D are genuinely parallel to A; B/E serialize behind A anyway, so extra PRs buy review granularity we don't need given per-phase gates.
  **Decision (2026-07-22, Zach):** chose (a) — 5 lanes, cut in Split below.
  **Falsified in practice (2026-07-29):** the recommendation reasoned only about *parallelism* ("C and D are genuinely parallel to A; B/E serialize behind A anyway, so extra PRs buy review granularity we don't need given per-phase gates"). That last clause was wrong. Per-phase gates prove a *batch*; they do not make the batch cheap to *re-gate*, and a lane that cannot be cheaply re-gated cannot absorb a fix round. Lane A came out at 59 items / 47 production files / 2963 insertions, which took four full gate rounds to review and never converged — four `NEEDS FIXES`, with the unit suite green at every one (3853 passing) while a private key was readable over the setup API. **Reviewability is a split criterion co-equal with parallelism.** Q1's answer for lanes B–E stands; only Lane A's sizing is superseded. See the 2026-07-29 amendment for what happened to the lane itself.
- **Q2:** The 17 *plausible* and 12 *unverified* findings — (a) in scope, gated on builder re-verification (drop with a dated note in the PR if refuted) / (b) exclude. Recommendation: (a) — they're all low/medium and the re-verification cost is one read each.
  **Decision (2026-07-22, Zach):** chose (a) — in scope, re-verify each before fixing; drop refuted ones with a dated PR note.
- **Q3:** `bobi setup --resume` is a documented no-op (D084/Q116) — (a) remove the flag and its help text / (b) implement resume. Recommendation: (a) — `bobi setup` now opens the webapp daemon; the flag's premise died with the old flow (`run_setup()` itself is dead, Q023).
  **Decision (2026-07-22, Zach):** chose (a) — remove the flag (Phase 5).
- **Q4:** Expired compat surfaces (curator→sleep-cycle shims Q019/Q073, policy→long_term_memory aliases Q020/Q068/Q092, `BRAIN_MODEL_ENV` Q085, `_delta_text` re-export Q079) — (a) delete now (verifiers confirmed zero importers incl. bobi-deploy) / (b) hold another release. Recommendation: (a) — the windows were stated in-code and are 4+ releases past.
  **Decision (2026-07-22, Zach):** chose (a) — delete now (Phase 5).
- **Q5:** `/api/credential/value` env fallback serves arbitrary process env vars (D077) — (a) restrict to the credential var names declared by the installed pack's services/tools / (b) remove the env fallback entirely. Recommendation: (a) — the legitimate use (showing which declared secrets are already satisfied by the environment) survives; arbitrary reads die.
  **Decision (2026-07-22, Zach):** chose (a) — restrict to declared credential var names (Phase 7).

## Phases

Every task line: builder re-verifies the appendix entry against the tree, fixes (failing-test-first where it's a bug), flips the marker in the same PR. Items tagged *(plausible/unverified — re-verify first)* per Q2.

### Phase 1 — Session failure honesty (the trust-restoring fixes)

Fix shape: `_drain_turn`'s dead-transport path must make the failure observable to callers (set `_last_is_error`/raise per the design the docstrings already claim), so ACK, terminal status, and retry decisions become honest. D001/D002 likely fall out of one mechanism — fix the mechanism, not four symptoms.

- [x] **D001** `bobi/session.py:972` — `_process_message` ACKs a message whose turn died on a dead transport; the message is lost on restart instead of replayed (violates `_ack_message`'s own #688 invariant). No-ack on dead-transport turns. *(shipped early via PR #825 + bot PR #800 to unblock headless execute — see 2026-07-22 amendment)*
- [x] **D002/Q011** `bobi/subagent.py:601` — `run_phase_blocking` (and `spawn_adhoc`, line ~784) report `success=True` and persist `TERMINAL_COMPLETED` when the startup turn's transport died mid-drain; `spawn_adhoc`'s persistent path hardcodes `success=True`. Terminal status must reflect the error state. *(shipped early via PR #825 — see 2026-07-22 amendment)*
- [x] **D003** `bobi/session.py:1214` — `stop()` no-ops when `_keep_alive` doesn't exist yet (startup turn in flight); the session thread + brain subprocess survive forever. Make stop interrupt the startup phase too.
- [x] **D021** `bobi/session.py:1205` — `start()` waits the full timeout on `_ready` even after the session thread has already died; also watch thread liveness and return early.
- [x] **D067** `bobi/subagent.py:511` — `_run_agent_supervised`'s `except asyncio.TimeoutError` is unreachable (timeout never enforced in the coroutine); enforce it or remove the dead handler honestly.
- [x] **D073** `bobi/events/drain.py:349` — `monitor.error` inbox pushes omit `on_done=batch_ack.attach()`, ACKing their event seq at push time instead of after processing; attach the completion callback like every other push site.

**Validation gate** — do not exit this phase until every line passes; if a command fails, fix the cause and re-run.

- [x] New regression tests, written failing-first: dead-transport turn does NOT advance the ack cursor; dead-transport phase persists `TERMINAL_FAILED`/`session.failed`; `stop()` during a hung startup turn tears the session down; `start()` returns promptly when the thread dies
- [x] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q`
- [x] `pytest tests/integration -q -k "session or subagent or drain"`
- [x] Real-Claude e2e leg (`[stub]+[claude]` parametrization per CLAUDE.md) for the dead-transport ack/terminal path — this is brain-path risk, the claude leg is required

### Phase 2 — Workflow engine + agent-pack routing correctness

**Mostly superseded 2026-07-26** by `plans/2026-07-26-checklist-execution-model.md`, which deletes the workflow step machine (steps, handoff contracts, route conditions, await/resume). Nine items below fix internals scheduled for deletion and are `[f]`-superseded; **three survive and remain in scope** because they are not step-machine code — see the dated Amendment. Item-level rationale is on each line.

Fix shape for conditions (`variables.py` numeric comparisons) is withdrawn with Q017/D026 and D015.

- [f] **D005** `bobi/workflow/orchestrator.py:231` — suspended (await) run emits `agent/workflow.completed`. *Superseded 2026-07-26: `await`/suspend is deleted by the checklist plan; nothing to fix.*
- [f] **D027** `bobi/workflow/orchestrator.py:88` — `try_resume_for_event` claims a run before checking the workflow exists; nothing calls it (D018). *Superseded 2026-07-26: the whole await/resume feature is deleted rather than wired. The checklist plan's Problem 8 records that this was a missing wire, not a wrong architecture — the decision is to remove the feature, not repair it.*
- [f] **D024** `bobi/workflow/orchestrator.py:314` — launch `--role`/agent identity lost across suspend/resume. *Superseded 2026-07-26: no suspend/resume to lose it across.*
- [x] **D029** `bobi/workflow/orchestrator.py:546` — `_make_session` exception in the initial connect loop escapes both the retry try and the terminal-honesty try/finally, leaving the registry entry stuck `running`. **SURVIVES and is now load-bearing:** the checklist plan's in-progress monitor resolves ownership from live registry entries (its Q3), so an entry stuck `running` makes a dead unit look permanently alive and blocks re-dispatch forever. Fix the leak in whichever spawn path survives the cutover.
- [f] **D025** `bobi/workflow/orchestrator.py:848` — stale handoff files not cleared before a prompt step. *Superseded 2026-07-26: handoff files are deleted.*
- [f] **D028** `bobi/workflow/orchestrator.py:873` — non-mapping handoff YAML crashes with AttributeError. *Superseded 2026-07-26: handoff files are deleted.*
- [f] **Q017/D026** `bobi/workflow/variables.py:92` — route conditions resolved by textual substitution; add numeric comparison operators. *Superseded 2026-07-26: route conditions are deleted. (`${{}}` interpolation may survive — the checklist plan's Phase 4 re-verifies consumers before deleting `variables.py`.)*
- [x] **D015** `agents/dogfood-content-review/workflows/dogfood-content-review.yaml:35` — `issues_count > 0` uses the unsupported `>`. *Superseded 2026-07-26: this workflow migrates to a checklist; the condition ceases to exist.*
- [f] **D016** `agents/eng-team/workflows/pr-closed.yaml:14` — `merged == true` references bare `merged`. *Superseded 2026-07-26: `pr-closed.yaml`'s deterministic pieces become checklist items naming commands (`gh pr view <n> --json merged -q .merged`), so the condition is replaced rather than fixed.*
- [x] **D017** `agents/eng-team/agent.yaml:126` — `auto_dispatch` rule `event: github.issues.assigned` matches a type never emitted (adapter emits `github.issues` with the action in fields); fix the rule to match reality and add a validate-time check for unmatchable event types. **SURVIVES:** this is event→dispatch routing (`bobi/workflow/triggers.py` is explicitly KEPT by the checklist plan), not step-machine code, and per this plan's own Notes issue pickup needs a Slack directive until it lands.
- [f] **D060** `agents/dogfood-content-review/roles/manager/ROLE.md:16` — routing table dispatches workflows that don't exist in the pack. *Absorbed 2026-07-26: the checklist plan's Phase 4 rewrites every pack's routing surface, and correcting a table that is about to be replaced is waste. If the cutover is abandoned, this returns to scope.*
- [x] **D119** `agents/dogfood-content-review/agent.yaml:4` — declares `chat: slack` and routes "Slack DM" events but no slack service is declared; make the pack internally consistent (and note `chat:` is currently parsed-but-unread, Q022 — resolve coherently with Phase 5's decision on that key). **SURVIVES:** pack/service declaration consistency, unrelated to the step machine.

**Validation gate**

Rewritten 2026-07-26 to prove only the three surviving items (D029, D017, D119). The withdrawn lines proved suspend/resume, the condition parser, and handoff re-prompting — all of which the checklist plan deletes.

- [x] Failing-first test (**D029**): a `_make_session` exception in the initial connect loop leaves the registry entry **terminal**, never stuck `running` — asserted on the surviving spawn path, because the in-progress monitor's ownership check depends on it
- [x] Failing-first test (**D017**): an `auto_dispatch` rule whose `event:` can never be emitted fails validation; and the corrected rule actually matches a real `github.issues` payload with the action in fields
- [x] Failing-first test (**D119**): a pack declaring `chat: slack` with no slack service fails validation
- [x] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q && pytest tests/integration -q -k "workflow or orchestrator"`
- [x] `bobi validate` (or the validate suite) passes on all three `agents/` packs with the new unmatchable-event check active

### Phase 3 — Persistence atomicity (one helper, all writers)

- [x] **D092** `bobi/workflow/state.py:36` — hoist the atomic-write pattern (5 existing re-implementations, appendix lists them) into ONE helper; all five sites adopt it.
- [x] **D034** `bobi/setup/state.py:231` — `SetupState.save` bare `write_text`, no locking, concurrent FastAPI threadpool handlers; adopt the helper + a lock.
- [x] **D038** `bobi/brain/codex_config.py:214` — `write_codex_config` non-atomic rewrite of `$CODEX_HOME/config.toml` (crash = foreign entries lost); adopt the helper.
- [x] **D087** `bobi/spend_governor.py:83` — `record_invocation` unlocked read-modify-write of spend_governor.json; adopt helper + file lock.
- [x] **Q062/D071** `bobi/workflow/state.py:89` — `claim()` writes the temp file before the atomic claim, leaving a crash window that makes the run permanently unresumable; reorder per the appendix trace.
- [x] **Q039** `bobi/monitors/scheduler.py:1470` — durable JSON persisted in two conflicting styles within the same subsystem; converge on the helper (house pattern).

**Validation gate**

- [x] Helper unit tests incl. a crash-window test (kill between tmp write and replace → old state intact)
- [x] `grep -rn "write_text" bobi/ | grep -iE "state|config\.toml|governor"` shows no remaining bare durable-state writes (document any justified survivors in the PR)
- [x] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q`

### Phase 4 — bobi/ bug batch (each independent; failing-test-first)

- [x] **D009** `bobi/brain/codex.py:369` — CodexBrain.make_session clears the bobi-managed mcp_servers block from $CODEX_HOME/config.toml whenever a call site omits options['mcp_servers'],…
- [x] **D041** `bobi/build_render.py:237` — The image-build `verify: requires` step sets BOBI_VERIFY_PHASE as an unexported shell variable (`BOBI_VERIFY_PHASE=build; <check>`), so any check…
- [x] **Q122/D064** `bobi/cli.py:789` — `bobi setup --resume` is parsed, documented in help ('Resume an interrupted setup'), then immediately discarded with `del resume`. *(unverified — re-verify first)*
- [x] **D018** `bobi/cli.py:2710` — `bobi agent <name> event-server stop` crashes with an unhandled traceback on a corrupt/empty event-server.pid and on PermissionError, instead of…
- [x] **D066** `bobi/cli.py:3019` — `bobi agents update` (update-all path) exits 0 even when every pack update fails, so scripts/CI cannot detect failure.
- [x] **D065** `bobi/cli.py:3110` — `bobi agents browse` crashes with 'Unknown format code s' when a registry.yaml declares an unquoted numeric version (e.g. version: 1.0), because the…
- [x] **D039** `bobi/compose.py:659` — _merge_keyed_list crashes with a raw TypeError when an overlay list removes and re-adds the same keyed entry, because the tombstone (None) left at…
- [x] **D040** `bobi/compose.py:767` — _prune_one performs no validation of prune names, so an absolute path or `..` in a `prune:` entry deletes files/directories outside the compose…
- [x] **D085** `bobi/config.py:500` — Config._parse crashes on null-valued YAML keys: `event_server:` with an empty value raises AttributeError (None.get) and `spend_cap:` raises…
- [x] **D086** `bobi/costs.py:183` — rollup_costs guards token fields against non-numeric values via _tok but not the cost fields, so a string total_cost_usd (or cost_usd) in one…
- [x] **D020** `bobi/doctor.py:35` — run_doctor unconditionally runs the Claude CLI and Claude auth checks as required failures, so doctor reports broken health (exit 1) on hosts running…
- [x] **D019** `bobi/doctor.py:386` — _check_event_server probes hardcoded http://localhost:8080, producing a false required failure (doctor exit 1) for remote-configured instances that…
- [x] **D075** `bobi/events/adapters.py:106` — _parse_github_url uses a substring match for 'github.com', so GitHub Enterprise hosts like github.company.com are mis-parsed into a garbage org/repo…
- [x] **D031** `bobi/events/server.py:706` — register_slack_workspaces ignores the HTTP status of the signed POST /slack/workspaces and logs success (returning [team_id]) even when the server…
- [x] **D069** `bobi/history.py:188` — _project_from_path replaces every '-' with '/', mangling project names for any repo with a hyphen in its name (including bobi-agent itself), which…
- [x] **D022** `bobi/history.py:262` — _index_file counts a trailing partially-written JSONL line as read, so once the writer completes that line it is never indexed — the message is…
- [x] **D068** `bobi/history.py:316` — _fts_query breaks on queries containing a double quote (or an all-whitespace query), producing invalid FTS5 syntax that raises…
- [x] **D010** `bobi/kb/embedder.py:127` — embed()'s dead-sidecar recovery catches OSError, but the pooled httpx client raises httpx.ConnectError (not an OSError subclass), so the…
- [x] **D043** `bobi/kb/store.py:127` — _fts_query wraps each whitespace token in double quotes without escaping embedded double quotes, so any query token containing an odd number of '"'…
- [x] **D045** `bobi/manager_health.py:30` — The health endpoint uses a single-threaded HTTPServer with no handler timeout, so one half-open or stalled client connection blocks /health and…
- [x] **D004** `bobi/monitors/script_cache_checks.py:978` — script_cache self-heal invokes the blocking agent runtime synchronously on the single scheduler thread, stalling every other monitor for minutes.
- [x] **D023** `bobi/monitors/tool_checks.py:151` — tool_poll/venn_poll cache the resolved command keyed only on monitor name with no config fingerprint, so editing a monitor's command/query keeps…
- [x] **D032** `bobi/registry.py:221` — fetch() for an unpinned team silently downgrades 'latest published version' to the rolling main-push tarball when the remote version read transiently…
- [x] **D044** `bobi/runtime_guard.py:242` — with_mutable_runtime_package runs the strict +w sweep before entering its try/finally, so an EPERM partway through the unlock leaves every…
- [x] **D035** `bobi/setup/actions.py:361` — install_team only enforces the validated/hash-freshness gate when state.mode == 'create', so open/modify-mode teams can be installed from unvalidated…
- [x] **D036** `bobi/setup/authoring.py:296` — merge_agent_yaml claims chat is a setup-managed overlay key but never removes or overwrites an existing `chat:` when the user switches the team to…
- [x] **D033** `bobi/setup/webui/server.py:637` — _resolve_pending writes a pre-probe snapshot of the MCP entry back into state.spec.mcp_servers after an up-to-60s await, silently reverting any edit…
- [x] **D007** `bobi/setup/webui/server.py:953` — /api/mcp/detect (and the /api/browse folder picker) confine paths to BOBI_HOME (~/.bobi by default), not the user's home directory the comments and…
- [x] **D081** `bobi/setup/webui/server.py:1284` — GET /api/credential/value falls back to os.environ for any requested var name, so the endpoint serves arbitrary process environment variables (AWS…
- [x] **D080** `bobi/setup/webui/server.py:1373` — GET /api/file calls target.read_text() with no decode-error handling, so any non-UTF-8 file in the pack (which /api/files happily lists) makes the…
- [x] **D076** `bobi/slack.py:170` — format_slack_message blanket-replaces literal \n/\t escape sequences across the whole message, including inside code fences, corrupting quoted…
- [x] **D074** `bobi/slack_manifest.py:39` — render_manifest substitutes the user-supplied app name unescaped into unquoted YAML scalar positions, so names containing YAML-special characters…
- [x] **D042** `bobi/tool_library.py:168` — resolve_dependencies de-dupes by name with FIRST occurrence winning while the tool_library union appends leaf entries after base entries, so a leaf…
- [x] **D084** `bobi/tool_library.py:225` — The dependency-guide leaf-wins check (`if not guide_path.exists()`) mistakes a stale guide from a previous install for a team-shipped file, so…
- [x] **D037** `bobi/webapp/daemon.py:191` — daemon stop() sends SIGTERM/SIGKILL to the pid from a possibly-stale pidfile without confirming it is the bobi app, so pid reuse causes it to kill an…

**Validation gate**

- [x] Every item above has a test that failed before its fix (or an `[x]`-with-note where re-verification refuted it)
- [x] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q && pytest tests/integration -q`

### Phase 5 — Dead-code purge (deletions only; grep-gated)

Includes Q4's expired compat layers and Q3's flag removal once decided. For config keys parsed-but-never-read (Q022: `registries`/`default_role`/`chat`): delete the parse OR wire the read — deleting is the default unless Phase 2's D119 fix gave `chat:` a real consumer.

- [x] **Q080** `bobi/brain/__init__.py:105` — The public BRAIN_MODEL_ENV compatibility alias has zero importers in the repo, tests, and the private deploy repo.
- [x] **Q078** `bobi/brain/claude.py:27` — BrainCapabilities is imported but unused in both brain/claude.py and brain/codex.py — a #789 refactor leftover.
- [x] **Q077** `bobi/brain/claude.py:125` — The trailing `if last_error is not None: raise last_error` after the connect retry loop is unreachable.
- [x] **D061** `bobi/cli.py:348` — _list_agent_packs in cli.py is a byte-for-byte duplicate of service.py's _list_agent_packs, and the cli copy is dead code.
- [x] **D062** `bobi/cli.py:958` — Dead helpers _find_pid_path (line 948), _stop_manager_pid (line 958) and _run_from_config (line 364) duplicate the live stop/start paths in…
- [x] **Q008** `bobi/cli.py:1594` — Six unreachable `if not project_path:` guards / else-branches follow `_detect_project_root()` (or `paths.home_dir()`), which can never return a falsy…
- [x] **Q032** `bobi/config.py:317` — Config fields `registries`, `default_role`, and `chat` are parsed from agent.yaml but never read by any production code.
- [x] **Q125** `bobi/config.py:409` — Config.brain_small_model property has zero callers — the one real consumer bypasses it and reads the raw brain dict. *(unverified — re-verify first)*
- [x] **Q038** `bobi/config.py:510` — Config.default_role is parsed from agent.yaml's `defaults.role` but never read by anything, so the key is silently ignored.
- [x] **Q045** `bobi/doctor.py:517` — `_check_policy` is a self-described 'deprecated compatibility wrapper for one release' with zero production callers, several releases after the…
- [x] **Q053** `bobi/history.py:15` — SESSIONS_DIR module constant is never referenced.
- [x] **Q013** `bobi/history.py:447` — context_for_events (49 lines) has zero production callers — only tests exercise it.
- [x] **Q048** `bobi/history.py:498` — start_background_indexer has zero callers anywhere in the repo, including tests.
- [x] **Q052** `bobi/inbox.py:102` — Inbox._closed is written in __init__ and close() but never read anywhere.
- [x] **Q094** `bobi/kb/store.py:174` — KBStore.kb_dir() and KBStore.db_path_for() static methods have zero callers in the repo, including tests.
- [x] **D083** `bobi/mcp/__init__.py:1` — bobi/mcp is an empty leftover package whose docstring claims 'Built-in MCP servers shipped with bobi' while it contains no servers and nothing…
- [x] **Q121** `bobi/memory.py:169` — reference_memory_path() has zero callers; the one place that needs the reference.md path (monitors/scheduler.py:1126) hand-builds it inline instead. *(unverified — re-verify first)*
- [x] **Q030** `bobi/memory.py:381` — The policy->long_term_memory deprecation aliases (memory.MAX_POLICY_CHARS/load_policy/format_policy_prompt, paths.policy_path/policy_cursor_path,…
- [x] **Q014** `bobi/monitors/scheduler.py:567` — The rest of the curator→sleep-cycle compat layer is five releases past its stated one-release window: _default_spawn_curator (567), the spawn_curator…
- [x] **Q055** `bobi/monitors/scheduler.py:1097` — Three deprecated curator wrappers — _load_curator_prompt (1097), _on_curator_result (1414), _publish_policy_updated (1452) — have zero callers…
- [x] **Q057** `bobi/monitors/script_cache_checks.py:1046` — approve_pending's scripts_dir parameter is never read — the body resolves all paths via _pending_path/_pin/_scripts_dir — yet tests pass a directory…
- [x] **Q095** `bobi/paths.py:91` — paths.agent_runtime_root is a pure alias of agent_run_root with zero callers.
- [x] **Q118** `bobi/paths.py:183` — compose_lock_path() has zero callers; every compose-lock consumer builds `<dest>/compose-lock.json` inline against its own base dir. *(unverified — re-verify first)*
- [x] **Q119** `bobi/paths.py:282` — worktrees_dir() points at state_dir()/worktrees, a location nothing in the codebase uses — workflow worktrees actually live under the repo's… *(unverified — re-verify first)*
- [x] **Q102** `bobi/prompts/resolver.py:194` — _load_policy_section() is a private 'deprecated alias for one release' with zero callers — a private name cannot serve external compat, so it was…
- [f] state:falsified **Q081** `bobi/sdk.py:42` — TERMINAL_STATUSES tuple is defined but never read anywhere.
- [x] **Q079** `bobi/sdk.py:71` — Module constant CLAUDE_CLI has zero readers anywhere and forces a shutil.which() at import time.
- [x] **Q113** `bobi/sdk.py:198` — sdk.state_dir() wrapper (delegating to paths.state_dir) has zero callers. *(plausible — re-verify first)*
- [x] **Q082** `bobi/sdk.py:456` — SessionRegistry.log_path's only caller is its own unit test.
- [x] **Q031** `bobi/service.py:709` — service.restart_team has zero callers anywhere in the repo, including tests.
- [x] **Q054** `bobi/session.py:17` — session.py imports hashlib (line 17) and Path (line 22) but uses neither.
- [x] **Q037** `bobi/setup/__init__.py:19` — run_setup() has zero callers; the `bobi setup` CLI command now opens the webapp daemon URL instead, leaving the setup package's only public entry…
- [x] **Q120** `bobi/setup/actions.py:108` — default_secret_prompt() — a click-based masked terminal prompt — has zero callers since setup became web-UI-driven. *(unverified — re-verify first)*
- [x] **Q065** `bobi/setup/actions.py:161` — installed_team_name has no production callers — only its own unit tests reference it.
- [x] **Q066** `bobi/setup/actions.py:173` — resolve_or_fetch has no production callers — only its own unit tests reference it.
- [x] **Q067** `bobi/setup/authoring.py:614` — _env_var_fallback is an unreachable fallback: the `conn.credential_var or _env_var_fallback(...)` branch in tools_prompt can never fire.
- [x] **Q068** `bobi/setup/llm.py:23` — The _delta_text re-export from bobi.brain.claude exists only so a test can import it from the old location; no production code uses llm._delta_text.
- [x] **Q049** `bobi/subagent.py:658` — _load_policy_prompt, the 'deprecated alias for one release' from the policy→long_term_memory rename, has zero callers and has outlived several…
- [x] **Q050** `bobi/subagent.py:1687` — _parse_check_output is a back-compat shim with zero production callers, while run_check_blocking re-inlines its exact verdict→(finding, summary,…
- [x] **Q086** `bobi/validate.py:77` — ValidationResult.errors property has zero callers anywhere.
- [x] **Q059** `bobi/workflow/orchestrator.py:592` — failed_step is assigned at 7 sites in _run_workflow_async and never read; the Any and AgentResult imports are also unused.

**Validation gate**

- [x] For every deleted name: `grep -rn "<name>" bobi/ tests/ agents/ docs/ skills/ event-server/` returns nothing (or only the changelog-adjacent mention the PR itself adds)
- [x] `pytest tests/ -q` (full suite incl. integration) green with zero test edits other than deleting tests of deleted code

### Phase 6 — Consolidations & simplifications (behavior-preserving)

Each appendix entry names the surviving implementation — move callers to it; never mint a third copy.

- [x] **Q012** `bobi/auth_bootstrap.py:192` — auth_bootstrap._parse_conversation hand-rolls the conversation-ref grammar that bobi.conversation.parse_conversation already implements.
- [x] **Q083** `bobi/brain/__init__.py:231` — GATEWAY_UNRESOLVED_BASE_URL lives in brain/__init__.py, forcing gateway.py's require_gateway_base_url into a function-level circular-import…
- [x] **Q026** `bobi/brain/base.py:148` — stream_once is part of the de facto BrainFactory contract but is missing from the Protocol and unimplemented by CodexBrain, so the brain interface is…
- [x] **Q027** `bobi/brain/claude.py:328` — _claude_transcript_path re-implements the Claude transcript locator that already exists in bobi/chat_history.py (_claude_projects_dirs +…
- [x] **Q087** `bobi/build.py:70` — _flatten_if_chained's re-read/setdefault/rewrite of the composed agent.yaml (lines 70-74) is redundant — compose() already sets the agent name to the…
- [x] **D050/Q007** `bobi/cli.py:95` — The entire local event-server port-resolution trio (_parse_local_event_server_port, _event_server_port_file, _selected_local_event_server_port, ~50…
- [x] **Q009** `bobi/cli.py:151` — `_ensure_root_bound()` is behaviorally identical to `_detect_project_root()` — its body re-implements the first two lines of the latter and then…
- [x] **Q046** `bobi/cli.py:925` — Three single-caller pass-through wrappers around bobi.service/bobi.events functions add indirection the file's own house pattern avoids:…
- [x] **Q126** `bobi/cli.py:1155` — The hidden `ask` command's `--source` option is exercised by nothing in any repo; every documented invocation relies on the 'engineer' default. *(unverified — re-verify first)*
- [x] **Q047** `bobi/cli.py:1661` — The doctor command's result loop uses `getattr(r, "required", True)` and `hasattr(r, "sandbox_error")` defensiveness against attributes that every…
- [f] state:refuted **Q127** `bobi/cli.py:2427` — `monitors add --url` is exercised by nothing in any repo, and Monitor.extra already accepts arbitrary keys (including url) straight from… *(unverified — re-verify first)*
- [x] **D063** `bobi/cli.py:2897` — The --requested-by JSON parse/validate block is copy-pasted identically in _dispatch_agent and _run_agent_wait.
- [x] **Q090** `bobi/compose.py:359` — merge_workspace hand-rolls a recursive copy that shutil.copytree(src, dest/'workspace', dirs_exist_ok=True) already provides.
- [x] **Q089** `bobi/compose.py:507` — The monitor merge threads a redundant monitor_order list through three functions when the insertion-ordered monitor_records dict already carries the…
- [x] **Q088** `bobi/compose.py:723` — _PRUNE_DIR_SURFACES is an identity-mapping dict whose values are never read and whose 'roles' entry is unreachable.
- [x] **D088/Q124** `bobi/config.py:336` — The launch-admission default values are hand-maintained in three places — the Config.launch_admission field default_factory,…
- [f] state:partly-applied **Q109** `bobi/config.py:496` — Three accepted spellings reach the one event-server-URL setting — `event_server: <str>`, `event_server: {url: ...}`, and `event_server_url:` — and… *(plausible — re-verify first)*
  - Re-derived 2026-08-10 against `03d4ee1`. The finding's load-bearing evidence is FALSE at current main: `tests/integration/conftest.py:71` writes `"event_server": {"url": ...}` — the dict form is what every integration test's agent.yaml uses — and the raw `event_server_url:` key appears in nine test files, not the two named. Deleting the dict branch breaks the integration harness, and narrowing an accepted config spelling is a behaviour change this behaviour-preserving phase cannot decide.
  - APPLIED (the half that is unambiguous and behaviour-preserving): the `ingress.py` hint now names `event_server`, the spelling every pack, doc and `bobi setup` actually writes, instead of the parses-but-unauthored `event_server_url`. Two hint assertions retargeted.
  - NOT APPLIED: collapsing the three spellings to one. Needs a lane that can take a schema-compatibility decision.
- [x] **Q093** `bobi/config.py:615` — The event-server deployment/cursor/bubble state persistence (config.py:601-701) is transport session state, not package configuration, and sits in a…
  - Applied 2026-08-17. The block (at `config.py:714-815` by then) moved whole
    to `bobi/events/state.py` beside its consumer `events/server.py:
    ensure_bubble`; function-local `json`/`os`/`re`/`paths` imports hoisted to
    module top. Five source importers (doctor, events/publish, events/server,
    service, subagent) and 19 test files retargeted; `fsutil.py`'s and
    AGENTS.md's `config.save_bubble_state` pointers updated. config.py sheds
    ~100 lines and its `atomic_write_json` import. The three deployment-state
    tests moved from test_config.py to test_bubble.py with the code they test.
  - The 0600 exception (CLAUDE.md's one sanctioned bypass of the atomic
    helper) was asserted by NO test; now pinned in test_bubble.py. Review
    caught the first version of that pin half-vacuous (the trailing chmod
    satisfied both arms; O_TRUNC unexercised) — the final test neutralizes
    chmod in the creation arm and proves truncation by byte-equality, with
    three named red mutants (loose create mode, dropped O_TRUNC, dropped
    trailing chmod).
  - Review additions: `event-server/core/src/core.ts`'s doubly-dead pointer
    (`bobi/config.py:load_or_mint_bubble` — wrong file AND a function that
    never existed) retargeted to `ensure_bubble`/`events/state.py`;
    config.py's docstring no longer claims an fsutil dependency it lost;
    the duplicate `TestDeploymentState` in integration/test_config_resolution
    deleted (verbatim copies of the moved unit tests, with dead setup);
    state.py's over-claiming module docstring rewritten.
- [x] **Q092** `bobi/costs.py:156` — rollup_costs takes a group_by parameter that its body never references.
- [x] **Q114** `bobi/dep_bootstrap.py:88` — ResolvedRecipe.from_install and ResolvedRecipe.from_agent are the identical one-line function under two names. *(plausible — re-verify first)*
- [x] **Q091** `bobi/dep_bootstrap.py:419` — pathlib.Path is imported inside four separate functions (and string-quoted in signatures) although a top-level stdlib import has no cycle risk.
- [x] **Q034** `bobi/env.py:18` — env._configured_brain/pin_brain_from_root re-implement the brain-mapping extraction and defaults that Config._parse and the Config.brain_* properties…
- [x] **D095** `bobi/events/adapters.py:81` — Running `git remote get-url origin` and normalizing the remote URL to a GitHub owner/repo slug is implemented twice:…
- [x] **Q019** `bobi/events/adapters.py:118` — adapters.py hand-rolls Slack channel name→ID resolution (_resolve_channel_names + _is_channel_id) that bobi/slack.py's resolve_channel_id already…
- [x] **D096** `bobi/events/adapters.py:209` — _detect_slack inlines a Slack auth.test call (GET + bearer header + team_id/bot_id extraction) that events/server._slack_auth_info already provides,…
- [ ] **Q108** `bobi/events/client.py:49` — Wall-clock timestamps are written in two conflicting conventions — local-naive time.strftime ISO strings in six modules vs timezone-aware UTC… *(plausible — re-verify first)*
- [x] **D077/Q105** `bobi/events/drain.py:76` — _without_placeholder_fields is duplicated verbatim in drain.py and channels.py.
- [x] **Q064** `bobi/events/server.py:236` — ensure_running remaps five unprefixed env vars to BOBI_ES_* with five identical two-line if-blocks.
- [x] **Q112** `bobi/events/server.py:413` — authorize_resources repeats the same 6-line 'log warning / append unbacked / keep-if-not-filtering / continue' tail four times. *(plausible — re-verify first)*
- [x] **Q020** `bobi/events/server.py:604` — _slack_auth_info and _slack_app_id are general Slack Web API helpers living in the event-server launcher module, privately imported across module…
- [x] **Q104** `bobi/history.py:14` — history.py hardcodes Path.home()/'.claude'/'projects' for locating Claude transcripts while chat_history.py's _claude_projects_dirs() is the house… *(plausible — re-verify first)*
- [x] **Q035** `bobi/http.py:57` — post/get/put/delete each hand-build the same optional-kwargs dict instead of being one-line delegates to the module's own request() helper.
- [x] **D078** `bobi/ingress.py:55` — The agent.yaml explicit `subscribe:` parsing (yaml load + env interpolation + str-or-list normalization + strip/filter) is implemented twice:…
- [x] **Q097** `bobi/kb/embedder.py:146` — embedder.stop() hand-parses the pid file (int(read_text().strip()) plus ValueError handling) while is_running() three functions above already uses…
- [x] **Q096** `bobi/memory.py:181` — cold_memory_kb_name() is a function wrapping the module constant COLD_MEMORY_KB_NAME, with exactly one caller.
- [x] **Q033** `bobi/memory.py:215` — The cold-memory sync code in memory.py reaches into KBStore internals (store._connect(), _fetchone, _chunk_text) in two places to answer a question…
- [x] **Q056** `bobi/monitors/scheduler.py:84` — _load_framework_checks hand-rolls importlib (sys.modules check + spec_from_file_location + module_from_spec + exec_module) to load modules that are…
- [x] **D093/Q058** `bobi/monitors/scheduler.py:149` — _parse_iso (ISO-8601 parse with 'Z'->'+00:00' replacement and naive-to-UTC defaulting) is duplicated verbatim in two modules of the same package.
- [x] **Q015** `bobi/monitors/scheduler.py:1215` — _on_sleep_cycle_result repeats the same ~10-line failure block seven times: build a detail string, log.warning('… - cursor NOT advanced, retrying…
- [x] **D094** `bobi/monitors/script_cache_checks.py:564` — _scripts_dir() and the monitor-name sanitizer (replace('/', '_').replace('..', '_')) are duplicated between script_cache_checks.py and tool_checks.py…
- [x] **Q016** `bobi/monitors/script_cache_checks.py:794` — _slack_notify bypasses the module's own policy-resolution chokepoint: it re-reads and re-parses agent.yaml via _install_policy() and consults…
- [ ] **Q018** `bobi/registry.py:52` — The project_path parameter is threaded through ~15 registry functions but never actually used — the cache and registry list are global.
- [ ] **Q028** `bobi/sdk.py:51` — Claude-CLI path resolution (_resolve_cli_path/get_cli_path/CLAUDE_CLI) lives in the generic session-registry module instead of the claude brain…
- [x] **Q085** `bobi/sdk.py:121` — Bound-root access is split between two routes: monitors import paths.bound_root directly (aliased as get_project_root) while kb/embedder.py and…
- [x] **Q084** `bobi/sdk.py:538` — load_resumable_session_id re-reads '<name>.brain' inline instead of calling load_session_brain defined 30 lines above.
- [x] **Q021** `bobi/setup/actions.py:185` — save_credential's prompt_fn injection is vestigial: every live caller passes a constant-returning lambda, and the only real prompt implementation…
- [x] **Q006** `bobi/setup/actions.py:353` — The setup web UI's library module imports `_install_pack`, `_write_install_gitignore`, and `_resolve_agent_pack` from `bobi.cli` instead of from…
- [x] **Q070** `bobi/setup/mcp_registry.py:45` — MCPServerSpec carries a configurable auth-header micro-DSL (auth_header + auth_value with {ref} formatting) and a transport field that no spec in the…
- [x] **Q071/D123** `bobi/setup/webui/server.py:85` — serialize_state hardcodes the four spec-slot names as a literal tuple instead of using the canonical SPEC_SLOTS constant from bobi.setup.state.
- [x] **Q069** `bobi/setup/webui/server.py:108` — _probe_event_server hand-rolls an outbound HTTP request with raw urllib.request, against the repo's explicit house rule that framework code uses the…
- [x] **Q022** `bobi/setup/webui/server.py:543` — The entire MCP connection-test conversational flow (~130 lines: _propose_test, _resolve_pending, _record) lives as nested async generators inside the…
- [x] **Q023** `bobi/setup/webui/server.py:1284` — Credential resolution precedence is handled in two conflicting styles: the documented house pattern is process-env-first (exported var wins over…
- [x] **D052** `bobi/slack.py:311` — Slack channel-name-to-ID resolution via paginated conversations.list is implemented twice with already-divergent behavior: slack.resolve_channel_id…
  - Re-derived 2026-08-13 against `bf11965`: **already resolved, by Phase 6 batch 1's Q019** (PR #993). `events/adapters._resolve_channel_names` no longer paginates `conversations.list` — it delegates to `bobi.slack.resolve_channel_id` and adds only the subscription-side drop-vs-raise policy. `conversations.list` now appears once in `bobi/`, at `slack.py:448`. Flipped as bookkeeping; no code change was owed.
  - The workspace-widening failure mode D052 describes is NOT closed by that consolidation and is deliberately still open — see the Q019 note in batch 1 and PR #993. `_slack_keys(team_id, [])` still subscribes workspace-wide when every configured channel fails to resolve.
- [x] **Q010** `bobi/subagent.py:1302` — _start_event_subscription's 5-branch registration decision tree re-implements the authorize→PUT-subscriptions→on-failure-re-register sync sequence…
  - The finding calls the two sync blocks' differences "log wording and whether
    authorize failure falls back to the raw subscribe list". The second is a
    real behaviour difference, so this needed a decision, not a merge.
    Provenance made it: the REMOTE block is the original (its history runs back
    through #488, `cf6559b4`, which added the pre-PUT authorization fallback);
    #538 (`a42106e9`) added the local arm later as a copy that flattened the two
    try/excepts into one and dropped the fallback. `authorize_resources` still
    documents the original's contract — "The server remains authoritative and
    will reject the update if the grant is truly absent". Consolidated onto the
    original as `_sync_or_reregister`, per this phase's "move callers to the
    surviving implementation" rule.
  - The collapse is larger than the finding describes: the two "register fresh"
    and two "pre-bubble upgrade" branches were byte-identical as well, so the
    whole local/remote split reduces to an `ensure_running` prelude plus one
    shared three-way decision. The unconfigured-local arm deliberately keeps its
    unconditional fresh register (an ephemeral 8080 server's old deployment is
    not something to sync onto) — that asymmetry is now commented, not implicit.
  - Every pre-existing test configured a REMOTE url, so the arm being changed
    had no coverage at all. Seven tests added; four mutants verified red.
- [x] **Q051/D070** `bobi/subagent.py:1738` — run_check_blocking, run_gate_blocking, and run_curator_blocking each repeat the same ~12-line preamble: local `import hashlib`, sha256 slug,…
  - Q051 applied: `_register_verdict_session` collapses the three preambles,
    which differ only in role, phase, hash seed and title. The slug prefix IS
    the phase in all three, so it is derived. `hashlib` moved to the module
    imports, retiring the fourth local copy — which is in `_adhoc_session_name`,
    not `spawn_adhoc` as the finding states.
  - **D070 is NOT applied, and needs no successor: its premise is dead.** It
    asks `run_check_blocking` to route through `_parse_check_output`, which no
    longer exists — Phase 5 batch 2 deleted it as **Q050**, resolving the same
    duplication in the opposite direction (keep the inline copy, delete the
    shim and its seven tests). Applying D070 as written would resurrect it.
  - The DEFAULT slug turned out to be untested: a mutant giving it the wrong
    prefix left all 155 tests green. Three tests added; three mutants red.
- [x] **Q029** `bobi/tool_library.py:44` — The tool_library.py module deliberately shadows the sibling bobi/tool_library/ data directory, a hazard the module spends a docstring paragraph…
  - Applied as prescribed — `bobi/tool_library/__init__.py`, `CATALOG_DIR`
    reduced to `Path(__file__).parent`, shadowing paragraph deleted. Wheel
    contents differ by exactly that one entry name (213 entries both sides).
  - Two corrections to the finding's evidence. Its enumeration is stale: the
    catalog holds **four** entries (`otel` was added), not the three named. And
    it omits `.github/workflows/container.yml`, which path-filters on the
    literal `bobi/tool_library.py`; left alone it would match nothing and
    silently stop triggering the image lane — the #909 vacuous-lane shape its
    own neighbouring comment warns about. Now `bobi/tool_library/**`, which also
    newly covers the catalog pins that bake INTO the image (#486).
  - The move puts `__init__.py` and `__pycache__/` beside the entries for the
    first time; two tests pin that only `tool.yaml`-bearing directories are
    offered as catalog ids. Two mutants red.
- [x] **D051/Q123** `bobi/webapp/daemon.py:92` — webapp/daemon.py reimplements pid-file helpers (_pid_alive, _read_int) that already exist as bobi.sdk.pid_alive / bobi.sdk.read_pid, and the copies…
  - Done: `_pid_alive`/`_read_int` deleted; daemon delegates to `bobi.sdk.pid_alive`
    and a new `sdk.read_int_file` (`read_pid` delegates to it, name and behavior kept).
    EPERM now reads as ALIVE, which newly reaches `os.kill` in `stop()` — handled as
    `AppStatus.not_permitted` with the state files deliberately NOT cleared. `_ping`
    moved to the pooled `bobi.http` client (Q123).
- [x] **D097/Q024** `bobi/webapp/daemon.py:203` — daemon.py re-implements three launch fragments that webui_common/launcher.py already owns: the socket-bind + uvicorn serve pattern, the…
  - Done: `launcher` grew `serve_socket`/`run_server`/`write_secret`/`open_browser_soon`
    as named primitives; `serve_local`, `serve_container` and `daemon.run_foreground`
    all compose them. `run_foreground` still binds its own socket — fixed port, and the
    pid/port files must land between bind and serve — which is why primitives were the
    right shape rather than a third whole-launch entry point.
- [x] **Q075** `bobi/webapp/server.py:158` — Success-only GET handlers inconsistently wrap runtime dicts in JSONResponse (agent_spend, agent_health, agent_sessions, agent_status, subagents,…
  - Done: re-derived at cb45d00 the wrapper-only always-200 set is TEN, not the eight
    listed (agent_overview and agent_runs were added after the review). All ten now
    return bare dicts. Byte-identity proven by an A/B probe over 24 responses, and the
    probe itself mutant-checked first — its initial run was vacuous.
- [x] **Q107** `bobi/webapp/server.py:246` — The setup_open handler's on_finish closure embeds ~30 lines of slot-rename filesystem business logic (shutil.move of agent dirs, nested run/ salvage,… *(plausible — re-verify first)*
  - Done as `bobi/webapp/slots.py::finalize_slot`, but the finding's stated premise is
    WRONG at cb45d00: server.py's docstring claims 'HTTP mapping ... AND the hosted-
    onboarding surface', so this was not a charter violation. Extracted anyway for the
    real reason — the closure was unreachable except through a full HTTP request plus a
    live SetupState. Kept in bobi/webapp/, not bobi/setup/, because slots are the
    webapp's hosted-onboarding concept.
- [x] **Q072** `bobi/webui_common/launcher.py:37` — serve_local's `announce` callback parameter (and the Announcer type alias) has exactly one caller, whose lambda reproduces the default label-based…
- [x] **Q060** `bobi/workflow/orchestrator.py:108` — _find_project_root(cwd) takes and ignores a cwd parameter, and both call sites pass a meaningful-looking cwd that has no effect.
- [f] state:falsified **Q063** `bobi/workflow/orchestrator.py:128` — _setup_worktree re-imports subprocess locally as sp even though the module already imports subprocess at top level and uses it directly elsewhere in…
  - Re-derived 2026-08-09/10. The premise is false: `bobi/workflow/orchestrator.py` has NO top-level `import subprocess`, and `_resolve_repo_root` uses no subprocess at all (D095 routed it through `bobi.gitutil.origin_url`). The only reference left is the function-local `import subprocess as sp`. Deleting that local import, as written, BREAKS the module. The inverse — hoisting it to module scope — is a judgement call, not this finding.
- [x] **Q061** `bobi/workflow/orchestrator.py:435` — _make_session's if/else on agent_name executes the identical call in both branches.

**Validation gate**

- [ ] `pytest tests/ -q` (full suite) green
- [ ] Spot e2e (stub brain, isolated `BOBI_HOME`): agent boots, one event round-trips — proves the moved plumbing still wires up

### Phase 7 — event-server TS + web UI (bugs, security, duplication)

Read `docs/FRONTEND_QA.md` before touching the static UIs. Security items first: D007 (attr injection via markdown links — `esc()` must escape quotes), D077 per Q5, D006 (path confinement), D082 (register payload validation).

- [ ] **D098** `bobi/setup/webui/static/app.css:8` — The framed app-window chrome (.app rule, body radial-gradient background, and the data-retro grid overlay) is copied verbatim between the setup UI…
- [ ] **D099** `bobi/setup/webui/static/app.js:20` — HTML-escaping is hand-rolled in three variants across the two UIs: setup app.js esc (5 chars incl. quotes), agent.js esc (3 chars, pre-markdown), and…
- [ ] **D124** `bobi/setup/webui/static/app.js:90` — The token-header JSON fetch wrapper with server-gone health tracking is implemented independently in both SPAs: setup app.js getJSON/postJSON +… *(plausible — re-verify first)*
- [ ] **Q073** `bobi/webapp/static/shell.js:166` — The dynamic `import("./views/agent.js").catch(() => null)` with the stub() fallback ('The agent view is coming in this build.') is leftover…
- [ ] **Q076** `bobi/webapp/static/views/agent.js:17` — The header interpolates the agent name into an innerHTML template with a hand-rolled `name.replace(/[&<>]/g, "")` strip — the single deviation from…
- [x] **D008/Q025** `bobi/webapp/static/views/agent.js:189` — Markdown link renderer interpolates the URL into a double-quoted href attribute without escaping quotes, allowing agent output to inject…
- [ ] **Q074** `bobi/webapp/static/views/agent.js:322` — loadMessages wraps `await api(...)` in try/catch, but api() is designed never to reject — its own catch returns {ok:false,status:0,data:null} — so…
- [x] **D011** `event-server/core/src/adapters/chat-sdk-slack.ts:82` — The blanket `if (innerEvent.subtype) skip` drops every Slack message carrying subtype 'file_share', so file uploads in DMs and thread replies are…
- [x] **D091** `event-server/core/src/adapters/discord.ts:91` — The attachment-to-files normalization loop (build Array<Record<string,string>> with per-key presence checks and String() coercion, then mirror into…
- [x] **Q117** `event-server/core/src/adapters/github.ts:42` — Webhook-payload field extraction is handled in two conflicting styles: linear/whatsapp/discord narrow at runtime (asRecord/stringField helpers,… *(plausible — re-verify first)*
- [x] **D047** `event-server/core/src/circuit-breaker.ts:237` — The tripped-breaker pause buffer is unbounded and only flushed lazily on the next event in the same conversation key, so a hot external loop grows…
- [x] **Q098** `event-server/core/src/circuit-breaker.ts:286` — isBreakerTripped has zero callers anywhere in the repo, including tests.
- [x] **Q101** `event-server/core/src/core.ts:27` — SlackNormalizationResult.skip is redundant state — it is always exactly `event === null`, and consumers already double-check both.
- [x] **Q100** `event-server/core/src/core.ts:980` — createIngestEvent hardcodes its topic spelling as [topic, `ingest/${topic}`] instead of calling sourceQualifiedTopics, even though that helper's own…
- [x] **D089** `event-server/core/src/core.ts:1314` — handleRegisterDeployment trusts `body.subscriptions as string[]` with no shape validation on the unauthenticated MINT path: a string value passes the…
- [x] **D046** `event-server/core/src/core.ts:2211` — In handleChannelsSend, a file reply resolving a placeholder (edit_ref + mode update/final) on a channel without edit support silently discards the…
- [x] **Q099** `event-server/core/src/core.ts:2381` — handleSlackWorkspaceRegister hand-rolls three raw fetch() calls to the Slack Web API (auth.test twice, bots.info once) that the already-imported Chat…
- [x] **D048** `event-server/core/src/core.ts:2445` — handleSlackWorkspaceRegister's global workspace record update is a non-atomic read-merge-write (getSlackWorkspace then putSlackWorkspace of…
- [x] **Q116** `event-server/core/src/gateway/discord.ts:203` — DiscordGatewaySession.onTimer takes a parameter typed as the literal "heartbeat" and guards against other values — generality for timer kinds that… *(plausible — re-verify first)*
- [x] **D090** `event-server/src/local.ts:293` — storage.deliver() contains three near-identical copies of the seq-assign / eventBuffer-push / trim / JSON-serialize / broadcast-to-websockets block…
- [x] **D049** `event-server/src/local.ts:402` — readBody buffers the entire request body into memory with no size cap, so the ingest route's 256KB limit (and every other route) is enforced only…
- [x] **Q115** `event-server/src/local.ts:838` — evictStaleDeployments re-implements subscription-index and deployment removal that the same file's storage.removeSubscription and… *(plausible — re-verify first)*

**Validation gate**

- [x] `cd event-server && npm test` (vitest) green, incl. new failing-first tests: `file_share` subtype delivered, breaker pause-buffer bound, `readBody` size cap, workspace-register shape validation
- [ ] New JS escaping tests (or minimal harness per FRONTEND_QA.md): quotes escaped in href interpolation; agent-name rendering
- [ ] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q -k "webapp or webui"`

### Phase 8 — Documentation drift sweep

For each item: re-verify the claim against the tree, then edit the doc to say the truth (or delete the stale artifact — the three shipped-issue spec files and superseded design docs get deleted, matching the plans/ convention). No runtime code changes in this phase.

- [x] **D113** `CLAUDE.md:70` — '(see Bug fixes above)' references a 'Bug fixes' section that does not exist anywhere in the file (nor in the identical root AGENTS.md).
- [x] **D106** `README.md:295` — The 'Under the hood' command block shows `bobi agent <name> subagents launch --role <role> --task "context"` without the -w/--workflow option, which…
- [x] **D121** `agents/eng-team/README.md:20` — README's package layout listing omits shipped files: workflows/stall-recovery.yaml and tools/image-gen.md.
- [x] **D120** `agents/eng-team/roles/engineer/ROLE.md:148` — The reusable base eng-team engineer prompt embeds bobi-agent-specific test-setup instructions ("For this repo, broad non-integration tests use `pip…
- [x] **D122** `agents/personal-assistant/agent.md:87` — Setup docs say install prompts for `SLACK_BOT_TOKEN` and `VENN_API_KEY`, omitting `SLACK_SIGNING_SECRET` which agent.yaml also declares as a required…
- [x] **D082** `bobi/sdk.py:5` — sdk.py module docstring claims every session 'wraps a ClaudeSDKClient', contradicting the post-#485 brain-agnostic session model.
- [x] **D079** `bobi/slack_manifest.py:23` — Module comment points at event-server/src/index.ts for the Slack webhook route, but that file does not exist.
- [x] **D110** `docs/BUILDING_AGENT_TEAMS.md:149` — The native-service list ('github', 'slack', 'linear') omits whatsapp and discord, which are fully registered native ingestion adapters.
- [x] **D111** `docs/BUILDING_AGENT_TEAMS.md:246` — The weekly-job worked example claims eng-team ships context/prep-doc.md 'wired from the director role's monitor/prep.weekly_due handler', but no such…
- [x] **D112** `docs/BUILDING_AGENT_TEAMS.md:271` — The raw-API tool-guide exception cites 'agents/eng-team/tools/linear.md', which does not exist — the Linear guide moved to context/.
- [x] **D057** `docs/BUILDING_AGENT_TEAMS.md:298` — The entire 'Decision log (memory)' section documents the retired per-session INDEX.md decision log and an agent-curated memory contract that the…
- [x] **D108** `docs/EVENT_SERVER.md:472` — Public-server prerequisites say to "set all three provider webhook secrets (WEBHOOK_SECRET, SLACK_SIGNING_SECRET, LINEAR_WEBHOOK_SECRET) so every…
- [x] **D054** `docs/EVENT_SERVER.md:485` — EVENT_SERVER.md documents the Cloudflare Worker runtime files (event-server/src/index.ts, event-server/src/deployment-session.ts, wrangler.jsonc) as…
- [x] **D055** `docs/MONITORS.md:61` — Doc says monitor configuration 'merges in tiers, later wins by name' with agent.yaml's monitors: key (tier 3) overriding run/package/monitors.yaml…
- [x] **D014** `docs/MONITORS.md:68` — Doc says 'Set enabled: false on a name to switch off a default', but a runtime-tier disable (run/package/monitors.yaml or the monitors: key in…
- [x] **D013** `docs/MONITORS.md:297` — Doc claims a self-healed script that widens its capability envelope 're-enters review even in auto mode' so self-healing 'cannot silently widen what…
- [x] **D107** `docs/OVERVIEW.md:46` — OVERVIEW states every tool dependency is "declared in the team's agent.yaml under tool_library:", but the eng-team example it uses declares its only…
- [x] **D053** `docs/QUICKSTART.md:123` — Quickstart Step 3a claims the setup client defaults the team library to ~/bobi-agents/, but the actual default is $BOBI_HOME/agents…
- [x] **D109** `docs/RELEASE_RUNBOOK.md:107` — The repo-split edit truncated the GHCR package-visibility instruction, leaving an incoherent orphaned sentence fragment and losing the actual…
- [x] **D030** `docs/WORKFLOW_ENGINE.md:97` — Doc claims a step's `timeout` is 'the declared deadline carried into the registry for the reconciler's dead-man check', but StepDef.timeout is parsed…
- [x] **D056** `docs/WORKFLOW_ENGINE.md:152` — Doc claims that when a step changes agent: the engine falls back to a fresh session because 'a new agent never inherits another agent's transcript',…
- [x] **D072** `docs/WORKFLOW_ENGINE.md:198` — Doc says notification failures are always non-fatal and the workflow continues, but the engine deliberately fails the run when an undeliverable…
- [x] **D006** `docs/WORKFLOW_ENGINE.md:302` — Doc claims the manager calls try_resume_for_event when an event arrives, but nothing in the repo calls it — suspended workflows only resume via the…
- [x] **D116** `docs/specs/747-sleep-cycle-memory-cap.md:5` — Completed pre-implementation spec for shipped issue #747 lingers in docs/, with a Problem section describing pre-fix behavior as current reality,…
- [x] **D117** `docs/specs/751-install-write-guard.md:21` — Completed pre-implementation spec for shipped issue #751 lingers in docs/, describing the runtime write guard as nonexistent when it has been shipped…
- [x] **D118** `docs/specs/issue-753-subagents-wait-max-turns.md:5` — Completed pre-implementation spec for shipped issue #753 lingers in docs/, describing the old broken --wait/check-harness behavior in the present…
- [x] **D114** `skills/discord-setup.md:100` — Doc claims `bobi agent <name> event-server status` shows a `discord_gateway` block with connection state, but the CLI command prints only Mode and…
- [x] **D059** `skills/linear-setup.md:76` — Section 5 ('Label issues for automation') and two troubleshooting rows describe a built-in Linear dispatcher (trigger_labels, 'Dispatch only picks up…
- [x] **D115** `skills/slack-setup.md:56` — The scope list presented as the complete set the manifest pins ('The manifest pins exactly the scopes...' + table + 'Plus chat:write,…
- [x] **D058** `skills/slack-setup.md:152` — The 'Multiple workspaces' section instructs storing per-workspace Slack tokens in ~/.config/bobi/credentials.yaml, a credential store no code reads…

**Validation gate**

- [x] Every edited claim re-checked against the tree by the builder (the appendix's "contradicting code" pointer is the check)
- [x] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q` (docs-only phase must not break collection)

### Phase 9 — Test-suite cleanup

- [ ] **D101** `tests/integration/COVERAGE.md:13` — COVERAGE.md's marker column claims the subagent-launch and e2e-event-flow tests are claude-gated, but both files now run dual-brain with an unmarked…
- [ ] **Q044** `tests/integration/test_context_rotation.py:1` — test_context_rotation.py lives in tests/integration/ but is a fully-mocked unit test (MagicMock SDK clients, no subprocess, no event server, no…
- [ ] **D103** `tests/integration/test_event_server.py:42` — An identical _free_port() helper is defined in seven separate integration test files instead of once in tests/integration/conftest.py.
- [ ] **D100** `tests/integration/test_event_server.py:877` — _send_and_drain ignores the ready.wait() result and its WS thread swallows every exception, so negative-delivery tests pass vacuously when the…
- [ ] **Q128** `tests/integration/test_gateway_openai_brain.py:244` — test_gateway_openai_brain.py defines its own async _drain(session) that duplicates tests/integration/conftest._drain — the helper whose docstring… *(unverified — re-verify first)*
- [ ] **Q041** `tests/integration/test_inbox_transport.py:53` — Four integration files hand-roll /health polling loops (raw urllib + json + status=='ok') that bobi.events.server.health() already implements — and…
- [ ] **D012** `tests/integration/test_manager_lifecycle.py:218` — The only two checks in the whole suite that the manager's drain loop actually starts convert boot failure into pytest.skip, so a drain-loop…
- [ ] **Q043** `tests/integration/test_pr_feedback_followup_dispatch.py:22` — The run-drain_loop-for-one-batch harness (_OneShotQueue + _CaptureInbox + register/unregister_local_inbox + patch time.sleep + swallow…
- [ ] **D105** `tests/test_kb_embedder.py:27` — The mock_project_root fixture is dead code — defined but never requested by any test.
- [ ] **D102** `tests/test_orchestrator.py:399` — The fake Claude-SDK message dataclasses (FakeResultMessage/FakeTextBlock/FakeAssistantMessage) are copy-pasted across four unit-test files, two of…
- [ ] **Q110** `tests/test_orchestrator.py:567` — test_orchestrator.py defines the same 4-line inline 'class FakeBrain: def make_session(...): calls.append(kwargs); return FakeBrainClient()' plus its… *(plausible — re-verify first)*
- [ ] **D104** `tests/test_subagent.py:28` — The tmp_cwd fixture is dead code — defined but never requested by any test.
- [ ] **Q042** `tests/test_subagent_blocking.py:67` — The fake brain/SDK message protocol (FakeTextBlock / FakeAssistantMessage / FakeResultMessage / FakeClient) is modeled independently in four unit…

**Validation gate**

- [ ] `pytest tests/ -q` (full suite) green; the de-skipped manager-lifecycle checks proven by forcing a boot failure locally (they must FAIL, then pass on real boot)
- [ ] `pytest tests/ --collect-only -q` count change explained in the PR (deleted dead tests enumerated)

## Proof of work

Decided here: every **bug** (Phases 1–4, 7) gets a failing test first. **Real-Claude e2e leg required for Phase 1** (dead-transport ack/terminal path is brain-path risk per CLAUDE.md's judgment call); all other phases are stub-proven. **Deletions** (Phase 5) are proven by grep gates + the full suite. **Consolidations** (Phase 6) by suite green + the stub e2e round-trip. **Docs** (Phase 8) by claim-by-claim re-verification. Suites that must stay green throughout: the unit run, `tests/integration`, `event-server` vitest. Convergence gate below is the whole-repo proof.

## Lane map

Five lanes per the Q1 decision. Dispatch issues filed by Split (Lane A first — it is the trust-restoring lane and gates the bot's own terminal-signal reliability; C and D run in parallel with A; B and E build in parallel but land after A since they share bobi/ files).

| Lane | Dispatch issue | Phases | One-line scope | Status |
|---|---|---|---|---|
| A | #818 | 1–4 | Behavioral fixes in bobi/ (session honesty, workflow+pack routing, persistence atomicity, bug batch) | open |
| B | #821 | 5–6 | Behavior-preserving cleanup in bobi/ (dead-code purge, consolidations) — lands after A | open |
| C | #819 | 7 | event-server TS + web UI (bugs, security, duplication) — parallel with A | open |
| D | #820 | 8 | Documentation drift sweep — parallel with A | open |
| E | #822 | 9 | Test-suite cleanup — lands after A | open |

**Lanes (filed 2026-07-22):** A=#818, B=#821, C=#819, D=#820, E=#822. A: Phases 1–4. C: Phase 7 (parallel with A, no shared files). D: Phase 8 (docs, parallel). B: Phases 5–6 (builds in parallel, *lands after* A — shares bobi/ files, not a build-blocking dependency). E: Phase 9 (builds in parallel, *lands after* A). Only "lands after" ordering here — nothing build-blocks except that B/E should rebase onto A's merge to avoid churn.

- [ ] Convergence gate: full `pytest tests/ -q` + `cd event-server && npm test` green on main after the last lane merges, plus the in-repo dogfood run (isolated `BOBI_HOME`, dogfood-content-review pack: agent boots, event round-trips, the D015 fix-step route actually takes) — run by the session landing the last lane

## Amendments

*(append-only)*

- **2026-07-22** (Split): filed lanes A–E as #818–822 (thin dispatch pointers). B/E land after A (#818); C/D parallel with A.
- **2026-07-22** (Zach + planning session): Q1–Q5 decided (all recommended option — 5 lanes, plausible/unverified in scope with re-verification, remove `--resume`, delete expired compat now, restrict credential endpoint to declared vars). Status → Approved. Lane map filled (A–E).
- **2026-07-24** (plan-file hygiene): renamed this plan and its findings appendix to date-prefixed filenames — `plans/review-remediation.md` → `plans/2026-07-22-review-remediation.md`, `plans/review-remediation-findings.md` → `plans/2026-07-22-review-remediation-findings.md` (dates = first-commit date; `git mv` preserved history). In-repo cross-references updated; GitHub issue bodies (#817–822) still cite the old paths and are left as historical record (this amendment is the pointer).
- **2026-07-22** (build session, authorized by Zach): **D001+D002 shipped early** (ahead of Lane A dispatch) to unblock headless execute — PR **#825** (`_drain_turn` dead-transport branch sets `_last_is_error`; two behavioral regressions + a two-brain e2e with a new `__stub__:raise` directive and a claude leg that SIGKILLs the live CLI mid-turn). Bot PR **#800** (issue #799) landed the same day and independently fixed the D001 ack-loss via `_drain_turn`'s `None`-return; #825 was rebased on top and carries the D002 phase-honesty half #800 didn't cover. Bot PR #810 (atomic registry state.json publish) landed in the same batch. Lane A's remaining Phase 1–4 items are unchanged.

- **2026-07-26** (planning session, authorized by Zach): **Phase 2 mostly superseded** by `plans/2026-07-26-checklist-execution-model.md` (tracking #852, PR #853), which retires the workflow step machine — steps, handoff contracts, route conditions, and await/resume all go. Nine of twelve Phase 2 items fix internals scheduled for deletion and are marked `[f]`-superseded with per-item rationale: D005, D027, D024, D025, D028, Q017/D026, D015, D016, and D060 (the last absorbed rather than superseded — the cutover rewrites every pack's routing surface). Phase 2's validation gate was rewritten to match, since its original lines proved suspend/resume, the condition parser, and handoff re-prompting.
  **Three items survive and stay in scope**, because they are not step-machine code: **D029** (registry entry stuck `running` — now *load-bearing*, since the checklist plan's in-progress monitor resolves ownership from live registry entries, so a stuck entry makes a dead unit look permanently alive and blocks re-dispatch forever), **D017** (`auto_dispatch` event routing — `bobi/workflow/triggers.py` is explicitly kept, and per Notes issue pickup needs a Slack directive until this lands), and **D119** (pack/service declaration consistency).
  **Phase 3 is untouched and explicitly retained as shared infrastructure** the successor plan consumes rather than forks: **D092** (hoist the atomic-write pattern into one `fsutil` helper) and **Q062/D071** (the `claim()` crash window). The checklist plan's Phase 2 depends on D092's helper for its artifact writes, so Phase 3 should land before it.
  If the cutover is abandoned, the `[f]`-superseded items return to scope — this amendment is the pointer, and nothing was deleted from the plan.

- **2026-07-29** (planning session, Zach): **this plan is DEPRIORITIZED, and two of its recorded decisions changed underneath it.** No lane work proceeds until `plans/2026-07-26-checklist-execution-model.md` (#852) reaches completion.
  1. **Lane A / PR #844 is parked, not decomposed.** It stays OPEN and draft at `f58081c` — four gate rounds, four `NEEDS FIXES`, four BLOCKINGs outstanding (three in `bobi/setup/webui/server.py`'s pack-root confinement, which the branch widened from `$BOBI_HOME` to all of `$HOME`; one in `bobi/brain/codex.py`). **`main` is unaffected — every one of those defects is branch-only.** A decomposition into four PRs was designed on 2026-07-29 and then superseded the same day when the checklist initiative took priority; the branch remains a parts bin.
  2. **Q1's sizing recommendation was falsified** — see the note on Q1. Reviewability is a split criterion co-equal with parallelism, and Lane A is the evidence: 59 items / 47 production files / 2963 insertions could not be re-gated cheaply, so its verdict went 28 commits stale and defects compounded unseen.
  3. **The 2026-07-26 supersession rationale no longer holds as written.** Nine Phase 2 items were marked `[f]` because "the checklist plan deletes the step machine." As of 2026-07-29 that plan **freezes** the engine rather than deleting it: eng-team migrates, the example packs stay on the engine, and deletion is gated on a mechanical trigger. The `[f]` markers stand, but the reason becomes *"frozen — repaired only where a live pack breaks."* **D015 returns to scope on that test**: `agents/dogfood-content-review/workflows/dogfood-content-review.yaml`'s `issues_count > 0` uses an unsupported `>`, and that pack is one this plan's successor deliberately **keeps** on the engine, so it is a broken route in live code with no scheduled removal. **D060** is arguably the same. Engine internals (D005/D027/D024/D025/D028/Q017/D026) stay `[f]`.
  4. **D092 (`fsutil`), Q062/D071 (`claim()`) and D029 are no longer shared infrastructure for anything.** The checklist plan dropped its `bobi/` parser, so the atomic-write coupling that made them load-bearing is gone. D029 is still worth fixing on its own merits — a registry entry stuck `running` makes `bobi/subagent.py:1063-1070` refuse re-dispatch of that unit forever, blocking a human recovering by hand — but it gates nothing.

- **2026-08-03** (re-triage session, Zach): **the plan is picked back up, and every item was re-verified against `main` @ `eac952b`** — 84 commits / 300 files / +52,268 / −4,479 past the review tree (`58aba2c`, 2026-07-17). Three decisions plus a full re-validation sweep. Ten markers move in this amendment's PR; **all per-item detail lives here rather than on the checklist lines, because the review surface above the appendix fence is insertion-only.**

  **Status:** the 2026-07-29 deprioritization is **LIFTED**. The header's `Status:`/`Tracking issue:` lines are approved text and stay as written; read them together with this amendment — tracking is now Linear **MOD-278** (see decision 4).

  ### 1. Lane A / PR #844 is closed — harvested, not rebased

  The branch stays at `f58081cc527c36eca850d4c36c09c9b615af5c6d` on `agent/818-lane-a`; **do not delete the ref.** Its true size was worse than the 2026-07-29 note recorded: **55 commits / 102 files / +9,888 / −974** (the "2963 insertions" figure was production-only), and it is `CONFLICTING` against the drift above. Rebasing costs more than re-deriving its 59 items and would carry all four outstanding BLOCKINGs through. **Every one of those defects is branch-only; closing keeps `main` clean.** Use it as a reference diff per item — never a blind cherry-pick, since it went 0-for-4 on gate rounds with 3,853 unit tests green while a private key was readable over the setup API.

  ### 2. Re-validation: 229 items → 220 still actionable

  Every cited path (**102/102**) still exists; the repo reorg moved none of them. **Line numbers in the checklist are deliberately NOT refreshed** — they drifted far enough (D003 `session.py:1214`→1653, D021 `1205`→1645, D031 `events/server.py:706`→930, D067 `511`→536, D029 `546`→527) that updating them would rot again within weeks. Locate by symbol, as this plan already instructs.

  **Nine items are already resolved and their markers flip to `[x]`:**

  | Item | Why it closed |
  |---|---|
  | D073 | The `monitor.error` push now carries `on_done=batch_ack.attach() if batch_ack else None` |
  | D113 | The dangling "see Bug fixes above" reference is gone from `CLAUDE.md` |
  | D121 | `agents/eng-team/README.md` now lists `stall-recovery.yaml` and `image-gen.md` |
  | D122 | `SLACK_SIGNING_SECRET` is now documented in `agents/personal-assistant/agent.md` |
  | D110 | `docs/BUILDING_AGENT_TEAMS.md` now lists whatsapp among native services |
  | D055 | The "later wins by name" claim is gone from `docs/MONITORS.md` |
  | D013 | The "re-enters review even in auto mode" claim is gone from `docs/MONITORS.md` |
  | Q125 | **REFUTED** — `bobi/brain/__init__.py:435` now calls `cfg.brain_small_model`; the property has a real consumer and stays |
  | Q098 | **REFUTED** — `isBreakerTripped` no longer exists anywhere in `event-server/` |

  **D008/Q025 is NARROWED, not closed — read this before building it.** The markdown link renderer now scheme-validates (`/^(https?:|mailto:)/i.test(url) ? url : "#"`), which fixes the `javascript:` half. **The attribute-injection half survives:** `safe` is still interpolated into `href="${safe}"` unescaped, so a URL containing a double quote breaks out of the attribute. **Fix the escaping only; do not re-add a scheme check.** A builder skimming the item would plausibly see the scheme check and mark it done.

  **Three items grew and their scope is larger than the checklist line says:**

  - **D103** — `_free_port` is now duplicated across **ten** integration test files, not seven.
  - **Q015** — `_on_sleep_cycle_result` now repeats its failure block **nine** times, not seven.
  - **Q027 / Q104** — the named survivor was renamed `_claude_projects_dirs` → **`claude_projects_dirs`** (`bobi/chat_history.py:83`, now public), and the hand-rolled copies grew to **four**: `bobi/cli.py:1568`, `bobi/history.py:14`, `bobi/brain/claude.py:374`, plus the house one. Consolidate onto `claude_projects_dirs` and resolve Q027 and Q104 together.

  **Two items shrank without closing:** Q008 (six unreachable `if not project_path:` guards → **three**, at `cli.py:1666`, `2489`, `2783`) and Q041 (four hand-rolled `/health` polling loops → **three**).

  **One item's inventory changed:** D092's five atomic-write re-implementations are now `bobi/monitors/script_cache_checks.py`, `bobi/sdk.py`, `bobi/launch_admission.py`, `bobi/workflow/state.py`, `bobi/brain/instructions.py`. Still five, still no shared helper — `bobi/fsutil.py` does not exist. **Survey these, not the appendix's list.**

  **D017 gained a second symptom.** `agents/eng-team/agent.yaml:126` still carries the unmatchable `event: github.issues.assigned`, and `bobi/events/reactor.py:316` now branches on that same never-emitted type to shape a dispatch prompt — so that branch is dead for the identical reason. **Fix both.** The adapter comment at `event-server/core/src/adapters/github.ts:21` ("making the issues.assigned miss impossible") is about **fields** being structurally present, not the event type; the action still travels in `fields.action`. **The Operational note in Notes therefore still holds: issue pickup needs a Slack directive until D017 lands.**

  **D015 returns to scope and its marker moves `[f]` → `[ ]`.** The 2026-07-29 amendment's test applies — the successor plan freezes the engine rather than deleting it and deliberately KEEPS `dogfood-content-review` on it — and re-verification confirms `dogfood-content-review.yaml:35` still reads `if: "issues_count > 0"`, unchanged. A broken route in live code with no scheduled removal. **Phase 2 therefore has FOUR survivors, not the three its section header says. D060 is arguably the same and should be re-decided when Lane A reaches it.**

  ### 3. PR sizing replaces Q1's falsified guidance

  The Lane map table stays as approved; what changes is the **PR boundary inside a lane**, which is precisely what Q1 got wrong. A lane is a tracking unit, not a PR. **Each lane lands as several PRs, each sized to what a gate can re-review: roughly one phase-section or ~10–15 items, under ~800 changed lines.** Target counts: Lane A ~55 items / 4–5 PRs · Lane B ~103 / 7–8 · Lane C 21 / 2 · Lane D 24 / 2 · Lane E 13 / 1. Two hard rules, both bought with #844's four failed gate rounds:

  - **Never bundle a security fix with mechanical cleanup.** Phase 4's `webui/server.py` items (D007, D081, D080, D033) and Phase 7's (D008, D089, D049) ship as their own PRs. Three confinement BLOCKINGs hid inside 2,963 lines of unrelated churn while the unit suite stayed green.
  - **One PR closes one issue** per `docs/TICKETING_POLICY.md`. Where a lane needs several PRs, the lane issue stays open until its last PR lands, and each PR names the item IDs it closes.

  ### 4. Tracking moved to Linear

  #817 was closed **NOT_PLANNED** on 2026-07-31 in the epic migration — this plan is now **MOD-278**, with lanes #818–822 staying as GitHub issues mirrored 1:1 as sub-issues. **#852 was closed the same minute for the same reason, not because it finished** — its Phase 4 (eng-team trial) is still 24 unchecked items. The 2026-07-29 hold ("no lane work until #852 completes") was therefore discharged administratively, not by work; Zach lifted it explicitly on 2026-08-03.

- **2026-08-03** (Lane C build session, authorized by Zach): **Phase 7's preamble names four security IDs and all four are wrong.** The section's opening sentence reads "Security items first: D007 (attr injection via markdown links — `esc()` must escape quotes), D077 per Q5, D006 (path confinement), D082 (register payload validation)." Every one of those IDs points at a different finding than the description beside it. The **checklist lines below it are correct**; only the prose is wrong. Read against the appendix:

  | Preamble says | That ID actually is | It meant |
  |---|---|---|
  | D007 — attr injection via markdown links | setup-webui path confinement, **phase 4** | **D008/Q025** |
  | D077 per Q5 | **no such finding exists** | **D081**, phase 4 |
  | D006 — path confinement | WORKFLOW_ENGINE doc-drift, **phase 8** (fixed in #939) | **D007**, phase 4 |
  | D082 — register payload validation | `sdk.py` docstring doc-drift, **phase 8** (fixed in #939) | **D089** |

  **Two of the four are Lane A work, not Lane C.** `D081` (GET /api/credential/value falls back to `os.environ`) and `D007` (path confinement rejects real MCP folders) are both **phase 4**, both in `bobi/setup/webui/server.py`. Building them here would duplicate Lane A and break Lane C's stated non-goal of no `bobi/` Python behavioral changes. **They stay in Lane A.** #819's scope line inherits the same four wrong IDs and is corrected in its issue body; this amendment is the durable pointer.

  **Phase 7's real security cluster is D008/Q025, D089, D049** — exactly the three the 2026-08-03 re-triage amendment §3 already names under "never bundle a security fix with mechanical cleanup." All three shipped in one PR this session, each re-derived by a failing test before the fix:

  - **D008/Q025** — the injected `onmouseover` was reproduced at parse level, not by grepping the source: `tests/test_webapp_markdown.py` executes the real module under Node and parses the output, and `html.parser` recovers `href="…"onmouseover="…"` into two attributes exactly as a browser does. The narrowing recorded above held — the scheme check was already there, the quote escaping was not. **Q025's extraction is what made the fix testable**, so the renderer moved to `bobi/webapp/static/views/markdown.js` rather than being deferred as cleanup; a test pins it against being pasted back into the view.
  - **D089** — `handleRegisterDeployment` now validates shape before minting. The pre-fix behaviour reproduced exactly as the appendix predicted, including the raw `key.startsWith is not a function` throw on `subscriptions: [42]`. A non-string `name` was the same hole on the adjacent line and is fixed with it.
  - **D049** — `readBody` moved to `event-server/src/http-body.ts` (local.ts is an entrypoint: importing it binds a port, so nothing in it is unit-testable; `slack-socket-local.ts` is the existing precedent for a testable sibling). The cap defaults to **8 MiB**, overridable via `BOBI_ES_MAX_BODY_BYTES`. It is deliberately generous because `/channels/send` inlines file uploads as base64 and has no size limit of its own — a low ceiling would reject legitimate traffic. Route-specific gates (ingest's 256KB) still fire first with their own errors. Over-cap now answers **413**, not 500.

  **The remaining Phase 7 items are NOT deferred on their merits — they collide with in-flight human work.** PR **#941** (`feat/single-agent-brand-restyle`, stacked on `feat/single-agent-view`) rewrites `bobi/webapp/static/app.css`, `views/agent.js`, and `bobi/setup/webui/static/app.css` — Lane C's whole web-UI surface. The **14 event-server items collide with nothing** and ship next. The **6 remaining web-UI items (D098, D099, D124, Q073, Q076, Q074) wait for that stack to land**, then rebase onto the new CSS/JS. Lane C is therefore **3 PRs, not the 2** the re-triage amendment's target table projected.

  **Phase 7's second gate line is half-satisfied.** "New JS escaping tests … quotes escaped in href interpolation; agent-name rendering" — the first clause is covered here; the second is **Q076**, which ships with the deferred web-UI batch. The gate marker stays `[ ]` until then.

- **2026-08-03** (Lane C PR 2, authorized by Zach): **the event-server batch is 12 items, not the 14 the amendment above states.** Counted off the checklist: D011, D091, Q117, D047, Q101, Q100, D046, Q099, D048, Q116, D090, Q115. #819's body carries the same correction. All 12 ship here, in one PR, with the `npm test` gate; the 6 web-UI items and the remaining two gates stay for PR 3, still behind the `feat/single-agent-*` stack (`feat/single-agent-view` is 20 commits behind main as of this writing).

  **The four bugs were each re-derived by a failing test before the fix**, and two of the four turned out to be worse than the appendix predicted:
  - **D011** — confirmed exactly as written: the blanket subtype skip runs before both the DM/thread classification and the file extraction. Slack stamps `file_share` on every message that shares a file, so DM and thread-reply uploads never became events at all. The pre-existing files test passed only because its synthetic payload omits the subtype real Slack always sends. `file_share` is now the one subtype that passes; the rest still skip, pinned by a test.
  - **D047** — both halves confirmed, and the second is the more serious. The buffer is unbounded (now capped at `BREAKER_MAX_PAUSED` = 50, newest wins, drops counted via `breakerStats()`), but resume is also **lazy**: only `recordDelivery`/`drainPaused` release a key and both need a NEW event on it, so a conversation that trips and goes quiet holds its state and its buffer for the life of the process. Added `sweepBreakers()`, hung on the local server's existing eviction interval. Deliberately NOT a timer inside the breaker — that module also runs in a Workers isolate. Stranded events are discarded on sweep, not delivered: nothing is asking for them and they are the loop traffic. The module header claimed "buffered, not dropped" and "Auto-resume after COOLDOWN_MS"; it now says what actually happens.
  - **D046** — confirmed, and it returns **200** while losing the text, which is why it could run unnoticed. Fixed by degrading the way the text-only path already does: with no edit support there is no placeholder to move the text into, so it rides as the file comment.
  - **D048** — confirmed by an interleaved-registration test; pre-fix, app A1's entry vanished entirely. Serialized per storage key. **Scope of the fix, stated plainly:** this closes the window within one runtime instance (the local server is one process; a DO is single-threaded), which is where the reported fleet-roll failure lives. It does **not** close a cross-isolate race on the Worker's KV backing — KV has no compare-and-swap, so narrowing that needs a storage-layer change. Not attempted here.

  **Three corrections to the appendix**, all found by re-deriving rather than re-checking the stated claim:
  - **Q099** — the entry says the SDK "signals failure via SlackApiError", so the hand-rolled `ok` checks could go. Reading the SDK: `callSlackApi` throws only on a non-2xx HTTP status, and Slack reports method-level failure as HTTP 200 with `{ok: false}`. The explicit `ok` checks stay. The reuse still holds (URL joining, Bearer header, encoding), and `auth.test`/`bots.info` move GET+query → POST+form as a side effect; a new test pins that wire change.

    **That wire change broke a test double, and the double was what was wrong.** `tests/integration/test_slack_socket_mode.py`'s Slack REST stub served `auth.test` on `do_GET` only, so the POST 404s and the signed registration path reports that as a 403 — Socket Mode never starts, and `test_packaged_event_server.py` fails on its socket-driver probe. Fixed by teaching the stub both verbs, on this evidence: real Slack accepts GET and POST for `auth.test`; the Chat SDK POSTs form-encoded for **every** Web API method, and this repo already calls it that way against live Slack for `chat.postMessage`, `conversations.history`, and `assistant.threads.setStatus`; and `tests/integration/test_channel_gateway.py`'s stub has always answered `auth.test` and `bots.info` on POST — it has no `do_GET` at all. The two doubles disagreed with each other, which is the tell. Verified the stub fix is load-bearing rather than incidental: reverting it alone puts 6 of that file's 7 tests back into ERROR. **Honest limit: POST against real Slack is proven by the SDK's production use here, not by a test in this PR — `test_slack_live.py` is credential-gated and did not run.**
  - **Q116** — confirmed, and `SlackSocketSession.onTimer` carries the **identical** dead literal-kind guard. The appendix names only the Discord one. Both are fixed: shipping one of two identical defects is worse than either choice.
  - **Q117** — confirmed, and converging `github.ts` on the narrowing helpers is **not** behavior-neutral on malformed payloads, contrary to the entry's "for well-formed provider payloads behavior is identical" framing (true, but it stops short). An `assignees` array containing a null entry used to **throw**, and a non-string `title` used to be written into `fields` as an object. Both now drop, matching the policy the other three adapters document. Tests cover both.

  **Q115 re-verified before reuse** (it is marked *plausible*): the claim that `removeDeployment`'s websocket-close loop is unreachable from the eviction path holds — `disconnectedAt` is set only when `websockets.size === 0` and reset to null the moment a socket connects. The eviction sweep also had to become async and collect-then-mutate, since deleting from `deployments` while iterating across an await is a foot-gun.

  **New modules:** `event-server/src/delivery-buffer.ts` (D090 — `local.ts` binds a port at import, so the shared block had to leave it to be testable; added to `PUBLIC_LOCAL_MODULES`) and `event-server/core/src/adapters/payload.ts` (D091 + Q117). Suite: **410 → 442 tests, 10 → 12 files**; all three compile units clean.

- **2026-08-04** (Zach): **the 6 remaining Phase 7 web-UI items are deferred — Lane C is 2 PRs, not 3.** D098, D099, D124, Q073, Q076, Q074 and the two remaining validation-gate lines all stay `[ ]` and wait for Luke's single-agent UI work to reach main, at which point they rebase onto the new CSS/JS. Zach's reason: those surfaces are about to change significantly, so fixing them now buys churn. **#819 stays OPEN** holding exactly these six; do not re-open the ordering question without him.

  **Two corrections to the amendment above, both from re-measuring rather than re-reading it.**
  - It says the six "wait for that stack to land." **There is no stack landing.** #941 is MERGED — into `feat/single-agent-view`, not main; `feat/single-agent-brand-restyle` no longer exists. All seven single-agent PRs (U1–U7 plus #941) target that integration branch, and **no PR from any `feat/single-agent-*` head to main has ever been opened**. The branch is 24 commits behind main as of this writing. So the earlier framing described a queue position that does not exist; this entry replaces it with a decision.
  - It says the restyle rewrites "Lane C's whole web-UI surface." It does not. Measured with `git diff --stat origin/main origin/feat/single-agent-view` over the four files: `bobi/webapp/static/views/agent.js` ~1,569 lines rewritten and `bobi/setup/webui/static/app.css` 26 lines, while **`bobi/setup/webui/static/app.js` and `bobi/webapp/static/shell.js` are untouched.** Filed location is not fix location, though: **D099 collides regardless** of being filed against `setup/webui/static/app.js`, because two of the three escaping variants it consolidates live in `agent.js` (`esc` at :177-178 and the :17 inline strip — which is Q076's own site). The genuinely collision-free residue is therefore **D124 and Q073 only** — two `low` dedup/dead-code items, far under the ~10–15 item PR bar. Shipping just those was offered and declined.

  **Nothing live is at risk from the deferral.** All six are severity `low` and none is a security hole. **Q076 is filed *consistency*, not security** — the existing `name.replace(/[&<>]/g, "")` already strips the dangerous characters, and the proposed `data-el` + `textContent` shape is hardening against a bespoke sanitizer, not a patch for a live hole. Phase 7's actual security cluster (D008/Q025, D089, D049) shipped in PRs 1 and 2.

- **2026-08-04** (Lane A PR 1, authorized by Zach): **Lane A lands as a series of PRs, not one.** #818's body says *"Work through all four phase gates in one PR"*; that predates the sizing rule Lanes C and D each proved four times (**~10–15 items under ~800 lines**, no PR bundling a security fix with mechanical cleanup). Lane A is **62 unchecked items** (P1 7 · P2 9 · P3 9 · P4 37), so one PR is not on the table. **#818 also carried a stale `status:blocked`**: its stated gate — the 229-item re-validation landing as a dated amendment — was discharged by **PR #937, merged seven minutes after the label was applied**. Nothing has blocked the lane since.

  **PR 1 = Phase 1 alone**: D003, D021, D067 plus the phase's four gate lines. Phase 1 goes first and by itself because it is the trust-restoring phase #818's Order note says the other lanes' builds depend on, all three findings are one subsystem (session lifecycle), and its gate requires the real-Claude e2e leg. Each was re-derived by a failing test before the fix; all three held as filed, but two needed more than the appendix's fix sketch:

  - **D003** — the entry says "make stop interrupt the startup phase too", which understates the mechanism: setting an event cannot interrupt a turn parked in `await client.receive_response()`. `_run` had to become a **cancellable task** so `stop()` can cancel it through the loop. **And cancelling alone is only half a fix** — `_run`'s `finally` guards the keep-alive wait, which the startup path never reaches, so the thread would exit with the brain subprocess still attached: the exact leak D003 is about. Teardown now disconnects explicitly (`_shutdown_client`), proven load-bearing by a mutant — removing that one call leaves `client.disconnected` False while every other assertion still passes. Fixed in passing: `stop()` called `asyncio.Event.set()` directly from another thread, which is not thread-safe; it goes through `call_soon_threadsafe` now.
  - **D021** — confirmed, and the obvious fix has an ordering trap worth pinning: a thread can set `_ready` and then exit before the next liveness poll, so liveness must be re-checked **only after** re-reading `_ready`, or a session that started perfectly well gets reported as failed. A test holds that ordering.
  - **D067** — confirmed. Worth recording *why the dead handler looked live*: `test_timeout_is_reported_as_subprocess_timeout` reaches it by making `connect()` raise `asyncio.TimeoutError` synthetically, which nothing in production does. The real overrun path arrives as `CancelledError` from the caller's `wait_for` and skips both handlers. Enforcing the deadline inside makes the caller's `wait_for` a backstop, and **a flat grace on it was wrong**: `test_timeout_returns_failed_check` patches `_run_agent_supervised` out entirely, so only the outer deadline exists there, and a flat +30s turned a `timeout=1` check into 31s per attempt. The grace is proportional instead — `timeout + max(1, min(30, timeout × 0.1))`.

  **Reading the diff:** `bobi/subagent.py` reports ~93 changed lines but `git diff -w` shows **25** — wrapping the supervised loop in `async with asyncio.timeout(...)` re-indented its body.

- **2026-08-04** (Lane A PR 2, authorized by Zach): **the 2026-08-04 Lane C deferral amendment's collision measurement is wrong, and the error is a reusable one.** That entry states `bobi/setup/webui/static/app.js` and `bobi/webapp/static/shell.js` are **untouched** by the restyle, and concludes D124 and Q073 are "genuinely collision-free". Both claims are false. Measured against PR **#948** (`feat/single-agent-view` → main, opened after that amendment was written) with the three-dot diff from its merge base `4cf045a`, the branch touches **all four** of Lane C's files plus a fifth: `bobi/setup/webui/static/app.css` 1,149 changed lines · `app.js` 97 · `bobi/webapp/static/app.css` 996 · `shell.js` 10 · `views/agent.js` 1,623. **Nothing in the deferred six is collision-free.**

  **Cause, worth carrying forward:** the earlier measurement used a **two-dot** `git diff origin/main origin/feat/single-agent-view`, which compares branch TIPS. A pull request's file list is the **three-dot** diff from the merge base — what the branch changed, not how it differs from a main that moved independently underneath it. Two-dot understated the branch precisely because main had since touched some of the same files. **When the question is "what does this PR change", use `git diff $(git merge-base A B) B`.**

  **Zach's deferral decision is unaffected and better supported** — it was made on "those surfaces are about to change significantly", which is now measured rather than asserted. What changes is the unblock trigger: it is no longer "rebase the two collision-free items early", it is **when #948 merges, rebase all six (D098, D099, D124, Q073, Q076, Q074) onto it**. #819 stays open holding exactly those six until then.

  **Phase 3 shipped in this PR** (D092, D034, D038, D087, Q062/D071, Q039 + all three gate lines). Four notes the checklist lines could not carry:

  - **D092 undercounts by one, and Q039 overcounts by one.** D092 lists five hand-rolled atomic writers; there are **six** — `bobi/sdk.py:_write_state` grew one after the review tree, and it was the best of them (process-unique temp name, cleanup in `finally`), so it is the implementation `bobi/fsutil.py` hoists. Conversely Q039 lists `bobi/sdk.py:291/312/358` among the **non**-atomic deviants; those line numbers no longer point at writes, and sdk.py's only state write is already atomic. That half of Q039 is stale and is dropped rather than "fixed".
  - **The grep gate has zero justified survivors.** The gate anticipated documenting some; instead the four small process files it surfaced outside the item list (`manager.pid`, `ui.port`, two `event-server.port` writers) were converted too — each a one-line adoption, and each otherwise able to be left empty by a kill between the truncating `open()` and the `write()`. `bobi/events/client.py` (the event cursor) and `bobi/supervisor/alerting.py` (incident state) were converted for the same reason; Q039 names the first explicitly.
  - **D071's crash window is NOT closed, and the reorder its own item prescribes slightly widens it.** Q062's trace claims the reorder "preserves every guarantee"; it preserves the ones it enumerates, but the claim is two file operations (rename the run aside, then write the updated status over it) and a crash between them leaves `<run_id>.resuming.json` reading `waiting` — `find_waiting` keeps matching it while `claim()` can never win again, which is exactly D071. No ordering of two operations removes that; closing it needs a **recovery protocol for a torn claim**. Built anyway from Q062: the loser now writes nothing at all, and the temp name is process-unique (it was derived only from the run id, so every racing claimer shared one temp path — a winner could rename a file another process was still writing). **D071 should be re-decided with the fate of await/resume itself** — `claim()`'s only caller is `try_resume_for_event`, which the runtime still does not call (Phase 2 D027, `[f]`). Recorded in `docs/WORKFLOW_ENGINE.md` where the resume protocol is described.
  - **A lock file is a file, and `SetupState`'s lives inside a hashed tree.** D034 asks for "the helper + a lock"; adding `setup.json.lock` broke `test_validate_then_install_when_state_file_sits_inside_source_tree`, because a run/ directory nested in a team source tree puts the lock under `source_tree_hash`, which already excludes the checkpoint for this exact reason. Both are now excluded through one `setup_state_artifacts()` list, so a future sidecar cannot be added to the save path and forgotten by the two hash callers.

- **2026-08-05** (Lane A PR 3, authorized by Zach): **Phase 2's four survivors ship; two judgment calls inside them are recorded here rather than buried in the diff.**

  **The check the gate asks for cannot be written the obvious way.** Phase 2's gate wants validation that rejects an `auto_dispatch` rule whose `event:` can never be emitted. The tempting rule — "`github.issues.assigned` has three dot-separated parts, so three parts is wrong" — is **false**, and shipping it would have broken every legitimate Linear rule: GitHub emits `github.${eventHeader}` and carries the action in `fields.action`, while **Linear emits `linear.${dataType}.${action}` with the action IN the type**. Arity is meaningful only per source. The check is therefore a per-source table (`_EVENT_TYPE_SHAPES`, `bobi/validate.py`) covering github/linear/slack/discord/whatsapp, and it **fails OPEN on any source it does not recognize** — a false positive on a new adapter is worse than the silent dead rule it exists to catch. The table is hand-maintained from TypeScript adapters nothing in Python can import, so it is pinned by tests that read the adapter sources and fail on drift, the same way `tests/test_webui_tokens.py` pins the design tokens.

  **The check is a WARNING, not a startup blocker — a deliberate deviation from a literal reading of "fails validation".** An unmatchable rule is inert: the agent is no worse off for starting. Making it `required` would refuse to start every already-deployed team whose *installed* pack still carries the old spelling — turning a silent dead rule into a fleet outage on upgrade, which is exactly the 0.44.0 runtime-guard EPERM shape. The check fails at the check level (`CheckResult.ok is False`, visible in the startup preflight report) without gating startup. **Revisit if a future release re-installs packs in lockstep with the binary.**

  **Per-item notes:**
  - **D017** confirmed exactly as filed, and it had the **second symptom** the 2026-08-03 re-verification predicted: `bobi/events/reactor.py` also branched on the never-emitted `github.issues.assigned` when composing the dispatch prompt, so even a hand-dispatched assignment got generic fallback text. Both fixed; the rule now mirrors the sibling `pull_request` rules.
  - **D015** fixed **in the pack, not the engine** — the `variables.py` numeric-comparison fix shape was withdrawn with Q017/D026, so the condition moves to `issues_count != 0`, which the parser supports and which routes correctly for 0 and for any non-zero count. It is also **fail-safe where the old condition was not**: a missing or empty `issues_count` now routes to `fix`, where `>` fell through to the bare-truthy check and routed it to `done`, closing an unreviewed issue as having passed.

    **Scope correction, recorded rather than glossed:** the 2026-08-03 amendment returned D015 to scope because `dogfood-content-review` is "one this plan's successor deliberately KEEPS on the engine". That commitment is real (`plans/2026-07-26-checklist-execution-model.md` keeps the example packs so the engine fallback stays exercised, with a convergence gate requiring one dogfood workflow to run end-to-end) — but it is **unexecuted**, and "kept" turns out to mean "still on disk", not "still exercising the engine". Measured 2026-08-05: **the pack has no `auto_dispatch` block at all**, so none of its four workflows is event-triggered by anything; **no CI workflow references the pack**; and the checklist plan's Phase 4 never started (#852 was closed NOT_PLANNED in the Linear epic migration, not because it finished). So **D015's fix is correct but cosmetic today** — it repairs a route nothing currently takes, and becomes load-bearing only if that convergence gate is ever honored. Shipped because it is a one-token change on a route that is wrong either way; **flagged so no one reads this item as having fixed a live break.**

    Two things this surfaced that belong to other lanes, not here: the engine's own **deletion trigger** ("no auto_dispatch rule naming a workflow, and no pack ships `workflows/`") is currently held open by the example packs shipping `workflows/` directories nobody runs — only `eng-team` still has dispatch rules; and the pack's long-standing comment claiming the **release smoke** depends on its credential-free start is unsupported by `docs/RELEASE_RUNBOOK.md`, which never mentions the pack. The comment is narrowed here to the `/dogfood` battery, which is real. **Whether the pack should simply be deleted is a Lane B question** (dead-code purge) and is entangled with the checklist plan's fallback-proof commitment, so it is asked there rather than decided here.
  - **D119** resolved by **removing `chat: slack`** and the two unreachable Slack rows from the manager ROLE, not by declaring a slack service: adding one would cost the credential-free start that the dogfood and release smokes depend on (Zach, #329/#405). A pack claiming a channel it cannot receive on is the defect.
  - **D029** confirmed. Fixed by moving `_make_session` inside the retry `try` (the loser path now null-checks `client`, since session construction itself can be what raised).
  - **Both packs' versions bumped in lockstep with `agents/registry.yaml`** — the repo's version-agreement test enforces this, and an exact-pin consumer would otherwise fetch a stale immutable tarball.

- **2026-08-05** (Zach, on #821): **`agents/dogfood-content-review` is deleted**, answering the Lane B question the 2026-08-05 D015 note above referred out of Lane A.
  The pack goes with `tests/test_dogfood_content_review_pack.py`, its rows in `tests/test_pack_routing_validation.py`, its `agents/registry.yaml` entry, the `_HIDDEN_TEMPLATES` filter in `bobi/setup/webui/server.py` that existed only to hide it, and `.claude/commands/dogfood.md`.

  **This plan's convergence gate is corrected here rather than edited in place**, per the review-surface rule that a gate command which turned out to be wrong is fixed by amendment.
  The gate above reads "plus the in-repo dogfood run (isolated `BOBI_HOME`, dogfood-content-review pack: agent boots, event round-trips, the D015 fix-step route actually takes)".
  That clause is void: the pack, the route, and the battery that drove it no longer exist.
  **The gate is now:** full `pytest tests/ -q` + `cd event-server && npm test` green on main after the last lane merges, plus a real-agent smoke on any surviving pack (`agents/eng-team` or `agents/personal-assistant`) in an isolated `BOBI_HOME` proving the agent boots and an event round-trips.
  The D015 fix-step clause is dropped outright, not retargeted, because no surviving pack carries that route.

  **Consequence for D015 (Phase 2), recorded so the item is not later read as live coverage:** its fix shipped in `agents/dogfood-content-review/workflows/dogfood-content-review.yaml`, which this amendment deletes, so the fix is now moot.
  The 2026-08-05 note above already flagged it as "correct but cosmetic".
  The engine-level semantics it pinned survive as `tests/test_variables.py::TestConditionEquality::test_count_not_equal_zero`, carried over when the pack test was removed: `issues_count != 0` is True for "1" and "3" and False for "0", where the defective `> 0` returns False for "3".
  That was the actual D015 symptom, so the regression stays pinned even though the pack that suffered it is gone.
  D015's `[x]` marker stands as a record of what shipped; it is not re-opened.

  **The stated rationale does not survive measurement, and that is recorded rather than glossed.** The option Zach chose was labelled "advance the deletion trigger".
  Measured on `c073c1d` before building: the trigger is a conjunction, and `agents/eng-team` holds open **both** clauses by itself.
  It is the only pack with `auto_dispatch` rules naming a workflow (6 of them), **and** it ships `workflows/`.
  The example packs hold open only the second clause, which eng-team already holds open.
  So this deletion advances the trigger by **zero** clauses; the engine stays undeletable until eng-team migrates off it, which is `plans/2026-07-26-checklist-execution-model.md` Phase 4, which never started.
  The deletion is still correct on dead-weight grounds alone (no `auto_dispatch` block at all, no CI workflow references it, last substantive change 2026-07-01), which is why it proceeded.
  **`agents/personal-assistant` is untouched** - no decision was taken on it, and it also ships an unused `workflows/`.

  **`.claude/commands/dogfood.md` went too, and it was more than a pack test.** It was a 420-line whole-repo manual integration battery: install/config/paths, runtime lifecycle, 16 local event-server tests, Cloudflare Worker parity, the live-Slack soak for #190/#643, workflows/roles, SDK, real-Claude manager comms, doctor, monitors, event pipeline, and Fly rollout.
  Only three parts were pack-specific (the email-service section, the agent-name assertion, the workflow-list assertion); the pack was merely the team it installed.
  Retargeting it to a surviving pack was offered and **declined by Zach on 2026-08-05** in favour of deleting it.
  The manual rendering-fidelity step it carried for the Slack soak was preserved into the `tests/integration/test_slack_live.py` docstring, which had pointed at the battery's Section 3c; the pytest half of that soak is unaffected.

- **2026-08-06** (Lane B build session): **Phase 5 batch 1 — 14 items shipped, and one item falsified.** Every item was re-derived by grep against `main` @ `14d9276` before deletion, not trusted from the appendix; line numbers had drifted on several (Q045 517→731, D061 348→321, Q030's memory aliases 381 exact). Shipped `[x]`: Q080, Q078, Q077, Q053, Q048, Q052, Q094, D083, Q121, Q095, Q118, Q119, Q113, Q079. All fourteen were zero-consumer at HEAD; the suite ran **4262 passed / 1 skipped** with **zero test edits**, matching the pre-change baseline exactly, which is the deletion proof for this batch.
  **Q081 is marked `[f]` — the finding is false at HEAD.** It claims `sdk.TERMINAL_STATUSES` "is defined but never read anywhere". It has a real production consumer: `bobi/webapp/health.py:154` imports it and reads it at :160 to decide the status strip's `Exit` segment (`docs/AGENT_STATE.md`). The consumer post-dates the review tree (`58aba2c`), so the finding was true when written and is not now. **Do not delete `TERMINAL_STATUSES`.**
  Two other appendix claims were confirmed stale in the same sweep and are recorded here so the next batch does not re-derive them: **Q032's `chat` clause is false** (three consumers — `bobi/validate.py:222`, `bobi/webapp/overview.py:125`, `bobi/setup/open_mode.py:250` — and both surviving packs declare `chat: slack`; Phase 5's own preamble defers to exactly this test, so only `registries` and `default_role` are deletable there), and Phase 5's "Q3's flag removal once decided" **already shipped** in Lane A as Q122/D064, so it carries no work.

- **2026-08-06** (Lane B build session): **Phase 5 batch 2 — the expired rename compat layers, 8 items.** Every alias deleted here was a pure one-line delegation to the name that replaced it, all re-derived against `main` @ `f2ae789`. Shipped `[x]`: Q014, Q055 (the curator→sleep-cycle layer: `_default_spawn_curator`, the `spawn_curator=` constructor kwarg, the `self.spawn_curator` attribute, and the `_load_curator_prompt` / `_on_curator_result` / `_publish_policy_updated` methods), Q030, Q045, Q049, Q102 (the policy→long_term_memory layer: `memory.MAX_POLICY_CHARS` / `load_policy` / `format_policy_prompt`, `paths.policy_path` / `policy_cursor_path`, `doctor._check_policy`, `subagent._load_policy_prompt`, `resolver._load_policy_section`), Q068 (`setup.llm`'s `_delta_text` re-export), and Q050 (`subagent._parse_check_output`).
  **Tests were retargeted, not deleted, wherever the alias was the only route to live behaviour.** `_default_spawn_curator`'s tests were the only coverage of `_default_spawn_sleep_cycle`, and `_check_policy`'s were the only coverage of `_check_long_term_memory` — the one check of the two that `doctor` actually registers. Deleting those tests would have silently dropped real coverage, so they now call the surviving name directly. Only `_parse_check_output`'s own 7 tests were deleted, because `run_check_blocking` carries its own copy of the verdict conversion and `TestParseCheckVerdict` already covers the live parser.
  Suite: **4255 passed / 1 skipped**, exactly 7 below the 4262 baseline — the 7 deleted shim tests and nothing else.

- **2026-08-06** (Lane B build session): **Phase 5 batch 3 — the cli/config/service dead-code cluster, 9 items.** Re-derived against `main` @ `b5388bb`. Shipped `[x]`: D061 (cli's duplicate `_list_agent_packs`), D062 (`_run_from_config`, `_find_pid_path`, `_stop_manager_pid`), Q008 (the unreachable `project_path` guards), Q032, Q038, Q031, Q054, Q086, Q059.
  **Q008's count in the finding is wrong but its analysis is right: there are seven sites, not six** — the finding's own prose lists seven while the headline says six. All seven confirmed unreachable at HEAD, because `_detect_project_root()` returns a `Path` or raises `click.UsageError`, and `paths.home_dir()` always returns a `Path`: `status`, `monitor_add`, `monitor_pause` (else-branch **and** the `where` ternary), `monitor_remove` (else-branch), `event_server_stop`, `_find_transcript` (the `else "bobi-manager"` arm), `agents_browse` (the `if project_path else []` after `home_dir()`). `_try_detect_project_root()` is the genuinely nullable helper and its one caller was left alone.
  **Q032 shipped partially, and deliberately so.** Only `registries` and `default_role` were deleted; **`chat` was kept**, per the falsification recorded in batch 1's amendment. `Config.registries` was the dead one — `registry.py:77` reads `raw.get("registries")` off its own dict and never touches the dataclass field, so registry resolution is untouched.
  Suite: **4262 passed / 1 skipped** with **zero test edits** — identical to the baseline.

- **2026-08-06** (Lane B build session): **Phase 5 batch 4 — the setup-package cluster plus three stragglers, 8 items; Phase 5 is now closed.** Re-derived against `main` @ `b5388bb`. Shipped `[x]`: Q037, Q120, Q065, Q066, Q067 (setup), Q013, Q082, Q057.
  **Q067's unreachability was derived, not assumed.** `tools_prompt` is only ever reached from `custom_services()`, which filters `conn.kind == "custom"`; every custom connector is built by `services._custom_connector`, which always constructs one non-optional `Secret(_env_var_for(clean), …)`; and `_env_var_for` is **byte-identical** to `_env_var_fallback`, ending in `f"{s or 'SERVICE'}_API_KEY"` and so never returning empty. `Connector.credential_var` therefore cannot be falsy on this path, and the `or` arm was a duplicate of the function it would have fallen back to.
  Deleting the three `setup/actions.py` helpers left `click` with no remaining use in that module, and deleting `run_setup` left `Path` unused in `setup/__init__.py`; both imports went with them.
  Suite: **4250 passed / 1 skipped**, 12 below baseline, and `git diff -- tests/ | grep -c '^-    def test_'` is exactly **12** — every one of them a test of code deleted here (7 for `context_for_events`, 4 for the two setup helpers, 1 for `SessionRegistry.log_path`). Q057's two call sites were edited rather than deleted, since they test `approve_pending`'s real behaviour and only passed an argument the body never read.
  **Phase 5's two validation-gate boxes are flipped, with the measurement stated exactly.** Gate 1 (grep) was run per batch over `bobi/ tests/ agents/ docs/ skills/ event-server/ scripts/` and is clean for all 40 deleted names. Gate 2 was satisfied by a real full-suite run on this batch's tree — unit **4250 passed / 1 skipped** plus integration **354 passed / 20 skipped, 0 failures, 0 errors** (17m03s, Node 20 + `event-server && npm ci`). That integration number is the handoff's 329 baseline **plus** the 25 tests that used to error, confirming `#975`'s fix holds.
  **Two honest qualifications on gate 2.** First, the green was measured per batch against `main`, not on the union of all four — no single tree ever held all 40 deletions at once, so the union is proven by post-merge CI on the last merge commit, not by this run. Second, the gate says "zero test edits other than deleting tests of deleted code", and that is narrower than what shipped: batch 2 **retargeted** `_default_spawn_curator`'s and `_check_policy`'s tests onto the surviving names, and batch 4 dropped an ignored argument at two `approve_pending` call sites. Those edits were deliberate — in each case the deleted alias was the only route a test took to live behaviour, so deleting the test would have dropped real coverage rather than dead coverage. The gate's spirit is met; its literal wording is not, and this records the difference rather than papering over it.

- **2026-08-06** (Lane B build session): **Phase 6 batch 1 — the `events/` cluster, 7 of its 8 items.** Re-derived against `main` @ `17ad48e`. Shipped `[x]`: D095, Q019, D096, D077/Q105, Q064, Q112, Q020. Line numbers had drifted again (Q020's helpers 604→940; D095's orchestrator half 992/1046→1269/1331); items were located by symbol.

  **Q020 is the spine of the batch and went first, because two other items resolve onto it.** `_slack_auth_info` / `_slack_app_id` moved out of the event-server launcher into `bobi/slack.py` as public `resolve_auth_info` / `resolve_app_id`. Bodies moved verbatim: both stay best-effort (empty strings on failure) and keep hand-building the request rather than routing through `_slack_api`, which raises on a non-ok response — converting them would have changed the error contract every caller depends on, which is a different change than the one this item asks for. The finding names two cross-module private importers; there are **three** — `bobi/doctor.py:498` was added after the review tree. All three now import the public names. Ten monkeypatch sites across three test files were retargeted from `bobi.events.server` to `bobi.slack`.

  **D095's prescribed fix is over-scoped, and only the true half shipped.** The finding says slug normalization "is implemented twice" and asks for "one shared remote-slug helper". The two normalizers answer **different questions** and their divergence is now deliberate and tested: `adapters._parse_github_url` extracts a slug and compares the host to `github.com` **exactly** — that is D075's fix, pinned by `tests/test_adapters.py` against `github.company.com`, `notgithub.com`, and `github.com.evil.test` — while `orchestrator._remote_matches_slug` asks "does this remote point at this slug" on any host. Merging them would either reopen D075 or silently stop `_resolve_repo_root` resolving a non-github remote; neither is behavior-preserving. What **is** genuinely duplicated is the `git remote get-url origin` shell-out plus its failure handling, and that is now `bobi/gitutil.py::origin_url`, used by both. The module docstring records why interpretation deliberately stayed with the callers. The item is `[x]` because the removable duplication was removed, not because the finding was executed as written.

  **A consequence for the orchestrator batch, recorded so it is not re-derived wrongly: Q063's premise is now inverted.** It observes that `_setup_worktree` re-imports `subprocess as sp` "even though the module already imports subprocess at top level". Routing `_resolve_repo_root` through `gitutil` left that top-level import with no remaining use, so it went. `bobi/workflow/orchestrator.py` now has **only** the function-local import, and Q063's fix is whatever the next builder decides it should be — not "delete the local one".

  **Q112's line-count claim is wrong; its analysis is right.** The finding predicts "~25 lines" saved by collapsing four unauthorized-topic tails. The four `log.warning` calls carry deliberately different sentence-shaped wording and different `action` word pairs, so only the bookkeeping is actually common: the real saving is ~4 lines. It shipped anyway, as a `mark_unbacked` closure, because the value is the **invariant** getting one home — an unbacked topic is always reported and is kept only when not filtering — so a fifth branch cannot get it wrong. Every log call is byte-identical to before.

  **Q019 exposed a latent widening that is NOT fixed here.** Channel resolution now delegates to `bobi.slack.resolve_channel_id`, which is strictly more capable (public-only degrade on `missing_scope`, `exclude_archived`, `@handle` DMs, correct `[CGD][A-Z0-9]{6,}` ID shape). Only `RuntimeError` is absorbed, deliberately: the pre-existing code let a transport failure escape to `_detect_slack`'s handler, which returns "no Slack subscription at all", and swallowing it would instead hand `_slack_keys` an empty channel list — which subscribes to the **whole workspace** rather than the configured channels. That transport path is now pinned by a new test. The same widening still reaches production by the other route — every configured channel resolving to "not found" also yields an empty list and a workspace-wide topic — and it is **left alone** because fixing it is a behavior change, not a consolidation. It belongs to a lane that can decide it deliberately.

  **Q105's redundancy was verified, not assumed.** `_prepare_chat_events`' `slack.thread_reply` fast path is reachable only when `source == "slack"`, and `channels._handlers` is a static dict that always maps `"slack"` to `SlackInputChannel`, whose `prepare` opens with the identical branch. One test, `test_drain_does_not_reuse_placeholder_for_thread_reply`, asserted `prepare` was **not called** — the call shape, not the delivered result, and it was the only test doing so. It was **retargeted** to drive the real handler and assert the delivered batch, which is the behaviour its own docstring names; the two sibling tests that already exercise the live path were untouched and still pass.

  **Q108 is deliberately left `[ ]`** despite being filed under this cluster. It is not an `events/` item: it spans ten write sites in six modules (`memory.py`, `history.py`, `kb/store.py`, `workflow/state.py`, `workflow/orchestrator.py`, `events/client.py`), and unlike everything else in this batch it changes the **content** of timestamps written to durable state, so an upgraded install mixes conventions on disk. Bundling it here would have put unlike things in one PR. Re-derived counts for whoever takes it: **ten** naive `time.strftime` sites (the finding says nine) and **six** aware-UTC sites in five modules (the finding says five in four) — `bobi/monitors/run_records.py:70` is a new one-line `now`-helper that is the natural surviving implementation.

  Suite: **4378 passed / 1 skipped**, exactly 1 below the 4379 baseline measured on `17ad48e` — two `_is_channel_id` tests deleted (its job is now `resolve_channel_id`'s regex, covered by `tests/test_slack_files_and_threads.py`) and one added for the transport-failure property above. A first baseline run in the worktree reported one failure in `tests/test_instructions.py`; it did not reproduce alone, did not reproduce in a clean full-suite run on the same commit, and CI's `Unit tests (3.13)` is green on `17ad48e`, so it is recorded as a flake rather than a regression.

- **2026-08-07** (Lane B build session): **Phase 6 batch 2 — the `cli.py` cluster, 6 of its 7 items.** Re-derived against `main` @ `f724ccf`. Shipped `[x]`: D050/Q007, Q009, Q046, Q047, D063, Q126. Q127 is `[f] state:refuted` — see below. Line numbers had drifted again (D063's pair 2856/2897→3131/3205; Q046's `_post_event` 2960→3256); items were located by symbol.

  **D050/Q007 was half-fixed before this session, and the finding does not say so.** It claims "all three bodies are line-for-line identical today" across `cli.py`, `service.py`, and `subagent.py`. They are not: `cli.py`'s trio had already been reduced to thin delegates onto `bobi/events/server.py`'s `local_port_from_url` / `resolve_local_port` (the D019 fix), so the surviving duplication was **`service.py:816-861`** — a verbatim copy of all three — and **`subagent.py:1378`**, a nested `_local_port` re-deriving the URL parser. Both now call the `events/server.py` definitions, and `cli.py`'s three wrappers are gone: two were pure delegation, and the third only added the `--port` override, which is now one readable line at its single call site. One extra consolidation the finding does not name but the chain needs: the literal `"event-server.port"` was spelled in **four** production places (`cli.py` once, `events/server.py` three times, on both the read and write sides). It is now `events.server.local_port_file`, which deliberately does **not** create the state directory — the two write sites already call `paths.state_dir` for that, and moving the mkdir into a path helper would have made a read path create state.

  **Q046's third wrapper had a test attached to it.** `_clear_manager_session` has exactly one production caller (`restart --fresh`, systemd path), but `tests/test_bubble.py` imported the CLI wrapper directly to exercise the wipe. The wrapper only added a `click.echo`; the behaviour under test lives in `service.clear_manager_session`. The test was **retargeted** to the service function rather than deleted, and its module docstring updated to name it. The wrapper's docstring also carried a stale claim — "start, `--fresh`, and transcript lookup all resolve the same name through here" — which was true of `_manager_session_name` when written and is not now; start and `--fresh` go through `bobi.service` directly. Deleting the wrapper deletes the claim.

  **Q047 needed the boundary checked, not just the dataclass.** The finding says every result in the doctor loop is a `bobi.doctor.CheckResult`. That is true, but not for the reason given: `bobi/validate.py` defines its **own** look-alike `CheckResult` which declares `required` but **not** `sandbox_error`, and `doctor._check_services()` calls `validate_config()`. It converts each result into `doctor.CheckResult` at the boundary, so the look-alike never reaches the loop — which is what makes plain attribute access total. A comment now records that, because the next reader will find the second dataclass before they find the conversion.

  **Q127 is refuted, per the Q2 decision on unverified items.** Its evidence holds — `monitors add --url` is referenced by nothing in `bobi-agent`, `moda-agents`, or `bobi-deploy` — but the finding's own caveat is the deciding fact: the flag is documented in the command's `--help` usage example and it **works**, folding into `Monitor.extra` exactly as the schema intends. Removing working, documented CLI surface is a user-facing capability regression, which is not what Phase 6 ("behavior-preserving") authorizes, and "no caller in three repos" is a weak signal for a flag whose users are humans at a terminal rather than code. Q126 is the contrasting case and shipped: `ask --source` is on a **hidden** command, appears in no help example, and every documented invocation relies on the `engineer` default.

  Suite: **4378 passed / 1 skipped**, level with the baseline measured in a second tree at `f724ccf`. No test was added or deleted; one was retargeted.

- **2026-08-07** (Lane B build session): **Phase 6 batch 3 — the `setup/` cluster, all 7 items.** Re-derived against `main` @ `90e1459`. Shipped `[x]`: Q021, Q006, Q070, Q071/D123, Q069, Q022, Q023. Line numbers had drifted again (Q071's `serialize_state` 85→54, `SPEC_SLOTS` 59→62; Q069's probe 108→104; Q022's generators 543→587; Q023's endpoint 1284→1354); items were located by symbol.

  **Q023's named deviant has since been fixed the other way, on purpose.** The finding cites `/api/credential/value` as reading `.env` first, and prescribes converting it to the process-env-first house order. That line no longer looks like the finding: the `os.environ` fallback was **deliberately removed**, with a comment explaining why — reading ambient environment for any requested var served every secret exported in the shell that launched setup (`AWS_SECRET_ACCESS_KEY`, `ANTHROPIC_API_KEY`, …), none of them saved through setup. Applying the prescription there would have reopened that hole, so the endpoint is **left alone** and the new `actions.env_value` docstring records that it is not a candidate. What remains of Q023 is real and was fixed: `venn_key`, the Slack finalization screen's `_env_value`, and two `mcp_probe` sites now share one helper implementing the documented precedence, where two of them previously spelled it in the opposite order.

  **One site in Q023's list must NOT resolve by precedence at all.** `mcp_probe._scrub_result` uses the same `saved.get(var) or os.environ.get(var)` shape, but it is a **redactor**: it collects the values to blank out of probe output. Resolving one value there would blank the losing copy and echo the winning one verbatim in exactly the case the two disagree — and changing `_resolved_env`'s precedence without touching it would have *introduced* that leak, since the child would then run on the value the redactor no longer scrubs. It now scrubs **both** candidates. Two tests pin it.

  **Q021 was already half-done, and its remainder was larger than stated.** `default_secret_prompt` and `actions.py`'s only `click` import — the deletions the finding leads with — were already gone. The live half was the `prompt_fn` callback itself, plus something the finding does not name: `service` and `instructions` were read **nowhere** except in the call to `prompt_fn`, so they died with it. The signature is now `save_credential(state, project, var_name, value)`. `app.js` stopped sending a `service` field that nothing reads.

  **Q006 halved before it was fixed.** The finding names two import sites in `actions.py` (`:175` and `:353`); only one survives. `_resolve_agent_pack` moved to `bobi/install.py` (not `registry.py`) because that is where the other two names already live, and `cli.py` keeps it as an alias exactly like them. The load-bearing result is checkable: `bobi/setup/` now imports `bobi.cli` **nowhere**, so the setup package no longer pulls in click and `truststore.inject_into_ssl()` at import time to reach three functions.

  **Q022 went to `mcp_probe`, not a new module.** The finding offered either. The complaint it makes is that the dialogue is *split across two modules*, so a third module would not answer it — `mcp_probe` already owned the matcher half. `_probe_identity` moved out of `server.py` with the generators, its only caller being the mid-probe edit guard. The five existing TestClient tests of the flow pass untouched, which is what proves the motion behavior-preserving; the tests that were previously **impossible** — driving the turns on a bare `SetupState` — are the value the move buys, and six were added.

  **Q069's rewrite changed a control-flow shape the finding does not mention.** `urlopen` raises `HTTPError` on 4xx/5xx; `httpx` does not. So the non-2xx path moves from an exception handler to the inline status check, and the previously near-dead `if not (200 <= status < 300)` branch becomes the live one. Every user-visible error string is unchanged. The 4096-byte read cap was kept rather than switching to `resp.json()`: an unauthenticated remote host should not decide how much we buffer. The one test that reached this function patched `server.urlopen`, so it was **retargeted** onto `bobi.http.get` and extended to cover the reshaped branches.

  **The Q069 transport swap regressed an error path, caught by this session's own review.** `httpx.InvalidURL` inherits from `Exception`, not `httpx.HTTPError`, and `_validate_public_event_server_url` checks only scheme and netloc — so `https://256.256.256.256` reached the probe and raised out of it, 500-ing `/api/ingress/verify` where `main` returned a friendly `(False, …)` from urlopen's gaierror path. Fixed, with an unstubbed regression test that drives the real client so it fails on the pre-fix code. The general lesson for the remaining batches: **swapping a transport swaps its exception hierarchy**, and the new one's non-obvious members (`InvalidURL` sits outside `HTTPError`) are where behavior preservation actually breaks.

  Suite: **4394 passed / 1 skipped**, exactly the `90e1459` baseline of 4378 plus the sixteen tests added here. No test was deleted; one was retargeted.

- **2026-08-10** (Lane B build session): **Phase 6 batch 4 — the `monitors/` + `sdk.py` clusters, 7 items.** Re-derived against `main` @ `58eaccf` (two commits past batch 3's base — #985 and #996 landed in between). Shipped `[x]`: Q056, D093/Q058, Q015, D094, Q016, Q085, Q084. Line numbers had drifted again (Q056's loader 84→86, Q015's function 1215→1498, Q084's re-read 538→699); items were located by symbol.

  **D093/Q058's two copies are not "duplicated verbatim", and the difference is load-bearing.** `scheduler._parse_iso` caught `ValueError`; `script_cache_checks._parse_iso` caught `(ValueError, AttributeError)`. That second clause is what lets a **non-string** pulled out of a JSON state document read as absent instead of raising — and both callers there read `state.get(...)` off exactly such a document. The tolerant body is therefore the survivor, which means the scheduler's two call sites are **widened**, not preserved: a torn `monitor_state.json` holding a non-string `last_run` now reads as "no last run" where it previously raised. That is the direction this repo's loaders already go (unparseable state is empty, never fatal), so it shipped, but it is a behavior change and is recorded as one rather than filed under "behavior-preserving".

  **The survivor went to `run_records.py`, not to `tool_checks.py` as the finding suggests.** The appendix justified `tool_checks` only by "a shared home exists". `run_records` is the better one on two counts: it is the package's only **leaf** module (it imports nothing intra-package, so it cannot ever be one end of a cycle), and it already owns `_now_iso`, the *writer* of these strings — the reader now sits beside the writer, so the accepted format has one definition. It is also where Q108 will land when someone takes it.

  **There is a fourth `_parse_iso`, deliberately left alone.** `agents/eng-team/monitors/github_checks.py` carries its own copy. It is a **pack** file, not framework code — it is loaded by `_load_checks`' spec machinery precisely because it has no importable package parent — and packs are the distribution unit for domain behavior, so it should not reach into a framework private. `tests/test_checks.py` binds that copy, not either of the two consolidated here.

  **D094 undercounts the sanitizers, and the third one must NOT be merged in.** The finding names two (`tool_checks`, `script_cache_checks`); there are three. `run_records._safe_name` is a **different algorithm** — a whitelist `re.sub` with a fallback, against the others' two-character blacklist — and the two disagree on any name containing e.g. a space. They may differ freely because they key artifacts in **different directories** (`monitor_runs/` vs `scripts/`), and nothing reads one subsystem's stems with the other's rule. Merging them would have silently renamed one subsystem's files and orphaned the other's cache. Only the genuine `tool_checks` ↔ `script_cache_checks` pair was consolidated. `run_records._safe_name`'s docstring **claimed** "same shape script_cache uses", which was false; it now records the divergence and why it is allowed. A test pins both halves — that the two shared helpers are the same object, and that the ledger's is not.

  **Q015 was already half-done and the finding does not say so.** It describes seven ~10-line blocks that each end in `_publish_monitor_error(..., publish=self.publish)`. Since it was written, `_on_sleep_cycle_result` was **split** (the validations now live in `_apply_sleep_cycle_result`), a `tracker` was threaded through, and a `self._sleep_cycle_error` helper already absorbed the publish half. The residual duplication was the `log.warning` + call + `return` triple, seven times. The log line could **not** simply fold into `_sleep_cycle_error`: three of its ten callers log a different shape — one with no retry note (`:1412`), one with a conditional note (`_sync_reference_kb_or_publish_error`), one with a `: %s` summary — so a paired `_sleep_cycle_turn_back` takes the seven uniform sites and the other three are untouched. The uniform format string now exists exactly once.

  **Q016 carries one deliberate divergence in a degenerate case.** `notify_channel` now resolves in `_policy` like every other key, which ends a `yaml.safe_load` of agent.yaml on *every notification*. `_policy`'s overlay is presence-based (`if k in extra`) where `_slack_notify`'s inline resolution was truthiness-based (`extra.get(...) or install.get(...)`), so an explicit `notify_channel: ""` in a monitor's `extra` now means "off" rather than "inherit the install value". Every other key in this policy already behaves that way, and consistency at the documented chokepoint is the whole point of the item. `notify_channel` was also missing from the module docstring's config list; it is there now.

  **Q058's cross-pass half needed doing too, and the author-run review is what found it.** The item is filed as `D093/Q058`, and Q058's note — "several functions locally re-import stdlib modules that are already imported at module top" — was still true after the `_parse_iso` consolidation: `_write_gate_request` re-imported `tempfile` and `time as time_mod`, both already at module top. Those are gone, and `timedelta` (used once, imported locally, and genuinely *not* at top) was hoisted to join `datetime`/`timezone`. `bobi/monitors/scheduler.py` now has no function-local stdlib import at all; the deferred imports that remain are all bobi-internal, which is the house pattern the finding appeals to.

  **Three small fold-ins, taken because the change was already in that exact signature or docstring.** `_bump_failure`'s first parameter `name` was dead — both call sites passed `monitor.name` *and* `monitor`, and the body only ever read the latter — so it went with the signature change Q016 required. `docs/MONITORS.md`'s script_cache config table listed every `extra` key **except** `notify_channel`, which is the same invisibility Q016 complains about one layer out; it has a row now. And `run_records._safe_name` got the docstring correction described above.

  **Q085 shipped as the finding scopes it, not as its headline implies.** The two consumers routing bound-root through the `sdk` delegate (`kb/embedder.py`, `events/drain.py`) now call `bobi.paths.bound_root` directly. The `sdk.get_project_root` / `set_project_root` wrappers **stay**: `service.py` still calls `set_project_root` at two sites and several tests bind through them, and retiring them is the "eventually" half the finding itself defers. Two monkeypatch sites in `tests/test_kb_embedder.py` were retargeted onto `bobi.paths.bound_root`; aiming them back at the retired route fails both tests, which is what proves they are load-bearing rather than decorative.

  **Q028 is deliberately left `[ ]` and needs its own PR.** The finding says moving Claude-CLI path resolution out of `sdk.py` means "webapp/server.py:99 updates its one import" and that test patches "move mechanically". Re-derived: there are **~25 patch sites of the literal string `"bobi.sdk.get_cli_path"` across 7 test files**, plus 3 direct `sdk.get_cli_path()` calls in `tests/test_container_paths.py`. That is a ~30-file diff for one item, and bundling it here would have put unlike things in one PR — the same call made for Q108.

  Suite: **4432 passed / 1 skipped**, the `58eaccf` baseline of 4417 plus the fifteen tests added here (one of them parametrized three ways). No test was deleted; two monkeypatch targets were retargeted. Note the baseline moved: batch 3 measured 4394 at `ac0b55b`, and #996 changed the count in between.

- **2026-08-13** (Lane B build session): **Phase 6 batch 6 — the `brain/` cluster plus the transcript-locator chain, 5 items.** Re-derived against `main` @ `efd886f` (seven commits past batch 5's merge — the 0.57.0 cut and six fixes landed in between). Shipped `[x]`: Q083, Q026, Q027, Q104, Q034. Line numbers had drifted again (Q083's constant 231→275 and its deferred import 69→72; Q026's protocol 148→205, `stream_once` 487→522 / 178→222, caller 70→66; Q027's copy 328→363; the plan's own note for Q027/Q104 said `cli.py:1568` where the fourth copy is at **1403**); items were located by symbol.

  **Q027 and Q104 are one change, as the 2026-08-05 re-validation note directs, and the fourth copy went with them.** The survivor is a new public `chat_history.find_claude_transcript` (the rename of `_transcript_path`, which had to become public because its callers are in other modules). It absorbed the **guarded** body from `brain/claude._claude_transcript_path` rather than the house one: the scan races Claude Code writing the tree, and an unreadable root now means "no transcript here" instead of a `PermissionError` out of a history read. **That is the batch's one deliberate behaviour delta** — every other path is byte-identical, and a test pins the guard. `history.index()` (Q104) and `cli._find_transcript` (the fourth copy, which had no test at all and is the one an operator hits as `bobi logs`) both gained CLAUDE_CONFIG_DIR awareness by moving onto the shared rule, which is exactly the silent miss Q104 describes: bobi renders per-team CLAUDE.md under a config dir (#779), so those two paths were blind to the transcripts of the agents bobi itself runs.

  **`history.index()` needed a dedupe the finding does not mention, and the first attempt was at the wrong level.** Two roots can be one tree. `claude_projects_dirs` dedupes by **spelling**, so a `CLAUDE_CONFIG_DIR` symlinked at `~/.claude` survives it as two distinct roots and would double-index every file. The dedupe resolves each **root** once rather than each file — same correctness, one `resolve()` per root instead of per transcript. A per-file version shipped first and its mutant **survived**, which is what exposed that the test could not reach the case; the symlink test that replaced it is red without the guard.

  **Q034 did NOT take the shape its own "Detail" prescribes, and the reason is a fail-loud path.** The finding asks for `_configured_brain` to load via `Config._parse(paths.agent_yaml_path(root), env)`. Probed empirically: `_parse` **raises** on a malformed `services:` or `requires:` block (`TypeError: 'int' object is not iterable`) where the current hand-parse still extracts the brain. This is the **spawn** path, wrapped in `except Exception: return {}`, so adopting `_parse` would turn a broken-but-live team's gateway pin into a silently native session dialing the real vendor with gateway credentials — the #789 leak, re-entered through the back door. What shipped is the finding's own "at minimum" clause, done properly: the tolerant `brain:`-only read stays, its result is wrapped in a `Config`, and the expansion goes through the `brain_*` properties, so the duplicated `'responses'` default and the presence-based gateway predicate now have exactly one definition. A test drives that single-definition property directly — move `Config.brain_wire_api` and the pin moves with it.

  **The two config-to-pins call sites are now twins.** `bobi.brain` gained `pin_process_brain_from_config` beside the existing `set_process_brain_from_config`, both expanding through one `_brain_pin_kwargs`. That is the docstring promise `set_process_brain_from_config` already made — "a new brain config field cannot be threaded into one site and missed in another" — and `bobi.env` was precisely the site it was missing. Equivalence was proven by a **588-case differential** (7 kinds × 3 base-url states × 7 extra-field shapes × 4 seed environments) comparing the old hand-rolled expansion against the new one: **zero divergences**. `bobi.env`'s module-level import set is unchanged at 2 (`Config` is imported inside the function, per batch 5's failure 4).

  **Q026 shipped as the "explicit NotImplementedError" arm, not the "real implementation" arm.** The finding offers both. A real codex `stream_once` is a new capability, not a consolidation: `codex exec` can run the one-shot turn but emits no token-level partials, so it would silently degrade the setup pour to one block of text at the end, and deciding what the pour should look like without partials is a behaviour question this phase cannot settle. Declaring `stream_once` on the `BrainFactory` protocol is documentation only at runtime — protocols are not enforced without `runtime_checkable` + `isinstance` — so the enforcement is a test that reads the protocol's own method list and asserts every entry of `_BRAINS` implements it. That test is what makes the contract real, and it is red if either the protocol declaration or the codex implementation is removed. The user-visible failure type is unchanged (`LLMError` either way); only the message changed, from `'CodexBrain' object has no attribute 'stream_once'` to one naming the brain and the fix.

  **Q083 is pure code motion and its test needed a second attempt.** The sentinel moved to `brain/gateway.py` beside the pin helpers that read it, re-exported from `bobi.brain` for the existing `__all__` entry, and the deferred `from bobi.brain import ...` inside `require_gateway_base_url` is gone. The first test tried to prove "no circular import" by importing `bobi.brain.gateway` in a subprocess and asserting the package was absent — **which can never hold**, since importing a submodule always executes its package `__init__`. The property that is actually testable is that `gateway.py` no longer reaches back: deleting `bobi.brain`'s re-export must still leave `require_gateway_base_url` working, which raises `ImportError` under the old shape.

  **Eleven mutants, eleven red** (after the one survivor described above was fixed): the deferred import restored, the re-export dropped, `stream_once` removed from `CodexBrain` and separately from the protocol, `CLAUDE_CONFIG_DIR` dropped from the shared locator, the `OSError` guard weakened, `brain.claude` pointed back at a home-only locator, the indexer pointed back at `~/.claude/projects`, the root dedupe dropped, and the `wire_api` default and gateway predicate each re-hardcoded in the expansion. Six pre-existing `brain.claude` transcript tests also turn red on the delegation mutant, so that consolidation was already covered.

  Suite: **4558 passed / 1 skipped**, the `efd886f` baseline of **4537** plus exactly the 21 tests added here (5 locator + 3 indexer + 3 `bobi logs` + 3 factory-contract + 2 gateway-ownership + 5 brain-pin). No test was deleted; ten monkeypatch targets were retargeted (eight from `chat_history._transcript_path`, two from `history.PROJECTS_DIR`). `pyflakes` is clean in all eight touched modules and unchanged repo-wide at 30. No doc quotes any renamed symbol, so no doc update was owed.

  **Integration: 342 passed / 49 skipped / 0 failed** (Node 20 + `event-server && npm ci`). The first run of this batch reported **one** failure — `test_setup_flow.py::TestCreateFlow::test_idea_to_installed_pack` — and it was NOT waved off as the usual otel flake, because that test drives a **real Claude session** through the digestion pour, which is the same `stream_once` surface Q026 touches. Four checks cleared it, and the order matters: it **passes alone** (82s); a **control run of the whole suite on unmodified `origin/main` passed** (342/49/0); a **re-run of the whole suite on this branch passed** (342/49/0, the identical figure); and statically there is **no mechanism** — `bobi/setup/` imports none of the eight changed modules, `brain/stub.py` and `get_brain` are untouched, and `ClaudeBrain.stream_once` is byte-identical, the only `claude.py` change being the transcript locator behind `_max_turns_from_transcript`, whose one delta (tolerate `OSError` rather than raise) is strictly more forgiving. The test's assertions are model-behaviour claims ("goal slot stayed empty"), so the failure is recorded as real-model nondeterminism.

  **The integration skip count is 49, up from the 20 batch 5 recorded — and it is not this batch's doing.** It is **49 on `origin/main` and 49 on this branch**, measured in the two runs above, so nothing here caused it; it grew across the seven commits since batch 5. Recorded rather than passed over, because a skip is a test that did not run and this repo has already shipped four green-but-vacuous lanes behind exactly that reasoning. **Nobody has audited what those 29 additional skips are** — worth a look from whoever takes the next batch, and explicitly NOT verified here.

  **Both config-to-pins entry points were differentially verified, not just the new one.** `pin_process_brain_from_config` and `set_process_brain_from_config` were each compared against their pre-refactor bodies over the same 588-case matrix (7 kinds × 3 base-url states × 7 field shapes × 4 seed environments): **zero divergences each**. The second differential was added during author review, on noticing the first proof covered only half of what the commit changed.

- **2026-08-13** (Lane B build session): **Phase 6 batch 7 — the vestigial-surface cluster (dead parameters and single-caller indirection), 6 items.** Re-derived against `main` @ `280c5e6` (batch 6's own merge). Shipped `[x]`: Q092, Q114, Q096, Q072, Q060, Q061. Line numbers had drifted on **every** item (Q092's `rollup_costs` 156→218 and its caller `cli.py` 3383→3630; Q114's classmethods 88→89; Q096's wrapper 181→175 and its caller 3333→3575; Q072's parameter 37→39 and its caller `setup/webui/server.py` 1468→1402; Q060's function 108→216 and its call sites 377/1126→504/1424; Q061's branch 435→568); items were located by symbol.

  **Q018 was re-derived, confirmed, and deliberately left `[ ]` for its own PR.** Its premise holds exactly as written — `_cache_dir` returns `paths.agent_cache_dir()` and `_all_registries` reads only `paths.ensure_global_config()`, neither dereferencing `project_path`, which makes `list_remote`'s `_all_registries(project_path) if project_path else [DEFAULT_REPO]` branch the accidental semantics the finding names. It was pulled from this batch on size, not doubt: ~18 signatures in `registry.py` plus call sites in `build.py`, `compose.py`, `cli.py` and `setup/open_mode.py`, plus 9 test files. That is a mechanical sweep of a different character from the six small removals here, and bundling it would have buried them — the same call already made for Q028, Q093 and Q108.

  **Q060 was carried one step past its own prescription, because stopping where it stops would have created a fresh instance of the defect it describes.** The finding's text says `_execute_notify_step` threads a `cwd` parameter "**partly** to feed it". That is wrong: `cwd` is referenced at exactly one line inside that function (the `_find_project_root(cwd)` call), so the moment `_find_project_root` loses its argument, `_execute_notify_step`'s own `cwd` is dead — a parameter that looks meaningful and does nothing, which is precisely Q060. It was dropped too, across both orchestrator call sites and 8 test call sites. One assertion in `tests/test_webapp_resume.py` had pinned the threading of `run.cwd` through that call; it was removed rather than retargeted at `run_key`, which `_seed_run` never sets and against which the comparison would have been vacuous.

  **Q072's deleted branch was the one user-visible string in the batch, and it had no test.** `serve_local` printed either the caller's `announce(url)` or a label-built default; `bobi setup` passed a lambda reproducing the default character-for-character. The existing `serve_local` test asserted the minted secret, the bound socket and the browser URL, but never the banner — so nothing was holding the text that tells an operator where the UI is. A test now pins the exact printed line, and a reworded banner is red against it.

  **Q092's `--by` flag was likewise exit-code-only.** `test_by_dimension_still_runs` asserted `exit_code == 0` for all four dimensions and nothing more, so dropping `group_by=` from the `format_costs` call would have survived it. With `rollup_costs` no longer taking the parameter, `format_costs` is the sole place the flag can act, so the coverage was strengthened to assert the output actually names the requested dimension. It needs a seeded session: with no cost data the command returns early and never formats — the first version of this test asserted against the empty branch and failed for that reason.

  **Seven mutants, seven red.** The `format_costs` call losing `group_by` (3 of 4 dimensions red — `provider` survives by being the formatter's own default, correctly); the `recall-memory` CLI pointed at a different KB name; `ResolvedRecipe.coerce` losing the `data or {}` guard the two collapsed classmethods used to carry; the `serve_local` banner reworded; `_find_project_root` regaining a required `cwd` (4 red); `_make_session` no longer forwarding `agent_name` (1 red); and — because the first `_find_project_root` mutant only killed tests at the notify call site — a targeted mutant breaking **only** the `run_workflow` call site, which turned **43** tests red and proves the second site is exercised unpatched.

  **Q061's collapse is safe because the falsy case is only `""`.** Both branches called `resolve_agent_prompt` with `agent_name or ""`; `agent_name` reaches `_make_session` as `current_agent`, which starts as `first_agent = role or ""` and is only ever reassigned from `step.agent or current_agent`, so it is always a `str` and the sole falsy value it can hold is the empty string the else-branch passed explicitly.

  Suite: **4564 passed / 1 skipped**, the `280c5e6` baseline of **4558** plus exactly the 6 tests added here (1 rollup-breakdown invariant + 4 parametrized `--by` assertions + 1 `serve_local` banner). The baseline is not inherited from batch 6's note — it was measured on this branch before any test was added, and it agrees. No test was deleted; one was renamed (`test_recipe_from_install_adopts_verbatim` → `test_recipe_coerce_adopts_pinned_install_verbatim`) and one had its stub signatures narrowed. `pyflakes` over the ten touched files is **14 findings on both `280c5e6` and this branch — the identical set**, none new. (An earlier differential reporting 1-vs-1 was vacuous: zsh does not word-split an unquoted `$F`, so both sides linted one nonexistent path.)


- **2026-08-13** (Lane B build session): **Phase 6 batch 8 — the missed-reuse cluster (hand-rolled helpers the house already owns), 6 items.** Re-derived against `main` @ `bf11965` (batch 7's own merge). Shipped `[x]`: Q012, Q091, Q035, Q097, Q033, **D052 as bookkeeping only**. Line numbers had drifted on every item (Q012's parser 192→277; Q091's local imports 419/458/531/658→415/454/527/654 and its quoted annotations 410/435→406/431; Q035's verbs 57→57 with the block ending at 132; Q097's hand-parse 146→159; Q033's two sites 215/262→205/252); items were located by symbol.

  **D052 was ALREADY RESOLVED, by this phase's own batch 1, and nothing recorded it.** Q019 (PR #993) rewrote `events/adapters._resolve_channel_names` to delegate to `bobi.slack.resolve_channel_id`; the second paginated `conversations.list` D052 names is gone, and that call now appears exactly once in `bobi/` (`slack.py:448`). Flipped with no code change. **Its second claim is NOT closed and must not be read as closed by the `[x]`:** the workspace-widening path — `_slack_keys(team_id, [])` subscribing to the entire workspace when every configured channel fails to resolve — is the deliberate non-fix recorded against Q019 in batch 1. An item can be half-fixed by a sibling finding; check both of its claims before flipping.

  **Q035 did NOT take the shape its own "Detail" prescribes, and applying it as written would have been a security regression.** The finding says post/get/put/delete become one-line delegates to `request()`. It predates `post`'s `follow_redirects` parameter, which `request()` does not carry — so a literal delegation **silently drops it**. That flag exists because httpx strips only `Authorization` and `Cookie` on a cross-origin redirect, so a credential in a CUSTOM header would travel verbatim to whatever host a redirect names. `request()` now takes and threads `follow_redirects`; `post` keeps it (a test pins its default at `True` by signature inspection). The mutant that drops it turns `test_a_redirect_never_forwards_the_credential` red — it was load-bearing, not hypothetical. **This is the fifth finding in this plan whose prescription aged wrong because its target moved after the review** (with Q034, Q023, D050/Q007, Q109).

  **Q033 moved a question to the object that owns the answer.** Both `cold_memory_kb_needs_sync` and `sync_reference_to_cold_memory_kb` opened `store._connect()` and imported the private `_fetchone`/`_chunk_text` to run the same entry-count/vector-count query. The new `KBStore.source_index_complete(source, source_hash, content)` answers it inside the store that owns both the chunking rule and the schema — same SQL, same comparison, and `memory.py` now imports nothing private from `bobi.kb.store`. The vector half is the subtle one: a source with entries but no vectors is **stale, not present**, and a mutant dropping that half survives an entry-count-only test.

  **Q091 fixed four real pyflakes findings as a side effect.** The four function-local `from pathlib import Path` forced string-quoted annotations, and pyflakes reported `undefined name 'Path'` at both signatures. `bobi/conversation.py` (Q012's target) imports nothing from `bobi`, so its top-level import is cycle-free — verified by importing, not by inspection.

  **Five mutants, five red:** `post` dropping `follow_redirects` on the way to `request()`; `source_index_complete` ignoring the vector half; `stop()` killing on a zero pid; a divergent private grammar re-inlined into `auth_bootstrap`; and the top-level `Path` import removed (9 red plus 4 pyflakes findings).

  Suite: **4576 passed / 1 skipped**, the `bf11965` baseline of **4564** plus exactly the 12 tests added (3 `source_index_complete` + 1 malformed-pid + 8 login-channel grammar, 7 of them parametrized cases). No test was deleted. `pyflakes` over the nine touched files goes **11 → 7 with ZERO new**, the four removed being Q091's own `undefined name 'Path'`. Both sides of that differential are non-zero — checked deliberately, after batch 7 shipped two vacuous "green" comparisons.

- **2026-08-13** (Lane B build session): **Phase 6 batch 10 — the `subagent.py` pair plus the `tool_library` shadowing, 3 items.** Re-derived against `main` @ `a7de3b8` (batch 9's own merge). Shipped `[x]`: Q010, Q051/D070, Q029. Line numbers had drifted on every item again (Q010's function 1302→1246; Q051's `run_check_blocking` 1738→1793, gate 1945→2010, curator 2019→2086; Q029's `CATALOG_DIR` 44→60); items were located by symbol.

  **A finding's cross-pass half can be resolved by an EARLIER batch of this same plan, in the opposite direction.** D070 asks `run_check_blocking` to route through `_parse_check_output`. That function does not exist: Phase 5 batch 2 deleted it as **Q050**, deciding the same duplication the other way — keep the inline copy, delete the shim and its seven tests. Applying D070 as written would have resurrected a deletion this plan had already reasoned through. **Before applying a cross-pass half, grep for the symbol it names; a sibling finding may have spent it.** This is the sixth prescription in this plan to have aged wrong (with Q034, Q023, D050/Q007, Q109, Q035).

  **Provenance decided Q010, where taste would have guessed.** Its two sync blocks differ in whether an authorize failure falls back to the raw subscribe list — a behaviour difference, so "reconcile them" needed a choice. `git log -L` over each block settles it: the remote one is the original (history through #488, `cf6559b4`, which ADDED the fallback) and the local one is a copy made later by #538 (`a42106e9`) that flattened the two try/excepts into one and lost it. `authorize_resources` still documents the original's contract. **When two copies disagree, `git log -L` tells you which is the surviving implementation — the phase rule already says to move callers to it.**

  **Three of this batch's own new tests were vacuous, and mutants caught all three.** Two raised `AssertionError` from inside an `httpx.MockTransport` handler to mean "must not PUT" — the caller wraps its PUT in `except Exception`, so the raise was swallowed and answered by the very re-register fallback under test, and the mutant passed. One asserted an authorize **403** as proof of the exception path, but `authorize_resources` absorbs denials internally via `mark_unbacked` and never raises; only `ensure_bubble` failing reaches that branch. **Assert on captured requests, never by raising inside a stub the production code catches** — and check that the error you inject is one the code under test can actually see.

  **Two untested behaviours surfaced only because a mutant survived.** The three verdict runners' DEFAULT slug was pinned nowhere (only the explicitly-named case was), so a wrong prefix left all 155 tests green. And `_start_event_subscription`'s "no saved deployment → register, do not PUT to the empty-id URL" branch was indistinguishable from the pre-bubble branch in every existing test, because none of them created a `bubble.json`. Q029's move is likewise the first time `__init__.py` and `__pycache__/` sit inside `CATALOG_DIR`.

  **The finding's own evidence went stale in two ways at once for Q029** — its enumeration (three catalog entries; there are four) and its blast radius (it does not mention `.github/workflows/container.yml`, which path-filters the literal `bobi/tool_library.py` and would have silently stopped triggering the image lane, the #909 shape). **A file move's blast radius includes non-Python files that name the path.**

  **Nine mutants, nine red** — 4 for Q010 (authorize-failure fallback removed; the no-deployment branch disabled; the configured-local `ensure_running` skipped; the unconfigured arm syncing instead of registering), 3 for Q051 (slug prefix from role; gate seed dropping the item keys; curator registered as monitor), 2 for Q029 (the `tool.yaml` predicate dropped; `CATALOG_DIR` left at the pre-move path). Each was verified APPLIED by diffing the file, after a guard using `grep` with an embedded `\n` silently failed to detect a mutation that had in fact landed.

  Suite: **4616 passed / 1 skipped**, the `a7de3b8` baseline of **4604** plus exactly the 12 tests added. No test was deleted — verified by `--collect-only` on both sides (4605 → 4617), not by arithmetic alone. A thirteenth test of mine was written and then removed before landing: `test_the_three_prefixes_are_distinct` exercised only a test-local helper, asserting that an f-string beginning `check-` begins with `check`, and would have stayed green against any production code at all. `pyflakes` over `bobi/` is **26 → 26 with ZERO new**, both sides non-zero. Q029's packaging is proven by a wheel-content diff (213 entries both sides, differing in exactly the one moved entry name) and a live `dep_bootstrap` differential — identical dependency hash and rendered build script — whose probe was sanity-checked to confirm the two sides genuinely import different files.


## Notes

- **Deferred to a successor structural-refactor plan** (explicitly out of scope here; do not partially attempt): Q001/Q040 (cli.py command-tree rewiring), Q002 (subagent.py event-subscription extraction, ~370 lines + 10 test monkeypatch sites), Q003 (`_run_workflow_async` decomposition), Q103 (run_phase_blocking/spawn_adhoc skeleton unification), Q106 (events/server.py split), Q004 (local.ts route table), Q036 (core.ts god-file split), Q005/Q111/Q129 (shared integration-test harness). The appendix entries carry the verifier traces for when that plan is written.
- **Provenance**: two Workflow review passes 2026-07-19/22 (146 + 135 agents); findings JSONs and the full HTML report live with the review session; the committed appendix is the durable extract.
- **Operational**: until D017 lands, issue pickup needs a Slack directive (the auto_dispatch rule never matches). Lane A (Phase 1) is the trust-restoring lane — the orchestrator session supervises its build actively rather than trusting terminal signals that lane exists to fix.
- Line numbers in checklist items are from the review tree (`58aba2c`); builders locate by symbol name when lines have drifted.
