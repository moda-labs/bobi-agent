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

## Work plan

Phase 1 — **run model + `GET /api/agents/{name}/runs`** (the enabler)
- [ ] Persist a run record per monitor firing from the scheduler tick:
      monitor name, started/ended, outcome (`notified` | `quiet` |
      `failed` + reason), runner session ref when one was spawned. This is
      the only new runtime write in the plan; today's single overwritten
      `last_run` timestamp cannot back the table.
- [ ] Server-side merge of sessions + workflow runs + monitor runs into one
      run shape (`status`, `title`, `origin`, `started_at`, `duration`,
      `tokens`, `est_cost`, `error`), newest first, with a status filter
      param and an explicit cap + `truncated` flag.
- [ ] Surface per-run token totals and est cost from `model_usage` (stop
      dropping it in the projections).

Phase 2 — **state band backend**
- [ ] Proxy the manager health probe into `LocalRuntime.health_summary`
      (read `run/state/manager-health.port`, call `/health`, fall back to
      the pidfile view) → tri-state `running` / `stopped` /
      `not_responding` with a human-readable detail line.

Phase 3 — **the page** (replaces the current agent view panels + chat)
- [ ] Status band with in-band recovery actions (start/stop/restart
      endpoints already exist).
- [ ] Identity header with SAVED and ABOUT popovers (needs a small
      `GET .../overview` from `Config.load` + `discover_roles` +
      `agent.md`).
- [ ] Runs table with tabs, origin sub-lines, error notes, per-run actions.
- [ ] Transcript slab fed by an enriched transcript source (timestamps +
      tool-call lines; the CLI `transcript show` path / `history.db`
      already have them — `/messages`' role+text projection does not).

Phase 4 — **savings**
- [ ] SAVED chip + popover from `CostSummary` (recorded, estimated,
      `tokens_by_model`) + aggregated script-cache savings from
      `run/state/scripts/*.state.json`.

Verification: this is admin/read-model surface — brain-agnostic, so the
stub-brain e2e suffices per the repo's two-brains criterion; a claude leg is
not required. Frontend changes follow `docs/FRONTEND_QA.md`. Exercise the
real flow with an isolated `BOBI_HOME`: seed sessions/workflow runs/monitor
state, kill the health probe to see `NOT RESPONDING`, stop the agent to see
`STOPPED` + start recovery.

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
