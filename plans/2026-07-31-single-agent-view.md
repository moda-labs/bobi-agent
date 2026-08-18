# Single-agent view: state, runs, and savings

> **Status:** Locked (design approved 2026-07-31)
> **Tracking issue:** moda-labs/bobi-agent#887 · **Linear:** MOD-261 · **Created:** 2026-07-31 · **Last amended:** — (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

Redesign the `#/agents/<name>` page in the unified web app around the two
things an agent steward actually opens it for, in order:

1. **Is the agent running — and if not, recover it in one click.**
2. **What failed** (crashed jobs, failed monitor runs, stalled workflows).

Everything else on the page serves debugging those runs (per-run tokens and
hypothetical cost) or tells the value story (what this team's work would have
cost at API list price). The design was prototyped and iterated against these
use cases; the end state is deliberately small: **a status band, an identity
header, and one runs table.** The current page's five panels (needs-attention,
health, spend, roster, session log) plus the chat column collapse into that.

## Problem

Verified against the working tree 2026-07-31:

- **Agent state is weakly derived and can't say "not responding."** The
  dashboard card's `running` is pidfile presence (`webapp/runtime.py:242`);
  `LocalRuntime.health_summary` hardcodes `idle_seconds: None` and
  `reachability: "live"` (`runtime.py:531`, `:565`). The manager health probe
  (`manager_health.py:151` — `/health`, `/ready`, server-derived
  `idle_seconds`) is not consumed by the web app, so a zombie manager
  (process alive, probe failing) renders as healthy.
- **The agent's autonomous work has no HTTP surface.** Monitors
  (`MonitorRegistry`, `run/state/monitor_state.json`) and workflow runs
  (`run/state/workflow/runs/*.json`: `status`, `suspended_at_step`,
  `await_event`) are CLI-only. Only sessions are served
  (`/api/agents/{name}/sessions`).
- **There is no run history for monitors at all.** A firing overwrites a
  single `last_run` timestamp (`monitors/scheduler.py:775`); outcome exists
  only as an ephemeral `system/monitor.error` bus event. A monitor that ran
  and posted, ran and stayed quiet, or failed are indistinguishable after the
  fact.
- **Token/cost detail is captured but dropped.** Per-session `model_usage`
  (input/output/cached tokens per `provider:model`, `sdk.py:245-249`) never
  reaches the session projections (`runtime.py:267`, `:294`). The
  `/messages` endpoint returns `{role, text}` only (`chat_history.py:157`)
  though `history.db` indexes timestamps and tool calls.
- **Spend is framed as cost, not value.** Under subscription auth the
  recorded cost is ~$0 and the fold-time list-price estimate
  (`costs.py:73`, `estimated_cost_usd`) is the real story — "this is what
  the team's work would have cost" — but the UI presents a spend number.

## Design

One page, three elements, in the setup design language (`DESIGN.md`: warm
chrome, one dark slab, amber accent, mono labels, system fonts, no build
step).

**Prototype — the visual spec for Phase 3:**
`plans/2026-07-31-single-agent-view.prototype.html`, a self-contained
static HTML file (vanilla HTML/CSS/JS, system fonts — the same constraints
as the real `bobi/webapp/static/` UI, so styles port directly). Open it in
a browser; the dark **PROTO · STATE** pill (bottom-right) previews the
three header states and is prototype-only chrome. Tabs, hover popovers,
and the row-click transcript slab are interactive; all data is static
sample data, and flipping states does not rewrite the static timestamps
below the strip. Where this document and the prototype disagree on visual
detail, the prototype wins; on scope and data semantics, this document
wins.

**1. Status strip** — the top edge of the header card is a dark
"ship's-computer" instrument strip (the design language's one dark surface
family — a status readout is the machine reporting its own vitals): a
phosphor status lamp + state word, then labeled telemetry segments in mono
(tiny eyebrow label over value, hairline-separated), with the recovery
action on the strip. State is the first read on the page.

- `RUNNING`: green phosphor lamp + glow; glowing amber top edge; segments
  UPTIME · MANAGER PID · LIVE RUNS · LAST ACTIVITY; quiet Restart/Stop.
- `STOPPED`: hollow lamp, muted state word, **no** amber edge (the screen
  is powered down); segments SINCE · EXIT · WAS UP; one primary
  **▸ Start agent** button.
