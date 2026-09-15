# Stop scheduled workflows publishing twice: match monitor auto_dispatch rules, key them on the finding, and keep the launch note out of step scope

> **Status:** In review
> **Tracking issue:** moda-labs/bobi-agent#1093 · **Created:** 2026-09-15 · **Last amended:** — (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

One scheduled standup run must produce one Slack post. On Mon 2026-09-14 roadmap-pm posted its 19:00 standup twice from a single run. #1016's 0.58.0 fix removed the pre-step task turn, but engine gaps still let this happen on any scheduled workflow. The plan closes them in the engine and makes roadmap-pm's post idempotent, so a model that runs ahead still leaves one message.

## Problem

Runtime evidence (instance `moda/roadmap-pm`, bobi 0.58.0, codex brain), read 2026-09-15:

- **One event, one run, two posts.**
  - The director transcript holds exactly one `monitor/standup.due` (`finding_key 2026-09-15T02:00:11Z`) and one launch, `standup-2026-09-14-due`.
  - `#bobi-moda` holds two PM Team posts from that run, with different text: ts `1789437875.926229` (19:04:35 PT) and `1789438203.620719` (19:10:03 PT).
- **Post 1 came from step 1.**
  - Worker transcript `wf-daily-standup-roadmap-pm-standup-2026-09-14-due`, msgs 2–25: during `review-yesterday` the agent ran the whole standup and `bobi reply`'d.
  - That step's prompt carried the director's launch note, "Prepare and publish the daily standup … Publish to the team channel", as the context prefix. The step text said "Do not write today's standup here".
- **Post 2 came from `publish-standup`** (msgs 45–52), which has no already-posted check.

Engine gaps, verified on `main` (these files are unchanged since v0.58.0):

1. **`auto_dispatch` never matches a monitor event.**
   - `post_event` strips the source and POSTs `/events/standup.due` with `source: monitor` (`bobi/events/publish.py:112`).
   - The server sets `type: topic`, i.e. `standup.due` (`event-server/core/src/core.ts:308`).
   - `AutoDispatchRule.matches` compares exactly (`bobi/events/reactor.py:51`), so the rule string `monitor/standup.due`, which is the name the monitor publishes under (`docs/MONITORS.md:57`), never matches.
   - Preflight `_unmatchable_reason` returns `""` for any name containing `/` (`bobi/validate.py:148`).
   - Observed: the event reaches the director with no `[AUTO-DISPATCHED …]` annotation, so the LLM launches with free-text task wording.
2. **Monitor events have no stable identity at dispatch.**
   - The monitor posts `{source, payload: {…, monitor, finding_key}}` with no `fields` and no `id` (`bobi/monitors/scheduler.py:1443-1448`, `publish.py:88`).
   - The server mints a random `id` (`core.ts:306`).
   - `dedup_key` therefore falls back to that per-publish id (`reactor.py:79`), and `run_key()` returns `None`, so launches get a random key (`reactor.py:~128`, `_dispatch` :251).
   - Result: a parked-publish retry, or a restart before ack followed by replay, launches a second run. The rule's `cooldown` only collapses redelivery of the same event id.
3. **A failed auto-launch is silent.**
   - `process` returns `"dispatched"` (`reactor.py:236`) and the drain adds `[AUTO-DISPATCHED: workflow launched — no action needed]` (`bobi/events/drain.py:382`) before the launch thread runs.
   - That thread swallows `RuntimeError`, i.e. cap timeout or already-active, at `log.info` (`reactor.py:~300`).
   - Once gap 1 is fixed, a busy slot means a missed standup, with the director told to do nothing.
4. **The launch note is unscoped inside step 1.**
   - `_context_prefix()` (`bobi/workflow/orchestrator.py:691`) is prepended to the first prompt step (`:1190`). No step prompt says which step it is.
   - Imperative launch text competes with the step prompt and can win, as #1016's spec §9 predicted.
5. **`bobi reply` discards the posted message ts.** It prints only `Sent to <conversation>` (`bobi/cli.py:1131-1134`), although the gateway returns `ts`. An agent cannot record what it posted.

