# Stop scheduled workflows publishing twice: match monitor auto_dispatch rules and keep the launch note out of step scope

> **Status:** Draft
> **Tracking issue:** moda-labs/bobi-agent#1093 · **Created:** 2026-09-15 · **Last amended:** — (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

One scheduled standup run must produce one Slack post. On Mon 2026-09-14 roadmap-pm posted its 19:00 standup twice from a single run. #1016's 0.58.0 fix removed the pre-step task turn but left two engine gaps that still let this happen on any scheduled workflow. Close both in the engine, then make roadmap-pm's publish idempotent so a model that runs ahead still leaves one message.

## Problem

Runtime evidence (instance `moda/roadmap-pm`, bobi 0.58.0, codex brain), read 2026-09-15:

- **One event, one run, two posts.** Director transcript holds exactly one `monitor/standup.due` (`finding_key 2026-09-15T02:00:11Z`) and one launch, `standup-2026-09-14-due`. Slack `#bobi-moda` holds two PM Team posts from that run with different text: ts `1789437875.926229` (19:04:35 PT) and `1789438203.620719` (19:10:03 PT).
- **Post 1 came from step 1.** Worker transcript `wf-daily-standup-roadmap-pm-standup-2026-09-14-due`, msgs 2–25: during `review-yesterday` the agent ran the whole standup and `bobi reply`'d. That step's prompt carried the director's launch note, "Prepare and publish the daily standup … Publish to the team channel", as the context prefix. The step text said "Do not write today's standup here". The model followed the launch note instead.
- **Post 2 came from `publish-standup`** (msgs 45–52), which has no already-posted check.

Engine gaps, verified on `main` (no change to these files since v0.58.0):

1. **`auto_dispatch` never matches a monitor event.**
   - `post_event` strips the source and POSTs `/events/standup.due` with `source: monitor` (`bobi/events/publish.py:112`).
   - The server sets `type: topic`, i.e. `standup.due` (`event-server/core/src/core.ts:308`).
   - `AutoDispatchRule.matches` compares exactly: `event.get("type") != self.event` (`bobi/events/reactor.py:51`), and a rule written `monitor/standup.due` never equals it.
   - Startup preflight skips any name containing `/` (`_unmatchable_reason`, `bobi/validate.py:148`), so nothing warns.
   - Docs teach the `monitor/<name>` spelling (`docs/MONITORS.md:57`).
   - Observed effect: the event reaches the director with no `[AUTO-DISPATCHED …]` annotation (0 occurrences in its transcript), so the LLM launches the run itself with free-text task wording, and the rule's `cooldown` never applies.
2. **The launch note is unscoped inside step 1.**
   - `_context_prefix()` (`bobi/workflow/orchestrator.py:691`) labels the task "reference for the instruction that follows" and is prepended to the first prompt step only (`:1190`).
   - No step prompt tells the agent which step it is on or that later steps own later work (`_build_step_prompt`, `:1672`).
   - An imperative launch note therefore competes with the step prompt and can win.
   - #1016's spec predicted this residue: "guarantees no turn precedes step 0, not that the agent can't publish early from a tool-enabled step 1."

Team gap (moda-agents): `publish-standup` and `publish-sprint-page` always post new; nothing records or checks a prior post for the same date.

## Solution

- **Reactor matches source-qualified rules.**
  - `matches` accepts `self.event == type`, OR `self.event == f"{source}/{type}"` when the event's `source` is set.
  - Exact type match stays first, so every existing GitHub rule is unaffected.
  - Preflight stops skipping `/` names: it validates the part after the slash against the source's type shapes, and still fails open for unknown sources.
