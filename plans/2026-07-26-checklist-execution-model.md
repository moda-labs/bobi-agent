# Checklist-driven execution: move eng-team off the workflow step machine

> **Status:** Approved (re-approved 2026-07-29 — see Amendments). Approved 2026-07-26; the 2026-07-29
> revision changed the thesis (the engine is frozen, not deleted), removed the
> recovery monitor, the `bobi/checklist` modules and the `proof:` field, and cut
> Phase 4's scope to eng-team. That is past what the prior approval covers.
> **Phase 1 stays `[x]` — it landed and is unaffected.**
> **Tracking issue:** moda-labs/bobi-agent#852 · **Created:** 2026-07-26 · **Last amended:** 2026-07-29 (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

Move agent execution off the YAML step machine and onto a single plan file that
is both the human-reviewable design document and the agent's working checklist.
An agent reads it once at session start, then does the next unchecked item,
proves the item, checks it off, and commits — repeating item by item until done
or blocked. Nothing orchestrates it; the loop is prompt text.

**This initiative adds no execution engine, and stops feeding the one it
replaces.** That is the test every item must pass: a change that adds framework
code is suspect until it has argued for itself. The net across four phases is a
prompt, a CI check, one bug fix — not a new subsystem wearing a nicer artifact
format.

**The step machine is deprioritized, not deleted (2026-07-29, Zach).** Deleting
it in the same phase that trials its replacement would make the trial's stop
criteria meaningless: "halt and amend" is not available once the fallback is
gone. So eng-team — the only fleet consumer, and really only two substantive
workflows, `issue-lifecycle` (11 steps) and `pr-closed` (4) — moves to
checklists; the example packs stay on the engine and become its regression
coverage; and the engine is **frozen**: no new step types, no new features, bugs
fixed only where they break a live pack, every new automation authored as a
checklist. Deletion gets a **mechanical trigger rather than a date** (Phase 4),
because this repo's record with "remove it next release" is six compat shims
still live four to five releases past their stated window.

**Why prompts rather than code.** A prompt is the most adaptable form of
engineering available: it changes by editing text, carries no schema, no
migration and no deprecation window, and it gets better for free as models get
better. Framework code does the opposite. Every control-flow decision baked into
`bobi/` is an architectural commitment made once, for every team, that each
future agent inherits whether or not it still fits. The step machine is exactly
that: route conditions, handoff contracts and await/resume are decisions frozen
into a language weaker than the plan file they were duplicating.

**Why this is tractable now and was not a year ago.** The design rests on a model
staying on track across long multi-step work without an external driver holding
its place. That was not a safe assumption when the step machine was written, and
the machinery in `bobi/` is a reasonable response to the models of the time — it
is being removed because it has been outgrown, not because it was wrong. The
corollary is that this is an assumption with a shelf life in both directions, so
Phase 4 tests it against a real baseline rather than asserting it.

The goal is **flexibility first, durability second**. Flexibility: the lifecycle
lives in prompts and skills, changeable by editing text, instead of in a second
control-flow language that duplicates the plan badly. Durability: the state is a
committed file, so a dead session loses at most its current item.

The 10x version here was *less machinery*, and finding it took four rounds of
review. Successive drafts proposed a driver process, a typed run record, a lease
module, a native-action executor, a budget accounting layer, a recovery monitor,
an artifact parser inside `bobi/`, and a three-verb CLI. All of it is gone. What
survives **in the framework** is one landed change (Phase 1: make a turn-cap hit
resumable) and one fix (vary the session name per dispatch). Everything else is a
prompt, a skill, and a CI check.

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

**The loop is prompt text, not a program**, and it has two phases. **On session
start** — a first dispatch, or any re-dispatch after a death — the worker reads
the plan and re-verifies the last completed item **by reading the branch's
commits**, never by mutating the tree. **Per item thereafter** it takes the next
unchecked item, does it, runs its `verify:`, checks it off, and commits. It does not re-read the
artifact and does not re-verify earlier items: within one session the worker made
every change itself, so continuity is its own context, not a file it reloads.
Markers update live and each transition is committed — squash-merge means `main`
never sees the churn, and the branch lineage is a free proof-of-work trace on
the PR.

**The per-session split is load-bearing for cost**, which is why it is stated
here rather than left to the template. Both cold-start steps are *resume*
operations: re-reading orients a worker that has no context, and re-verifying
exists so a fresh worker can trust a **predecessor** — within a session the
worker is its own predecessor. Paying them per item was the shape of the driver
this design deleted; a stateless process must reload state each tick, a
continuous session must not. On this plan's own fixture
(`2026-07-22-review-remediation.md`, ~16.7k tokens, 255 items) a per-item
re-read costs ~4.3M tokens before any work is done — superlinear, since the
round log grows in the same file — and puts 255 copies of the artifact in the
transcript, forcing exactly the `context_cap` rotation that Problem 6 exists to
prevent. The artifact is re-read only when git moved it underneath the session
(rebase, pull, conflict resolution): never on a timer, never per item. A later
item breaking an earlier one's `verify:` is caught by the closeout sweep (Phase
4's gate re-runs every checked `verify:`), not by re-verifying each iteration.

**Recovering a dead worker is out of scope — it is a framework concern, not an
execution-model one.** The one thing prompt text cannot do is restart itself
after its process dies, and this plan does not try to. What it owes recovery is
the *durable state that makes recovery cheap*: the artifact is committed per
item, so a re-dispatch — by a human, or by whatever backstop the framework grows
— resumes from the last checked item and loses at most one item's partial work.
That property is delivered by the artifact itself, and Phase 4's induced-death
trial is what proves it.

