# Retire multi-step YAML workflows: team behavior moves to skill files

> **Status:** Draft
> **Tracking issue:** moda-labs/bobi-agent#{N} · **Created:** 2026-10-19 · **Last amended:** — (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

Every run becomes one prompt: the builtin `adhoc` run, given a task that names a
skill file. Team behavior lives in `context/skills/<name>/SKILL.md` (prose, edited
like any prompt) instead of a second control-flow language. The step machine
(route, await/resume gates, notify, native-action steps, handoff contracts,
role switching between steps, checkpoints) is deleted.

This carries out what `plans/2026-07-26-checklist-execution-model.md` (#852,
now Linear MOD-284) froze but never finished. Its Phase 4 trial never ran. The
fleet has since run the experiment informally (see Problem), so this plan
replaces that A/B trial with a post-roll stop criterion (Phase 2 gate).

## Problem

Fleet audit, 2026-10-14, run records on every Fly box (records reach back to
2026-07-30 for `issue-lifecycle` and 2026-08-22 for everything else):

| Workflow | Runs | What it shows |
|---|---|---|
| `adhoc` (1 prompt step) | eng-team 116, baohua 69, roadmap-pm 12 | Carries ~half of all runs. On eng-team the director routes almost everything through it (100 of ~110 runs in 30 days). |
| `pr-closed` | eng-team 97, baohua 67 | Mostly deterministic: `cleanup_worktree` native action → route on `merged_live` → engineer closes issue → Slack notify. |
| `issue-lifecycle` (12 steps, `await: approval`) | eng-team 13, baohua 27 | **16 runs parked at `await_approval` and never answered** (eng-team 11, since 2026-07-30). |
| `report-investigation` (support → engineer → director) | baohua 8 | The one live multi-role workflow. |
| `build-failure`, `merge-conflict`, `pr-feedback`, `weekly-marketing` | ≤7 each | Single prompt step, or several steps by one role. |
| `plan-execute`, `stall-recovery`, `prd-draft`, `prd-design` | **0** | Never ran. |
| personal-assistant `daily-briefing` / `request` | 6 / 1 | No run since 2026-08-27. |

- roadmap-pm already made this move in moda-agents #163 (2026-10-12): its
  standup is `auto_dispatch` → `workflow: adhoc`, `role: pm`, `task: "Read
  package/context/skills/daily-standup/SKILL.md and run the scheduled standup…"`.
  It runs in production.
- Engine size: `bobi/workflow/` is 3,227 lines (`orchestrator.py` 2,078).
  Multi-step features also reach into `bobi/cli.py` (`workflows resume`,
  `_require_step_roles`), `bobi/subagent.py` (waiting/resuming admission,
  `validate_workflow_inputs`), the console (gate composer, awaiting-action tab,
  `run_actions.py`, three server routes), the supervisor's admin verbs
  `resume_run` / `remind_run` / `close_run` (in `bobi/supervisor/admin.py` **and**
  `event-server/worker/src/fleet.ts`), `bobi/setup/authoring.py` (writes
  `await: approval` for every "ask first" automation), and about 4,400 lines of
  tests that cover only multi-step features.
- New teams keep becoming engine users: `bobi setup` writes `workflows/*.yaml`
  with `await:` gates (`authoring.py:402-490`).

## Solution

1. **Additive first (bobi-agent).** Add what the migrated packs need, without
   deleting anything:
   - An `action:` key on `auto_dispatch` rules. The reactor calls a registered
     native action (`cleanup_worktree`) in-process: no session, no tokens.
   - `--workflow/-w` optional everywhere, defaulting to `adhoc`.
   - Migrate the `eng-team` and `personal-assistant` packs to skills.
   - `bobi setup` authors skills plus `auto_dispatch` rules, never `workflows/`.
   - Release.
2. **Migrate the overlays (moda-agents).** Move baohua and moda-eng-team onto
   that release. Delete the workflows of the three packs nothing deploys. Roll
   the fleet and observe.
