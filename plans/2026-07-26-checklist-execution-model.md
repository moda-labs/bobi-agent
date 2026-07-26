# Checklist-driven execution: retire the workflow engine's step machine

> **Status:** Draft
> **Tracking issue:** moda-labs/bobi-agent#852 · **Created:** 2026-07-26 · **Last amended:** — (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

Move agent execution off a YAML step machine and onto a checklist model: the plan
file is the one human artifact for a unit of work — enhanced in place with
engineering-lifecycle tasks, or minted when none exists — and a mechanical driver
re-dispatches short-lived workers against a typed run record until the work is
done, pausing at declared gates and yielding to a human when it must.

The goal is **flexibility and durability**, in that order. Flexibility: the
lifecycle lives in prompts and skills, where it can be changed by editing text,
instead of in a second control-flow language that duplicates the plan badly.
Durability: work survives any single session's death, because the state is on
disk and the driver just dispatches again.

The 10x version is not more machinery, it is less: the framework contributes a
generic checklist-coordination primitive that knows nothing about engineering,
and the `build` skill contributes all the lifecycle detail. That split is the
reason this is worth doing rather than patching the engine a third time.

**One correction to the motivation, up front.** An earlier draft argued this was
needed to escape the 200-turn cap. Review refuted that: the cap is per prompt,
multi-step workflows already get one budget per step, and the only sessions that
died were single-step `adhoc` runs (see Problem 3). Turn-budget survival is a
*consequence* of this design, not its justification — and a much cheaper fix
exists for that symptom alone (Alternatives, option 5). The case for this plan is
the duplication and rigidity, not the cap.

## Problem

Verified against the working tree and the live `moda-eng-team` box on 2026-07-26.
Line references are from that day's `main` (`29a382b`).

**1. Step completion is structural, so a run can finish without the work being
done.** `_validate_handoff` (`bobi/workflow/orchestrator.py:1211-1213`) checks
only that required handoff *keys* exist, never their values. `status: in_progress`
is accepted identically to `status: complete`; the run walks to the end of its
step list and emits `agent/session.completed`. Making this semantic inside the
engine would mean teaching `bobi/` what "reviewed" and "documented" mean, which
CLAUDE.md's first principle forbids.

**2. The step list has stopped carrying information, while the plan file carries
more.** Of 14 workflow YAMLs in `agents/`, **8 are single-step** (eng-team
`adhoc`, `build-failure`, `merge-conflict`, `stall-recovery`, `pr-feedback`;
`personal-assistant/adhoc`; `smoke-test`) — `agents/eng-team/workflows/adhoc.yaml`
is one step whose entire body is `prompt: "${{input.task}}"`. The multi-step
survivors delegate their substance to skills: every agent step in `moda-agents`'
`plan-execute.yaml` amounts to "read the `build` skill in full and do what it
says." Meanwhile `plans/2026-07-22-review-remediation.md` carries 251 checklist
items across 9 phases with `file:line` anchors and gates like
`grep -rn "write_text" bobi/ | grep -iE "state|config"`. The step list is a
weaker copy of a better artifact — but note the corollary the review surfaced:
that plan's items are **undifferentiated markdown**, with runnable commands and
prose intent side by side and only one literal `verify:` in the whole file (and
that one is prose). Machine-verifiable done-ness does not exist in today's plans
and has to be built (Q2).

**3. Long ad-hoc work dies in one session and loses everything unpushed.** Two
Lane A workers died on 2026-07-26 at `maxTurns: 200, turnCount: 201`
(`state.json status=failed, error="turn failed"`), both mid-edit with no handoff.
The budget is per prompt (`orchestrator.py:851` queries per step;
`session.py:1171` per inbox message), so:

| session | kind | prompts | tool calls | hit the cap |
|---|---|---|---|---|
| `91856674` | `issue-lifecycle` (11 steps) | 9 | 296 | no |
| `53fbe1a7` | `pr-feedback` | 3 | 250 | no |
| `fcc79fc7` | `adhoc` (1 step) | 2 | 213 | **yes** |
| `05d25f94` | `adhoc` (1 step) | 1 | 231 | **yes** |

Read honestly, this table says *per-step re-query already works* and only the
single-prompt path lacks it. `max_turns=200` is hardcoded at
`orchestrator.py:457`, `subagent.py:746` (`spawn_adhoc`), `subagent.py:587`
(`run_phase_blocking` — **no production callers**, tests only), and
`subagent.py:351`. The deliberate small caps (`CHECK_MAX_TURNS = 8` at `:1564`,
`GATE_MAX_TURNS = 2` at `:1847`, `CURATOR_MAX_TURNS = 10` at `:1992`) must keep
failing fast and are out of scope for any resumability change.