- `NOT RESPONDING`: red lamp + glow; segments carry the diagnosis
  (MANAGER PID `N · alive` · HEALTH PROBE `failing 12m` · LAST ACTIVITY);
  primary **Restart agent** button.

Amber marks only the primary recovery action; state is always carried by
green/red/neutral so the accent never sends a status signal.

**2. Identity summary** — below the strip: name, one-line description
(from `agent.md`), and two hover popovers: **SAVED** (see Savings below)
and **ABOUT** (roles, chat channel, services, automation counts,
brain/model, spend cap — read-only). **Edit design** is a link into the
setup editor for this team (`/setup/<slot>`).

**3. Runs table** — everything the agent does, one table, newest first,
live runs at top. Tabs cycle **ALL / RUNNING / FAILED** (status, not type).
Per row:

- **Status chip:** `RUNNING` (live green) · `IDLE` (manager) · `DONE`
  (quiet gray) · `FAILED`/`CRASHED` (red) · `STALLED` (hollow red; a
  suspended workflow awaiting an event — grouped under the Failed tab
  because it needs a human).
- **Run cell:** title; an origin sub-line in faint mono — *what kicked it
  off* is metadata, not navigation (`monitor · every 15m`, `workflow · on
  github/issue.opened`, `review-worker · dispatched by manager`); an
  outcome/error note (`posted digest to #editorial`, `spawn-failed: claude
  CLI exited before first turn`, `exit 137`).
- **When** (start · duration) and **Tokens · est cost** — the debugging
  currency, on every row.
- **One action:** Open / Transcript / Details / Resume (stalled runs only).
- **Row click → transcript in a dark slab**: timestamped lines including
  tool calls, with duration/tokens/cost in the slab header.

**Savings framing.** The header chip reads `SAVED ⌁ ~$X · N runs`; its
popover: est. API list-price value of tokens used, paid via subscription
($0 recorded), script-cache runner savings, total saved, tokens by model.
Maps directly onto `CostSummary`'s recorded-vs-estimated split; estimates
never present as a bill.

**Decisions and rejected alternatives** (from the prototype iterations):

- *One runs table, status tabs* — an earlier draft had a type-filtered
  activity feed plus a separate automations board plus an attention strip;
  stress-testing the real flows showed type is not a navigation axis and
  the panels triple-listed the same objects. Origin became a row sub-line;
  the Failed tab replaced the attention strip.
- *Chat column removed* — Slack/CLI are the chat surfaces; the page is for
  observing and recovering.
- *Automations table removed* — schedule/next-due display served flows
  (approval review, surgical pause) judged not real today. The About
  popover keeps automation counts; definitions and pause stay in setup/CLI.
  A failed monitor run still surfaces where it matters: as a failed run.
- *Decisions and raw event deliveries stay CLI-only* (`bobi agent events`)
  — they are log lines, not runs (no status, no cost).
- *No approval Approve/Reject UX, no workflow-trigger pause* — cut with
  their flows; Resume on a stalled run is the only workflow action.

**Design deltas from the implementation audit (2026-07-31, pre-approval):**

- **Session-less runs open a Details slab, not a Transcript.** Cached
  script-cache monitor ticks and spawn-failed monitor runs have no session
  and therefore no transcript. Their row action is **Details**: the run
  record (outcome, reason, timings) plus the monitor definition, in the
  dark slab. The prototype already hints at this ("Details" on the
  spawn-failed row); it is now the rule: rows with a session get
  Transcript, rows without get Details.