3. **Delete (bobi-agent).** Remove the step machine and the gate surface.
   - `bobi validate` hard-errors on a pack that ships `workflows/` or on an
     `auto_dispatch` rule naming any workflow but `adhoc`.
   - Legacy `waiting`/`resuming` run records load as `cancelled`.
   - Removing the admin verbs is a breaking protocol change: bump
     `SUPERVISOR_VERSION` and add a migration note, per `docs/ADMIN_PROTOCOL.md`.
   - Release.
4. **Roll and verify.**

**What stays:** `WorkflowRun` as the run record, the launch path, run-key and
period dedupe, `variables.resolve` (adhoc's `${{input.task}}` and the reactor's
`task:` templates), `triggers.builtin_adhoc_workflow`, the runs view,
`cleanup.py`. Renaming "workflow" fields and paths is out of scope.

**How a skill replaces each feature:**

- **Sequence and branching.** Prose in the skill.
- **Approval.** The skill says: post the proposal (spec or plan PR link) in the
  thread, stop, act only on an explicit yes. The human's reply is a new chat
  event. The director routes it to a fresh launch with the same `--id`.
- **Role handoff.** The finishing worker launches the next role's `adhoc` run
  and passes its findings in `--task`. Nothing blocks the director.
- **Deterministic steps.** Exact commands named in the skill, or a
  reactor `action:` when no model is needed.

**Alternatives rejected** (decided 2026-10-19, Zach; see Questionables):

- Keep `await` gates only: 16 parked, zero answered.
- Single-step pack templates: two ways to say one thing.
- First-class `--skill` and native per-brain skill rendering: adds framework
  code, and the read-by-path pattern already works in production.
- `pr-closed` as a skill: a model session on every closed PR, about 3 a day
  per team.
- A one-release deprecation warning: house shims outlive their windows.

## Relevant files

### Existing (verified 2026-10-19)

- `bobi/workflow/orchestrator.py` (2,078). Multi-step-only parts to delete:
  - `remind_workflow` 115-155, `try_resume_for_event` 205-253 (docstring: no
    production caller), `resume_workflow` 497-623.
  - Checkpoint adoption 391-427, `_exhaust_step`/`_checkpoint` 1070-1114.
  - Route/action/notify/await branches 1134-1273; agent/model switching
    between steps 1296-1412.
  - Handoff retry and capture 1589-1641, `brief_undelivered` 1658-1676.
  - Native-action registry and notify 1823-2040 (registry 1943-1951); handoff
    read/validate 2047-2078.
- `bobi/workflow/schema.py` (300): `GATE_VERDICT*`, `HandoffContract`,
  route/await/notify/action fields, `steps_fingerprint`, back-edge validation.
- `bobi/workflow/state.py` (269): `claim` 98-147, `close` 150-176,
  `find_waiting` 179-203, `.resuming.json` handling 65-68; fields
  `suspended_at_step`, `await_event`, `checkpoint_step`,
  `checkpoint_fingerprint` (multi-step only); `variable_scopes`, `cwd`,
  `resumed_at` (mixed).
- `bobi/workflow/variables.py` (201): keep `resolve` (31); delete the condition
  parser (77-201).
- `bobi/workflow/cleanup.py` (255): `pr_merge_state`, `cleanup_worktree`. Kept;
  the reactor calls them.
- `bobi/workflow/triggers.py` (116): `builtin_adhoc_workflow`. Kept.
- `bobi/events/reactor.py` (438): `AutoDispatchRule` (33-53), `_dispatch`
  295-363 (builds `input_fields` incl. `pr_number`, all event fields),
  `_build_task` 397-404.
- `bobi/cli.py`:
  - `GATE_VERDICTS` import 26-29.
  - `workflows resume` 3000-3099; `workflows status` await fields 2992-2995.
  - `_require_step_roles` 4127-4159; `--wait` suspended branch 4365-4372.
  - `-w required=True` at 4001, guard at 4206.
- `bobi/subagent.py`: `validate_workflow_inputs` 876-901 (call 1345); `_admit`
  waiting/resuming branches 1490-1562; `collect["suspended"]` 1659-1700.