- **Step-aware prompt framing.**
  - Every prompt step's prompt gets a step header: `Workflow <name> — step "<step>"; remaining prompt steps: <names>. Do only this step's instruction; later steps own their own work.`
  - The context prefix is relabelled as background: the launch task and prior handoffs describe the run, never an instruction to execute now.
  - Remaining steps are listed by name, not counted, because route steps can loop.
  - No YAML change, so all workflows get it.
- **roadmap-pm (second repo, `moda-labs/moda-agents`).**
  - Give both `auto_dispatch` rules a context-only `task:`.
  - Director ROLE: an `AUTO-DISPATCHED` event needs no launch.
  - Scrum-manager and sprint-planning-manager ROLEs: after any successful standup or sprint post, record the ts in a workspace marker. Before any post, check the marker (fall back to reading the channel) and edit in place with `bobi reply … --edit <ts>` instead of posting again.

Alternatives rejected:
- *Side-effect gate (`posts: true` step field enforced in `bobi reply`)*: structural, but a schema change that breaks every workflow posting from a non-final step until each opts in. Deferred (Decision below).
- *Withhold the launch note from step 1*: removes the trigger, but steps lose the director's gathered evidence.
- *Rename the rule to `event: standup.due` in moda-agents only*: works around gap 1 without fixing it; every other pack still writes the documented `monitor/` spelling.
- *`period: daily` on the standup workflows*: doesn't catch a duplicate inside one run, and its bucket is container-local time, which is UTC on the fleet. Monday 19:00 PT keys as `…-2026-09-15`, so a Tuesday-morning manual rerun is refused as the same period. Out of scope; recorded in Notes.

## Relevant files

### Existing (verified 2026-09-15)

**bobi-agent**
- `bobi/events/reactor.py`: `AutoDispatchRule.matches` (:49–51), the exact-type comparison to extend. `dedup_key` (:79) and `_build_task` (:314) stay unchanged.
- `bobi/validate.py`: `_unmatchable_reason` (:148), the `/` early return to replace with source-qualified validation.
- `bobi/events/client.py`: `format_event_for_manager` (:80) already renders `source/type`. Reference only.
- `bobi/workflow/orchestrator.py`: `_is_prompt_step` (:680), `_context_prefix` (:691), prefix injection (:1190), `_build_step_prompt` (:1672). **Load-bearing: every workflow run in the fleet goes through here.**
- `tests/test_reactor.py`: reactor tests (GitHub types only today).
- `tests/test_orchestrator.py`: asserts the literal `Workflow \`t\` context` at :933, :1058, :1109, :1567. These break if the prefix wording changes, so update them in the same phase.
- `tests/test_pack_routing_validation.py`: pins `_EVENT_TYPE_SHAPES` to the adapters.
- `docs/MONITORS.md` (:57), `docs/EVENT_SERVER.md` (:216): document the rule spelling.

**moda-agents** (`/Users/lukelin/Documents/Moda Labs/Github Repos/moda-agents`)
- `agents/roadmap-pm/agent.yaml`: `auto_dispatch` (:186–192), two `monitor/…` rules with no `task:`. `version: 0.6.4`.
- `agents/registry.yaml`: roadmap-pm `version: "0.6.4"` (:21). Moves in lockstep with the agent.yaml version.
- `agents/roadmap-pm/roles/director/ROLE.md`: routing table (:43) and launch guidance (:57–65).
- `agents/roadmap-pm/roles/scrum-manager/ROLE.md`: "Where the standup gets posted" (:77–83).
- `agents/roadmap-pm/roles/sprint-planning-manager/ROLE.md`: its Slack post rule.
- `agents/roadmap-pm/workflows/daily-standup.yaml`: `publish-standup` (:108).
- `agents/roadmap-pm/workflows/sprint-planning-prep.yaml`: `publish-sprint-page` (:100, post at :112).
- `.github/fleet-version`: `BOBI_VERSION=0.58.0`. Rewritten by `version-gate.yml` after a release clears the canary. Not edited by this plan.

### New

- None. Tests extend existing files.

## Questionables

- **Q:** Where does the fix live? Options: (a) moda-agents workarounds plus a bobi issue / (b) bobi engine fix. Recommendation was (a), for speed.
  **Decision (2026-09-15, Luke):** (b) engine fix, with the roadmap-pm changes as a named second-repo phase. Every team gets the fix, not just one pack.
- **Q:** How is step 1 kept from executing the launch note? Options: (a) step-aware framing / (b) framing plus a side-effect gate / (c) withhold the note from step 1. Recommendation: (a).
  **Decision (2026-09-15, Luke):** (a). No schema change, and it applies to every workflow. (b) stays a follow-up if (a) proves insufficient on the fleet.
- **Q:** What does publish do when the same period was already posted? Options: (a) edit in place / (b) skip / (c) thread correction. Recommendation: (a).
  **Decision (2026-09-15, Luke):** (a). One message, carrying the latest and most complete draft.