**4. A worker cannot wait without paying turns.** In `fcc79fc7`, **79 of 201**
Bash calls were `tail -1 /tmp/final-suite.log` in one contiguous idle block while
5 background finders and a suite ran. Every blocking primitive was refused:
`sleep 240` was silently backgrounded, `sleep 540` hard-blocked, an `until` loop
backgrounded, and `Monitor` replied *"Keep working — do not poll or sleep"* when
there was no work left. ~40% of that budget bought nothing. `subagents launch
--wait` does block on the agent (#753) but **only for the `adhoc` workflow**
(`cli.py:2931` rejects anything else), and the fan-out used the harness `Agent`
tool, which has no blocking join.

**5. Ad-hoc work keeps no durable record of what it learned.** `spawn_adhoc`
(`bobi/subagent.py:663`) takes a freeform prompt and holds everything in session
context; when the session dies or rotates on `context_cap`, the research and
decisions die with it. `fcc79fc7` spent its first ~30 turns (~15%) re-orienting.

**6. What is actually broken about human yield is a missing wire, not the
architecture.** `await:` is already durable: `orchestrator.py:721-745` marks the
registry `waiting`, persists the full `WorkflowRun` with variable scopes, emits
`agent/workflow.suspended`, disconnects and returns — no process waits. The real
defect is that `try_resume_for_event` has **no production caller** (its own
docstring, `orchestrator.py:76-83`), so the only live resume is the operator CLI
(`cli.py:2250`). `StepDef.timeout` is parsed and never read, so the `timeout:`
values on await steps are dead config. Any replacement inherits this same gap and
must say who provides the wire.

**7. The engine has non-LLM capabilities the checklist model has no answer for
yet.** `agents/eng-team/workflows/pr-closed.yaml` uses all four deterministic step
types in one file: `action: cleanup_worktree` (→ `bobi/workflow/cleanup.py`, which
runs `git worktree remove --force` and `git branch -D`), `if: "merged == true"` /
`goto:` / `else:`, and `notify: slack`. `content-lifecycle.yaml` rotates roles
`editor → researcher → editor → fact_checker → editor`, and
`orchestrator.py:767-786` deliberately starts a **fresh session** on an agent
change so a reviewer step is not contaminated by the builder's reasoning. Handing
destructive git cleanup and `merged == true` to an LLM worker is a regression, not
a simplification.

**8. `bobi setup` generates workflows.** `bobi/setup/authoring.py:420` emits
`{"name": f"{name}-approval", "await": "approval", ...}` for every human-in-the-loop
automation, and `:309`/`:394-438` emit `steps:`. Deleting `await`/`steps` without
touching the authoring path breaks new-team creation and
`tests/test_setup_authoring.py` (936 lines).

## Solution

**One human artifact: the plan file.** If a plan exists, the lifecycle appends to
it; if none exists, ad-hoc execution mints one in the same format. There is no
second checklist type and no second control-flow language.

**But run state is a typed record, not the markdown.** This is the one place the
design departs from "the plan file *is* the state machine", and it is forced by
mechanics the review surfaced (see Q1): the markdown lives in git, so a
`rebase`/`stash`/`checkout` silently reverts it, two lanes conflict at the append
point, a mid-rebase file contains conflict markers, and a worker's worktree copy
diverges from the `main` copy the driver reads. So:

- **`ChecklistRun` (typed, under `state_dir()`) is authoritative** for item
  status, ids, proof references, round history, and the lease.
- **The plan file is the reviewable projection**: human-authored design and tasks,
  with markers written back at phase boundaries. Machines write nothing but
  markers above the fence, and lifecycle expansions only inside a fenced appendix.

This keeps one artifact for a human to read and review, while the thing the driver
trusts is typed, single-writer, and outside git's reach.

**Two surfaces, one file — the human surface stays human.** A hard invariant, not
a convention:

- **Review surface** — Purpose, Problem, Solution, Relevant files, Questionables,
  Phases (tasks + gates), Proof of work, Lane map, Amendments, Notes.
  Human-authored; machines may change only the marker character inside an existing
  `- [ ]`, which is already today's approved contract.
- **Execution surface** — one fenced appendix block at the *end* of the file
  holding rendered lifecycle tasks and resolved item ids. A reviewer reads the
  design top-down and stops at the fence.

Enforced three ways, because CI alone does not cover it: the mutating CLI is the
only sanctioned writer, the run record stores a sha256 of the review surface and
the driver **refuses to dispatch on mismatch**, and a CI check guards the path
(placed in the always-run job — `.github/workflows/ci.yml:26-66` skips heavy jobs
for `plans/`-only changes, so a naive placement would never run).

**Three layers, split on the framework boundary:**

- **`bobi/checklist/` (framework — generic).** Run record, lease, driver loop,
  artifact projection, mutating CLI, native-item execution, worker prompt
  template. It knows `id`, `status`, `proof`, `verify`, `role`, `kind`, and
  `awaiting-human`. It does not know what a PR, a review, or a doc is.
- **The `build` skill (`moda-skills`).** Renders its Stage 1–7 lifecycle
  (worktree, implement, test, verify, document, adversarial review + fix, PR) into
  appendix items with concrete `verify:` lines, expanded against the plan's
  phases. This is the detail `plan` does not emit: `plan` says *what*, `build`
  says *how*.
- **The driver (mechanical, no LLM).** Dispatch a worker; on exit re-read the run
  record and decide continue / escalate / done. Progress is defined as **item-state
  transitions**, never file bytes (a byte digest both false-passes on a cosmetic
  edit or an upstream merge, and false-escalates on a research round that writes
  only the journal). An absolute `max_rounds` backs it up.

**Item kinds, so determinism survives.** `kind: agent` items dispatch a worker.
`kind: native` items name a whitelisted Python action the driver executes itself —
the same registry `bobi/workflow/cleanup.py` feeds today — so worktree cleanup and
notification stay deterministic and no LLM decides `merged == true`. Items carry
an optional `role:`, and the driver starts a fresh session when the role changes,
preserving the reviewer/builder isolation `orchestrator.py:767-786` provides now.

**`verify:` is a sandboxed, provenance-gated, default-off capability.** It is a
shell string in a file that agents write and that can arrive from a public repo,
so it gets the treatment `bobi/monitors/script_cache_checks.py` already
established for LLM-written shell — `validate_script`'s binary allowlist/denylist
and flat-command rule, the rlimit sandbox with its pre-run re-verify, and a
sha256-pinned envelope so changing a `verify:` re-enters approval. Default: a
`verify:` executes only when its provenance is trusted, defined mechanically (the
artifact is under `state_dir()` and written by this driver, or the file is at a
commit on a protected branch). Untrusted provenance means the item is **refused,
not run**.

**Human yield keeps today's strongest property: `awaiting-human` clears only via
the operator CLI.** An inbound event may *notify* the driver that a reply exists;
it may never satisfy the gate. There is no sender allowlist anywhere in `bobi/`
today, and event-bus authorization proves resource access, not personhood — so
event-driven resume would let anyone who can comment on a public repo walk a run
past its approval gate. The `awaiting-human` emit is `blocking=True`, and a
failed emit marks the item `blocked` and escalates loudly rather than entering a
silent forever-halt (the guard `orchestrator.py:695-716` already implements for
undeliverable notifications before an await).

**The artifact is never an authorization source.** Landing authorization is read
from GitHub — a SHA-stamped LANDABLE verdict plus human approval — never from a
checklist item. `proof` must be a machine-resolvable reference (commit sha, PR
review id, run URL); free text is `[f]`, not `[x]`.

**The accumulating journal**, local and uncommitted per CLAUDE.md's continuity
rule: research findings, dated decisions, dead ends, verified-vs-assumed. Entries
are structured records carrying mandatory `source` (`self-derived` |
`untrusted:<origin>`) and are rendered to the next worker inside an explicit
untrusted-data envelope — without that, one injected "decision" becomes
first-party trusted context for every future worker in the run. The journal is
never a source of `verify:` commands, item ids, or status. It is bounded in size
with a summarization step, because it is read whole each round and would otherwise
cost O(N²) tokens across N rounds.

**Alternatives considered.** (1) *Patch the engine with `success_when`* (#846) —
forces engineering vocabulary into `bobi/` and keeps two control-flow languages.
(2) *Raise `max_turns` to ~1M* — discards the cheap runaway-loop tripwire while
leaving work non-resumable. (3) *One long-lived persistent session driven by its
inbox* — already gets per-message budget resets and `context_cap` rotation free,
but its state dies with its process. (4) *Keep the engine for multi-step packs,
checklists for ad-hoc* — two execution models is the current problem wearing a
compromise. (5) **Make the ad-hoc path multi-prompt** (a re-prompt loop in
`spawn_adhoc`, or give `adhoc.yaml` steps) — this is the *correct minimal fix for
Problem 3 alone*, and it is much cheaper than this plan. It loses because it fixes
only the turn symptom: no durable state, no yield gate, no lifecycle rendering, and
the plan-vs-YAML duplication (Problems 1, 2, 6, 7) all survive. If the
Questionables resolve against this plan, option 5 is the fallback to file instead.

**Not in scope:** a harness-side blocking join for `Agent`-tool fan-out (Problem
4's second half) is an upstream affordance, not a `bobi/` change; a sender-identity
model for event-driven resume (its own initiative, prerequisite to ever relaxing
the operator-CLI rule).

## Relevant files

### Existing (verified 2026-07-26 against `29a382b`)

- `bobi/workflow/orchestrator.py` (1213) — step loop; `_validate_handoff:1211`,
  `_drain_response:961` (literal `"turn failed"` at `:984-985`), `"unknown error"`
  at `:933`, `max_turns` at `:457`, per-step `client.query` at `:851`, await
  suspend at `:721-745`, undeliverable-notify guard at `:695-716`, agent-change
  fresh session at `:767-786`, `_setup_worktree:131`, `_execute_notify_step:1110`,
  `_execute_native_action:1097`, `try_resume_for_event:66` (no production caller).
- `bobi/workflow/schema.py` (186) — `StepDef` incl. `agent`/`model`/`effort`
  (`:34-35`), `HandoffContract`, `DEFAULT_ROUTE_LOOP_MAX_ITERATIONS = 3`
  auto-applied to every back-edge (`:178-186`); `StepDef.timeout` never read.
- `bobi/workflow/state.py` (157) — `WorkflowRun`; `claim():76` has the **double
  `os.replace` crash window** (`:95-97`) that leaves a run permanently
  unresumable, with no lease TTL or force-unclaim, and `find_waiting:117` globs
  `*.json` so the wedged file still reports `waiting`. Already ticketed as
  **Q062/D071** in `plans/2026-07-22-review-remediation.md:112`.
- `bobi/workflow/variables.py` (201) — `${{scope.key}}` interpolation +
  `evaluate_condition`.
- `bobi/workflow/triggers.py` (80) — event→dispatch routing. **Keep.**
- `bobi/workflow/cleanup.py` (168) — `cleanup_worktree`, the native-action impl.
- `bobi/subagent.py` — `spawn_adhoc:663` (`run_key = sha256(task)[:8]` at `:694`,
  `max_turns` `:746`), `run_phase_blocking:530` (`:587`, no production callers),
  `launch_agent:954` (`persistent` at `:960`), the admission check that **raises
  on an existing `starting|running|idle` entry** at `:1036-1042`,
  `_check_spend_governor:930-950`, `_emit_lifecycle_event:166-204` (swallows
  errors when non-blocking), `_make_defer_hook:322` (`on_input_needed`, no live
  caller), `CHECK_MAX_TURNS:1564`, `GATE_MAX_TURNS:1847`, `CURATOR_MAX_TURNS:1992`,
  `break` on `max_turns_reached` at `:1823`.
- `bobi/session.py:1171,1195` — per-inbox-message `client.query()`; `:1404`
  `load_resumable_session_id(self.name, …)` — **a re-dispatch reusing a session
  name resumes the dead transcript**.
- `bobi/cli.py` — `subagents launch:2799` (`--workflow` `required=True` at `:2799`,
  `--wait:2804`, second "required" guard in `_dispatch_agent:2853-2855`),
  `_run_agent_wait:2927` (rejects any workflow but `adhoc` at `:2931`), the
  `workflows` CLI group (`list`/`status`/`resume`/`validate`, `:2201-2327`,
  registered `:3409`,`:3419`).
- `bobi/spend_governor.py` — **not a cost ceiling**: `DEFAULT_CAP = 50` agent
  *invocations* per rolling hour per deployment, shared by every launch path;
  breach makes `launch_agent` raise.
- `bobi/service.py:613-617` — `MonitorScheduler(...).start()` inside the manager;
  `bobi/monitors/scheduler.py:617-622` — `threading.Thread(daemon=True)`.
- `bobi/monitors/script_cache_checks.py` (1139) — `validate_script`,
  `run_sandboxed`, `CapabilityEnvelope`: the existing pattern for LLM-written
  shell, to be reused rather than reinvented.
- `bobi/setup/authoring.py:309,383,394-438` — generates `steps:` and
  `await: approval`.
- `bobi/validate.py:293` — `_check_workflow_effort`.
- `bobi/webapp/runtime.py:275` — folds run state on `phase`; N sessions per unit
  breaks the fold.
- `bobi/paths.py` — `workflows_dir:187`, `state_dir:219`, `worktrees_dir:282`
  (**zero callers in `bobi/`** — dead; a third worktree convention alongside
  `orchestrator._setup_worktree` and CLAUDE.md's policy).
- `agents/{eng-team,personal-assistant,dogfood-content-review}/workflows/*.yaml`
  (14) + `agents/*/agent.yaml` + `agents/registry.yaml` (pack versions),
  `agents/eng-team/roles/engineer/ROLE.md` (prompt surface naming handoffs).
- `docs/WORKFLOW_ENGINE.md` (344), `docs/SECURITY.md`, `docs/MONITORS.md`.
- Tests broken by the cutover (re-derived, not the earlier 2,868 estimate):
  `test_orchestrator.py` 1934, `test_setup_authoring.py` 936, `test_validate.py`
  717, `test_notify_step.py` 569, `test_cleanup.py` 574, `test_variables.py` 390,
  `test_setup_digestion.py` 305, `test_workflow_state.py` 273,
  `integration/test_workflow_orchestrator.py` 271, `test_triggers.py` 235,
  `integration/test_effort_selection.py` 214,
  `integration/test_cross_model_resume.py` 201, `test_cli.py:221`
  (`test_workflow_required`), `test_dogfood_content_review_pack.py:43`,
  `workflow_utils.py` 19 — **~6,900 lines**.
- `plans/2026-07-22-review-remediation.md` (9 phases, 251 items) +
  `plans/2026-07-22-review-remediation-findings.md` — the live plan this one
  collides with, and the multi-file-spec case.

### New

- `bobi/checklist/run.py` — `ChecklistRun`: authoritative item state, round
  history, review-surface sha256, lease with `claimed_by` + `lease_expires_at`.
- `bobi/checklist/lease.py` — **single**-`os.replace` claim with TTL and
  mechanical takeover, adopted by `WorkflowRun` too (one lease, not two).
- `bobi/checklist/artifact.py` — parse the plan file; project markers back;
  enforce the review-surface freeze.
- `bobi/checklist/journal.py` — bounded, provenance-tagged, summarizing journal.
- `bobi/checklist/driver.py` — the loop, transition-based tripwire, `max_rounds`,
  budget accounting, native-item execution.
- `bobi/checklist/verify.py` — provenance gate + sandbox for `verify:`.
- `bobi/templates/checklist-worker.md` — generic worker prompt (override path and
  precedence specified, since `bobi/templates/` has no override mechanism today).
- `docs/CHECKLIST_EXECUTION.md`.
- `tests/fixtures/plan-snapshot.md` — frozen copy; the live plan is mutating.
- `tests/test_checklist_*.py`, `tests/integration/test_checklist_driver.py`,
  `tests/e2e/test_checklist_worker.py`.
- (`moda-skills`) a new `build` rendering stage.

## Questionables

- **Q1 — Where does authoritative run state live?** Zach's direction was to
  centralize on the plan file. Review found the markdown cannot safely *be* the
  state machine: it lives in git (rebase/stash/checkout silently revert it; a
  worker's worktree copy diverges from the `main` copy the driver reads; two lanes
  conflict at the append point; a mid-merge file contains conflict markers that
  make the parser raise), five item states do not fit four markers, and `[f]`
  renders "waiting for a human" as "failed". Options: (a) typed `ChecklistRun`
  authoritative + the plan file as reviewable projection with markers written back
  at phase boundaries; (b) markdown authoritative, accepting the git hazards and
  adding conflict-recovery rules; (c) markdown authoritative but moved out of git
  into `state_dir()` — which abandons human reviewability, the thing Zach asked to
  protect. Recommendation: **(a)** — it preserves the intent (one artifact a human
  reads and reviews, enhanced with lifecycle tasks) while the thing the driver
  trusts is typed and single-writer. This is a deliberate narrowing of "the plan
  file is the state machine" and needs Zach's explicit call.
- **Q2 — How do items become machine-verifiable?** `verify:` appears exactly once
  in the designated fixture, as prose. Today's plan items are undifferentiated
  markdown, so "re-verify before trusting `done`" has nothing to run for
  human-authored items, and Phase 6's "zero falsely-done items" gate would be
  vacuously true. Options: (a) `verify:` exists only in the machine-written
  appendix; human-authored gates stay unverified and the plan says so plainly;
  (b) the `plan` skill's template gains explicit `verify:` syntax, making every
  future plan machine-checkable (and this a migration for existing plans);
  (c) hybrid — appendix items require `verify:`, human gate lines get an optional
  one that `build`'s rendering step proposes and a human accepts. Recommendation:
  **(c)** — it gets mechanical verification where it matters most (the lifecycle
  stages, which is exactly the detail Zach wants added) without silently
  rewriting approved plans, and the proposal step keeps a human in the loop on
  what will be executed.
- **Q3 — What hosts the driver?** It must be mechanical (no LLM: the director
  rotated on `context_cap` mid-thread on 2026-07-26 and burned turns replying "No
  action" to individual `check_run` webhooks) and survive a box restart. The
  earlier draft recommended "a monitor-scheduler flavor, no new process" — that is
  **wrong**: `MonitorScheduler.start()` is a `daemon=True` thread started inside
  the manager (`service.py:615`, `scheduler.py:617-622`), so it dies with the
  manager, and it is the same thread implicated in the curator GIL-starvation
  incident. Real options: (a) a supervised standalone `bobi checklist run` process,
  like the event server — survives independently, adds a process to supervise;
  (b) the manager/scheduler thread **plus** boot-time reconciliation of orphaned
  leases — no new process, but every driver outage is a manager outage.
  Recommendation: **(a)**, with driver logic in `bobi/checklist/driver.py` so the
  host stays swappable.
- **Q4 — Where does an ad-hoc unit's minted plan file live?** "Plans only for
  initiatives" (single-unit work is issue-only) collides with centralizing on a
  plan file. Options: (a) local under `state_dir()`, never a repo artifact —
  preserves the ticketing rule, but invisible to a human and lost with the volume;
  (b) committed to `plans/` like any plan — visible and durable, but makes every
  one-line fix a plan file; (c) local, mirrored **write-only** into a pinned PR
  comment for visibility, promoted to `plans/` only if the work turns out
  initiative-sized. Recommendation: **(c)**, with the mirror explicitly never a
  read source (otherwise any GitHub user can post a look-alike comment and become
  the state machine's input).
- **Q5 — Sequencing against the live `2026-07-22-review-remediation` plan.** That
  plan is Approved with lanes A–E open (#818–822). Its **Phase 2** is "Workflow
  engine + agent-pack routing correctness" — 12 open items in exactly the
  `orchestrator.py`/`variables.py`/`pr-closed.yaml` code this plan's cutover
  deletes, including extending the condition parser with `>` operators. Its
  **D092** hoists five copies of the atomic-write pattern into one helper, and its
  **Q062/D071** fixes the `claim()` crash window — both of which this plan also
  needs, and landing this plan's Phase 1 first would create a sixth copy. Options:
  (a) this plan blocks on that plan's Phases 2–3 landing; (b) that plan's Phase 2
  is formally descoped as superseded by an Amendment, keeping only its
  `fsutil`/claim work; (c) run both and reconcile at merge. Recommendation:
  **(b)** — fixing engine internals scheduled for deletion is waste, but the
  `fsutil` helper and the lease fix are shared infrastructure this plan consumes,
  so they stay and this plan adopts rather than forks them.
- **Q6 — What happens to the in-flight #845/#846 work?** The directive was to
  ignore it. PR **#848** (#846, `handoff.success_when`, draft) is cleanly
  superseded — it gates a step machine this plan deletes. PR **#847** (#845,
  **ready**, green) is two changes in one: the *reporting* half (surface
  `error_message`/`error_kind` instead of the literal `"turn failed"`, widen the
  `stop` log record) is engine-agnostic and is the defect that masked a full day of
  debugging — the same shape currently logs `unknown error` every ~15 min from
  monitor `check-c561144f` — while the *`max_turns` raise* half encodes a ceiling
  this plan removes. Options: (a) land #847's reporting half now as a standalone
  fix, close #848 and the `max_turns` half as superseded; (b) close both PRs and
  re-derive the reporting fix inside this plan; (c) land #847 whole.
  Recommendation: **(a)** — the observability fix is real, orthogonal, and already
  green; folding it into a 5-phase initiative delays a fix that costs nothing to
  take, and the `max_turns` half would encode a ceiling this design deletes.

## Phases

Phase order note: **Phase 1 depends on Q5's `fsutil` durable-write helper and the
shared lease fix**; do not start it while those are unresolved.

### Phase 1 — Run record, lease, artifact projection, mutating CLI

- [ ] `bobi/checklist/lease.py`: single-`os.replace` claim (write the new state
      into the temp, one atomic rename — never the current two), `claimed_by`
      pid/host, `lease_expires_at` TTL, mechanical takeover on expiry, and a
      force-unclaim. `WorkflowRun` adopts it too — one lease implementation, not a
      second copy of a known-buggy one.
- [ ] `bobi/checklist/run.py`: `ChecklistRun` — typed item records (`id`, `status`
      ∈ {`todo`,`wip`,`done`,`blocked`,`awaiting-human`}, `kind` ∈
      {`agent`,`native`}, optional `role`, optional `verify`, `proof` as a
      machine-resolvable reference), round history, `review_surface_sha256`.
      Authoritative per Q1.
- [ ] `bobi/checklist/artifact.py`: parse the plan file into items; project status
      back as markers; a fenced appendix appended at EOF (never inserted).
      `awaiting-human` renders `[f]` with a machine-readable tag the parser
      asserts on, so the driver's halt-vs-fail branch never depends on prose.
- [ ] **Mutating CLI as the only sanctioned write path**:
      `checklist set-status <id> <state> --proof … --note …`, `checklist expand`,
      `checklist approve`, `checklist unclaim`. Read-only `show|verify` too.
      Without this the worker uses `Edit`/`Write` and no lock, atomicity, or fence
      invariant holds.
- [ ] Atomic writes via Q5's shared `fsutil` helper — adopt, never re-implement.
- [ ] `bobi/checklist/journal.py`: bounded, append-only, provenance-tagged
      (`source: self-derived | untrusted:<origin>`), with a summarization step;
      rendered to workers inside an untrusted-data envelope.
- [ ] Genericness guard over `bobi/checklist/` **and** `bobi/templates/checklist-*`
      (the template says "push", which is git vocabulary in `bobi/`), as an
      explicit denylist of *domain* terms (`pull_request`, `changelog`, `pytest`,
      `landable`, `worktree`) — not a substring scan for `test`/`review`/`merge`,
      which collide with unavoidable identifiers.
- [ ] `docs/SECURITY.md` updated in **this** phase for the new shell surface and
      the artifact-is-not-an-authorization-source invariant.

**Validation gate** — do not exit this phase until every line passes.

- [ ] Failing-first: parse/project/mutate round-trip on `tests/fixtures/plan-snapshot.md`
      (frozen copy — the live plan is being mutated by lanes A/B/E right now)
- [ ] Failing-first: **review-surface freeze** — every mutation API leaves bytes
      above the fence identical except markers, and RAISES on an attempt to write
      prose above it
- [ ] Failing-first: an out-of-band edit above the fence is **detected on next
      parse** via `review_surface_sha256` mismatch and refuses dispatch
- [ ] Failing-first: `[f]` without a state tag raises rather than defaulting
- [ ] Failing-first: a `proof` that is not machine-resolvable yields `[f]`, not `[x]`
- [ ] Failing-first: **crash inside the claim window** (kill between rename steps)
      leaves the run claimable, and the next driver takes over on TTL expiry
- [ ] Failing-first: journal text resembling a `verify:` line or status marker is
      not honored by `artifact.py`
- [ ] `pytest tests/ --ignore=tests/integration --ignore=tests/e2e --timeout=30 -q`

### Phase 2 — Driver + worker + dispatch contract

Merged from the earlier draft's Phases 3 and 4: the driver cannot be validated
without a worker that mutates the artifact, and the worker cannot be dispatched
without the driver.

- [ ] **Dispatch contract, explicitly** — the earlier draft left this to the
      builder and both available paths are broken: `launch_agent` raises on an
      existing `starting|running|idle` registry entry (`subagent.py:1036-1042`), so
      a killed worker blocks re-dispatch until the dead-man reconciler expires it;
      `spawn_adhoc` derives `run_key` from `sha256(task)` (`:694`) and
      `session.py:1404` then **resumes the dead worker's transcript**, destroying
      the fresh-budget premise. Specify per-round session identity, driver-owned
      termination of the prior round's registry entry, and one event-server
      deployment per run (not per round).
- [ ] `bobi/checklist/driver.py`: one worker per round per artifact, enforced by
      the Phase 1 lease. Progress = **item-state transitions**, never file bytes.
      Absolute `max_rounds` alongside the tripwire.
- [ ] Budget: correct the earlier draft's error — `spend_governor` is a shared
      50-invocations-per-hour admission gate, not a cost ceiling. The driver gets
      its own accounting (wall-clock + `TurnResult.total_cost_usd`) and must
      **reserve against, never monopolize**, the shared cap; a governor breach
      halts the run observably instead of propagating `RuntimeError`.
- [ ] `kind: native` items execute in the driver against the whitelisted action
      registry (`bobi/workflow/cleanup.py`'s `cleanup_worktree` and the notify
      path), so destructive git and `merged == true` stay non-LLM.
- [ ] `role:` per item; a role change starts a fresh session, preserving
      `orchestrator.py:767-786`'s reviewer-isolation invariant.
- [ ] `awaiting-human`: emit `blocking=True`; a failed emit marks `blocked` and
      escalates loudly. **Clears only via `checklist approve` on the operator CLI**
      — an inbound event may notify, never satisfy.
- [ ] `bobi/checklist/verify.py`: provenance gate (default off) + reuse of
      `script_cache_checks.validate_script`/`run_sandboxed` + sha256-pinned
      accepted-`verify:` set in the run record.
- [ ] `bobi/templates/checklist-worker.md` + the override path/precedence contract.
      The template forbids polling and mandates `subagents launch --wait` for
      fan-out (note `--wait` currently rejects non-`adhoc` workflows,
      `cli.py:2931` — widen or document).
- [ ] `subagents launch --checklist <path>`; `--workflow` becomes optional at
      **both** guard sites (`cli.py:2799` and `_dispatch_agent:2853-2855`), and
      `tests/test_cli.py:221` (`test_workflow_required`) is updated.
- [ ] Lifecycle events carry `checklist_run_id` + `round` so N sessions per unit
      still fold; audit the private `bobi-deploy` webapp consumers and
      `bobi/webapp/runtime.py:275`'s `phase` fold before claiming "unchanged".

**Validation gate**

- [ ] Failing-first integration (stub): a 5-item checklist with a worker
      **SIGKILLed** at item 3 reaches all-`done` across re-dispatches, losing only
      item 3's partial work
- [ ] Failing-first: re-dispatch succeeds within one tick against an
      un-reconciled `running` registry entry
- [ ] Failing-first: two re-dispatches on one run do **not** resume each other's
      transcripts (assert distinct session ids)
- [ ] Failing-first: a 20-round run consumes ≤ K spend-governor invocations and
      creates exactly one event-server deployment
- [ ] Failing-first: a cosmetic-only round (marker churn, no state transition)
      trips the tripwire; a journal-only research round does **not**
- [ ] Failing-first: `max_rounds` halts observably
- [ ] Failing-first: two concurrent drivers — exactly one claims
- [ ] Failing-first: an inbound reply does **NOT** clear `awaiting-human`; only
      `checklist approve` does (the earlier draft's "a simulated reply resumes it"
      was a test that the vulnerability works)
- [ ] Failing-first: a `verify:` from untrusted provenance is **refused, not run**;
      a denylisted binary is refused; a changed `verify:` re-enters approval
- [ ] Failing-first: consecutive items with different `role:` dispatch to
      different sessions
- [ ] Failing-first: a `kind: native` item never dispatches an LLM
- [ ] **Real-Claude e2e, `[stub]+[claude]`, claude leg required**: a real session
      completes a 4-item checklist in order, records resolvable proof, does not
      mark `done` an item whose `verify:` fails, and mutates the artifact **only**
      through the CLI (assert no direct file edit in the transcript)
- [ ] `pytest tests/ --ignore=tests/e2e --timeout=30 -q`,
      `pytest tests/integration -q -k checklist`, `pytest tests/e2e -q -k checklist`

### Phase 3 — `build`-skill lifecycle rendering (moda-skills)

- [ ] New `build` stage: render Stage 1–7 into the plan file's fenced appendix as
      items with `verify:` lines, `kind`, and `role`, expanded against the phases
      the unit covers. This is the detail `plan` does not emit.
- [ ] Ad-hoc path: no plan → mint one in the same format (location per Q4) from the
      issue's acceptance criteria plus the same stages. One code path, not two.
- [ ] Pre-planned path: append only; never rewrite approved plan text (Q1/Q2).
- [ ] Multi-file specs: record a `spec:` companion reference (the fixture's spec
      spans a second 1,500-line file) that the worker reads selectively.
- [ ] `plan` skill: document the `verify:` proposal flow per Q2(c).
- [ ] Bump the moda-skills pack version + `plugin.json`; update `guide` routing.

**Validation gate**

- [ ] Rendering `tests/fixtures/plan-snapshot.md` produces an artifact
      `checklist verify` accepts, every original gate line preserved
- [ ] Rendering a real planless issue produces a valid artifact with lifecycle
      stages and acceptance criteria
- [ ] **The review surface is byte-identical after rendering** (`git diff` confined
      to the appendix), and a human confirms the plan still reads top-down as a
      design document — `[f]` if it got harder to review
- [ ] The rendering runs against a **released** bobi carrying Phase 1–2 (name the
      release and pin move; a moda-skills lane runs against the installed version)

### Phase 4 — Parallel trial with a real baseline

- [ ] Gate the `build` skill's rendering behind a flag so **both** paths are live —
      without this, Phase 3 rewrites the engine's only consumer and nothing drives
      the engine during the "parallel" trial.
- [ ] Publish the **engine baseline first**: run one lane the existing way and
      record turns, rounds, wall-clock, spend, human interventions.
- [ ] Run a comparable lane on the checklist model. Induce a worker death
      (SIGKILL mid-item) — the "survived a death" evidence is unobtainable
      otherwise.
- [ ] Run one ad-hoc unit through the minting path.
- [ ] **Binary stop criteria, written before the trial:** halt and amend if spend
      exceeds the baseline by >2×, if human interventions exceed the baseline, if
      any item is falsely `done`, or if the review surface is ever violated.

**Validation gate**

- [ ] Baseline numbers published before the checklist arm runs
- [ ] The checklist lane reaches a PR with a SHA-stamped LANDABLE verdict, with the
      run record showing ≥3 rounds, ≥1 non-terminal round, and **zero operator
      edits to the artifact**
- [ ] The induced death was survived without human intervention, evidenced from the
      run record
- [ ] Every `done` item with a `verify:` re-passes on re-run, **and** every
      verify-less `done` item has a resolvable proof reference
- [ ] The comparison table is written into Notes with real numbers against the
      stop criteria

### Phase 5 — Hard cutover

- [ ] Per-step-type disposition table (route / await / notify / action / agent /
      model / effort → replacement or explicit keep) before deleting anything.
- [ ] Migrate **all 14** workflows with step counts and target shape — the earlier
      draft accounted for 10, omitting `dogfood-content-review.yaml` (5, incl. a
      route), `research-task.yaml` (2), `daily-briefing.yaml` (2), `request.yaml` (2).
- [ ] `bobi/setup/authoring.py` emits checklists instead of `steps:`/`await:`
      (`:309`, `:394-438`), with `tests/test_setup_authoring.py` (936) updated.
- [ ] Delete the step loop, `HandoffContract`, handoff validation, back-edge
      validation, route/await conditions. **Keep** `triggers.py`; re-verify
      `${{}}` interpolation consumers before deleting `variables.py`.
- [ ] Retire the `workflows` CLI group (`cli.py:2201-2327`, `:3409`, `:3419`) or
      re-point it at checklist runs.
- [ ] Bump every touched pack version **and** `agents/registry.yaml` (exact-pin
      consumers otherwise fetch the stale immutable tarball); update
      `agents/eng-team/roles/engineer/ROLE.md`.
- [ ] Port or delete ~6,900 lines of tests with per-file disposition.
- [ ] `docs/CHECKLIST_EXECUTION.md` replaces `docs/WORKFLOW_ENGINE.md`; update
      `docs/OVERVIEW.md`, `QUICKSTART.md`, `BUILDING_AGENT_TEAMS.md`,
      `EVENT_SERVER.md`, `MONITORS.md`, `README.md`, `skills/bobi.md`,
      `skills/create-agent.md`, `skills/linear-setup.md`, and `DESIGN.md` if the
      setup UI's automation step changes.
- [ ] Close #845/#846 and PRs #847/#848 per Q6 with dated pointers.

**Validation gate**

- [ ] `grep -rn "StepDef\|HandoffContract\|evaluate_condition\|await_event" bobi/ tests/`
      **and** `grep -rnE "^\s+(await|handoff|notify|action|goto|if):" agents/`
      (the YAML surface — identifier greps alone miss every pack residue) return
      only deliberately-kept survivors, each named in the PR
- [ ] `grep -rn "handoff:" docs/ skills/ README.md agents/*/roles/*/ROLE.md` clean
- [ ] `pytest tests/ -q` (full suite incl. integration) green
- [ ] `bobi validate` passes on all three `agents/` packs and on `moda-eng-team`
- [ ] Real-Claude e2e green on the migrated `issue-lifecycle` equivalent
- [ ] A fresh `bobi setup` run produces a working team with a human-approval step

## Proof of work

- **Bugs get a failing test first.** The lease crash window, the review-surface
  freeze, the provenance gate, and the resume-authorization rule each land with a
  test that fails against current `main`.
- **Suites:** unit every phase; `pytest tests/integration -q` from Phase 2;
  `pytest tests/ -q` at Phase 5.
- **Real-Claude e2e required in Phase 2 and Phase 5.** Per CLAUDE.md's judgement
  call: the lease, run record, driver, and event emission are brain-agnostic and
  the stub proves them — but "does a real model faithfully use the mutating CLI,
  refuse to mark an unverified item `done`, and record resolvable proof" is
  exactly where the risk lives.
- **Security properties are tests, not prose:** untrusted `verify:` refused;
  inbound reply does not clear `awaiting-human`; journal content never becomes a
  command or a status; artifact never an authorization source.
- **Genericness is mechanical** (Phase 1 guard) and must fail when a domain term
  enters `bobi/checklist/` or the worker template.
- **Phase 4 is acceptance evidence with a baseline and a kill switch**, not a demo.
- **Migration completeness is grep-gated on both the Python and YAML surfaces.**

## Lane map

{Filled by Split. Cross-repo: Phases 1, 2, 4, 5 → `moda-labs/bobi-agent`; Phase 3
→ `moda-labs/moda-skills`. Cross-repo lanes are always marker mode `concurrent`.
Phase 3 depends on Phase 1–2 shipping in a cut bobi release with the pin moved;
Phase 4 depends on Phase 3. The bobi-agent phases are sequential by construction,
so same-repo parallelism is not warranted and Split should cut one lane per repo
absent a recorded wall-clock justification. **All lanes block on Q5.**}

| Lane | Dispatch issue | Phases | One-line scope | Marker mode | Status |
|---|---|---|---|---|---|
| — | — | — | {filled by Split} | — | — |

- [ ] Convergence gate: a real unit of work runs end-to-end through the rendered
      checklist on a released bobi + released moda-skills pack, with the run record
      showing a survived death and zero review-surface violations (fuse-runnable on
      a merged preview for the code half; the pack-release half is deferred).

## Amendments

- **2026-07-26** (plan/checklist-execution-model): created.
- **2026-07-26** (plan/checklist-execution-model): substantially revised after a
  3-lens adversarial review (implementer / staff-engineer / red-team). Corrections
  to load-bearing claims that were **false** in the first draft: `await:` is
  already a durable disk-persisted suspend, not a live suspension (the real defect
  is `try_resume_for_event` having no caller); `spend_governor` caps invocations
  per hour, not cost; `MonitorScheduler` is a daemon thread inside the manager, so
  the original Q3 recommendation failed Q3's own restart requirement;
  `verify:` does not exist in the designated fixture; the fixture has 9 phases not
  6; 8 workflows are single-step not 7; the test surface is ~6,900 lines not 2,868;
  `run_phase_blocking` has no production callers; `launch_detached:960` was a
  wrong reference (`launch_agent:954`). Design changes: run state moved to a typed
  record (Q1); `verify:` became a sandboxed provenance-gated capability;
  `awaiting-human` resume restricted to the operator CLI; a mutating CLI added as
  the only write path; native item kinds and per-item roles added to preserve
  determinism and reviewer isolation; the progress tripwire re-keyed from file
  digest to item-state transitions; Phases 3+4 merged; a baseline arm and binary
  stop criteria added to the trial; Q5 (collision with the live review-remediation
  plan) added.

## Notes

- **Evidence base.** Session numbers come from the live `moda-eng-team` box on
  2026-07-26: `/data/.bobi/agents/eng-team/run/state/sessions/` for run state and
  `/data/claude/projects/-data--bobi-agents-eng-team-run/*.jsonl` for the raw CLI
  transcripts. `fcc79fc7` (= run `a9135266`) and `05d25f94` (= run `cf501439`) are
  the two turn-cap deaths; the `max_turns_reached` attachment is the ground truth
  the engine discards.
- **The two deaths had different profiles** and both matter: `fcc79fc7` wasted
  ~40% of its budget idle-polling, while `05d25f94` did 231 tool calls of genuine
  work across 13 subagent launches and still did not finish — no turn budget makes
  a 90-file/7,550-line review gate fit in one session. Fixing only one would not
  have saved Lane A. **Splitting that diff remains worthwhile independent of this
  plan.**
- **Prior art:** #753 (closed) made `subagents launch --wait` block on the launched
  agent and started normalizing `max_turns_reached`; `bobi/brain/claude.py:278-292`
  detects it correctly today and only the orchestrator drain discards it.
- **The observability fix is not in this plan** (Q6): surfacing the real terminal
  error instead of the literal `"turn failed"` is engine-agnostic, already green in
  PR #847, and would have made the original incident diagnosable in one minute
  instead of one day. It should land on its own schedule.
- **Deferred:** a harness-side blocking join for `Agent` fan-out; a sender-identity
  model for event-driven resume; consolidating the three worktree conventions
  (`orchestrator._setup_worktree:131`, the dead `paths.worktrees_dir:282`,
  CLAUDE.md's policy) — Phase 5 should pick one rather than adding a fourth.