- `bobi/webapp/`:
  - `run_actions.py` (185, all gate).
  - `server.py` resume/remind/close routes 306-335.
  - `runtime.py` abstract `resume_run`/`remind_run`/`close_run` 867-907 and
    local delegation 2155-2178; `event_bus.py` 471-486.
  - `runs.py` `AWAITING_ACTION` 32-53, 172-187, 232-237, 526-532.
  - `static/views/composer.js`; `static/views/agent.js` awaiting tab, Close
    button 634-707, gate composer 815-945, detail lines 1024-1026.
- `bobi/supervisor/admin.py` `ADMIN_COMMANDS` 60-67, `_RUN_ACTIONS` 80,
  `_run_action` 740-772; `bobi/supervisor/snapshot.py:36`
  `SUPERVISOR_VERSION = "0.4.0"`.
- `event-server/worker/src/fleet.ts` 370-372: the same three verbs.
  `tests/test_admin_command_parity.py` keeps the two lists equal.
- `bobi/setup/`:
  - `authoring.py` `build_adhoc_yaml` 311-318, `_workflow_steps` 402-430,
    `build_workflow_yaml` 433-446, `build_automation_workflow_yaml` 455-490,
    file list 699-728.
  - `actions.py` 189-201 (requires `adhoc.yaml`).
  - `digestion.py` hitl 75/109/143-145/258-269; `automate.py:31`.
  - `webui/static/app.js` "Human gate" 807-810, "ask first · waits for
    approval" 1947.
- `bobi/validate.py` `_check_workflow_effort` 407-445, auto_dispatch target 209.
- `bobi/metrics/runtime.py:1102` `observe_workflow_step`; keep the table, only
  `prompt` rows remain.
- `bobi/launch_lineage.py` 70-71 (resume note); `bobi/reconcile.py` 52-56.
- `agents/eng-team/`:
  - `workflows/` (7 files).
  - `agent.yaml` `auto_dispatch` (`pr-feedback`, `issue-lifecycle`,
    `pr-closed` rules).
  - `roles/director/ROLE.md` routing table 114-123 and launch examples 151-214.
  - `roles/engineer/ROLE.md` handoff-file text 102-116, 460-591, pr-closed 345.
- `agents/personal-assistant/`: `workflows/` (3); `roles/assistant/ROLE.md`
  13-77, 149, 170.
- `agents/registry.yaml`: pack versions.
- Docs:
  - `docs/WORKFLOW_ENGINE.md` (587), `docs/RUN_RESUME.md` (172),
    `docs/specs/987-awaiting-action-reply-box.md`.
  - `docs/RUNS_VIEW.md`, `docs/RUN_DRILLDOWNS.md`, `docs/ADMIN_PROTOCOL.md`
    (run-command section 344-375, error codes 164-178).
  - `skills/create-agent.md` 271-289 and its `agent.md` template, `skills/bobi.md`.
  - `README.md`, `DESIGN.md`, `CLAUDE.md` (reference-doc list and design rule 2).
  - `docs/design-system/SKILL.md:24` (rule 2) and
    `components/product/GateApproval.*`.
- moda-agents (`origin/main` 9ad91f5):
  - `agents/baohua/`: `workflows/` (4); `agent.yaml` `auto_dispatch`
    `marketing.due → weekly-marketing`; `roles/*/ROLE.md`;
    `context/org-chart.md`; `agent.md`.
  - `agents/moda-eng-team/`: `workflows/plan-execute.yaml`;
    `roles/director/ROLE.md` 74-94.
  - `agents/{gtm-team,market-research,support-manager}/workflows/` (12 + 5 + 3).
  - `agents/roadmap-pm/agent.yaml`: the reference pattern.

### New

- `agents/eng-team/context/skills/{issue-lifecycle,build-failure,merge-conflict,pr-feedback}/SKILL.md`.
  These replace the workflow files and carry their prompts.
- `agents/personal-assistant/context/skills/{daily-briefing,request}/SKILL.md`.
- moda-agents `agents/baohua/context/skills/{report-investigation,weekly-marketing,prd-draft,prd-design}/SKILL.md`.