Team gaps (moda-agents, `agents/roadmap-pm`):
- `publish-standup` and `publish-sprint-page` always post new.
- The scrum-manager ROLE's Run sequence ends "8. **Publish** … post it to `#bobi-moda`" (`roles/scrum-manager/ROLE.md:~76`). That ROLE is the system prompt for every step.
- The director ROLE routes standups to `subagents launch` (`roles/director/ROLE.md:43`).

## Solution

- **Reactor, source-qualified match.**
  - Exact `type == rule.event` stays first. Ingest events carry `type: "<source>/<type>"` and rely on it (`createIngestEvent`, `core.ts:~966`).
  - Otherwise match when `rule.event == f"{source}/{type}"`.
  - Reserve `monitor`, `agent`, `system` and `inbox` in `INGEST_RESERVED_SOURCES` (`core.ts:1544`), so an ingest token cannot bind a topic that impersonates an internal source.
- **Reactor, finding-keyed identity.**
  - When `payload.finding_key` is present and `fields` has none, derive `dedup_key = f"{workflow}:{source}/{type}:{finding_key}"` and `run_key = f"{monitor}-{finding_key-slug}"`.
  - Copy `finding_key` and `monitor` into `input_fields`.
  - Launch admission refuses a *completed* run for a finding-derived key, mirroring the period rule (`subagent.py:1321-1335`), with `--fresh` as the escape.
- **Reactor, failed launch is loud.** When the launch thread fails, publish `agent/auto_dispatch.failed` (workflow, event type, finding_key, error) to the director. The drain stays non-blocking.
- **`bobi reply` prints the ts:** `Sent to <conversation> (ts=<ts>)` / `Updated <ts> in <conversation>`.
- **Orchestrator, step-aware framing** at the single assembly site (`orchestrator.py:1188-1191`).
  - Order: `[background block] → [step header] → [step prompt] → [handoff contract]`.
  - Header: `` Workflow `<name>` steps: a, [b], c — you are on [b]. Do only this step's instruction; routing may skip or repeat steps. ``
  - Background label, worded as precedence rather than negation: `` Workflow `<name>` background for run `<run_key>` — the launch input and prior handoffs. The step instruction below is what you do now; if it restates the task, do it. ``
  - The turn-cap nudge (`:1245`) and the handoff-fix prompt (`:1288`) get no header.
- **roadmap-pm (second repo, `moda-labs/moda-agents`).**
  - Give both rules a literal context-only `task:`.
  - Director ROLE: act on `AUTO-DISPATCHED` by doing nothing; act on `agent/auto_dispatch.failed` by launching once with `--id` set to the failed run key.
  - Both manager ROLEs: inside a workflow, the step prompt decides which part of the Run sequence you do, and only `publish-*` steps post.
  - `publish-*` steps record the ts printed by `bobi reply` in `$BOBI_PROJECT/run/workspace/<run_key>-slack-ts`. If that file exists, edit in place with `--edit <ts>`.
  - Hard character budget, so an edit is never chunked.

Alternatives rejected:
- *Side-effect gate (`posts: true` step field enforced in `bobi reply`)*: a schema change that breaks every workflow posting from a non-final step until each opts in. Follow-up if Phase 5 still shows an early post.
- *Withhold the launch note from step 1*: removes the trigger, but director-launched and adhoc runs lose their evidence or task.
- *Strict `source == prefix` for slash rules*: breaks existing ingest rules, whose `source` is `ingest`.
- *Marker keyed on PT date*: a manual catch-up and the evening run collide on the same date. Keyed on the run instead.
- *Director polls `subagents list` after `AUTO-DISPATCHED`*: timing-based, and it races a slow launch into a duplicate.
- *`period: daily`*: doesn't catch in-run duplicates, and it buckets in UTC on the fleet (Notes).

## Relevant files

### Existing (verified 2026-09-15)

