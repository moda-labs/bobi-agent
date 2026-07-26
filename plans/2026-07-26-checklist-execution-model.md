# Checklist-driven execution: retire the workflow step machine

> **Status:** Approved
> **Tracking issue:** moda-labs/bobi-agent#852 · **Created:** 2026-07-26 · **Last amended:** 2026-07-26 (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

Move agent execution off the YAML step machine and onto a single plan file that
is both the human-reviewable design document and the agent's working checklist.
An agent reads it, does the next unchecked item, proves the item, checks it off,
commits, and repeats until done or blocked. Nothing orchestrates it; the loop is
prompt text.

The goal is **flexibility first, durability second**. Flexibility: the lifecycle
lives in prompts and skills, changeable by editing text, instead of in a second
control-flow language that duplicates the plan badly. Durability: the state is a
committed file, so a dead session loses at most its current item.

The 10x version here was *less machinery*, and finding that took three rounds of
review. Successive drafts of this plan proposed a driver process, a typed run
record, a lease module, a native-action executor, and a budget accounting layer.
All of it is gone. What survives is one code change (make a turn-cap hit
resumable), one small parser, one default monitor, and prompt text — because the
framework already has the pieces: the session registry knows what is alive, the
monitor scheduler is a mechanical tick that spawns agents, and the director is
already good at deciding whether a stalled unit needs a poke.

**A correction to the motivation, up front.** An earlier draft argued this was
needed to escape the 200-turn cap. That is false: the cap is per prompt,
multi-step workflows already get a fresh budget per step, and the only sessions
that died were single-prompt `adhoc` runs (Problem 3). Turn-budget survival is a
*consequence* of this design, and the narrow fix for that symptom alone is
Phase 1. The case for the rest is duplication and rigidity, not the cap.

## Problem

Verified against the working tree and the live `moda-eng-team` box on 2026-07-26.
Line references are from that day's `main` (`29a382b`).

**1. Step completion is structural, so a run can finish without the work being
done.** `_validate_handoff` (`bobi/workflow/orchestrator.py:1211-1213`) checks
only that required handoff *keys* exist, never their values. `status: in_progress`
is accepted identically to `status: complete`; the run walks to the end of its
step list and emits `agent/session.completed`. Fixing this inside the engine
would mean teaching `bobi/` what "reviewed" and "documented" mean, which
CLAUDE.md's first principle forbids.

**2. The step list has stopped carrying information, while the plan file carries
more.** Of 14 workflow YAMLs in `agents/`, **8 are single-step** (eng-team
`adhoc`, `build-failure`, `merge-conflict`, `stall-recovery`, `pr-feedback`;
`personal-assistant/adhoc`; `smoke-test`). `agents/eng-team/workflows/adhoc.yaml`
is one step whose entire body is `prompt: "${{input.task}}"`. The multi-step
survivors delegate their substance to skills: every agent step in `moda-agents`'
`plan-execute.yaml` amounts to "read the `build` skill in full and do what it
says." Meanwhile `plans/2026-07-22-review-remediation.md` carries 251 checklist
items across 9 phases with `file:line` anchors and gates like
`grep -rn "write_text" bobi/ | grep -iE "state|config"`. The step list is a
weaker copy of a better artifact.

**3. Long single-prompt work dies and loses everything unpushed.** Two Lane A
workers died on 2026-07-26 at `maxTurns: 200, turnCount: 201`
(`state.json status=failed, error="turn failed"`), both mid-edit with no handoff:

| session | kind | prompts | tool calls | hit the cap |
|---|---|---|---|---|
| `91856674` | `issue-lifecycle` (11 steps) | 9 | 296 | no |
| `53fbe1a7` | `pr-feedback` | 3 | 250 | no |
| `fcc79fc7` | `adhoc` (1 step) | 2 | 213 | **yes** |
| `05d25f94` | `adhoc` (1 step) | 1 | 231 | **yes** |

`max_turns=200` is hardcoded at `orchestrator.py:457`, `subagent.py:746`
(`spawn_adhoc`), `subagent.py:587` (`run_phase_blocking` — **no production
callers**), and `subagent.py:351`. `bobi/brain/claude.py:278-292` already detects
`max_turns_reached` correctly; the orchestrator drain
(`orchestrator.py:984-985`) throws the diagnosis away and substitutes the literal
string `"turn failed"`. The deliberate small caps (`CHECK_MAX_TURNS = 8` at
`:1564`, `GATE_MAX_TURNS = 2` at `:1847`, `CURATOR_MAX_TURNS = 10` at `:1992`)
must keep failing fast and are out of scope.