No new framework module: `action:` dispatch extends `reactor.py`, and the
native-action registry moves next to its functions in `bobi/workflow/cleanup.py`.

## Questionables

- **Q1:** Delete human approval gates, or keep `await` as the one surviving step
  type? Recommendation: delete; 16 parked, 0 answered, and plan approval
  already happens by PR merge.
  **Decision (2026-10-19, Zach):** delete. Approval becomes skill text (ask in
  the thread, act only on an explicit yes). The console gate composer, the three
  run admin verbs, `docs/RUN_RESUME.md`, and design-system rule 2's gate wording
  go.
- **Q2:** End state: only the builtin `adhoc`, single-step pack templates, or
  first-class skills? Recommendation: builtin `adhoc` only.
  **Decision (2026-10-19, Zach):** only builtin `adhoc`. Packs ship no
  `workflows/`, and behavior is `context/skills/<name>/SKILL.md` named in the
  task. No new framework code. Renaming "workflow" identifiers is deferred.
- **Q3:** Where does `pr-closed`'s worktree cleanup live? Recommendation: the
  reactor runs the native action directly.
  **Decision (2026-10-19, Zach):** a reactor `action:`. Issue closing relies on
  GitHub's `Closes #N`; the Slack notify is dropped.
- **Q4:** The undeployed moda-agents packs and outside users? Recommendation:
  delete those workflows, and give outside packs a hard validate error.
  **Decision (2026-10-19, Zach):** delete the 20 workflow files from
  `gtm-team`, `market-research` and `support-manager` with a README note.
  Outside packs get one explicit `bobi validate` error that links the migration
  section. No deprecation window.
- **Q5:** Re-run #852's Phase 4 A/B trial (baseline, induced death, stop
  criteria) before deleting? Recommendation: no.
  **Decision (2026-10-19, Claude, decide-and-record):** no. What changed since
  #852:
  - Production already prefers `adhoc` (100 of ~110 eng-team runs in 30 days).
  - roadmap-pm runs on adhoc plus a skill.
  - The gate the trial would have protected is unused.

  Replaced by: the claude-leg e2e in Phase 1, and the post-roll stop criterion
  in the Phase 2 gate.
- **Q6:** How do multi-role flows (baohua `report-investigation`, `prd-design`'s
  engineer step) hand off without role switching? Recommendation: the
  finishing worker launches the next role's adhoc run with its findings in
  `--task`, so nothing blocks the director.
  **Decision (2026-10-19, Claude, decide-and-record):** as recommended. The
  director's former delivery step folds into the last worker, which posts to
  the thread the director's routing rules name.
- **Q7:** The admin protocol promises additive-only changes pre-1.0. Is removing
  three verbs allowed? Recommendation: yes, as the documented breaking-change
  path.
  **Decision (2026-10-19, Claude, decide-and-record):** yes.
  `docs/ADMIN_PROTOCOL.md` allows a breaking change with a `SUPERVISOR_VERSION`
  bump (0.4.0 → 0.5.0) and a migration note; that path is used.
- **Q8:** #852 marked "make `--workflow` optional" `[f] state:not-needed`. Do it
  now? Recommendation: yes.
  **Decision (2026-10-19, Claude, decide-and-record):** yes. With `adhoc` the
  only workflow, a required `-w` is pure ceremony; `-w adhoc` still works.

## Phases

### Phase 1 — bobi-agent: add the replacements, migrate the in-repo packs (nothing deleted)

- [ ] `auto_dispatch` rule key `action: <name>`.
  - Valid only with no `workflow:`/`task:`/`role:`.
  - The reactor calls the registered native action in-process with the rule's
    `input_fields`; no session and no run record.
  - On failure it publishes the existing auto-dispatch failure event
    (`reactor.py` ~386).
  - Move `_NATIVE_ACTIONS`/`_NATIVE_ACTION_INPUTS`/`native_action_required_inputs`
    into `bobi/workflow/cleanup.py`; the orchestrator imports them from there
    until Phase 3.
- [ ] `bobi validate`: an unknown `action:` name or a missing required input
  field is an error.