**bobi-agent**
- `bobi/events/reactor.py`: `matches` (:49–51), `dedup_key` (:79), `run_key` (~:115), `process` (:196–240), `_dispatch` (:251, launch thread ~:290–305), `_build_task` (:314).
- `bobi/subagent.py`: launch admission. Period completed-refusal (:1321–1335) is the pattern to extend to finding keys.
- `bobi/validate.py`: `_EVENT_TYPE_SHAPES` (:139), `_unmatchable_reason` (:148). The comment at :130 cites a nonexistent `tests/test_event_type_shapes.py`; fix it.
- `bobi/events/drain.py`: annotation (:381–382).
- `bobi/monitors/scheduler.py`: publish payload with `finding_key` (:1443–1448).
- `bobi/cli.py`: `_channels_reply_send` output (:1131–1134).
- `event-server/core/src/core.ts`: `createTopicEvent` (:281–318), `INGEST_RESERVED_SOURCES` (:1544).
- `bobi/workflow/orchestrator.py`: `_is_prompt_step` (:680), `_first_prompt_step` (:685), `_context_prefix` (:691), assembly (:1188–1191), turn-cap nudge (:1245), handoff fix (:1288), `_build_step_prompt` (:1672; keep its signature, `tests/test_orchestrator.py:397,407` call it). **Load-bearing: every workflow run goes through here.**
- `tests/test_orchestrator.py`: exact prompt assertions at :933, :1058, :1109, :1174 (`== "publish the page"`), :1187 (`endswith("do the thing")`), :1567.
- `tests/test_reactor.py`, `tests/test_pack_routing_validation.py` (:88 pins `system/` fail-open).
- `tests/integration/test_workflow_orchestrator.py` (`TestConnectIsNeverATurn` :256), `tests/integration/conftest.py` (`dual_brain_env` :401–416, `requires_claude` :325), `tests/integration/test_e2e_event_flow.py`, `tests/integration/test_slack_live.py` (edit test :155; needs `SLACK_BOT_TOKEN` + `SLACK_TEST_CHANNEL`).
- Event-server tests under `event-server/` (the ingest reservation).
- Docs:
  - `skills/create-agent.md` (:100–113) says "match the event type by exact equality"; this must change.
  - `docs/EVENT_SERVER.md` (:216, :306).
  - `docs/MONITORS.md` (:57): add the rule spelling.
  - `docs/WORKFLOW_ENGINE.md` (:426–439): context block wording.
  - `docs/BUILDING_AGENT_TEAMS.md` (:276–286): check consistency.

**moda-agents** (`moda-labs/moda-agents`)
- `agents/roadmap-pm/agent.yaml`: comment claiming routing and cooldown (:128–130, :181–183), `auto_dispatch` (:187–192), `version: 0.6.4`.
- `agents/registry.yaml`: roadmap-pm `"0.6.4"` (:21), lockstep with the agent version.
- `agents/roadmap-pm/roles/director/ROLE.md`: routing (:43), launch guidance (:57–65).
- `agents/roadmap-pm/roles/scrum-manager/ROLE.md`: Run sequence (:~66–76), posting rules (:77–83).
- `agents/roadmap-pm/roles/sprint-planning-manager/ROLE.md`: its run sequence and post rule.
- `agents/roadmap-pm/workflows/daily-standup.yaml`: `publish-standup` (:108).
- `agents/roadmap-pm/workflows/sprint-planning-prep.yaml`: `publish-sprint-page` (:100, post :112).
- `agents/roadmap-pm/workflows/sprint-planning-update.yaml`: replies in Luke's thread (:~67). It must NOT inherit the edit rule.
- `.github/fleet-version`: `BOBI_VERSION=0.58.0`, moved by `version-gate.yml`.
- `.github/workflows/deploy-agent-teams.yml`: redeploys on a `deploy-*` tag or `workflow_dispatch` with `rebuild: true`.

### New

- None. Tests extend existing files.

## Questionables

- **Q:** Where does the fix live?
  **Decision (2026-09-15, Luke):** bobi engine, with the roadmap-pm changes as a named second-repo phase, because every team gets the fix.
- **Q:** How is step 1 kept from executing the launch note? Options: (a) framing / (b) framing plus a side-effect gate / (c) withhold the note.
  **Decision (2026-09-15, Luke):** (a) step-aware framing: no schema change, and it covers every workflow. (b) is the follow-up if Phase 5 fails.
- **Q:** What does publish do when the run already posted?
  **Decision (2026-09-15, Luke):** edit in place. One message, carrying the latest draft.
- **Q:** `period: daily` now?
  **Decision (2026-09-15, Luke):** no; the UTC-bucket hazard is recorded in Notes.