- **Q:** Add `period: daily` now? Recommendation: no.
  **Decision (2026-09-15, Luke):** Leave it out and record the UTC-bucket hazard in Notes. It doesn't catch the observed in-run duplicate.
- **Q:** Same-day hotfix before the plan lands?
  **Decision (2026-09-15, Luke):** No. Accept a possible duplicate on the interim standup nights.

## Phases

### Phase 1 — Reactor matches source-qualified `auto_dispatch` rules

- [ ] Write the failing reactor test first: rule `event: monitor/standup.due`, event `{type: "standup.due", source: "monitor"}` → `process` returns `"dispatched"`.
- [ ] Extend `AutoDispatchRule.matches`: exact `type` first, then `f"{source}/{type}"` when `source` is non-empty.
- [ ] Add a negative test: the same rule must not match `{type: "standup.due", source: "github"}`, `{type: "other.due", source: "monitor"}`, or an event with no `source`.
- [ ] Add a regression test that an existing GitHub rule (`github.pull_request` + `match`) still matches exactly as before.
- [ ] Replace `_unmatchable_reason`'s `/` early return: split `source/type`, validate `type` against that source's shape, and fail open for unknown sources and for `system/*`.
- [ ] Update `docs/MONITORS.md` and `docs/EVENT_SERVER.md` to say rules match either `type` or `source/type`.

**Validation gate**

- [ ] `pytest tests/test_reactor.py tests/test_pack_routing_validation.py --timeout=30 -q`: the new dispatch test fails on `main` and passes with the change.
- [ ] Mutant: make the qualified branch compare only the text after `/` (drop the source equality). The `source: "github"` negative test must fail.
- [ ] Preflight: a pack rule `event: monitor/standup.due` produces no warning, and `event: github/github.nope` produces the existing unmatchable warning.
- [ ] `pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q` is green.

### Phase 2 — Step-aware framing for every prompt step

Independent of Phase 1 and could run in parallel. **Load-bearing seam: the orchestrator's prompt assembly.**

- [ ] Write the failing orchestrator test first (stub or recording brain): a 3-step workflow's first query contains the step header naming `step "a"` and remaining steps `b, c`, and its second query names `step "b"` and remaining `c`.
- [ ] Add the header to every prompt step's prompt, both fresh and resumed sessions, including the model-switch fresh session. List remaining **prompt** steps by name (skip route/action/notify/await).
- [ ] Relabel `_context_prefix()`: the launch task and handoffs are background that describe the run; they are not an instruction to execute in this step.
- [ ] Update the four `Workflow \`t\` context` assertions in `tests/test_orchestrator.py` to the new wording, keeping their intent: context rides the first step turn, never a turn of its own.
- [ ] Add an e2e-style stub test replaying the 2026-09-14 shape: a launch task containing "publish", and a step-1 prompt whose query shows the task inside the background block and the step header after it.

**Validation gate**