- [ ] `--workflow/-w` optional, defaulting to `adhoc`, at `cli.py:4001` and the
  guard at 4206 (Q8).
- [ ] eng-team pack:
  - Each prompt in `issue-lifecycle`, `build-failure`, `merge-conflict` and
    `pr-feedback` becomes `context/skills/<name>/SKILL.md`.
  - The issue-lifecycle skill carries the old sequence as prose: setup worktree
    → pickup → spec if needed → **ask for approval in the issue/thread and stop**
    → implement → PR → QA → report.
  - Delete the 7 workflow files. `stall-recovery` is deleted, not ported; it
    never ran (#852 Phase 4 handed it to the liveness work).
- [ ] eng-team `agent.yaml`:
  - The `issues assigned` and `changes_requested` rules become
    `workflow: adhoc`, `role: engineer`, and
    `task: Read package/context/skills/<name>/SKILL.md …` with the event
    fields templated in.
  - The `pull_request closed` rule becomes `action: cleanup_worktree` (Q3).
- [ ] eng-team `roles/director/ROLE.md`:
  - The routing table names skills, not workflows.
  - Launch examples use `--role engineer --task "Read …SKILL.md …"`.
  - An approval reply in a thread routes to a fresh launch with the same `--id`.
- [ ] eng-team `roles/engineer/ROLE.md`: replace the handoff-file sections
  (102-116, 460-591) with "report results in the thread / PR". At 345, cleanup
  is automatic via the reactor action.
- [ ] personal-assistant pack: `daily-briefing` and `request` become skills;
  `ROLE.md` launches adhoc with the skill path; delete `workflows/`.
- [ ] Bump both pack versions and `agents/registry.yaml`.
- [ ] `bobi setup`:
  - `authoring.py` emits `context/skills/<name>/SKILL.md` plus an
    `auto_dispatch` rule to `adhoc`, never `workflows/`. The "ask" leash
    becomes skill text: propose in the thread, act only on an explicit yes.
  - `actions.py` stops requiring `adhoc.yaml`.
  - Update the "Human gate" / "waits for approval" copy in
    `webui/static/app.js` and `DESIGN.md` to match.
- [ ] Docs: `skills/create-agent.md` (pack layout, `agent.md` template "##
  Workflows" → "## Skills", `action:` rules), `skills/bobi.md`, `README.md`,
  `docs/MONITORS.md` (monitor → adhoc + skill example).

**Validation gate**

- [ ] `find agents -type d -name workflows` prints nothing.
- [ ] `for p in agents/eng-team agents/personal-assistant; do bobi validate $p; done`
  passes.
- [ ] Reactor unit test: a `github.pull_request` closed event on an `action:`
  rule removes the worktree and launches **zero** sessions. Assert on the
  session launcher mock, called 0 times. Mutant: route the action rule through
  `launch_agent` → the test fails.
- [ ] Validate unit test: `action: nope` and a rule with both `action:` and
  `workflow:` are errors. Mutant: delete the check → the test fails.
- [ ] `bobi agent X subagents launch --role engineer --task t` (no `-w`)
  launches adhoc; `-w adhoc` still works.
- [ ] e2e `[stub]+[claude]`, **claude leg required**:
  - Install eng-team into an isolated `BOBI_HOME` and publish a
    `github.issues` assigned event.
  - The reactor launches an adhoc engineer run whose task names
    `package/context/skills/issue-lifecycle/SKILL.md`.
  - The claude transcript shows a Read of that path before any other tool
    call.
- [ ] `tests/test_setup_authoring.py`: a fresh authoring with an "ask" automation
  produces a skill plus a rule, and no `workflows/` dir.
- [ ] `pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q`
  and `pytest tests/integration -q` green. The engine tests still pass: nothing
  is deleted in this phase.
- [ ] Released per `docs/RELEASE_RUNBOOK.md` (release N, named here when cut).

### Phase 2 — moda-agents: migrate overlays, delete unused workflows, roll

Depends on Phase 1's release. The undeployed-pack deletion task is independent
and could run in parallel with Phase 1.

- [ ] Pin bobi release N; bump baohua and moda-eng-team `from: eng-team@<new>`.
- [ ] baohua `report-investigation` → support skill.
  - When the triage verdict is not `not-real`, the support worker launches an
    engineer adhoc run with the triage in `--task`.
  - The engineer skill ends by posting the report where the director's rules
    say (Q6).
- [ ] baohua `weekly-marketing` → marketing skill; rule `marketing.due` →
  `workflow: adhoc`, `role: marketing`, `task: Read …SKILL.md …`.
- [ ] baohua `prd-draft` / `prd-design` → pm skills, using the same chained
  launch for the engineer design step.
- [ ] baohua: update `roles/*/ROLE.md`, `context/org-chart.md` and `agent.md`
  to name skills.
- [ ] moda-eng-team: delete `plan-execute.yaml`. The director's "execute plan"
  route becomes an engineer adhoc run whose task says to run the `moda:build`
  skill on the named plan path. "continue plan" routes the same way, with the
  plan's markers as state.
- [ ] Delete `workflows/` in `gtm-team`, `market-research` and
  `support-manager`, with a one-line README note pointing at git history (Q4).
- [ ] Bump every changed pack's version.
- [ ] Close the 16 parked approval runs with the console Close action (still
  present in release N), reason "gates retired —
  plans/2026-10-19-retire-yaml-workflows.md".
- [ ] Deploy with `deploy-agent-teams -f rebuild=true`.

**Validation gate**

- [ ] `git grep -l "" -- 'agents/*/workflows/*'` in moda-agents prints nothing.
- [ ] `bobi validate` passes on every moda-agents pack.
- [ ] Fleet probe (the 2026-10-14 audit script, re-run): no box has
  `run/package/workflows/`, and there are 0 runs with status `waiting`.
- [ ] **Stop criterion, 7 days after the roll.** Each of these produced an
  `adhoc` run that completed, and a human saw its result:
  - eng-team: an issue assignment or Slack-requested unit, and a closed PR
    whose worktree is gone.
  - baohua: a `weekly-marketing`, and a `report-investigation` when a report
    arrives.

  If a migrated flow fails where its workflow previously completed, halt
  Phase 3 and amend this plan.

### Phase 3 — bobi-agent: delete the step machine and the gate surface (load-bearing; strictest gate)

Code may be developed in parallel with Phase 2, but release N+1 is cut only
after Phase 2's gate passes.

- [ ] `orchestrator.py`: delete every multi-step part listed in Relevant files,
  keeping the single-prompt `run_workflow` path, turn-cap resume, drain, the
  honest terminal `finally`, and model routing (drop its checkpoint `_runtime`
  record; keep routing on the launch).
- [ ] `schema.py`:
  - Reduce to `Workflow` (name, trigger, description, period, one `StepDef`:
    prompt, model, effort, max_turns, timeout).
  - `load_workflow` loads only the builtin adhoc.
  - Delete `GATE_VERDICT*`, `HandoffContract` and back-edge validation.
- [ ] `variables.py`: keep `resolve`; delete the condition parser.
- [ ] `state.py`:
  - Delete `claim`/`close`/`find_waiting`/`.resuming.json` and the
    multi-step-only fields.
  - `from_dict` ignores legacy keys.
  - A legacy `waiting`/`resuming` record loads as `cancelled`, error `"approval
    gates removed in bobi <N+1>"`, rewritten with
    `fsutil.atomic_write_json` under `ledger_lock`.
- [ ] `bobi validate` hard errors, both naming the migration section:
  - a pack containing `workflows/`;
  - an `auto_dispatch` `workflow:` other than `adhoc`.
- [ ] `cli.py`:
  - Delete `workflows resume`, the `GATE_VERDICTS` import,
    `_require_step_roles`, and the `--wait` suspended branch.
  - `workflows status` drops the await fields.
- [ ] `subagent.py`:
  - Delete `validate_workflow_inputs` and the `_admit` waiting/resuming
    branches.
  - Delete `collect["suspended"]`.
  - Check whether the legacy phase `_build_prompt` (99-111) has a single-prompt
    caller; delete it if not.
- [ ] Console:
  - Delete `run_actions.py`, the three `server.py` routes, and the
    `runtime.py`/`event_bus.py` run-action methods.
  - Delete the gate mode in `composer.js`, plus the awaiting-action tab, the
    Close button and the gate detail lines in `agent.js`.
  - `runs.py`: delete `AWAITING_ACTION`; waiting no longer exists.
- [ ] Admin protocol:
  - Remove `resume_run`/`remind_run`/`close_run` from **both**
    `bobi/supervisor/admin.py` and `event-server/worker/src/fleet.ts`.
  - Bump `SUPERVISOR_VERSION` to 0.5.0.
  - `docs/ADMIN_PROTOCOL.md`: delete the run-command section and `not_waiting`;
    add a migration note (Q7).
- [ ] `reactor.py`: drop the "so native actions and route conditions can
  resolve" rationale; keep `input_fields` for `task:` templates and `action:`.
- [ ] `metrics`: `observe_workflow_step` writes only `prompt`; keep the schema.
  Clean up the resume notes in `launch_lineage.py` 70-71 and `reconcile.py`
  52-56.
- [ ] Tests:
  - Delete the multi-step-only files: `test_cleanup.py` stays (cleanup.py
    stays); delete `test_workflow_route_resume.py`, `test_webapp_resume.py`,
    `test_webapp_composer.py`, `integration/test_gate_verdict_resume.py`,
    `integration/test_pr_closed_cleanup.py` (replaced by Phase 1's reactor
    test), and the notify-step cases in `test_notify_step.py`. Keep its Slack
    formatter cases if the formatter has another caller; otherwise delete them.
  - Trim the mixed files listed in Notes. Replace the `two-step.yaml`
    integration fixture with a single-step one.
- [ ] Design system:
  - `docs/design-system/SKILL.md` rule 2 and `CLAUDE.md`'s copy: an approval
    request names the action and where to answer it, and is never a toast.
  - Retire `components/product/GateApproval.*`.
- [ ] Docs:
  - Delete `docs/RUN_RESUME.md` and `docs/specs/987-awaiting-action-reply-box.md`.
  - Rewrite `docs/WORKFLOW_ENGINE.md` as a short doc on runs and dispatch:
    adhoc, `task:` templates, `action:` rules, skills, and the migration
    section the validate errors link to.
  - Update `docs/RUNS_VIEW.md`, `docs/RUN_DRILLDOWNS.md`, `docs/SECURITY.md` if
    it names gates, and `CLAUDE.md`'s reference-doc list.

**Validation gate**

- [ ] Absence:
  - `grep -rnE "find_waiting|resume_workflow|remind_workflow|try_resume_for_event|evaluate_condition|HandoffContract|GATE_VERDICT|_execute_notify_step|resume_run|remind_run|close_run" bobi/ event-server/worker/src/`
    prints nothing.
  - `test -e bobi/webapp/run_actions.py` fails.
- [ ] Mutation: the validate test for a pack shipping `workflows/x.yaml` fails
  validation. Mutant: delete the guard → the test fails.
- [ ] Mutation: the validate test for `auto_dispatch` `workflow: issue-lifecycle`
  fails validation. Mutant: delete the guard → the test fails.
- [ ] Mutation: a legacy `waiting` record (fixture copied from the fleet shape
  above) loads as `cancelled` and is rewritten on disk. Mutant: skip the mapping
  → the test sees `waiting`.
- [ ] Mutation: `tests/test_admin_command_parity.py` green. Mutant: re-add
  `close_run` to `fleet.ts` only → it fails.
- [ ] Mutation: `e2e/test_webapp_ui.py` — the agent page renders a legacy
  cancelled run with no Approve/Reject/Close control and no awaiting tab.
  Mutant: restore composer gate mode → the test fails.
- [ ] `wc -l bobi/workflow/*.py` before and after, reported in the PR.
- [ ] `pytest tests/ -q` (full, with Node 20 and `cd event-server && npm ci`) and
  the Worker suite (`event-server`, Node 22) green.
- [ ] Real flow in an isolated `BOBI_HOME` on the release wheel:
  - eng-team installed; a closed-PR event prunes the worktree.
  - An issue-assigned event runs the skill (claude leg).
  - `bobi app` agent page loads.
- [ ] Released per the runbook (release N+1, named here when cut).

### Phase 4 — Roll release N+1 and verify on the fleet

- [ ] moda-agents: pin N+1, roll teams (`-f rebuild=true`), and deploy the
  hosted console separately.
- [ ] Mark `plans/2026-07-26-checklist-execution-model.md` Superseded, pointing
  here (a status-level amendment). Comment on Linear MOD-284.

**Validation gate**

- [ ] Fleet probe:
  - Every pre-existing `waiting` record now reads `cancelled`.
  - Every run started after the roll has `workflow_name == "adhoc"`.
- [ ] `bobi_fleet_status`: every instance is live on N+1, with supervisor
  0.5.0.
- [ ] The hosted console agent page loads for every instance, with no awaiting
  tab and no 4xx/5xx in its network log.

## Proof of work

- **No bug fixes here.** Every change is new behavior or a deletion, so nothing
  is failing-test-first.
- **New behavior gets plain assertions:**
  - the reactor `action:` (0 sessions, worktree gone);
  - `-w` defaulting to adhoc;
  - setup emitting skills;
  - legacy-record mapping.
- **Absence and negative claims get mutation proof:** the engine is gone, packs
  can't ship workflows, no gate control renders, and the verbs are gone from
  both lists. Each names its mutant in the Phase 3 gate.
- **Suites:** unit and integration in every bobi-agent phase. Phase 3 also runs
  the full `pytest tests/ -q`, the Worker suite, and `e2e/test_webapp_ui.py`.
- **Real-Claude e2e is required in Phase 1** (`[stub]+[claude]`). The risk is
  whether a real session follows the skill named in its task, which only the
  real brain proves. Deleting the engine (Phase 3) is brain-agnostic; stub plus
  the real-flow check suffice.
- **Phase 2's stop criterion replaces #852's A/B trial (Q5):** production
  evidence over 7 days, with a halt-and-amend rule.

## Lane map (only if fanned out)

| Lane | Phases | One-line scope | Depends on / lands after | Dispatch issue (if any) | Status |
|---|---|---|---|---|---|
| — | — | — | — | — | — |

## Amendments

## Notes

- **Mixed test files to trim in Phase 3** (multi-step cases only):
  - `test_orchestrator.py`: delete route/await/handoff/resume/QA/gate classes;
    keep single-step, honest terminal, period and worktree.
  - `test_workflow_state.py`: `find_waiting`/`claim`.
  - `integration/test_workflow_orchestrator.py`.
  - `test_cli.py` 288-334, ~679.
  - `test_subagent.py` 1072-1092, 1280, 1619, 1770.
  - `test_webapp_runs.py` 139-203, 380-385.
  - `e2e/test_webapp_ui.py` 2304, 2427-2548.
  - `test_setup_authoring.py` 709, 786.
  - `metrics/test_routing.py` 1432, 1451.
  - `test_eng_team_role_constraints.py`.
  - `test_hosted_single_agent_view.py` 160-198.
- **Ordering constraint:** moda-agents consumes released PyPI bobi. Its overlays
  inherit eng-team's workflows from the wheel, so release N must ship the
  migrated base pack before moda-agents can drop its own workflows. N+1 (the
  hard validate error) must not reach moda-agents before Phase 2 lands.
- **Observed, not investigated:**
  - personal-assistant has not run since 2026-08-27.
  - About 25 non-adhoc and adhoc records on eng-team/baohua read `running`. A
    `/proc` cmdline scan found no process for them, but that scan also missed a
    run that started minutes earlier, so staleness is **unverified**.

  Neither is in scope.
- **Non-goals:**
  - renaming `workflow_name`, `workflows` CLI group, `state/workflow/` paths;
  - native per-brain skill rendering;
  - a `--skill` flag;
  - the run-record staleness question above;
  - roadmap-pm's preview agents.