- **Q:** Same-day hotfix?
  **Decision (2026-09-15, Luke):** no.
- **Q:** Restart/retry duplicates once monitor rules match? Options: key on `finding_key` / note as residual.
  **Decision (2026-09-15, Luke):** key dedup and run_key on `finding_key`, and pass it into workflow input. The implementation extends completed-run refusal to finding-derived keys, so a replay after completion is also refused.
- **Q:** Silent missed run when an auto-launch fails? Options: engine alert / director polls / accept.
  **Decision (2026-09-15, Luke):** engine publishes a failure event, and the director launches once on it.
- **Q:** Notion page idempotency?
  **Decision (2026-09-15, Luke):** scope out (Notion is currently out of free blocks, and Phases 1–3 remove the double run). Follow-up in Notes.

## Phases

### Phase 1 — Reactor: match, identify, and surface monitor dispatches

- [ ] Failing test first: rule `monitor/standup.due`, event `{type: "standup.due", source: "monitor", payload: {finding_key, monitor}}` → `"dispatched"`.
- [ ] Extend `matches`: exact `type` first, then `f"{source}/{type}"` when `source` is non-empty.
- [ ] Negative tests: no match for `{type: "standup.due", source: "github"}`, `{type: "other.due", source: "monitor"}`, or no `source`. A GitHub `match:` rule is unchanged.
- [ ] Finding identity: `dedup_key` and `run_key` from `payload.finding_key` when present. Copy `finding_key` and `monitor` into `input_fields`.
- [ ] Admission refuses a completed run whose key is finding-derived (unless `fresh`), mirroring the period branch.
- [ ] Launch-failure event: on a `RuntimeError` or exception in the launch thread, publish `agent/auto_dispatch.failed` with workflow, event type, `finding_key`, run key and error.
- [ ] `_unmatchable_reason`: for `source/type`, validate `type` against `_EVENT_TYPE_SHAPES[source]` only when the dot prefix equals `source`; fail open for unknown sources and `system/*`. Fix the stale test-file comment.
- [ ] Event server: add `monitor`, `agent`, `system` and `inbox` to `INGEST_RESERVED_SOURCES`, with a test.
- [ ] Update `skills/create-agent.md`, `docs/EVENT_SERVER.md`, `docs/MONITORS.md`, and check `docs/BUILDING_AGENT_TEAMS.md`.

**Validation gate**

- [ ] `pytest tests/test_reactor.py tests/test_pack_routing_validation.py tests/test_subagent*.py --timeout=30 -q`: the dispatch test fails on `main` and passes after the change.
- [ ] Mutant: qualified branch drops the source equality. The `source: "github"` test must fail.
- [ ] Mutant: `dedup_key` ignores `finding_key`. A test that processes two events with the same `finding_key` and different `id`s (after clearing the in-memory dict, to simulate a restart) must fail with two launches.
- [ ] Mutant: delete the completed-refusal branch for finding keys. The replay-after-completion test must fail.
- [ ] Mutant: remove the failure publish. The test making `launch_agent` raise `RuntimeError` must fail to observe `agent/auto_dispatch.failed`.
- [ ] Preflight unit asserts: `_unmatchable_reason("monitor/standup.due") == ""`, `("system/x") == ""`, `("github/github.issues.assigned") != ""`, `("monitor/github.issues.assigned") == ""`.
- [ ] Event-server test: an ingest token bound to `monitor/x` is rejected.
- [ ] `pytest tests/integration/test_e2e_event_flow.py -q` green (event routing changed).
- [ ] Unit suite `pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q` green.

### Phase 2 — `bobi reply` prints the posted ts

Independent of Phase 1; could run in parallel.

- [ ] `_channels_reply_send`: print `Sent to <conversation> (ts=<ts>)` when the gateway returns `ts`, and `Updated <ts> in <conversation>` on edit.
- [ ] Update any test asserting the old exact output.

**Validation gate**

- [ ] Unit test with a stubbed gateway returning `ts` asserts the printed ts. It fails on `main`.
- [ ] Live edit on a channel ref (not a thread): `SLACK_BOT_TOKEN=… SLACK_TEST_CHANNEL=C… pytest tests/integration/test_slack_live.py -m live -k edit`, plus a one-off post → printed ts → `--edit <ts>` → one message. With no test-channel creds, mark `[f]` naming who holds `SLACK_TEST_CHANNEL`.