**4. A worker cannot wait without paying turns.** In `fcc79fc7`, **79 of 201**
Bash calls were `tail -1 /tmp/final-suite.log` in one contiguous idle block while
5 background finders and a suite ran. Every blocking primitive was refused:
`sleep 240` was silently backgrounded, `sleep 540` hard-blocked, an `until` loop
backgrounded, and `Monitor` replied *"Keep working — do not poll or sleep"* when
there was no work left. ~40% of that budget bought nothing. `subagents launch
--wait` does block on the agent (#753) but **only for `adhoc`** (`cli.py:2931`).

**5. Retroactive verification is expensive and self-inflicted.** `fcc79fc7`
inherited 1,293 insertions of uncommitted work from a dead predecessor and spent
turns 34–47 and 106–120 — **~25 turns, >12% of its budget** — proving
failing-test-first by reverting source in scratch worktrees (`git worktree add`,
`git checkout HEAD -- .`, `git stash -u`). None of that is required by TDD, which
is forward-only: the `build` skill's Stage 2 says *"write the failing test that
reproduces the bug FIRST, then fix."* The cost came from having no durable record
of what the predecessor had proven, so it re-derived instead of reading.

**6. Ad-hoc work keeps no durable record of what it learned.** `spawn_adhoc`
(`bobi/subagent.py:663`) takes a freeform prompt and holds everything in session
context; when the session dies or rotates on `context_cap`, research and
decisions die with it. `fcc79fc7` spent its first ~30 turns re-orienting.

**7. `bobi setup` generates workflows.** `bobi/setup/authoring.py:420` emits
`{"name": f"{name}-approval", "await": "approval", ...}` for human-in-the-loop
automations, and `:309`/`:394-438` emit `steps:`. Deleting `await`/`steps` without
touching the authoring path breaks new-team creation and
`tests/test_setup_authoring.py` (936 lines).

**8. Human yield is a missing wire, not a wrong architecture.** `await:` is
already durable — `orchestrator.py:721-745` marks the registry `waiting`,
persists the run, emits `agent/workflow.suspended`, disconnects and returns. No
process waits. The real defect is `try_resume_for_event` having **no production
caller** (`orchestrator.py:76-83`), so the only live resume is the operator CLI
(`cli.py:2250`). `StepDef.timeout` is parsed and never read, so await timeouts are
dead config.

## Solution

**One artifact: the plan file.** Human-authored beforehand, or authored by the
agent as its first step when none exists. It carries the design (human-readable,
reviewable), the checklist, the proof of each item, and the accumulated research
and decisions. There is no sidecar, no second state file, and no separate journal.

**The loop is prompt text, not a program.** The worker is told: read the plan;
re-verify the last completed item from its recorded proof; take the next unchecked
item; do it; run its `verify:`; check it off with proof; commit; repeat until
everything is checked or an item blocks. Markers update live and each transition
is committed — squash-merge means `main` never sees the churn, and the branch
lineage is a free proof-of-work trace on the PR.

**Recovery is a monitor plus the director, not a driver.** The one thing prompt
text cannot do is restart itself after its process dies. That does not need new
machinery: a scheduled monitor enumerates in-progress units (unchecked items,
liveness from the existing session registry) and publishes a finding; the
director exercises judgement on it. This is the framework's existing detect→publish
path. The director is demonstrably good at exactly this call — on 2026-07-26 it
declined to dispatch onto PR #847 because *"the owning worker is still live…
intermediate red CI on a draft bot PR is the live owner's to self-heal"*, which a
mechanical no-progress rule would have gotten wrong. `stall-recovery.yaml` is
deleted rather than ported.

**Proof is a commit range, never a re-derivation.** An item's proof is
machine-resolvable — `proof: <test-sha>..<fix-sha>` — so failing-test-first is
provable read-only from history (`git log`), not by reverting source in a scratch
tree. This is load-bearing: it removes Problem 5's entire cost class, it gives
resuming workers something to trust without touching the tree, and it makes proof
un-fakeable (a commit ordering either resolves or it does not). **The `build`
skill's rendering must never emit an item that re-derives what history can
prove.**

**Two surfaces, one file.** The review surface (Purpose → Notes) is
human-authored and machine-read-only except for the marker character inside an
existing `- [ ]`. A fenced appendix at the end of the file carries rendered
lifecycle items and the accumulated round log. A reviewer reads the design
top-down and stops at the fence. The invariant is checkable: a mutation leaves
every byte above the fence unchanged apart from markers.

**Gate lines are classified, so "done" is never vacuous.** Every gate line either
carries a `verify:` or is explicitly tagged `judgement:`. Most existing gate lines
already qualify as-is (`pytest …`, `grep …`), and the failing-first ones become
commit ranges. This closes the hole where "no falsely-completed items" would be
trivially true because nothing was checkable, without rewriting approved plan text.

**Security rules that survive however thin the machinery gets.** `verify:` is a
shell string in a file that agents write and that can arrive from a **public**
repo — `agents/eng-team/agent.yaml:97-101` auto-dispatches `pr-feedback` on any
account's `changes_requested` review, and workers run with
`permission_mode="bypassPermissions"` (`bobi/brain/claude.py:499,549`). So:
`verify:` executes only when its provenance is trusted, defined mechanically (the
file is at a commit on a protected branch, or was written by this agent under
`state_dir()`); untrusted provenance means the item is **refused, not run**; and
the existing pattern for LLM-written shell is reused rather than reinvented
(`bobi/monitors/script_cache_checks.py`'s `validate_script` binary
allowlist/denylist and `run_sandboxed`). Blocked items clear only through a human
act, never through an inbound event — there is no sender allowlist anywhere in
`bobi/`, and event authorization proves resource access, not personhood. And the
artifact is **never an authorization source**: landing authorization is read from
GitHub, never from a checklist item.

**Alternatives considered.** (1) *Patch the engine with `success_when`* (#846) —
forces engineering vocabulary into `bobi/` and keeps two control-flow languages.
(2) *Raise `max_turns` to ~1M* — discards the cheap runaway-loop tripwire while
leaving work non-resumable. (3) *A driver process re-dispatching short workers* —
what earlier drafts of this plan proposed; loses to prompt text plus the existing
monitor scheduler, and every module it needed (lease, run record, tripwire,
budget) was machinery for a job the framework already does. (4) *Keep the engine
for multi-step packs, checklists for ad-hoc* — two execution models is the current
problem wearing a compromise. (5) **Make the ad-hoc path multi-prompt** — the
correct minimal fix for Problem 3 *alone*, and it is Phase 1 of this plan; it
leaves Problems 1, 2, 5, 6, 8 standing.

**Not in scope:** a harness-side blocking join for `Agent`-tool fan-out (Problem
4's second half is an upstream affordance, not a `bobi/` change); a sender-identity
model for event-driven resume (its own initiative, and a prerequisite to ever
relaxing the human-act rule).

## Relevant files

### Existing (verified 2026-07-26 against `29a382b`)

- `bobi/workflow/orchestrator.py` (1213) — step loop; `_validate_handoff:1211`,
  `_drain_response:961` (literal `"turn failed"` `:984-985`, `"unknown error"`
  `:933`), `max_turns:457`, per-step `client.query:851`, await suspend `:721-745`,
  undeliverable-notify guard `:695-716`, agent-change fresh session `:767-786`,
  `_setup_worktree:131`, `_execute_notify_step:1110`, `_execute_native_action:1097`,
  `try_resume_for_event:66` (no production caller).
- `bobi/workflow/schema.py` (186) — `StepDef` incl. `agent`/`model`/`effort`
  (`:34-35`), `HandoffContract`, `DEFAULT_ROUTE_LOOP_MAX_ITERATIONS = 3`
  auto-applied to every back-edge (`:178-186`); `timeout` never read.
- `bobi/workflow/{state.py:157, variables.py:201, triggers.py:80, cleanup.py:168}`
  — `WorkflowRun.claim():76` has the double-`os.replace` crash window (`:95-97`)
  that wedges a run unresumable, already ticketed as **Q062/D071** in
  `plans/2026-07-22-review-remediation.md:112`. **Keep `triggers.py`** (event→dispatch
  routing is orthogonal to control flow).
- `bobi/subagent.py` — `spawn_adhoc:663` (`run_key = sha256(task)[:8]` `:694`,
  `max_turns:746`), `run_phase_blocking:530` (`:587`, no production callers),
  `launch_agent:954`, admission check raising on a live entry `:1036-1042`,
  `_check_spend_governor:930-950`, `_emit_lifecycle_event:166-204`,
  `break` on `max_turns_reached` `:1823`, small caps `:1564,:1847,:1992`.
- `bobi/session.py:1171,1195` — per-inbox-message `client.query()`; `:1404`
  `load_resumable_session_id` (a re-dispatch reusing a session name resumes the
  dead transcript — the monitor's dispatch must not).
- `bobi/sdk.py` — `SessionEntry:230-258` (`cwd`, `run_key`, `status`, `pid`; **no
  `branch` field**), `SessionRegistry:282`, `get_registry:500`, and
  `list_active()` (used at `bobi/launch_admission.py:207`): the liveness signal
  the in-progress monitor reads instead of inventing a lease. Note
  `bobi/registry.py` is the *pack* registry, unrelated.
- `bobi/monitors/scheduler.py` (1472) — `_spawn_monitor_agent:256`,
  `_load_framework_checks:84`; `start():617` is a `daemon=True` thread inside the
  manager (`bobi/service.py:613-617`).
- `bobi/monitors/script_cache_checks.py` (1139) — `validate_script`,
  `run_sandboxed`, `CapabilityEnvelope`: the existing pattern for LLM-written
  shell, reused for both the monitor's check and the `verify:` gate.
- `bobi/cli.py` — `subagents launch:2799` (`--workflow` `required=True`, second
  guard in `_dispatch_agent:2853-2855`), `_run_agent_wait:2927` (adhoc-only
  `:2931`), `workflows` CLI group `:2201-2327` (registered `:3409`,`:3419`).
- `bobi/spend_governor.py` — **not a cost ceiling**: `DEFAULT_CAP = 50` agent
  *invocations* per rolling hour per deployment, shared across every launch path.
- `bobi/setup/authoring.py:309,383,394-438`; `bobi/validate.py:293`;
  `bobi/webapp/runtime.py:275` (folds run state on `phase`).
- `bobi/brain/claude.py:278-292` (detects `max_turns_reached`), `:499,549`
  (`bypassPermissions`); `bobi/brain/base.py:71-86` (`TurnResult` carries
  `error_kind`/`error_message`/`num_turns`/`duration_ms`).
- `agents/eng-team/agent.yaml:97-101` — `pr-feedback` auto-dispatch on any
  account's `changes_requested`; `agents/*/workflows/*.yaml` (14);
  `agents/*/agent.yaml` + `agents/registry.yaml` (pack versions);
  `agents/eng-team/monitors/defaults.yaml`; `agents/eng-team/roles/engineer/ROLE.md`.
- `docs/{WORKFLOW_ENGINE.md:344, SECURITY.md, MONITORS.md}`;
  `.github/workflows/ci.yml:26-66` (skips heavy jobs on `plans/`-only changes — a
  guard job must not live behind that gate).
- Tests broken by the cutover: `test_orchestrator.py` 1934,
  `test_setup_authoring.py` 936, `test_validate.py` 717, `test_notify_step.py` 569,
  `test_cleanup.py` 574, `test_variables.py` 390, `test_setup_digestion.py` 305,
  `test_workflow_state.py` 273, `integration/test_workflow_orchestrator.py` 271,
  `test_triggers.py` 235, `integration/test_effort_selection.py` 214,
  `integration/test_cross_model_resume.py` 201, `test_cli.py:221`,
  `test_dogfood_content_review_pack.py:43`, `workflow_utils.py` 19 — **~6,900 lines**.
- `plans/2026-07-22-review-remediation.md` (9 phases, 251 items) +
  `-findings.md` — the live plan this collides with (Q1), and the multi-file-spec case.

### New

- `bobi/checklist/artifact.py` — parse the plan file (items, markers, `verify:`,
  `judgement:`, `proof:`, the appendix fence); project marker updates back;
  enforce the review-surface freeze. The whole framework module.
- `bobi/checklist/verify.py` — provenance gate + sandbox for `verify:`.
- `bobi/templates/checklist-worker.md` — the worker loop prompt (override path and
  precedence specified; `bobi/templates/` has no override mechanism today).
- A framework-default `in-progress-work` monitor definition + its check script.
- `docs/CHECKLIST_EXECUTION.md`.
- `tests/fixtures/plan-snapshot.md` — frozen fixture (the live plan is mutating
  under lanes A/B/E).
- `tests/test_checklist_*.py`, `tests/integration/test_checklist_loop.py`,
  `tests/e2e/test_checklist_worker.py`.
- (`moda-skills`) a new `build` rendering stage.

## Questionables

- **Q1 — Sequencing against the live `2026-07-22-review-remediation` plan.** That
  plan is Approved with lanes A–E open (#818–822). Its **Phase 2** is "Workflow
  engine + agent-pack routing correctness" — 12 open items in exactly the
  `orchestrator.py`/`variables.py`/`pr-closed.yaml` code this plan's cutover
  deletes, including extending the condition parser with `>` operators.
  Separately its **D092** (hoist five copies of the atomic-write pattern into one
  `fsutil` helper) and **Q062/D071** (the `claim()` crash window) are shared
  infrastructure. Options: (a) block on that plan's Phases 2–3 landing;
  (b) Amend it to mark Phase 2 superseded, keeping only the `fsutil` and lease
  work; (c) run both and reconcile at merge. Recommendation: **(b)** — fixing
  engine internals scheduled for deletion is waste, but the shared helpers stay
  and this plan adopts rather than forks them.
  **Decision (2026-07-26, Zach):** (b) — descope its Phase 2 as superseded, keep
  D092 and Q062/D071 as shared infrastructure this plan consumes.
- **Q2 — Does the accumulated context survive the merge?** The plan file
  accumulates research, decisions, and dead ends as *"a reusable artifact"*. Two
  readings, and they pull opposite ways: if the value is reuse *within* a run
  (cheap re-dispatch), the appendix should be pruned to a summary at closeout so
  `main` stays clean; if the value outlives the work, it should survive the merge
  in full. Squash-merge means only the file's final state lands on `main`, so this
  is purely a choice about what the closeout step does. Options: (a) prune to a
  summary at closeout, full trace stays on the PR; (b) keep it in full on `main`;
  (c) keep in full, but move it to a companion file at closeout so the plan proper
  stays readable. Recommendation: **(a)** — the trace is already durable on the PR
  and in branch lineage, and an unpruned appendix grows without bound in a file
  whose front half must stay reviewable.
  **Decision (2026-07-26, Zach):** (a) — the closeout step prunes the appendix's
  round log to a short summary before the PR merges. The full trace stays on the
  PR and in branch lineage; `main` carries the summary. "Reusable artifact" means
  reusable across worker lives *within* a run.
- **Q3 — What is the unit of ownership for the in-progress monitor?** The monitor
  must not dispatch a second agent onto a unit whose agent is alive. The session
  registry knows liveness per *session*, but the mapping from "plan artifact" to
  "the session working it" is not recorded anywhere today. Options: (a) derive it
  from what the registry already carries, adding no new state; (b) record the
  owning session name in the artifact's appendix; (c) a real lease file.
  Recommendation: **(a)**.
  **Decision (2026-07-26, Zach):** (a), with the mechanism corrected after
  verification. `SessionEntry` (`bobi/sdk.py:230-258`) carries `cwd`, `run_key`,
  `status`, and `pid` but **no `branch` field**, so ownership is resolved by
  **`cwd` containment**: an artifact is owned when a live entry's `cwd` contains
  it — which holds because `build` Stage 1 works inside
  `worktrees/<stem>-<slug>/` and the artifact lives in that worktree.
  `run_key` carrying the plan stem is the **cross-repo fallback**, where the
  artifact is not under the worker's `cwd` and containment cannot resolve.
  Two constraints ride this decision: the monitor reads liveness via
  `get_registry().list_active()` (`bobi/sdk.py:500`), and the dispatch path must
  **vary the session name deliberately** — `session.py:1404`
  (`load_resumable_session_id`) resumes a dead transcript if a re-dispatch reuses
  a name, which would silently defeat the fresh-budget property.

## Phases

Phase 1 is independently valuable and should land regardless of the rest.
Phase 2 depends on Q1's `fsutil` helper for its atomic writes.

### Phase 1 — Make a turn-cap hit survivable, and stop lying about errors

- [x] Surface the real terminal error instead of the literal `"turn failed"`:
      `orchestrator.py:984-985`, `:933`, `bobi/subagent.py:275,489` read
      `error_message`/`error_kind`/`api_error_status`. `bobi/brain/claude.py:278-292`
      already produces the diagnosis; only the consumers discard it.
      *One shared composition (`bobi.brain.turn_error_text`) rather than four
      call sites; both `unknown error` fallbacks now name the gap that lost the
      cause; `_named_exception` covers the bare-`raise` case (empty `str(e)`).*
- [x] Widen the `stop` log record (`orchestrator.py:982`) with `is_error`,
      `error_kind`, `error_message`, `num_turns`, `duration_ms`.
      *Also `api_error_status`.*
- [x] `max_turns` configurable per role/launch, replacing the hardcoded literals
      at `orchestrator.py:457`, `subagent.py:746`, `:351` (and `:587`, or delete it
      as dead).
      *Chain: step > `roles.<role>.max_turns` > `brain.max_turns` >
      `DEFAULT_MAX_TURNS` (1000). Two deviations, both deliberate — see the
      2026-07-26 (Lane A) amendment: there is **no launch flag**, and `:587`
      (`run_phase_blocking`) was kept rather than deleted.*
- [x] **A cap hit auto-continues** within the wall-clock and spend budget instead
      of terminating: re-query and keep going. Scoped to the long-job caps only —
      `CHECK_MAX_TURNS`, `GATE_MAX_TURNS`, `CURATOR_MAX_TURNS` keep failing fast,
      and `subagent.py:1823`'s `break` on `max_turns_reached` stays for those.
      *Bounded by `MAX_TURN_BUDGET_RESUMES` (3) and `step.timeout`. **No spend
      bound**, because there is no cost budget in the framework to bind to —
      `spend_governor` counts agent *invocations* per hour and a resume is not a
      new invocation. Recorded in the amendment rather than left implied.*
- [x] Document the fan-out-and-block pattern (background `subagents launch --wait`
      joined in one Bash call) in the engineer role prompt, and widen `--wait`
      beyond `adhoc` (`cli.py:2931`) or document the limit.
      *Took the "document the limit" branch, in four places (flag help, runtime
      error, `skills/bobi.md`, role prompt). Rationale in the amendment.*

**Validation gate** — do not exit this phase until every line passes.

- [x] Failing-first: a session killed by the turn cap surfaces
      `max_turns_reached (max=…, turns=…)` in `state.json` and the lifecycle event
- [x] Failing-first: a configured non-default `max_turns` is honored at every
      former literal site
- [x] Failing-first: a long-job cap hit auto-continues and completes; a
      `CHECK_MAX_TURNS` hit still fails fast
- [x] Failing-first: the `unknown error` shape (`subagent.py:275`,
      `orchestrator.py:933`) no longer masks a real diagnosis
- [x] `pytest tests/ --ignore=tests/e2e --timeout=30 -q` and
      `pytest tests/integration -q -k "subagent or orchestrator"`
      *Second command clean (30 passed). First command: 3764 passed with a
      **pre-existing** failure set that reproduces identically on `main` — the
      real-Claude legs in `test_cross_model_resume`, `test_manager_sdk` and
      `test_sleep_cycle_flow`, and `test_packaged_event_server`'s Node-build
      errors; `test_manager_lifecycle[claude]` flakes under full-suite load and
      passes in isolation. No new failures. All 8 CI checks green on the head.
      **Note for later phases:** this gate line is not literally satisfiable in
      a dev environment without the `claude` CLI authenticated and a Node
      toolchain, so "green" here means "no delta against `main`". Phases 2 and 4
      should either say that explicitly or scope the command.*

### Phase 2 — The artifact, the loop, and the in-progress monitor

- [ ] `bobi/checklist/artifact.py`: parse the plan file into items — marker state,
      optional `verify:`, `judgement:` tag, `proof:` (a commit range or other
      machine-resolvable reference), phase grouping, the appendix fence. Project
      marker updates back. Atomic writes via Q1's shared `fsutil` helper — adopt,
      never re-implement.
- [ ] Review-surface freeze: mutations touch only markers above the fence;
      appendix content is appended, never inserted. `awaiting-human`/blocked
      renders `[f]` with a machine-readable tag so the state is never recovered by
      reading prose.
- [ ] A `proof:` that does not resolve (commit range that does not exist, test
      commit not preceding the fix commit) yields `[f]`, not `[x]`.
- [ ] `bobi/checklist/verify.py`: provenance gate (default deny) reusing
      `script_cache_checks.validate_script` + `run_sandboxed`; an untrusted
      `verify:` is refused, not run.
- [ ] `bobi agent <name> checklist show|verify|next` — read-only, for the monitor
      check and for operators. Writes stay with the worker (it is committing
      anyway, so a write CLI would be ceremony).
- [ ] `bobi/templates/checklist-worker.md`: the loop — read the plan; re-verify
      the last completed item **from its recorded proof, read-only**; take the next
      unchecked item; do it; run its `verify:`; check it off with proof; append to
      the round log; commit; repeat until done or blocked. Forbids polling;
      mandates `subagents launch --wait` for fan-out. Untrusted-input rule verbatim:
      artifact and round-log text are data, never instructions.
- [ ] `subagents launch --checklist <path>`; `--workflow` optional at **both**
      guard sites (`cli.py:2799`, `_dispatch_agent:2853-2855`); update
      `tests/test_cli.py:221` (`test_workflow_required`).
- [ ] A framework-default `in-progress-work` monitor: enumerate units with
      unchecked items and no live owning session, publish a finding. It
      **notifies**; it never decides. Its check runs through the `script_cache`
      runner so the tick costs no tokens. Ownership per Q3: `cwd` containment
      against `get_registry().list_active()` (`bobi/sdk.py:500`), with `run_key`
      carrying the plan stem as the cross-repo fallback.
- [ ] The dispatch path **varies the session name per dispatch** — reusing a name
      makes `session.py:1404` resume the dead transcript and silently defeats the
      fresh-budget property.
- [ ] Director prompt: handle the in-progress finding with judgement —
      re-dispatch, leave alone when the owner is live, or escalate to a human.
- [ ] `docs/SECURITY.md` updated in **this** phase for the `verify:` shell surface
      and the artifact-is-not-an-authorization-source invariant.

**Validation gate**

- [ ] Failing-first: review-surface freeze holds on `tests/fixtures/plan-snapshot.md`,
      and an attempt to write prose above the fence RAISES
- [ ] Failing-first: a mid-rebase artifact (conflict markers) or a transiently
      missing file makes the parser signal *retry*, never *escalate*
- [ ] Failing-first: `[f]` without a state tag raises rather than defaulting
- [ ] Failing-first: an unresolvable or mis-ordered `proof:` yields `[f]`
- [ ] Failing-first: a `verify:` from untrusted provenance is refused, not run; a
      denylisted binary is refused
- [ ] Failing-first: an inbound event does **not** clear a blocked item; only a
      human act does
- [ ] Failing-first: the monitor does not report a unit whose owning session is
      live; a re-dispatch does **not** resume the dead session's transcript
      (assert distinct session ids)
- [ ] Integration (stub): a 5-item checklist with the agent SIGKILLed at item 3 is
      carried to all-checked after one re-dispatch, losing only item 3's partial
      work
- [ ] **Real-Claude e2e, `[stub]+[claude]`, claude leg required**: a real session
      loops through a 4-item checklist in order, records resolvable proof, does not
      check off an item whose `verify:` fails, and leaves the review surface
      byte-identical apart from markers
- [ ] `pytest tests/ --ignore=tests/e2e --timeout=30 -q`,
      `pytest tests/integration -q -k checklist`, `pytest tests/e2e -q -k checklist`

### Phase 3 — `build`-skill rendering (moda-skills)

- [ ] New `build` stage: render Stage 1–7 (worktree, implement, test, verify,
      document, adversarial review + fix, PR) into the plan's fenced appendix as
      items with concrete `verify:` lines. This is the lifecycle detail the `plan`
      skill does not emit.
- [ ] **Never emit an item that re-derives what git history can prove.**
      Failing-test-first is a commit-range proof, not a revert-and-rerun task —
      this is what removes Problem 5's ~12%-of-budget cost.
- [ ] Gate-line classification: every gate line gets a `verify:` or an explicit
      `judgement:` tag; the rendering proposes, a human accepts.
- [ ] Ad-hoc path: no plan → author one in the same format from the issue's
      acceptance criteria plus the lifecycle stages, **commit and push the branch**
      so a human can read it, and pause for input if the scope is ambiguous. One
      code path with the plan-born case, not two.
- [ ] Pre-planned path: append only; never rewrite approved plan text.
- [ ] Multi-file specs: record a `spec:` companion reference (the fixture's spec
      spans a second 1,500-line file) the worker reads selectively.
- [ ] Closeout step per Q2: prune the appendix's round log to a short summary
      before the PR merges, so `main` carries the summary while the full trace
      stays on the PR and in branch lineage.
- [ ] Bump the moda-skills pack version + `plugin.json`; update `guide` routing.

**Validation gate**

- [ ] Rendering `tests/fixtures/plan-snapshot.md` produces an artifact
      `checklist verify` accepts, every original gate line preserved and classified
- [ ] Rendering a real planless issue produces a valid artifact with lifecycle
      stages and acceptance criteria
- [ ] **The review surface is byte-identical after rendering** (`git diff`
      confined to the appendix), and a human confirms the plan still reads
      top-down as a design document — `[f]` if it got harder to review
- [ ] No rendered item asks a worker to revert source to prove a test
- [ ] The rendering runs against a **released** bobi carrying Phases 1–2 (name the
      release and the pin move)

### Phase 4 — Trial with a real baseline, then cut over

- [ ] Gate the `build` rendering behind a flag so **both** paths are live —
      without it Phase 3 rewrites the engine's only consumer and nothing drives the
      engine during the trial.
- [ ] Publish the **engine baseline first**: run one lane the existing way, record
      turns, wall-clock, spend, human interventions.
- [ ] Run a comparable lane on the checklist model, with an **induced** worker
      death (SIGKILL mid-item) — the "survived a death" evidence is otherwise
      unobtainable. Run one ad-hoc unit through the authoring path.
- [ ] **Binary stop criteria, written before the trial:** halt and amend if spend
      exceeds baseline by >2×, human interventions exceed baseline, any item is
      falsely checked off, or the review surface is ever violated.
- [ ] Then cut over: per-step-type disposition table (route / await / notify /
      action / agent / model / effort → replacement or explicit keep) before
      deleting anything.
- [ ] Migrate **all 14** workflows. `stall-recovery.yaml` is **deleted**, not
      ported (the monitor + director replace it). Named explicitly:
      `issue-lifecycle` (11), `content-lifecycle` (7), `pr-closed` (4),
      `dogfood-content-review` (5), `research-task` (2), `daily-briefing` (2),
      `request` (2), and the 8 single-step files.
- [ ] `pr-closed.yaml`'s deterministic pieces become items naming commands
      (`verify: gh pr view <n> --json merged -q .merged`, a worktree-cleanup
      command) — determinism comes from the item text, not an LLM judgement.
- [ ] `bobi/setup/authoring.py` emits checklists instead of `steps:`/`await:`;
      update `tests/test_setup_authoring.py` (936) and `DESIGN.md` if the setup UI's
      automation step changes.
- [ ] Delete the step loop, `HandoffContract`, handoff validation, back-edge
      validation, route/await conditions. **Keep `triggers.py`**; re-verify `${{}}`
      interpolation consumers before deleting `variables.py`.
- [ ] Retire or re-point the `workflows` CLI group (`cli.py:2201-2327`,
      `:3409`, `:3419`).
- [ ] Bump every touched pack version **and** `agents/registry.yaml`; update
      `agents/eng-team/roles/engineer/ROLE.md`.
- [ ] Port or delete ~6,900 lines of tests with per-file disposition.
- [ ] `docs/CHECKLIST_EXECUTION.md` replaces `docs/WORKFLOW_ENGINE.md`; update
      `OVERVIEW.md`, `QUICKSTART.md`, `BUILDING_AGENT_TEAMS.md`, `EVENT_SERVER.md`,
      `MONITORS.md`, `README.md`, `skills/bobi.md`, `skills/create-agent.md`,
      `skills/linear-setup.md`.
- [ ] Close #845/#846 and PRs #847/#848 with dated pointers (see Notes).

**Validation gate**

- [ ] Baseline numbers published before the checklist arm runs
- [ ] The checklist lane reaches a PR with a SHA-stamped LANDABLE verdict, with the
      artifact showing ≥1 survived death and **zero operator edits**
- [ ] Every checked item with a `verify:` re-passes on re-run; every
      `judgement:`-tagged item is explicitly tagged, not merely unverified
- [ ] The comparison table is written into Notes against the stop criteria
- [ ] `grep -rn "StepDef\|HandoffContract\|evaluate_condition\|await_event" bobi/ tests/`
      **and** `grep -rnE "^\s+(await|handoff|notify|action|goto|if):" agents/`
      return only deliberately-kept survivors, each named in the PR
- [ ] `grep -rn "handoff:" docs/ skills/ README.md agents/*/roles/*/ROLE.md` clean
- [ ] `pytest tests/ -q` (full suite incl. integration) green
- [ ] `bobi validate` passes on all three `agents/` packs and on `moda-eng-team`
- [ ] A fresh `bobi setup` run produces a working team with a human-approval step
- [ ] Real-Claude e2e green on the migrated `issue-lifecycle` equivalent

## Proof of work

- **Bugs get a failing test first.** Phase 1's reporting and auto-continue
  changes, the review-surface freeze, the provenance gate, and the proof-resolution
  rule each land with a test that fails against current `main`.
- **Suites:** unit every phase; `pytest tests/integration -q` from Phase 2;
  `pytest tests/ -q` at Phase 4.
- **Real-Claude e2e required in Phase 2 and Phase 4.** Per CLAUDE.md's judgement
  call: the parser, the monitor check, and error surfacing are brain-agnostic and
  the stub proves them — but "does a real model loop faithfully, refuse to check
  off an unverified item, and record resolvable proof" is exactly where the risk
  lives.
- **Security properties are tests, not prose:** untrusted `verify:` refused; an
  inbound event never clears a blocked item; round-log text never becomes a
  command or a status; the artifact is never an authorization source.
- **Phase 4 is acceptance evidence with a baseline and a kill switch**, not a demo.
- **Migration completeness is grep-gated on both the Python and YAML surfaces**,
  because identifier greps alone miss every pack residue.

## Lane map

{Filled by Split. Cross-repo: Phases 1, 2, 4 → `moda-labs/bobi-agent`; Phase 3 →
`moda-labs/moda-skills`. Cross-repo lanes are always marker mode `concurrent`.
Phase 1 is independently landable and should go first. Phase 3 depends on Phases
1–2 shipping in a cut bobi release with the pin moved; Phase 4 depends on Phase 3.
The bobi-agent phases are otherwise sequential, so same-repo parallelism is not
warranted absent a recorded wall-clock justification. **All lanes after Phase 1
depend on Q1's resolution being executed** (the review-remediation Amendment).}

| Lane | Dispatch issue | Phases | One-line scope | Marker mode | Status |
|---|---|---|---|---|---|
| A | — (plan is the spec) | 1 | Honest turn/error reporting + a resumable turn cap; `max_turns` configurable | solo | in review (PR #847) |
| B | — (plan is the spec) | 2, 4 | Artifact parser, `verify:` provenance gate, worker loop prompt, in-progress monitor; then trial + cutover | solo | open |
| C | — (moda-skills) | 3 | `build`-skill lifecycle rendering into the plan appendix | concurrent | open |

**Lanes:** STACKED, three lanes, no fuse (no same-repo concurrency).

- **Lane A** (bobi-agent, Phases 1) — cut as its own lane against Split's
  one-lane default. **Same-repo justification:** Phase 1 fixes a *live* production
  defect — the masked-error shape is logging `unknown error` every ~15 min from
  monitor `check-c561144f` and cost a full day of debugging on 2026-07-26 — and it
  is independently valuable whether or not the rest of this initiative proceeds.
  Holding it behind Phases 2–4 would leave a diagnosable outage undiagnosable for
  the length of the initiative. It also subsumes PR #847's reporting half.
- **Lane B** (bobi-agent, Phases 2 + 4) — **depends on A** (build-blocking: Phase 2
  treats a cap hit as re-dispatchable, which A makes possible) and on
  `2026-07-22-review-remediation` **Phase 3** (D092's `fsutil` helper, which
  Phase 2 adopts rather than forks; and D029, which Phase 2's ownership check
  needs). Phases 2 and 4 stay one lane: 4 is the trial + cutover for what 2 builds,
  and splitting them would ship a checklist runner nothing uses.
- **Lane C** (moda-skills, Phase 3) — **lands after B** (merge ordering, not
  build-blocking: it renders into the artifact format B defines, so it can be
  authored in parallel once that format is fixed, but its gate needs a released
  bobi carrying A+B with the pin moved). Cross-repo, so always `concurrent`.

Dispatch issues are omitted deliberately: all three lanes can read this plan, so
per `build`'s rule the plan is the spec and no issue is required. File one only if
a lane turns out to need an inlined context slice.

- [ ] Convergence gate: a real unit runs end-to-end through the rendered checklist
      on a released bobi + released moda-skills pack, with the artifact showing a
      survived death and zero review-surface violations (fuse-runnable on a merged
      preview for the code half; the pack-release half is deferred)

## Amendments

- **2026-07-26** (plan/checklist-execution-model): created.
- **2026-07-26** (plan/checklist-execution-model): revised after a 3-lens
  adversarial review (implementer / staff engineer / red team). Corrected
  load-bearing claims that were **false** in the first draft: `await:` is already a
  durable disk-persisted suspend (the real defect is `try_resume_for_event` having
  no caller); `spend_governor` caps invocations per hour, not cost;
  `MonitorScheduler` is a daemon thread inside the manager; `verify:` does not
  exist in today's plans; the fixture has 9 phases not 6; 8 workflows are
  single-step not 7; the test surface is ~6,900 lines not 2,868;
  `run_phase_blocking` has no production callers.
- **2026-07-26** (plan/checklist-execution-model): **design simplified twice on
  Zach's direction.** (i) Run state stays in the plan file — no typed sidecar and
  no separate journal; the earlier "the markdown is unsafe" argument was
  overreach, traced to a session doing *retroactive* verification of a dead
  predecessor's uncommitted work rather than forward-only TDD. Proof became a
  commit range so nothing is ever re-derived by mutating the tree, which removes
  that cost class. Markers commit per transition (squash-merge keeps `main`
  clean and the branch lineage is a free proof-of-work trace). (ii) **The driver
  is deleted.** The loop is prompt text; recovery is a scheduled in-progress-work
  monitor that notifies the director, who exercises judgement; `stall-recovery.yaml`
  is deleted rather than ported. This removed `driver.py`, the typed run record,
  the lease module, the native-action executor, and the budget layer — the
  framework already provides every one of those jobs. Phases went 7 → 5 → 4.
- **2026-07-26** (plan/checklist-execution-model): Q2 and Q3 decided; **Status →
  Approved** (no open Questionables, claims verified, every phase gated, proof of
  work concrete). Q3's mechanism was corrected during verification: the earlier
  recommendation said "derive ownership from the branch", but `SessionEntry`
  (`bobi/sdk.py:230-258`) has no `branch` field — ownership resolves by `cwd`
  containment, with `run_key` as the cross-repo fallback. The Relevant-files entry
  citing `bobi/registry.py` as the liveness signal was also wrong (that is the
  *pack* registry); corrected to `bobi/sdk.py`.
- **2026-07-26** (Lane A): **Phase 1 complete**, all five task items and all
  five gate lines verified. Landed by taking over PR #847, whose reporting half
  the plan already identified as Phase 1's substance. Four deviations from the
  phase text, each deliberate and none silent:
  1. **No launch flag for `max_turns`.** The chain is step > role > team >
     framework default, with no `subagents launch --max-turns`. The cap is a
     runaway-loop backstop an operator configures, not a per-invocation dial
     like `--model`/`--effort`, and adding one would invite exactly the
     "raise it until the job fits" habit the resume behavior removes the need
     for.
  2. **No spend bound on the resume chain.** The phase text says "within the
     wall-clock and spend budget"; the implementation bounds by
     `MAX_TURN_BUDGET_RESUMES` (3) and `step.timeout` only. There is no cost
     budget to bind to: `spend_governor` caps agent *invocations* per rolling
     hour (as this plan's own Relevant-files entry notes) and a turn-cap resume
     is not a new invocation. The phrase was loose, not unmet. Worst case per
     prompt step is `max_turns × (MAX_TURN_BUDGET_RESUMES + 1)` turns, because
     `step.timeout` gates whether a resume *starts* and nothing enforces it
     against a running drain — a real bound worth knowing, documented in
     `docs/WORKFLOW_ENGINE.md`.
  3. **`--wait` limit documented rather than widened.** `--wait` blocks by
     running one prompt through `spawn_adhoc`; a workflow goes through the
     orchestrator and returns once dispatched, so there is no in-process handle
     to join. Widening it means running the orchestrator synchronously inside a
     CLI invocation — disproportionate to Phase 1, and unnecessary because
     fan-out units are adhoc-shaped anyway.
  4. **`run_phase_blocking` (`subagent.py:587`) kept, not deleted.** The phase
     text allowed either. It has no production callers but substantial test
     coverage; deleting it is a separable cleanup, not turn-budget work.
  **A finding for Lanes B and C, from doing this:** the plan's
  `proof: <test-sha>..<fix-sha>` model does **not survive a rebase** — this
  lane's ranges rotated when the branch was rebased onto the plan merge, so a
  proof recorded in a commit message went stale while remaining true in
  ordering. Phase 2's `proof:` resolution should verify *ordering* against the
  current branch, not pin absolute SHAs, or the parser will report `[f]` on
  every rebased artifact.
- **2026-07-26** (Split): Lane map filled — three STACKED lanes, no fuse. **A**
  (Phase 1, bobi-agent) is cut against Split's one-lane default on a recorded
  same-repo justification: it fixes a live defect (`unknown error` spam every ~15
  min from monitor `check-c561144f`) and is valuable whether or not the rest
  proceeds. **B** (Phases 2+4, bobi-agent) depends on A and on
  `2026-07-22-review-remediation` Phase 3 (D092's `fsutil` helper, D029's registry
  leak). **C** (Phase 3, moda-skills) lands after B. Dispatch issues omitted —
  every lane can read this plan, so the plan is the spec.

## Notes

- **Evidence base.** Session numbers come from the live `moda-eng-team` box on
  2026-07-26: `/data/.bobi/agents/eng-team/run/state/sessions/` for run state and
  `/data/claude/projects/-data--bobi-agents-eng-team-run/*.jsonl` for the raw CLI
  transcripts. `fcc79fc7` (= run `a9135266`) and `05d25f94` (= run `cf501439`) are
  the two turn-cap deaths; the `max_turns_reached` attachment is the ground truth
  the engine discards.
- **The two deaths had different profiles** and both matter: `fcc79fc7` wasted
  ~40% of its budget idle-polling and another ~12% on retroactive revert-testing,
  while `05d25f94` did 231 tool calls of genuine work across 13 subagent launches
  and still did not finish — no turn budget makes a 90-file/7,550-line review gate
  fit in one session. **Splitting that diff remains worthwhile independent of this
  plan.**
- **The director as poke-responder, not driver.** On 2026-07-26 it declined to
  dispatch onto PR #847 because the owning worker was still live and intermediate
  red CI on a draft bot PR is the owner's to self-heal. A mechanical no-progress
  rule would have double-dispatched. That judgement is why recovery is a
  notification, not an algorithm — and it is a narrow, low-frequency job, unlike
  the per-round decisions a driver would have needed.
- **#847 / #848 disposition (decided 2026-07-26, Zach):** #847's *reporting* half
  is the substance of Phase 1 and lands on its own schedule; its `max_turns` raise
  is superseded by Phase 1's auto-continue; #848 (`handoff.success_when`) is closed
  as superseded — it gates a step machine this plan deletes. The same
  masked-error defect is currently logging `unknown error` every ~15 min from
  monitor `check-c561144f`.
  **Executed 2026-07-26:** #848 and #846 both closed as superseded (#846 closed
  ahead of its Phase 4 slot because an open issue with no PR re-dispatches the
  bot onto machinery Phase 4 deletes). #847 was **taken over** rather than
  closed and now carries all of Phase 1; its `max_turns` raise was kept, not
  discarded — the auto-continue makes the raise safe rather than redundant,
  since a low cap would just resume more often. Three defects in
  `evaluate_condition` that #848's spec found are live *today* and outlive it:
  the substring-matching allow-list idiom (owned by #844), a handoff value
  containing `\1` raising `re.error` via `re.sub`'s replacement slot, and a
  stale-value leak from the run-wide flat condition scope. They are recorded on
  #846/#848 for whoever needs them before Phase 4 deletes the surface.
- **Prior art:** #753 (closed) made `subagents launch --wait` block on the launched
  agent and started normalizing `max_turns_reached`.
- **Deferred:** a harness-side blocking join for `Agent` fan-out; a sender-identity
  model for event-driven resume; consolidating the three worktree conventions
  (`orchestrator._setup_worktree:131`, the dead `paths.worktrees_dir:282`,
  CLAUDE.md's policy) — Phase 4 should pick one rather than adding a fourth.