- **Start-failure state.** `POST .../start` can 409 with a preflight
  report. The strip renders that report inline under the STOPPED band
  (mono, error hue) instead of failing silently. (Not in the prototype;
  build to the strip's existing visual grammar.)
- **Resume confirms.** `workflows resume` force-resumes the suspended
  step — on an `await: approval` gate that proceeds as if approved.
  Resume therefore asks a one-line native confirm naming the awaited
  event before firing.
- **Edit design is an action, not a static href.** It calls
  `POST /api/setup/open` for the team and navigates to the returned URL
  (the setup mount is per-session; a hardcoded `/setup/<slot>` link is
  not guaranteed to resolve).
- **The PROTO state pill is prototype-only chrome** and is not built.

## Interaction validity audit (2026-07-31)

Every interaction on the page, audited against the working tree. Rule:
an interaction ships only if its backing is **exists** or **planned
below**; anything else was cut (see Design deltas above and Non-goals).

| # | Interaction | Backing | Verdict |
|---|---|---|---|
| 1 | Stop / Restart (strip, running) | `POST .../stop` / `.../restart` exist (`webapp/server.py:183,187`) | **valid** |
| 2 | Start agent (stopped strip) | `POST .../start` exists; 409 → preflight report | **valid** — must render the 409 report (Design deltas) |
| 3 | NOT RESPONDING state + detail | manager health probe exists (`manager_health.py`), not proxied | **valid** — U3 builds the proxy |
| 4 | Strip telemetry segments | registry `SessionEntry` (`started_at`, `last_activity`), pidfile; stopped-state SINCE/EXIT/WAS UP from the last manager terminal record | **valid, best-effort** — a segment with no data is omitted, never faked |
| 5 | SAVED hover popover | `GET .../spend` exists (recorded + estimated + `tokens_by_model`); script-cache savings needs a small fold | **valid** — U4 extends |
| 6 | ABOUT hover popover | `Config.load` / `discover_roles` / `agent.md` on disk; no endpoint | **valid** — U4 adds `GET .../overview` |
| 7 | Edit design | `POST /api/setup/open` exists, returns URL | **valid** — wired as an action (Design deltas) |
| 8 | Tabs ALL/RUNNING/FAILED + counts | `GET .../runs` (U2) | **valid** |
| 9 | Row click → Transcript (session-backed runs) | session transcripts exist (`chat_history.py`, `history.db`); need timestamps + tool lines | **valid** — U5 enriches |
| 10 | Open (live run) → live transcript | same source, polled | **valid** |
| 11 | Row click → Details (session-less runs) | monitor run record (U1) + `MonitorRegistry` definition | **valid** — replaces Transcript for these rows |
| 12 | Resume (stalled workflow run) | `workflows resume` logic exists in CLI; no endpoint | **valid** — U6 adds `POST .../workflows/runs/{id}/resume` + confirm |
| 13 | PROTO state pill | prototype-only | **cut** — not built |
| 14 | Hover popovers on touch | hover-only is dead on touch | **valid** — tap toggles too (CSS `:hover` + click handler) |

## Work plan — stacked units

**Branch strategy.** The plan PR (`plan/single-agent-view` → `main`) lands
first. Implementation stacks on an integration branch
**`feat/single-agent-view`** cut fresh from `main`:

- Each unit below is one dispatch issue (`[single-agent-view] U<n> — …`)
  and one PR **into the integration branch** (small, reviewable, CI-green
  at every step; unit branches `feat/single-agent-view-u<n>`).
- Units are ordered so each is testable the moment it lands: backend
  reads first, page after, wiring last. Rebase the integration branch on
  `main` between units as needed.
- When U8 lands, the integration branch IS the finished page: run the
  Manual QA script below against it. The final
  `feat/single-agent-view` → `main` PR carries the already-reviewed
  commits plus the recorded QA evidence.
- Execution follows `skills/checklist-execution.md`: this section is the
  committed checklist; flip markers per unit inside its PR.

**U1 — monitor run records** *(Phase 1 enabler; the only new runtime write)*
- [x] Scheduler persists a run record per firing: monitor name,
      started/ended, outcome (`notified` | `quiet` | `failed` + reason),
      script-cache mode, runner session ref when one spawned. Failed
      publishes (`pending_publish`) count as not-yet-`notified`.
- [x] Bounded retention (cap per monitor); unit tests over the fold.
- Testable: records appear under `run/state/` for a live monitor tick.

**U2 — unified runs read model + `GET /api/agents/{name}/runs`** *(Phase 1)*
- [x] Merge sessions + workflow runs + U1 monitor records into one run
      shape: `status` (`running|idle|done|failed|crashed|stalled`),
      `title`, `origin`, `started_at`, `duration`, `tokens`, `est_cost`,
      `error`, `session_id?`, `run_id?`. Newest first; `status=` filter;
      explicit cap + `truncated`.
- [x] `stalled` = suspended past threshold (default 24h, constant).
- [x] Per-run tokens/est cost surfaced from `model_usage`.
- Testable: `curl` returns the merged list for a seeded home.

**U3 — health tri-state + strip telemetry** *(Phase 2)*
- [x] `LocalRuntime.health_summary` proxies the manager probe
      (`run/state/manager-health.port` → `GET /health`, fall back to
      pidfile view) → `running` / `stopped` / `not_responding` + detail
      + segments (uptime, live count, last activity; stopped:
      since/exit/was-up).
- Testable: `curl .../health` while stopping the agent / SIGSTOPping the
  manager shows all three states.

**U4 — overview + savings reads** *(Phases 3+4 data)*
- [x] `GET .../overview`: description, roles, chat channel, services,
      automation counts, brain/model, spend cap (`Config.load`,
      `discover_roles`, `agent.md`).
- [x] Spend payload gains aggregated script-cache savings
      (`run/state/scripts/*.state.json`).
- Testable: `curl` both; values match `agent.yaml` / `bobi agent costs`.

**U5 — transcript + details reads** *(Phase 3 data)*
- [x] Transcript endpoint variant with timestamps + tool-call lines
      (reuse the `transcript show` / `history.db` path; keep `/messages`
      untouched for chat compatibility).
- [x] Details payload for session-less runs: run record + monitor
      definition YAML.
- Testable: `curl` a session transcript and a cached-tick's details.

**U6 — runs write action** *(Phase 3 data)*
- [x] `POST .../workflows/runs/{run_id}/resume` (reuse CLI resume logic;
      single-winner claim semantics preserved; 409 when not resumable).
- Testable: resume a seeded suspended run via `curl`; status flips.

**U7 — the page** *(Phase 3 UI — replaces the current agent view)*
- [x] Status strip (three states, glow semantics, preflight-report
      failure state) + start/stop/restart wiring.
- [x] Identity header: SAVED + ABOUT popovers (hover + tap), Edit design
      via `/api/setup/open`.
      (Edit design is cut from scope - see Amendments 2026-08-05.)
- [x] Runs table: tabs + counts, origin sub-lines, outcome/error notes,
      polling (reuse existing 4s/10s + backoff pattern).
- [x] Transcript/Details dark slab (row click; Esc closes).
- [x] Resume button + confirm.
- [x] Remove the replaced panels (needs-attention, health, spend, roster,
      session log, chat column) and their dead endpoints' UI callers.
- Testable: the full page against a live seeded agent.

**U8 — QA seed + docs + polish** *(acceptance enabler)*
- [x] A dev seed script that populates an isolated `BOBI_HOME` with every
      state the page renders: completed/failed/crashed sessions, a live
      run, completed + stalled workflow runs, monitor records in all
      three outcomes, script-cache state, spend history.
- [x] Docs updated in-PR: `README.md` (agent page description),
      `docs/MONITORS.md` (run records), `DESIGN.md` (agent view section
      supersedes the old panel description).
- [x] Empty states: zero runs, zero failures, fresh agent.
      (Per-tab empty copy lives in `agent.js`'s `renderRuns()`.)
- Testable: the Manual QA script passes end-to-end.

Verification bar: brain-agnostic admin/read-model surface — stub-brain
e2e suffices per the repo's two-brains criterion. Frontend work follows
`docs/FRONTEND_QA.md`.

## Manual QA script (final acceptance, run on `feat/single-agent-view`)

Seed via the U8 script into an isolated `BOBI_HOME`, start the agent and
`bobi app`, then verify every audited interaction:

1. **State machine:** page shows RUNNING with real uptime/pid/live-count/
   last-activity → Stop → STOPPED strip (hollow lamp, no amber edge,
   SINCE/EXIT/WAS UP) → Start → RUNNING again → `kill -STOP` the manager
   → NOT RESPONDING (red, probe detail) → Restart recovers.
2. **Preflight failure:** break a required credential → Start → the 409
   report renders under the strip; fix → Start succeeds.
3. **Tabs:** ALL/RUNNING/FAILED counts match the seed; FAILED contains
   the crashed session, the failed monitor run, and the stalled workflow.
4. **Drill-downs:** completed session row → transcript slab with
   timestamps + tool lines; cached monitor row → Details slab
   (definition + outcome); spawn-failed row → Details with reason; live
   row → Open shows a growing transcript.
5. **Resume:** stalled run → Resume → confirm names the awaited event →
   run leaves FAILED, completes or re-waits; a second Resume 409s
   gracefully.
6. **Popovers:** SAVED matches `bobi agent costs` (+ script-cache line);
   ABOUT matches `agent.yaml`/roles; both open on hover and on tap.
7. **Edit design:** lands in the setup editor for this team; Back
   returns to a live page.
   (Cut from scope - see Amendments 2026-08-05; skip this step.)
8. **Empty states:** fresh agent renders sanely (no runs, no failures).
9. **Polling:** leave the page open through a monitor tick — the new run
   appears without reload; stop the app daemon — the page surfaces the
   disconnect rather than freezing silently.

## Non-goals

Time-series cost charts (no data; the savings number is lifetime and says
so) · per-turn latency (not captured) · raw `manager.log` viewer · doctor
panel · transcript search / FTS · KB and memory surfaces · config editing
(setup owns composition) · approval decision UX · workflow-trigger pause ·
decisions/raw events in the UI · event-server administration · fleet
comparison (dashboard's job) · RBAC/audit.

## Open questions

1. **Stalled threshold** — when does a suspended workflow move from
   "waiting" to `STALLED` under the Failed tab? Default 24h, or
   per-workflow config?
2. **Run retention / pagination** — the merge reads everything on disk;
   at what count does the endpoint need real pagination rather than a cap?
3. **"Today" savings bucket** — derivable from session `started_at`; worth
   adding to the chip (`TODAY ~$2 · TOTAL ~$51`) or noise?

## Amendments

— none yet.

*(Both lines above are frozen review surface: the header's `Last amended`
and this placeholder predate the amendments below, and the plan-artifact
check is insertion-only, so the first amendment lands beneath the
placeholder rather than replacing it. Last amended: **2026-08-01**.)*

- **2026-08-01** (U2 build session): **row identity is three fields, not
  two.** The U2 row shape above lists `session_id?` / `run_id?`; the built
  row carries a third, `key` (`session:<name>` / `workflow:<run_id>` /
  `monitor:<run_id>`). `key` is stable UI row identity across polls and is
  never a handle for opening anything; `session_id` and `run_id` say what a
  row can actually open — a transcript, a run's details, both, or neither.
  The "neither" case is real and load-bearing: a `$0` script-cache monitor
  tick spawned no session, so its only drill-down is the Details slab (U5).
  Contract documented in `docs/RUNS_VIEW.md`.
- **2026-08-01** (U2 build session): **open question 1 answered — 24h
  constant, not per-workflow config.** `STALLED_AFTER_SECONDS` is a module
  constant because the threshold encodes a judgement about human attention
  ("nobody is coming back to this today"), not anything about a particular
  workflow; per-workflow config would make the Failed tab mean something
  different per row. The clock runs from the last resume, not the original
  start. Questions 2 (pagination) and 3 (today bucket) stay open.
- **2026-08-01** (U3 build session): **the NOT RESPONDING strip's
  `Health probe: failing 12m` is not built.** The prototype is the visual
  spec, so this is a deliberate deviation, not an omission. A failure
  *duration* requires remembering when the probe first failed; the webapp
  answers each request from disk and holds nothing between them, so any
  number there would start whenever the browser happened to poll — a
  fabricated figure in the one place the page is asked to be precise. The
  segment stays qualitative (`no answer on :<port>`) and LAST ACTIVITY, a
  real recorded fact, carries the "how long has this been wrong" signal.
  Contract documented in `docs/AGENT_STATE.md`.
- **2026-08-01** (U4 build session): **script-cache savings are priced per
  monitor, and not priced at all without a basis.** The Savings framing
  above lists "script-cache runner savings" without saying how it is
  computed; the built fold uses each monitor's own arithmetic (its cached
  ticks × what its own agent ticks cost on average), never a fleet-wide
  blend, which would price a cheap monitor's savings with an expensive
  one's bill. Where a monitor has never paid for an agent tick — the normal
  case under subscription auth, where a tick records $0 — no dollar figure
  is estimated at all, and the payload carries `priced_monitors` so a
  caller can tell "$0 saved" from "nothing could be priced". The cached-run
  count always tells the true story. Contract documented in
  `docs/AGENT_OVERVIEW.md`.
- **2026-08-01** (U5 build session): **the Details slab shows a CURATED
  monitor definition, not the whole one.** U5 says "run record + monitor
  definition YAML"; the built payload omits `command`, because a monitor's
  command can carry credentials interpolated into it and this is a
  debugging view, not a config dump. Everything else identifying the
  monitor's intent (schedule, event, description, check, relevance,
  id_field) is carried. Contract documented in `docs/RUN_DRILLDOWNS.md`.
- **2026-08-01** (U6 build session): **resume SPAWNS the CLI command rather
  than reusing its logic in-process, and the claim moved into that
  command.** U6 says "reuse CLI resume logic; single-winner claim semantics
  preserved"; the orchestrator's own docstring rules out the in-process
  reading — `resume_workflow` stamps the registry entry with `os.getpid()`
  and assumes a dedicated per-run process, so resuming inside the web app
  would stamp the web app's pid (and the web app binds no runtime root).
  The endpoint therefore spawns `bobi agent <name> workflows resume
  <run_id>` detached and returns `accepted`, with the page polling the runs
  table. The claim went into the spawned command because a claim held by a
  caller that then fails to spawn strands the run — which also closed a
  real gap: the CLI resume never claimed at all, so two concurrent
  invocations both ran the same run. Contract documented in
  `docs/RUN_RESUME.md`.
- **2026-08-01** (U7 build session): **a session-less WORKFLOW row renders
  its Details slab from the row itself, with no fetch.** The design delta
  "rows with a session get Transcript, rows without get Details" was
  written with monitor rows in mind, and U5's details endpoint serves
  monitor run records only — so a stalled workflow run, which is exactly
  the session-less row a human most needs to open, 404'd. Its row already
  carries the whole story (`await_event`, `suspended_at_step`, `run_key`,
  `repo`, the error), so the slab renders from that. Found by driving the
  real page in a browser, not by a unit test.

- **2026-08-01** (QA session on the integration branch): **one piece of work
  produces one row — the run record claims its session.** U2 says "merge
  sessions + workflow runs + monitor records into one run shape" and the
  built fold merged them without deduplicating, so a monitor firing that
  spawned a check agent, and a workflow run that ran through a session,
  each produced TWO rows: the run's and the session's. They listed the same
  seconds twice, offered the same transcript from two rows, and printed the
  same tokens and estimated cost twice in a column a reader totals by eye —
  exactly the "panels triple-listed the same objects" this design cut. The
  run record now claims its session and the session row is dropped, with the
  claimed session still able to hand up a failure or a still-running status
  its record can be wrong about. Found by reading the seeded table in a
  browser. Contract documented in `docs/RUNS_VIEW.md`.
- **2026-08-01** (QA session on the integration branch): **`stopped` is a
  clean exit, and the shutdown path now stamps when it happened.** The
  STOPPED strip's SINCE · EXIT · WAS UP never rendered: the manager's
  teardown wrote `status="stopped"` with no `terminal_at`, and the strip
  treated that status as non-terminal. Every unit test had seeded
  `completed`, a status that path never writes, so the suite stayed green
  while one of the three specified states was empty in practice. Contract
  documented in `docs/AGENT_STATE.md`.
- **2026-08-05** (post-closure review, MOD-261): **Edit design via
  `/api/setup/open` is cut from U7, not deferred.** The U7 checklist had
  it checked off, and `DESIGN.md` claimed the identity header carried it,
  but neither was ever built — `agent.js` imports only the formatters
  module and never calls `openSetup()`. The Non-goals list already named
  "config editing (setup owns composition)," and the ABOUT card's own copy
  ends "Composition is read-only here," so this checklist line was a
  drafting error rather than an intended, unfinished feature. The single
  entry point out of the page stays `back.href = "#/"`; editing composition
  remains a `#/setup` job. `DESIGN.md` and the Manual QA script are
  corrected in the same change as this amendment.
- **2026-08-18** (#987 / MOD-372, build session): **the page regains a
  typing surface, inside the run modal.** This plan removed the chat column
  with the reason "Slack/CLI are the chat surfaces; the page is for
  observing and recovering", and that reason still holds for a persistent
  panel beside the table - which is not what this adds. The run slab's
  transcript branch gains a **composer**: one reply box, on a modal the
  operator deliberately opened on one run. Approved by Luke on the issue
  after the design was reworked twice on his direction.

  Two things make it narrower than the column that was cut. It is scoped to
  one session the operator is already reading, not a standing conversation
  surface; and it is *recovery*, which is the half of this page's purpose
  the plan kept: a live session can be asked what it is doing, and a
  finished one can be carried forward into a fresh session. The plan's
  "Resume on a stalled run is the only workflow action" is untouched -
  the composer never resumes anything, and cannot: a suspended run records
  the step *after* its gate, so resuming one skips the approval it is
  waiting for. Contracts in `docs/RUN_DRILLDOWNS.md` and `docs/RUNS_VIEW.md`
  (the additive `detail.live` field the composer branches on).