- [ ] `pytest tests/test_orchestrator.py --timeout=30 -q`: the new header tests fail on `main` and pass with the change.
- [ ] Mutant: skip header injection on non-first steps. The second-query assertion must fail.
- [ ] Mutant: drop the background label from `_context_prefix`. The replay test must fail.
- [ ] Stub turn accounting holds: `turns == queries`, with no added turns (#1016 invariant).
- [ ] `pytest tests/` (full suite, including integration) is green. This touches workflow orchestration, which AGENTS.md says requires integration.
- [ ] Real-brain leg (judgment call per AGENTS.md: the risk lives in model obedience): run the replay workflow on a real Claude brain in an isolated `BOBI_HOME`, with step 1 = "read a file", step 2 = "write `published.txt`", and a launch task saying "publish". Step 1 must not create `published.txt`. Codex leg when authenticated, otherwise record `[f]` with the reason.

### Phase 3 — roadmap-pm: dispatch through the rule, publish idempotently (moda-agents)

Prompt/config work is independent of Phases 1–2. The `task:` and director changes only take effect once a bobi release containing Phase 1 reaches the fleet pin.

- [ ] `agent.yaml` `auto_dispatch`: give both rules a context-only `task:` (e.g. "Scheduled 19:00 America/Los_Angeles standup, fired by monitor standup-due. Background only."), with no imperative verbs.
- [ ] Director ROLE: a `monitor/*` event marked `[AUTO-DISPATCHED …]` needs no `subagents launch`; confirm with `subagents list` and do nothing else.
- [ ] Scrum-manager ROLE, "Where the standup gets posted":
  - After a successful post, write the ts to `workspace/standup-<PT date>-slack-ts`.
  - Before any standup post, if that file exists (or the channel has a PM Team standup since 19:00 PT today), edit it with `bobi reply slack:T0952RZRZ0X:channel:C0BLMPSEBLK --edit <ts> "…"`.
  - This rule holds in every step, not only publish.
- [ ] `daily-standup.yaml` `publish-standup`: same check-then-edit instruction.
- [ ] Sprint-planning-manager ROLE and `sprint-planning-prep.yaml` `publish-sprint-page`: same pattern with `workspace/sprint-<PT date>-slack-ts`.
- [ ] Bump roadmap-pm `0.6.4` → `0.6.5` in `agent.yaml` and `agents/registry.yaml` together.

**Validation gate**

- [ ] moda-agents lint gates (`.github/workflows/lint.yml`) green on the PR.
- [ ] `bobi reply … --edit <ts>` against a channel ref (no `:thread:`) edits the message in a test channel. If the gateway requires a thread ref, amend the prompt to the working form before merge.
- [ ] Preflight on 0.58.0 still loads the pack (a `task:` on a monitor rule is accepted).

### Phase 4 — Release and fleet proof

- [ ] A bobi release containing Phases 1–2 is cut per Release Rules (runbook-driven, not part of this plan's PRs).
- [ ] `version-gate.yml` moves `.github/fleet-version` past 0.58.0 after the canary answers; roadmap-pm redeploys with 0.6.5.

**Validation gate**

- [ ] The next scheduled standup: the director transcript shows `[AUTO-DISPATCHED: workflow launched — no action needed]` on `monitor/standup.due`, and no director `subagents launch` for it.
- [ ] Two consecutive scheduled standups each leave exactly **one** PM Team message in `#bobi-moda` (conversations.history over 18:55–20:30 PT).
- [ ] The worker transcript for each shows no `bobi reply` before the `publish-standup` step, or, if one happened, the later post is an `--edit` of the same ts.

## Proof of work

- **Bug claims (reactor mismatch, unscoped step 1):** failing test first. Each new test must fail on current `main` and pass with the change (Phase 1, Phase 2).
- **Negative claims ("a github-sourced event never matches a monitor rule", "no step lacks the header", "step 1 does not publish"):** mutation proof. The mutants are named in the Phase 1–2 gates, and each test must fail with its mutant applied.
- **New behavior (preflight validates `source/type`, edit in place):** plain assertion (Phase 1 preflight check, Phase 3 `--edit` check).
- **Suites:** `tests/test_reactor.py`, `tests/test_pack_routing_validation.py`, `tests/test_orchestrator.py`, unit suite, full `pytest tests/` for Phase 2; moda-agents lint.
- **E2E judgment:** the reactor is event routing, so the stub alone suffices. The framing's risk is the real brain, so it gets a real-Claude leg (Phase 2) plus fleet observation (Phase 4), which is the only codex proof available.

## Lane map (only if fanned out)

| Lane | Phases | One-line scope | Depends on / lands after | Dispatch issue (if any) | Status |
|---|---|---|---|---|---|
| — | — | — | — | — | — |

## Amendments

- None yet.

## Notes

- Prior art: #1016 (same symptom; the 0.58.0 fix moved the task into step 1), draft spec on branch `agent/1016` §9 (scenario matrix; predicted this residue), #1048 (`period` + WorkflowRun ledger), #814 (earlier exact-type reactor mismatch), #856 (cooldown key for non-GitHub sources).
- Follow-up, not in scope: `Workflow.period_run_key` buckets with `time.localtime`, which is UTC in fleet containers, while monitor `at:` uses `tz`. Derive the bucket in the monitor's timezone before any team adopts `period:` on an evening schedule.
- Follow-up if Phase 4 still shows an early post: the side-effect gate (Decision above, option b).
- Also observed in the 9/14 run, unrelated: Notion rejected the page (workspace out of free blocks), and Venn approval gates blocked unattended Linear/Notion writes.