### Phase 3 — Step-aware framing for every prompt step

Independent of Phases 1–2; could run in parallel. **Load-bearing seam: prompt assembly at `orchestrator.py:1188-1191`.**

- [ ] Failing test first: a 3-step stub workflow `a, b, c`. Query 1 has `steps: [a], b, c`; query 2 has `a, [b], c`; the header sits after the background block and before the step prompt.
- [ ] Add a `_step_header(step)` closure next to `_first_prompt_step`; inject it at the assembly site. Do not change `_build_step_prompt`. No header on the turn-cap nudge or the handoff-fix prompt.
- [ ] Relabel `_context_prefix()` with the precedence wording from Solution. Drop the "issue #" wording.
- [ ] Update the exact assertions at `tests/test_orchestrator.py` :933, :1058, :1109, :1174, :1187, :1567, keeping their intent (context rides the first step turn; the adhoc prompt still ends with the task).
- [ ] Stub replay of 9/14: the launch task contains "publish", and a ```` ``` ```` fence plus a forged `steps: …` line. Assert the block's fence is intact and the real header follows the block.
- [ ] Update `docs/WORKFLOW_ENGINE.md` (:426–439).

**Validation gate**

- [ ] `pytest tests/test_orchestrator.py --timeout=30 -q`: the header tests fail on `main` and pass after.
- [ ] Mutant: skip the header after the first step. The query-2 assertion must fail.
- [ ] Mutant: emit the header before the background block. The ordering assertion must fail.
- [ ] Turn accounting unchanged: `turns == queries` (#1016 invariant).
- [ ] Real brain, `TestLaunchNoteScopedToStep` in `tests/integration/test_workflow_orchestrator.py` via `dual_brain_env`:
  - Claude leg: task "Prepare and publish the standup. Publish now.", step 1 "Read README.md and summarize it", step 2 "Create published.txt containing ok". Assert `published.txt` is absent when step 1's turn ends (poll after step 1, or check the step-2 start timestamp against the file's mtime).
  - Adhoc leg: a one-step `${{input.task}}` workflow still performs the task.
  - Stub leg: header ordering in `log.jsonl`.
  - Codex: no env fixture exists; mark `[f]` unless one is added.
- [ ] Full `pytest tests/` green.

### Phase 4 — roadmap-pm: dispatch through the rule, post idempotently (moda-agents)

Depends on Phases 1–2 being in a released bobi. Prompt edits can be written in parallel.

- [ ] `agent.yaml`: a literal `task:` on both rules (e.g. "Scheduled standup fired by monitor standup-due. Background only."). `${{input.*}}` has no date. Correct the routing and cooldown comments (:128–130, :181–183).
- [ ] Director ROLE:
  - `AUTO-DISPATCHED` on a `monitor/*` event means no launch.
  - `agent/auto_dispatch.failed` means launch that workflow once with `--id <run_key from the event>`.
  - Remove standups from the manual-launch routing row, except catch-ups requested by a human.
- [ ] Scrum-manager and sprint-planning-manager ROLEs: "Inside a workflow, the step prompt decides which part of the Run sequence you do; only a `publish-*` step posts to Slack."
- [ ] `publish-standup` and `publish-sprint-page` prompts:
  - Read `$BOBI_PROJECT/run/workspace/${{input.run_key}}-slack-ts` (verify the exact variable and path resolve identically in two runs).
  - If the file exists, `bobi reply slack:T0952RZRZ0X:channel:C0BLMPSEBLK --edit <ts> "…"`. Otherwise post, then write the printed ts.
  - On an edit failure, report it; never post a second message.
  - Hard budget under the Slack chunk limit.
- [ ] Leave `sprint-planning-update.yaml` untouched (thread reply, no marker).
- [ ] Bump roadmap-pm `0.6.4` → `0.6.5` in `agent.yaml` and `agents/registry.yaml`.

**Validation gate**

- [ ] moda-agents lint (`python3 scripts/check-team-pins.py`, `scripts/verify-tool-library.py`, `scripts/check-deploy-compose.py`) green. This is a smoke check only.
- [ ] Preflight on the new bobi release against the pack: `python -c 'from bobi.validate import validate_config; …'` reports no unmatchable rule, and the rules load with `task:`.
- [ ] Two `publish-standup` executions against one marker file in the test channel leave one message (the second an edit).

### Phase 5 — Release, ordered rollout, fleet proof

- [ ] A bobi release containing Phases 1–3 is cut per Release Rules (runbook; not in this plan's PRs).
- [ ] **Ordering (hard):** roadmap-pm 0.6.5 (Phase 4) deploys in the same roll that moves `.github/fleet-version` past 0.58.0, never after. On bobi 0.58.0 the new ROLE text is harmless. On the new bobi without it, the reactor and director both launch.
- [ ] Redeploy: `gh workflow run deploy-agent-teams.yml -f only=roadmap-pm -f rebuild=true` (or a `deploy-*` tag) after the pin moves.

**Validation gate**

- [ ] Director transcript (`bobi_read_transcript`, fleet `moda`, instance `roadmap-pm`) on the next two scheduled standups (Mon/Tue/Thu/Fri): `monitor/standup.due` carries `[AUTO-DISPATCHED: workflow launched — no action needed]`, with no director `subagents launch` for it.
- [ ] Slack `#bobi-moda`, 18:55–20:30 PT on each of those nights: exactly **one** PM Team standup message (Venn `conversations_history` or in-container `bobi read-conversation … --json-output`).
- [ ] Worker transcript (session found via `bobi_instance_detail`; its name derives from `finding_key`): no `bobi reply` before `publish-standup`, or any later post is an `--edit` of the same ts.

## Proof of work

- **Bug claims:** the reactor mismatch, random monitor identity, unscoped step 1, and missing ts each get a failing test first that fails on current `main` (Phases 1–3).
- **Negative claims:** "a github-sourced event never matches", "a replay never launches twice", "a failed launch is never silent", "no step lacks its header", "step 1 does not publish". Each has a mutation proof with its mutant named in the gate.
- **New behavior:** preflight on `source/type`, the ingest reservation, edit in place. Plain assertions.
- **Suites:** `tests/test_reactor.py`, `tests/test_pack_routing_validation.py`, subagent admission tests, `tests/test_orchestrator.py`, event-server tests, `tests/integration/test_e2e_event_flow.py`, `tests/integration/test_workflow_orchestrator.py`, full `pytest tests/`; moda-agents lint plus preflight.
- **E2E judgment:**
  - Routing, identity and ts output are brain-agnostic, so stub and integration tests suffice.
  - Framing's risk lives in the model, so it gets a real-Claude leg (Phase 3) plus fleet observation on codex (Phase 5).

## Lane map (only if fanned out)

| Lane | Phases | One-line scope | Depends on / lands after | Dispatch issue (if any) | Status |
|---|---|---|---|---|---|
| — | — | — | — | — | — |

## Amendments

- None yet.

## Notes

- **Prior art:**
  - #1016: same symptom; the 0.58.0 fix moved the task into step 1. Its draft spec on branch `agent/1016` §9 predicted this residue.
  - #1048: `period` and the WorkflowRun ledger.
  - #814: an earlier exact-type reactor mismatch.
  - #856: cooldown key for non-GitHub sources.
  - #934: silent-miss failure class.
- **Review (2026-09-15, three lenses):** red-team, staff-engineer and implementer findings are folded in above. Rejected: F12's "remaining" wording concern, superseded by listing all steps with the current one marked.
- **Follow-up:** `Workflow.period_run_key` buckets with `time.localtime` (UTC in fleet containers) while monitor `at:` uses `tz`. Derive the bucket in the monitor's tz before any evening-scheduled team adopts `period:`.
- **Follow-up:** Notion page check-then-update in `publish-standup` (Decision above).
- **Follow-up if Phase 5 still shows an early post:** the side-effect gate.
- **Residual:** rules spelled `system/*` or `agent/*` would also start matching. None ship in either repo today.
- **Also seen on 9/14, unrelated:** the Notion workspace is out of free blocks, and Venn approval gates blocked unattended Linear and Notion writes.