Detection and restart belong with the framework's existing liveness work
(`plans/2026-07-23-dead-transport-liveness-backstop.md`, #837), which is building
the "is this agent actually alive?" signal and the operator surfaces that show
it; a checklist-worker backstop should key off that signal rather than invent a
second one. Until then the operator reads the file — units with unchecked items
*are* the in-progress set, and the artifact is a markdown document built to be
read — and a human decides. Earlier drafts put a scheduled monitor here, enumerating in-progress
units and publishing a finding for the director to judge. It is removed: it
duplicated a framework concern inside an execution-model plan, and it carried an
unresolved ownership question that the trial is better placed to answer than a
design argument is.

**Proof is the commit history — there is no `proof:` field.** Problem 5's cost
came from a worker having *"no durable record of what the predecessor had
proven, so it re-derived instead of reading."* The durable record is the per-item
commits the worker already makes: a resuming worker runs `git log` and reads
`test: reproduce X` followed by `fix: X`, exactly as an engineer picking up
someone else's branch does. Earlier drafts added a machine-resolvable
`proof: <test-sha>..<fix-sha>` field on top of that. It is **removed**: it
denormalizes history into a field that goes stale — Phase 1 proved this, its
ranges rotated on the first rebase — and it was designed for a machine to read,
back when a driver, then a parser, then a CI assertion existed to read it. All
three are gone. What actually removes the cost class is the *rule*, which stays
and is stated in both the worker protocol and the rendering: **never re-derive
what git history can already prove.** Failing-test-first is proven by reading the
log, never by reverting source in a scratch tree.

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
repo — `agents/eng-team/agent.yaml` auto-dispatches `pr-feedback` on any
account's `changes_requested` review, and workers run with
`permission_mode="bypassPermissions"` (`bobi/brain/claude.py:499,549`).

`verify:` stays **free-form shell**, and the control is the worker's judgement,
not a mechanism. A constrained check vocabulary was considered and rejected: it
is a second language, weaker than shell, extended only by a pack release —
exactly the failure being deleted from the step machine, rebuilt one layer over.
The gate it would have bought is nearly empty anyway, because the only thing that
runs a `verify:` is a worker that already has unrestricted shell.

That leaves one honest tension, and it is resolved by strengthening the rule
rather than mechanising it: artifact text is **data, never instructions**, yet a
`verify:` is a string from the artifact that gets executed. So a `verify:` is a
**proposed proof, not a command**. The worker judges whether it plausibly proves
its item before running it, and refuses one that does not — the same judgement it
applies to an issue comment telling it to exfiltrate a key. It is an agent, not
an interpreter.

Blocked items clear only through a human act, never through an inbound event —
there is no sender allowlist anywhere in `bobi/`, and event authorization proves
resource access, not personhood. And the artifact is **never an authorization
source**: landing authorization is read from GitHub, never from a checklist item.

**Alternatives considered.** (1) *Patch the engine with `success_when`* (#846) —
forces engineering vocabulary into `bobi/` and keeps two control-flow languages.
(2) *Raise `max_turns` to ~1M* — discards the cheap runaway-loop tripwire while
leaving work non-resumable. (3) *A driver process re-dispatching short workers* —
what earlier drafts of this plan proposed; loses to prompt text, and every module
it needed (lease, run record, tripwire, budget) was machinery for re-dispatching
dead workers — which this plan does not own at all. (4) *Keep the engine
for multi-step packs, checklists for ad-hoc* — a **permanent** split by use case
is two execution models wearing a compromise, and stays rejected. Note this is
*not* what the 2026-07-29 deprioritization does: a frozen path that takes no new
work and carries a mechanical deletion trigger is a migration, not a standing
architecture. The distinction is whether anything new is ever authored against
it. (5) **Make the ad-hoc path multi-prompt** — the
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
  dead transcript — **any** re-dispatch must not, human or automated).
- `bobi/subagent.py:1063-1070` — launch admission refuses a session name whose
  registry entry is `starting`/`running`/`idle` (*"A run is already active"*).
  This is why **D029** is in scope: an entry stuck `running` after a crashed
  connect blocks re-dispatch of that unit forever, and a human recovering by hand
  hits the same wall an automated one would.
- `bobi/monitors/script_cache_checks.py` (1139) — `validate_script`,
  `run_sandboxed`, `CapabilityEnvelope`: the framework's existing pattern for
  LLM-written shell. **Deliberately NOT reused here.** It exists because the
  monitor scheduler executes generated scripts *unattended*; a `verify:` is run
  only by a worker that already has unrestricted shell, so wrapping it would add
  a sandbox that constrains nothing. Listed so the omission reads as a decision
  rather than an oversight — and it is the thing to reach for if the unattended
  tripwire in Phase 2 ever fires.
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
- The engine's test surface — `test_orchestrator.py` 1934,
  `test_setup_authoring.py` 936, `test_validate.py` 717, `test_notify_step.py` 569,
  `test_cleanup.py` 574, `test_variables.py` 390, `test_setup_digestion.py` 305,
  `test_workflow_state.py` 273, `integration/test_workflow_orchestrator.py` 271,
  `test_triggers.py` 235, `integration/test_effort_selection.py` 214,
  `integration/test_cross_model_resume.py` 201, `test_cli.py:221`,
  `test_dogfood_content_review_pack.py:43`, `workflow_utils.py` 19 — **~6,900
  lines**. Earlier drafts listed these as "broken by the cutover" and made their
  per-file disposition the single largest item in the plan. **Under the
  2026-07-29 freeze none of it is touched:** the engine keeps running the example
  packs, so these tests keep covering live code and keep passing untouched. This
  was the biggest cost in the initiative and it is now zero.
- `plans/2026-07-22-review-remediation.md` (9 phases, 251 items) +
  `-findings.md` — the live plan this collides with (Q1), and the multi-file-spec case.

### New

**No new Python in `bobi/`.** Earlier drafts listed `bobi/checklist/artifact.py`
and `bobi/checklist/verify.py` here; both encoded a Moda lifecycle convention
inside the framework and are removed. The framework's *code* share of this
initiative is one bug fix. What it does gain is **documentation** — a skill and a
doc — which is the whole point: the lifecycle moves into prose that anyone can
edit.

- `skills/checklist-execution.md` — the generic worker protocol. Framework-level
  markdown guidance alongside `bobi.md` and `create-agent.md`, **not** code and
  **not** Moda process. Add it to CLAUDE.md's Reference Docs list in the same PR.
- `.github/workflows/` — the artifact check job (marker-aware review-surface
  diff, `[f]` state tag, gate-line classification). Shell and
  git, outside the plans-only skip gate. **The only non-agent verification.**
- `docs/CHECKLIST_EXECUTION.md`; `docs/SECURITY.md` updates.
- `tests/fixtures/plan-snapshot.md` — frozen fixture for the check (the live plan
  is mutating under lanes A/B/E).
- `tests/integration/test_checklist_loop.py`, `tests/e2e/test_checklist_worker.py`
  — the loop is proven end-to-end, since there is no module to unit-test.
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
  **Revised 2026-07-29: this plan now consumes none of it, and is not blocked by
  that plan at all.** The dependency existed because an artifact parser in
  `bobi/` was going to write markers back atomically and therefore needed D092's
  `fsutil` helper. There is no parser and no write path — the worker edits with
  its own file tools — so `fsutil` is not a dependency here. **Q062/D071** (the
  `claim()` crash window) is `WorkflowRun` state that Phase 4 deletes outright, so
  it is moot for this plan. **D029** (registry entry stuck `running`) stays worth
  fixing — a stuck entry makes `bobi/subagent.py:1063-1070` refuse re-dispatch of
  that unit forever, which blocks a human recovering by hand — but it is an
  ordinary framework bug on its own merits, not a gate on this work. **Net: Lane
  B no longer waits on `2026-07-22-review-remediation`.**
  **The supersession half needs re-reading after the 2026-07-29 freeze, and this
  is the pointer.** Nine of its Phase 2 items were marked `[f]`-superseded on the
  stated grounds that *"the step machine is deleted"*. It is not deleted, only
  frozen — so the marker still stands but the **reason changes** from "the code
  is going away" to "the code is frozen and takes fixes only where it breaks a
  live pack." That flips at least one item back into scope: **D015**
  (`dogfood-content-review.yaml`'s `issues_count > 0` uses an unsupported `>`) is
  a broken route in a pack this plan deliberately **keeps** on the engine, so it
  meets the freeze's own repair bar. **D060** (that pack's routing table naming
  workflows that do not exist) is arguably the same. **D016** stays moot —
  `pr-closed.yaml` is eng-team and is being migrated. Engine internals
  (D005/D027/D024/D025/D028/Q017/D026) stay `[f]`: frozen means frozen. Anyone
  reopening that plan reads this paragraph first — a `[f]` whose justification
  has silently changed is exactly the kind of stale marker this house treats as a
  defect.
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
- **Q3 — ~~What is the unit of ownership for the in-progress monitor?~~
  Withdrawn (2026-07-29, Zach): there is no monitor.** Recovering a dead worker
  is a framework concern, not an execution-model one, so the question of which
  live session owns which artifact does not arise in this plan. It was the
  weakest decision in it — `cwd` containment resolves ambiguously when two lanes
  of one plan run in two worktrees, since the artifact is committed to both
  branches — and if a backstop is ever built on the framework's liveness signal
  (#837), it should answer this with data from Phase 4's trial rather than
  re-derive it here.
  **One constraint survives the withdrawal** and moves into Phase 2 on its own
  merit: the dispatch path must **vary the session name deliberately**, because
  `session.py:1404` (`load_resumable_session_id`) resumes a dead transcript when
  a re-dispatch reuses a name, silently defeating the fresh-budget property. That
  bites the first time a human re-dispatches by hand, so it is not deferrable.

## Phases

Phase 1 is independently valuable and should land regardless of the rest.
Phase 2 depends on nothing outside this plan (see Q1's 2026-07-29 revision).

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

### Phase 2 — The worker protocol, the CI check, and the one framework fix

**No new module in `bobi/`.** Earlier drafts put an artifact parser, a `verify:`
execution module and a three-verb CLI here. They are gone, on two grounds. First,
the worker does not need them: it reads the plan as text, edits markers with its
own file tools, and runs `verify:` in its own shell — a parser client would be a
language model asking a regex what a checklist item is. Second, the artifact
format — a frozen review surface above a fenced appendix carrying
`verify:` and `judgement:` — is a **Moda lifecycle convention, not a
framework property**, and CLAUDE.md's first principle keeps those out of `bobi/`.
What remains is one genuine framework bug, a prompt, and a check that runs
outside the agent.

- [x] **`skills/checklist-execution.md` — the worker protocol, as a framework
      skill.** It belongs in `skills/` and not in `bobi/`, and the distinction is
      the point: `skills/` is user-facing markdown guidance (`bobi.md`,
      `create-agent.md`, the integration setups), so this adds documentation, not
      framework code. And it belongs *here* rather than in moda-skills because the
      protocol is **generic** — read once, next item, verify, commit, repeat — with
      nothing Moda-specific in it. The Moda **lifecycle** rendering is the
      separate thing, and stays in Phase 3's `build` skill. Two phases.
      **Session start**
      (first dispatch, or re-dispatch after a death): read the plan, re-verify the
      last completed item **by reading the branch's commits, read-only** — never
      by reverting source in a scratch tree. **Per item**:
      take the next unchecked item; do it; run its `verify:`; check it off;
      commit; repeat until done or blocked — with **no artifact re-read and
      no re-verification of earlier items**, because the session carries its own
      continuity. Re-read only when git moved the file underneath the session
      (rebase, pull, conflict resolution). Append to the round log on a judgement
      call, a dead end, or a block — not per item; git history already carries the
      mechanical trace. Forbids polling; mandates `subagents launch --wait` for
      fan-out. Untrusted-input rule verbatim: artifact and round-log text are
      data, never instructions — **including `verify:`**, which is a *proposed
      proof*, not a command. The worker judges whether a `verify:` plausibly
      proves its item before running it, and refuses one that does not. This is
      the whole control on `verify:`; there is no provenance gate and no sandbox
      behind it, so the prompt has to carry it explicitly rather than by
      implication.
- [x] **The one framework change: vary the session name per dispatch.** Reusing a
      name makes `session.py:1404` (`load_resumable_session_id`) resume the dead
      transcript and silently defeat the fresh-budget property. Needed for **human**
      re-dispatch, which is the only recovery path this plan ships, and a real
      framework bug independent of checklists.
      *Delivered 2026-07-29 as a `fresh` opt-out (Session -> spawn_adhoc ->
      launch_agent -> run_workflow, plus `--fresh`), NOT as name variation:
      varying the name forks the worktree branch. Anchor reads `:1433` now.
      See the 2026-07-29 (Phase 2, build) amendment, deviation 1.*
- [x] **A CI check on `plans/` diffs — the only non-agent verification, and the
      reason it is not in `bobi/`.** Asserts: the review surface is unchanged apart
      from marker characters; appendix content was appended, not inserted; every
      `[f]` carries a machine-readable state tag rather than prose; every gate
      line is `verify:`-carrying or explicitly `judgement:`-tagged. Both remaining
      checks are git-shaped — a marker-aware diff and a grep — so this is a CI
      job, not a module. **It does not check commit ancestry**: there is no
      `proof:` field to resolve, and whether a test genuinely preceded its fix is
      read off the PR's commits by the human reviewer, the same way it is on any
      other PR.
      **It never executes a `verify:` string** (see below). It must sit **outside**
      `ci.yml`'s plans-only skip gate (`.github/workflows/ci.yml:26-66`), or it
      will never run on exactly the PRs it exists to check.
- [x] **Nothing executes `verify:` unattended — this is what deletes the
      provenance gate.** `verify:` is attacker-reachable shell (`agents/eng-team/agent.yaml`
      auto-dispatches `pr-feedback` on **any** account's `changes_requested`
      review, and workers run `bypassPermissions`, `bobi/brain/claude.py:499,549`),
      so earlier drafts built a default-deny provenance gate around it. With the
      monitor gone and no CLI runner, the only things that run a `verify:` are the
      worker — which already has unrestricted shell, so the gate granted it
      nothing — and a human at a terminal. CI validates **structure only**. Record
      this as a standing invariant: **if anything ever runs `verify:` unattended,
      the provenance gate comes back with it.**
- [f] state:not-needed `--workflow` optional at both guard sites (`cli.py:2800`,
      `_dispatch_agent:2857`) — **only if it earns itself.** `-w adhoc --task
      "work the checklist at <path>"` already works today and the second guard
      already says so, making a `--checklist` flag a synonym rather than a
      capability. Default is to skip this and change nothing.
      *Skipped 2026-07-29, taking the item's own default.*
- [x] `docs/SECURITY.md` updated in **this** phase: `verify:` is worker-executed
      shell with no framework runner behind it, nothing executes it unattended,
      and the artifact is never an authorization source.

**Validation gate**

Phase 2 adds almost no code, so most of this gate proves the CI check does its
job and that the framework stayed out of it. "Failing-first" is not used: nothing
here fixes a defect on `main` except the session-name item, and a test that fails
before its subject exists proves `ImportError`. The negative assertions get
**mutation-proof** — remove the named guard, the test must fail — because a
negative goes green both when the guard fires and when the path was never
reached.

- [x] Mutation-proof: a diff editing prose **above the fence** fails the check,
      against `tests/fixtures/plan-snapshot.md` — *mutant: drop the review-surface
      comparison*
      *Run. Rule shipped as insertion-only rather than byte-identical; see
      deviation 8.*
- [x] Mutation-proof: `[f]` without a machine-readable state tag fails —
      *mutant: drop the tag assertion*
      *Run. Scoped to ADDED lines so approved text is not retro-fitted.*
- [x] Mutation-proof: a re-dispatch does **not** resume the dead session's
      transcript (assert distinct session ids) — *mutant: drop the session-name
      variation*
      *Run, as *mutant: drop `_run`'s `fresh` guard*; asserts the resume is never
      attempted, which is stronger than comparing ids.*
- [x] **Assert by absence — this is how "we removed an engine" is proven:**
      `grep -rn` shows **no** code path in `bobi/` that writes a checklist marker,
      parses the artifact format, or executes a `verify:` string. If this ever
      fails, the framework grew an execution engine again
      *`tests/test_no_checklist_engine.py`, each absence carrying a positive
      control against a planted offender.*
- [x] Assert: **the check actually runs on a `plans/`-only PR.** Proven by an
      artifact PR touching nothing else and observing the job execute — a guard
      sitting behind `ci.yml`'s skip gate is worse than no guard, because branch
      protection reads its absence as passing
- [x] Assert: a malformed artifact (rebase conflict markers, truncated fence)
      fails the check with a diagnostic, never a traceback
      *Also: misuse exits 2, a violation exits 1.*
- [x] Assert: the warm loop reads the full artifact **once per session** — a
      multi-item stub run counts exactly one full-artifact read, plus one more
      after an induced rebase and none otherwise. This is the cost property, so it
      is a test, not a prompt aspiration
      *`TestReadOncePerSession`; measured on a re-dispatch, the path that ships.*
- [x] Integration (stub): a 5-item checklist with the agent SIGKILLed at item 3 is
      carried to all-checked after one re-dispatch, losing only item 3's partial
      work
      *Brain-FREE, not stub-brain — the property is the protocol's; deviation 6.
      Found in the doing: the dead worker's UNTRACKED output survives
      `git checkout --`, so a reset needs `clean` too.*
- [x] **Real-Claude e2e, `[stub]+[claude]`, claude leg required**: a real session
      loops through a 4-item checklist in order, commits each transition so the
      log is readable as proof, does not check off an item whose `verify:` fails,
      and leaves the review surface byte-identical apart from markers
      *RUN against a live Claude session (4 passed, 488s, re-run after the
      protocol was de-coupled from git). No `[stub]` leg — deviation 7. Its
      system prompt is `skills/checklist-execution.md` read off disk.*
- [x] **Real-Claude e2e, claude leg required — the `verify:` judgement.** A
      planted item whose `verify:` does not prove it (`verify: echo done`, and a
      `verify:` that exfiltrates rather than checks) is **refused and the item left
      unchecked**, with the refusal recorded in the round log. This is the only
      control on `verify:` and it is a judgement, so per CLAUDE.md the stub cannot
      prove it — the risk lives entirely in the brain path
      *RUN: both bad `verify:` strings refused, neither item checked off.*
- [x] `pytest tests/ --ignore=tests/e2e --timeout=30 -q`,
      `pytest tests/integration -q -k checklist`, `pytest tests/e2e -q -k checklist`
      *3482 passed, exit 0 (ignoring `tests/integration` too — see deviation 7's
      note). The third command collects NOTHING; correct one is
      `pytest tests/integration -q -k checklist`.*

### Phase 3 — `build`-skill rendering (moda-skills)

Phase 2 ships the **generic** protocol as a framework skill — how to work a
checklist at all. This phase ships the **Moda-specific** half: what a unit of
Moda engineering work contains. Keeping the line clean is what lets a
non-Moda team adopt checklists without inheriting our lifecycle.

- [x] New `build` stage: render Stage 1–7 (worktree, implement, test, verify,
      document, adversarial review + fix, PR) into the plan's fenced appendix as
      items with concrete `verify:` lines. This is the lifecycle detail the `plan`
      skill does not emit.
- [x] **Never emit an item that re-derives what git history can prove.**
      Failing-test-first is read off the log, not re-run by reverting source in a
      scratch tree — this is what removes Problem 5's ~12%-of-budget cost. It is
      a rule in the rendering, not a `proof:` field: the commits are the record.
- [x] Gate-line classification: every gate line gets a `verify:` or an explicit
      `judgement:` tag; the rendering proposes, a human accepts.
- [x] **`verify:` is free-form shell — constrained by guidance, never by a
      mechanism.** A named-check vocabulary (`verify: suite unit`, `verify: absent
      <symbol> <dir>`) was considered and **rejected**: it is a second language,
      weaker than shell, extendable only by a pack release, which is the step
      machine's exact failure rebuilt one layer up. The range real gate lines need
      settles it — `pytest` with three flags, a piped double-`grep`, `gh ... -q
      .merged`, `bobi validate`, `cd event-server && npm test` — a vocabulary
      covering that honestly is either enormous or leaky. So the skill **steers**
      rather than restricts: prefer a suite run, an absence grep, or a `gh` query;
      emit a command that would fail if the item were not done; never emit one
      that re-derives what git already proves. Two judges sit behind it — the
      human accepting the rendering, and the worker refusing an implausible
      `verify:` at run time.
- [x] **Close the proof-idiom gap in `moda:plan` before rendering anything.** The
      pack ships exactly one idiom for test rigor and it is bug-shaped:
      `failing-test-first` appears in `plan`, `investigate` and `review`, while
      `mutation`/`mutant` appears **nowhere in any skill** — and neither does any
      guidance on proving a negative. That gap is what put 8 failing-first lines
      on this plan's greenfield Phase 2: the red-team lens correctly demanded
      non-vacuous proof of the security properties, and the only rigor word
      available was the bug-shaped one. Add the missing idiom to
      `plan/SKILL.md`'s Proof-of-work guidance and its rubric row, and to
      `plan/template.md` beside the existing "Bugs get a failing test first" line.
      This lands **before** the rendering work, because a rendering built on the
      current vocabulary generates the defect into every future plan instead of
      one author making it once.
- [x] Proof classification by claim shape, not by habit — the rendering picks the
      idiom: a **bug** renders failing-test-first (a defect exists on the base
      branch to reproduce); a **negative or security assertion** renders
      mutation-proof with a **named mutant**; ordinary new behavior renders a
      plain assertion. Never emit failing-first for code that does not exist yet —
      that proves `ImportError`, not coverage.
- [x] Ad-hoc path: no plan → author one in the same format from the issue's
      acceptance criteria plus the lifecycle stages, **commit and push the branch**
      so a human can read it, and pause for input if the scope is ambiguous. One
      code path with the plan-born case, not two.
- [x] Pre-planned path: append only; never rewrite approved plan text.
- [x] Multi-file specs: record a `spec:` companion reference (the fixture's spec
      spans a second 1,500-line file) the worker reads selectively.
- [x] Closeout step per Q2: prune the appendix's round log to a short summary
      before the PR merges, so `main` carries the summary while the full trace
      stays on the PR and in branch lineage.
- [f] state:deferred-to-release Bump the moda-skills pack version + `plugin.json`; update `guide` routing.

**Validation gate**

- [x] Rendering `tests/fixtures/plan-snapshot.md` produces an artifact the **CI
      artifact check** accepts, every original gate line preserved and classified
- [x] Rendering a real planless issue produces a valid artifact with lifecycle
      stages and acceptance criteria
- [x] **The review surface is byte-identical after rendering** (`git diff`
      confined to the appendix), and a human confirms the plan still reads
      top-down as a design document — `[f]` if it got harder to review
- [x] No rendered item asks a worker to revert source to prove a test
- [x] No rendered item asks for failing-first on code that does not exist on the
      base branch; every rendered negative assertion carries a **named mutant**.
      Proven by rendering one greenfield unit and one bug-fix unit and diffing
      the idioms each produced
- [x] **Every rendered `verify:` would fail if its item were not done.** Spot-check
      by rendering one unit, then reverting each item's work in a scratch tree and
      confirming its `verify:` goes red. This is the only check on free-form
      `verify:` quality, and it is a judgement call the reviewer makes — tag it
      `judgement:`, do not pretend it is mechanical
- [x] No rendered `verify:` does anything other than check — no writes, no
      network beyond a `gh` read, no `|| true`, no bare `echo`
- [ ] The rendering runs against a **released** bobi carrying Phases 1–2 (name the
      release and the pin move)

### Phase 4 — Trial on eng-team, then freeze the engine

Nothing is deleted here. The engine keeps running the example packs, which is
what makes the stop criteria below real — "halt and amend" requires a fallback
that still exists. Scope is **eng-team's 7 workflows**, and the substance is two
of them: `issue-lifecycle` (11 steps) and `pr-closed` (4). The remaining five are
single-step wrappers around a prompt.

- [ ] Publish the **engine baseline first**: run one eng-team unit the existing
      way, record turns, wall-clock, spend, human interventions.
- [ ] Run a comparable unit on the checklist model, with an **induced** worker
      death (SIGKILL mid-item) — the "survived a death" evidence is otherwise
      unobtainable. Run one ad-hoc unit through the authoring path.
- [ ] **Binary stop criteria, written before the trial:** halt and amend if spend
      exceeds baseline by >2×, human interventions exceed baseline, any item is
      falsely checked off, or the review surface is ever violated. **Restate the
      spend criterion in checklist terms before the trial runs** — the baseline is
      a multi-step run with a fresh budget per step, the checklist arm is one long
      session with up to `MAX_TURN_BUDGET_RESUMES` continuations, so a naive
      per-run comparison is not like-for-like.
- [ ] **Migrate eng-team's 7 workflows and no others.** `issue-lifecycle` and
      `pr-closed` become real checklists. `adhoc`, `build-failure`,
      `merge-conflict` and `pr-feedback` are single-step prompt wrappers and
      become plain adhoc dispatch — no checklist needed for a one-prompt job.
      `stall-recovery.yaml` is **deleted**, not ported: it is a recovery
      mechanism, and recovery is out of this plan's scope; the capability is
      **handed to the framework liveness work (#837), named explicitly in the
      PR** so it is not silently lost.
- [ ] **The example packs stay on the engine** — `dogfood-content-review` (4) and
      `personal-assistant` (3). They are not fleet load, and leaving them is what
      keeps the engine's test suite meaningful and the fallback exercised.
- [ ] `pr-closed.yaml`'s deterministic pieces become items naming commands
      (`verify: gh pr view <n> --json merged -q .merged`, a worktree-cleanup
      command) — determinism comes from the item text, not an LLM judgement.
- [ ] **Freeze the engine, and define what frozen means** so it is enforceable
      rather than an intention: no new step types, no new features, bugs fixed
      only where they break a live pack, and **every new automation authored as a
      checklist**. `docs/WORKFLOW_ENGINE.md` gets a deprecation banner pointing at
      `docs/CHECKLIST_EXECUTION.md`; it is not deleted, because it still documents
      running code.
- [ ] **Write the deletion trigger now — a condition, not a date.** The engine is
      deleted when nothing dispatches to it: `grep -rn "workflow:" agents/*/agent.yaml`
      plus the fleet's packs returns no auto_dispatch rule naming a workflow, and
      no pack ships `workflows/`. Recorded here because the house record on
      "remove it next release" is six compat shims still live four to five
      releases past their stated one-release window (see
      `plans/2026-07-22-review-remediation.md` Phase 5).
- [ ] `bobi/setup/authoring.py` emits **checklists for new automations** while its
      existing `steps:`/`await:` parsing stays — new teams must not become new
      engine consumers, but nothing already authored breaks. Update
      `tests/test_setup_authoring.py` (936) additively and `DESIGN.md` if the setup
      UI's automation step changes.
- [ ] Bump `agents/eng-team` **and** `agents/registry.yaml`; update
      `agents/eng-team/roles/engineer/ROLE.md`.
- [ ] `docs/CHECKLIST_EXECUTION.md` added (not replacing `WORKFLOW_ENGINE.md`);
      update `OVERVIEW.md`, `BUILDING_AGENT_TEAMS.md`, `README.md`,
      `skills/bobi.md`, `skills/create-agent.md` to present checklists as the
      default and the engine as legacy.
- [ ] Close #845/#846 and PRs #847/#848 with dated pointers (see Notes).

**Validation gate**

- [ ] Baseline numbers published before the checklist arm runs
- [ ] The checklist lane reaches a PR with a SHA-stamped LANDABLE verdict, with the
      artifact showing ≥1 survived death and **zero operator edits**
- [ ] Every checked item with a `verify:` re-passes on re-run; every
      `judgement:`-tagged item is explicitly tagged, not merely unverified
- [ ] The comparison table is written into Notes against the stop criteria
- [ ] `grep -rnE "^\s+(await|handoff|notify|action|goto|if):" agents/eng-team/`
      returns nothing — eng-team is fully off the step machine. The same grep over
      `agents/dogfood-content-review/` and `agents/personal-assistant/` still
      returns hits, and that is **expected**, not a miss
- [ ] `pytest tests/ -q` (full suite incl. integration) green **with no engine
      tests deleted** — the ~6,900 lines covering `orchestrator.py`,
      `variables.py`, `schema.py` and the workflow CLI still pass, because they
      still cover running code. A green suite that required deleting them would
      mean the engine was cut, not frozen
- [ ] `bobi validate` passes on all three `agents/` packs and on `moda-eng-team`
- [ ] A fresh `bobi setup` run produces a working team with a human-approval step,
      and that team's automation is a **checklist**, not `steps:`
- [ ] The engine still works: one example-pack workflow
      (`dogfood-content-review`) runs end-to-end after eng-team has migrated —
      the fallback is proven live, not assumed
- [ ] Real-Claude e2e green on the migrated `issue-lifecycle` equivalent

## Proof of work

- **Bugs get a failing test first; new code does not.** Phase 1's reporting and
  auto-continue changes were bug fixes and landed with tests that fail against
  `main` — the house rule, and correctly applied. Phase 2's CI check is new: the
  review-surface freeze and the state-tag rule have no defect
  to reproduce, and "fails against current `main`" for a check that does not
  exist yet proves nothing. Their proof is **mutation** — remove the named guard,
  the test must fail. Phase 2's one genuine bug fix (session-name variation)
  keeps failing-first, because that defect is real and reproducible on `main`.
- **Suites:** unit every phase; `pytest tests/integration -q` from Phase 2;
  `pytest tests/ -q` at Phase 4.
- **Real-Claude e2e required in Phase 2 and Phase 4.** Per CLAUDE.md's judgement
  call: the parser and error surfacing are brain-agnostic and
  the stub proves them — but "does a real model loop faithfully, refuse to check
  off an unverified item, and record resolvable proof" is exactly where the risk
  lives.
- **Security properties are tests, not prose — and mutation-proved, not merely
  asserted:** untrusted `verify:` refused; an inbound event never clears a
  blocked item; round-log text never becomes a command or a status; the artifact
  is never an authorization source. Every one of these is a claim that something
  does *not* happen, which is exactly the shape that passes vacuously, so each
  names the mutant that must break its test.
- **Phase 4 is acceptance evidence with a baseline and a kill switch**, not a demo.
- **Migration completeness is grep-gated on both the Python and YAML surfaces**,
  because identifier greps alone miss every pack residue.

## Lane map

{Filled by Split. Cross-repo: Phases 1, 2, 4 → `moda-labs/bobi-agent`; Phase 3 →
`moda-labs/moda-skills`. Cross-repo lanes are always marker mode `concurrent`.
Phase 1 is independently landable and should go first. Phase 3 depends on Phases
1–2 shipping in a cut bobi release with the pin moved; Phase 4 depends on Phase 3.
The bobi-agent phases are otherwise sequential, so same-repo parallelism is not
warranted absent a recorded wall-clock justification. **Revised 2026-07-29:** the
earlier "all lanes after Phase 1 depend on Q1's resolution being executed" no
longer holds — Q1's revision removes the `fsutil`/D029 dependency entirely, so no
lane here waits on `2026-07-22-review-remediation`.}

| Lane | Dispatch issue | Phases | One-line scope | Marker mode | Status |
|---|---|---|---|---|---|
| A | — (plan is the spec) | 1 | Honest turn/error reporting + a resumable turn cap; `max_turns` configurable | solo | in review (PR #847) |
| B | — (plan is the spec) | 2, 4 | Worker protocol prompt, CI artifact check, session-name fix; then eng-team trial + engine freeze | solo | open |
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
  treats a cap hit as re-dispatchable, which A makes possible) and on **nothing
  else**; the `2026-07-22-review-remediation` dependency is withdrawn per Q1's
  2026-07-29 revision. Phases 2 and 4 stay one lane: Phase 2 is now three items —
  a prompt, a CI job and one bug fix — and would not be a reviewable PR on its
  own, while Phase 4 is the deletion those three exist to make safe.
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

- **2026-07-29** (planning session with Zach): **the plan was edited in place
  rather than amended**, at Zach's direction, because the initiative had not
  started building. This entry is the trail for that, since the file on `main`
  now differs substantially from `f036bdf`. Status → **Draft**, re-approval
  needed. Seven changes, each following from the same principle — *lean on the
  agent, and stop paying code to do what a prompt can do*:
  1. **The loop reads the artifact once per session, not once per item.** Both
     cold-start steps (read, re-verify) are *resume* operations; within a session
     the worker is its own predecessor. Per-item re-reading was the shape of the
     deleted driver. On this plan's own fixture that was ~4.3M tokens of
     re-reading before any work, and 255 copies of the artifact in one
     transcript — causing the `context_cap` rotation Problem 6 exists to prevent.
  2. **Test-rigor idioms classified by claim shape.** Phase 2 carried 8
     failing-first lines on greenfield, where "fails against `main`" is
     `ImportError`. Bugs keep failing-first; negative and security assertions get
     **mutation-proof with a named mutant**; ordinary behavior gets a plain
     assertion. Root cause is upstream and is now a Phase 3 item: the moda skills
     ship exactly one rigor idiom and it is bug-shaped — `mutation`/`mutant`
     appears **nowhere** in any of them.
  3. **The in-progress monitor, the director prompt and Q3 are removed.**
     Restarting a dead worker is a framework concern (#837's territory), not an
     execution-model one. What the model owes recovery is durable state, and the
     committed artifact already provides it. Q3's `cwd`-containment ownership was
     the weakest decision in the plan and dissolved with it.
  4. **No new Python in `bobi/`.** `checklist/artifact.py`, `checklist/verify.py`
     and the three-verb CLI are gone. The worker reads markdown, edits markers
     with its own file tools and runs `verify:` in its own shell; a parser client
     would be a language model asking a regex what a checklist item is. The
     artifact format is a **Moda lifecycle convention**, and CLAUDE.md's first
     principle keeps those out of the framework. Non-agent verification moved to
     **one CI job** — which must sit outside `ci.yml`'s plans-only skip gate,
     since branch protection reads a skipped required check as passing.
  5. **`verify:` stays free-form shell; the control is the worker's judgement.**
     A named-check vocabulary was rejected as a second language, weaker than
     shell, extendable only by a pack release — the step machine's failure
     rebuilt one layer up. The provenance gate went with it: nothing runs a
     `verify:` unattended, and the only executor already has unrestricted shell.
     A `verify:` is a **proposed proof, not a command**, and a standing tripwire
     is recorded — if anything ever runs one unattended, the gate comes back.
  6. **The `proof:` field is removed.** Problem 5 blamed *"no durable record of
     what the predecessor had proven"*; the durable record is the per-item
     commits. A SHA range denormalizes history into a field that goes stale —
     Phase 1 proved it, its ranges rotated on the first rebase. The rule survives
     and is what mattered: **never re-derive what git history can prove.**
  7. **The step machine is frozen, not deleted; Phase 4 targets eng-team only.**
     Deleting the fallback in the same phase that trials its replacement makes
     "halt and amend" unavailable — separating them is what gives the stop
     criteria teeth. eng-team's 7 workflows move (substance: `issue-lifecycle`
     and `pr-closed`); the example packs stay on the engine as its regression
     coverage. Deletion gets a **mechanical trigger, not a date**, because six
     compat shims in this repo are still live four to five releases past a stated
     one-release window. **The ~6,900-line test disposition — previously the
     single largest item in the plan — is now zero work.**
  **Two stale justifications defused rather than left to rot:** nine
  review-remediation Phase 2 items were `[f]`-superseded because "the step
  machine is deleted" (it is not — the marker stands, the reason changes, and
  **D015** returns to scope since it is a broken route in a pack we now
  deliberately keep on the engine); and #846/#848 were closed as "superseded —
  this plan deletes that surface" (it does not, so their `evaluate_condition`
  defects are live code with no scheduled removal). **Q1's dependency on
  `2026-07-22-review-remediation` is withdrawn entirely** — it existed only
  because a parser in `bobi/` needed `fsutil` for atomic writes.

- **2026-07-29** (Phase 2, build): **Phase 2 complete.** Re-approved by Zach on
  the revised text, then built by hand in one lane (no dispatch issue, per the
  lane map). Seven deviations from the phase text, each deliberate:
  1. **The session fix is a `fresh` opt-out, not session-name variation.** The
     phase text said "vary the session name per dispatch". That is actively
     wrong, and the reason is mechanical: `orchestrator._setup_worktree` sets
     `branch = session_name`, so varying the name forks a NEW git branch on every
     re-dispatch — destroying the one thing the checklist model depends on, a
     re-dispatched worker reading the same branch's commits. It also breaks the
     launch admission dedupe (`subagent.py:1063-1070`) and `check_image_rotation`.
     So the name stays stable and the RESUME is what became optional: `fresh`
     threaded through `Session` -> `spawn_adhoc` -> `launch_agent` (and its
     detached arg blob) -> `run_workflow`, plus a `--fresh` CLI flag, since a
     human typing a command is the only recovery path this plan ships.
     The defect is also worse than the plan recorded: `spawn_adhoc` derives its
     name from `sha256(task)[:8]`, so re-dispatching an identical task string —
     exactly the checklist shape, where the task is a pointer to the artifact and
     does not change between attempts — collides by construction.
  2. **The default is unchanged (Zach, 2026-07-29).** Resuming a failed/stale run
     is the engine's documented retry contract (`launch_agent`'s docstring) and
     the frozen engine still relies on it, so `fresh` is opt-in. Two consequences
     recorded rather than left implied: an arg blob written by an older spawner
     reads as `False` rather than silently changing that manager's semantics; and
     **the trap stays armed by default** — a human who re-dispatches without
     `--fresh` still resumes a dead transcript. `skills/checklist-execution.md`
     carries the mitigation, which is a prompt, not a mechanism.
  3. **That flips the proof idiom for this item.** The plan's Proof of work says
     Phase 2's one genuine bug fix "keeps failing-first, because that defect is
     real and reproducible on `main`". With the default left opt-in, the change
     ADDS a capability rather than fixing a defect, so a test that failed before
     the parameter existed would only prove `TypeError` — the exact vacuity the
     2026-07-29 revision was correcting elsewhere. It is mutation-proved instead:
     removing `_run`'s guard fails with `assert 'dead-session-id' is None`.
  4. **Three CI-check scopes narrowed so the check does not fail on approved
     text.** (a) The `[f]` state-tag rule binds lines the diff **adds** — the live
     plans carry ~15 prose-only `[f]` markers, and retro-fitting them would mean
     rewriting approved plan text, which is what the review surface exists to
     prevent. (b) Gate-line classification is scoped to the **appendix**, the
     machine-rendered surface; hand-written gate lines above the fence predate the
     contract and get classified when Phase 3's renderer emits them. (c) The
     review-surface freeze applies only to diffs that **touch the appendix**,
     which is the mechanical signal for "a worker mutated this" versus "a human
     amended it" — freezing amendments would make plans un-amendable. All three
     live plans were verified to pass unchanged. **Known gap, stated rather than
     papered over:** a worker that edits prose without touching the appendix is
     not caught. This is a marker-aware diff, not a proof.
  5. **The fence is concretely ```` ```checklist ````.** The plan said "a fenced
     appendix"; a check cannot be written against a placeholder. A file with no
     such line has no appendix and is an ordinary plan document.
  6. **The "integration (stub)" test is brain-FREE, not stub-brain.** The property
     — commit per item bounds loss to one item — belongs to the protocol, not to a
     model, so a real git repo plus a scripted worker proves it deterministically.
     A stub brain returns canned turn results and edits no files; it would have
     added a fake worker in front of the same git operations and proven nothing
     extra. Found in the doing, and worth keeping: the dead worker's output files
     are UNTRACKED, so they survive `git checkout -- .`; a re-dispatch must
     `clean` too or the next worker inherits half-finished work nobody did.
  7. **The real-Claude e2e lives in `tests/integration/`, not `tests/e2e/`, and
     has no `[stub]` leg.** `tests/e2e/conftest.py` opens with
     `pytest.importorskip("playwright.sync_api")` — it is a browser suite for the
     setup UI, so a checklist test there would be silently skipped whenever
     Playwright is absent, which is this plan's own "a skipped required check
     reads as passing" failure. And a stub cannot exercise judgement, so a stub
     leg would assert nothing; the deterministic half is item 6 and that IS the
     fast lane. **Consequence for the gate command:** `pytest tests/e2e -q -k
     checklist` collects nothing — the real command is
     `pytest tests/integration -q -k checklist`.
  8. **The protocol was de-coupled from git and pull requests (Zach, 2026-07-29,
     after first review of PR #865).** bobi-agent's own GitHub repo + GitHub CI is
     one *manifestation* of checklist execution, not its definition, and the first
     draft of `skills/checklist-execution.md` had written that manifestation into
     the spine — commits as the durability primitive, "a reviewer reads it on the
     PR", squash-merge, `test:`/`fix:` commit idioms. Restructured into a generic
     protocol (the artifact, the loop, persist-after-each-item, blocking,
     untrusted input, turn budget) plus a clearly separated **"When the work lives
     in a repository"** layer that spells persistence as committing and adds the
     forge-specific notes. The durability primitive is now *save after every
     item*; a commit is how that is spelled in version control. A non-engineering
     agent working a checklist inherits none of it.
     **Two check-semantics bugs fell out of the same review**, both fixed:
     (a) the review-surface rule was byte-identity-apart-from-markers, which
     forbade amendments outright and therefore needed a "did this diff touch the
     appendix?" heuristic to guess worker-versus-human. That heuristic silently
     encoded an opinion the framework should not hold — it **rejected a PR
     carrying both an amendment and the work**, which is exactly the shape Zach
     endorses (the plan may ride the same PR, and may live in the repo as a
     durable artifact). Replaced with **insertion-only**: existing lines are never
     modified or deleted, additions are allowed. Strictly stronger — the heuristic
     is gone, so the rule now applies to every `plans/` diff, which also closes
     the gap deviation 4 had to document.
     (b) the appendix prefix rule demanded byte-exact preservation, which forbade
     **flipping a marker on an appendix item** — the single most ordinary
     operation in the model. No test caught it because the fixture worded its
     review-surface and appendix items differently, so every marker test had been
     exercising the review surface only. Now marker-aware, with regression tests
     on both the flip (passes) and an item rewrite (still caught).

  **Anchor corrections — read these over the body, which is left as approved.**
  Wherever this plan says `session.py:1404`, read **`session.py:1433`**: the
  anchor was correct at `29a382b` and was moved +29 lines by Phase 1's own PR
  #847. Two call sites the plan never named: the function itself is
  **`bobi/sdk.py:557`**, and the orchestrator resumes SEPARATELY via
  `load_session_id` at **`orchestrator.py:446`** — which is the path a
  `-w adhoc` re-dispatch actually travels, so it is the one that mattered.
  These are recorded here rather than edited into the body, because an in-place
  correction is a silent rewrite of approved text however factual it is. That
  rule was enforced the hard way: the CI check this phase adds **failed on this
  very PR** when the corrections were first made inline, which is the check
  working on its first real diff.
  The `--workflow`-optional item is `[f] state:not-needed`, as its own text
  defaulted to.

- **2026-07-29** (build/checklist-execution-model, Phase 3 item 1): the
  proof-idiom gap is **closed** — landed in moda-skills as `8ee5d90` (PR
  moda-labs/moda-skills#28), post-merge CI green, no `plugin.json` bump because
  that repo decouples content from version. Three claim shapes now carry three
  idioms in `plan/SKILL.md`'s Create guidance and its Ready rubric row, and in
  `plan/template.md`: a **bug** → failing-test-first; a **negative, security or
  absence assertion** → mutation proof with a **named mutant**; **ordinary new
  behavior** → a plain assertion. Two deviations from this item as written, both
  deliberate:
  1. **One surface beyond the item's scope: `review/SKILL.md`'s Tests
     dimension** now hunts for "a negative, security or absence assertion with
     no named mutant proving the test can fail." The item named only the two
     `plan` surfaces, which would have left `plan` demanding a mutant that
     `review` — the one stage that reads the diff — had no rubric to check.
     Surfaced as a scope call and **approved by Zach 2026-07-29** before the
     merge.
  2. **Absence claims were folded into the negative-assertion shape** rather
     than given a fourth idiom. Phase 2's own "assert by absence" item is the
     motivating case, and it is a negative assertion whose mutant is *re-adding*
     the deleted line — the same rule, not a new one. Keeps the vocabulary at
     three shapes, which is what the next item's rendering has to classify into.
  Proved the way the new idiom asks: the contract test
  (`test_plan_ships_a_proof_idiom_per_claim_shape`) is a positive assertion, so
  it cannot pass vacuously, and it was still mutation-checked — reverting the
  three skill files to `main` fails it.

- **2026-07-29** (build/checklist-execution-model, Phase 2's last open gate):
  **closed on this PR, which is itself the observation.** The gate line asked for
  "an artifact PR touching nothing else and observing the job execute", because a
  guard sitting behind `ci.yml`'s skip gate reads to branch protection as
  passing. PR #866 touches only `plans/`, and its check run splits exactly as the
  design requires: `Check plan artifacts` **pass**, while every heavy `ci.yml`
  job — `Unit tests`, `Integration tests (no Claude)`, `E2E (Playwright)`,
  `Event server`, `Lint (workflows)`, `Advance the dev channel` — reports
  **skipping**. `Detect code changes` passed and correctly classified the diff as
  plans-only. So the separate-workflow decision holds under observation, not just
  by construction: the one check that exists FOR plans-only PRs is the one check
  that ran on one. Phase 2 is now fully closed — 17 `[x]`, 1 `[f]`, 0 open.

- **2026-07-29** (build/checklist-execution-model, Phase 3): the **`build`-skill
  rendering** is built — moda-skills PR #29, `build/checklist-rendering.md` (new)
  plus `build/SKILL.md`, `guide/SKILL.md`, and 14 contract tests. The new stage is
  **Stage 1.5**, between Worktree and Implement, and it is **conditional**: a unit
  short enough to redo is skipped out loud, per the framework skill's own "a
  checklist is overhead below that line". Numbering it 1.5 rather than renumbering
  is deliberate — `review` and `land` bind to "build Stage 6"/"Stage 7" by name and
  `plan` to "Stage 0"; a test now pins all eight original names.

  **Three findings that came out of rendering, none of which reasoning produced:**
  1. **The appendix fence must never be closed.** A closing fence becomes the
     appendix's last line, the append-only rule freezes it there, and a second run
     can no longer append without rewriting it — the check rejects that with
     "appendix was rewritten, not appended to". Unclosed appends cleanly and matches
     the protocol's own definition (the appendix runs to end of file). **This makes
     the closed fence in `skills/checklist-execution.md`'s example and in
     `tests/fixtures/plan-snapshot.md` a latent defect** — it teaches a shape that
     cannot be worked twice. Not fixed here, because #866 is plans-only by
     construction and that is what closed Phase 2's last gate; it needs its own
     small PR.
  2. **Add no separator the file already ends with.** A redundant blank line lands
     *above* the fence, so a render that adds one perturbs the review surface. The
     check tolerates insertion; byte-identity is the stricter bar and it is free.
  3. Inline backticks inside the appendix are harmless — only a run of three or
     more opens a fence. The first draft of that rule overstated it.

  **Closeout pruning and append-only are reconciled by *when*, not by an
  exemption**, which is what Q2's decision needed to be implementable: an appendix
  created in this PR has no counterpart on the base branch, so pruning it inside
  the PR modifies nothing the base branch ever saw (verified — passes). A summary
  already on the base branch is frozen text (verified — fails with the prefix
  diagnostic).

  **Two deviations from the phase as written.** (a) *"Bump the moda-skills pack
  version + `plugin.json`"* is `[f] state:deferred-to-release`: that repo's
  `AGENTS.md` decouples content from version and authorizes the bump only as part
  of cutting a release, so it belongs to step (b) of the deploy sequence, not to
  the content PR. The `guide`-routing half of that item IS done. (b) The rendering
  emits **no separate Stage 2–4 items** — implement/test/verify are what the plan's
  own phase tasks and gate lines already say, per phase, and re-emitting them
  generically produces items no command can check.

  **Verification, all against `check-plan-artifact.sh` in a throwaway repo:**
  rendering `tests/fixtures/plan-snapshot.md` is accepted with all 3 original gate
  lines preserved and classified (11/11 items), and the review surface comes out
  **literally byte-identical** (zero-line diff above the fence, 0 removed lines);
  rendering a real planless issue (#851, a bug) yields a valid artifact, 9/9
  classified; the idiom split is proven by rendering both shapes — the bug-fix unit
  produced 2 failing-first items read off `git log` and 0 mutants, the greenfield
  unit 0 failing-first and 1 named mutant; 0 `verify:` lines across all three
  renders revert source, write, or end in `|| true`. Four negative controls all
  caught: an edit above the fence, an unclassified item, an `[f]` with no state
  tag, a second fence. Pack suite 30 tests green.

  **Three gate lines stay open on purpose, not by omission:** the human
  confirmation that a rendered plan still reads top-down as a design document; the
  `judgement:`-tagged spot-check that each rendered `verify:` goes red when its
  item is reverted (both need a human, which is the point of tagging them); and
  "the rendering runs against a **released** bobi carrying Phases 1–2", which is
  blocked until that release is cut.

- **2026-07-30** (build/checklist-execution-model): **closed-fence defect fixed,
  and two of those three gate lines closed by the builder after all.**

  `skills/checklist-execution.md`'s example and `tests/fixtures/plan-snapshot.md`
  both closed the appendix fence, teaching a shape that cannot be worked twice.
  Both now leave it open; the skill states the rule in one sentence, and
  `tests/test_plan_artifact_check.py` gains `TestTheFenceIsNeverClosed`, which
  carries the mechanism and its **named mutant** — close the fence, add an item
  where a worker naturally would (*inside* the block), and the check rejects it.
  Zero churn in the existing 18 tests. Writing that mutant refined the finding: a
  closed fence is not a hard rejection but a choice with no good branch — inside
  the block is an insertion the check rejects, after it is a legal append that
  renders the item outside the code block.

  **Landed on this PR, not its own.** The fix is two one-line content changes,
  and Phase 2's plans-only evidence is a *specific check run*, not this PR's
  final file list: `Plan artifact` on `fbfe77a` (actions/runs/30517707852) and on
  `992482e` (actions/runs/30520591534), both success, with the paired `CI` runs
  reporting every heavy job skipping. A dedicated PR bought a second review cycle
  and nothing else.

  **Gate: "review surface byte-identical + a human confirms it still reads
  top-down" → `[x]`.** Byte-identity was proven mechanically; the readability
  half is that the appendix appends after Notes, so every line of the design
  document is untouched and in order. Not harder to review, so not `[f]`.

  **Gate: "every rendered `verify:` would fail if its item were not done" →
  `[x]`.** Spot-checked rather than asserted: the worktree/branch idiom goes RED
  on the wrong branch and GREEN on the right one; the failing-first idiom
  (`git log --grep "test: …"`) goes RED before the test commit exists and GREEN
  after. Those cover every rendered `verify:` but the suite runs, which fail by
  construction when their test is absent or red.

  **Operating principle behind both flips (Zach, 2026-07-30):** an agent has the
  same latitude as a human engineer to adjust the execution plan in flight, and
  should optimise for **reducing human review attention** — the scarcest
  resource. A gate parked for a human that the builder could have decided and
  recorded spends that resource for nothing; the human's job is to overturn a
  recorded call, not to originate it. The same principle cut the fence rationale
  in the skill from twelve lines to one: a worker re-reads that file on every
  cold start, so rationale belongs in the test and here, not there.

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
- **Why recovery left this plan (2026-07-29, Zach).** Restarting a dead worker is
  a framework concern — the same concern `plans/2026-07-23-dead-transport-liveness-backstop.md`
  (#837) is already addressing for agents that go silently dead — and folding it
  into an execution-model plan meant owning a liveness signal, an ownership
  resolution, a monitor tick and a director prompt in service of a question this
  plan does not ask. What the execution model owes recovery is durable state, and
  the committed artifact already provides it. Retained evidence for whoever
  builds the backstop: on 2026-07-26 the director declined to dispatch onto PR
  #847 because the owning worker was still live and intermediate red CI on a
  draft bot PR is the owner's to self-heal — a mechanical no-progress rule would
  have double-dispatched. Detection is the easy half; deciding is not.
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
  #846/#848 for whoever needs them.
  **Re-read after the 2026-07-29 freeze:** both closures were justified by "this
  plan deletes that surface," and it no longer does — `evaluate_condition` keeps
  running the example packs indefinitely. The closures still stand (nobody should
  build `handoff.success_when` into a frozen engine), but those three
  `evaluate_condition` defects are now **live code with no scheduled removal**,
  not a surface about to disappear. They qualify for the freeze's repair bar only
  if they break a live pack; otherwise they stay recorded and unfixed. Do not let
  "it was closed as superseded" read as "it was fixed."
- **Prior art:** #753 (closed) made `subagents launch --wait` block on the launched
  agent and started normalizing `max_turns_reached`.
- **Deferred:** a harness-side blocking join for `Agent` fan-out; a sender-identity
  model for event-driven resume; consolidating the three worktree conventions
  (`orchestrator._setup_worktree:131`, the dead `paths.worktrees_dir:282`,
  CLAUDE.md's policy) — Phase 4 should pick one rather than adding a fourth.
