# MOD-216 — Weekly recurring scheduled jobs (weekly cron) in bobi

**Status:** DRAFT spec — awaiting human approval. Implementation is HELD until this PR is approved.
**Ticket:** [MOD-216](https://linear.app/moda-labs/issue/MOD-216) — *Enable scheduling of weekly cron jobs in bobi/bobi*
**Type:** Feature · MEDIUM · spec-first
**Revised:** 2026-06-22 per Zach's `changes_requested` review on PR #424 — re-architected to
cleanly separate the **framework** (a generic day-of-week filter) from the **use-case**
(a prose skill, shipped in the agent package, that defines the task). The prep-doc is now
one *example*, not a framework primitive. All §7 decisions are resolved below.

---

## 1. Problem & Solution

### Problem (from the ticket)
> As a user, I want to schedule a weekly Sunday-night job that generates my prep
> doc for the week. I want the scheduler to be able to accommodate this.

The driving use case is a **recurring weekly job** — "every Sunday at 9pm, generate
my prep doc for the upcoming week." Today bobi has a scheduler, but it cannot
express *weekly* (day-of-week) recurrence. That is the one gap the **framework** must
close. *Generating a prep doc* is **not** a framework concern — it is one example of a
task, and tasks belong in a **prose skill** that lives in the agent package.

### Solution (summary) — separate the framework from the use-case

This is the central design correction from review. Two cleanly separated layers:

1. **Framework (bobi core) — a generic, task-agnostic day-of-week filter.**
   Add an optional `days:` field that *gates* the existing `at:`/`tz:` wall-clock
   scheduling to specific weekdays (e.g. Sunday 21:00 America/Los_Angeles). This is the
   **only** change to bobi. It knows nothing about prep docs, sources, outputs, or
   delivery. Any monitor — any schedule trigger, weekly or otherwise — can use it.

2. **Use-case (agent package) — a prose skill that defines the task.**
   The task is expressed as a **skill markdown file that ships in the agent package, not
   in bobi.** The flow:

   1. A monitor fires on its schedule (weekly cadence here, but the pattern works for
      *any* schedule trigger).
   2. The monitor's **`description` references a skill markdown file**.
   3. The consuming agent reads that skill and performs the task it describes.
   4. The skill defines **what the task is** (a weekly roundup / prep doc is the worked
      example), **its sources**, **its outputs**, and **where the output lands + how it is
      delivered** (Slack, workspace file, email, …).

   Monitors are explicitly allowed to **generate content** — they are no longer limited to
   condition detection. A `notify` monitor whose consuming agent produces an artifact is a
   first-class, supported pattern.

The prep-doc job ships as the **worked example** of this pattern (a monitor + a skill in the
agent package), proving the framework change end-to-end without baking any use-case into core.

---

## 2. Existing scheduling primitives (survey)

A survey of what already exists, so we design around it rather than reinventing.

### 2.1 The monitor scheduler — `bobi/monitors/`
The framework's one scheduling primitive. A background thread (`MonitorScheduler`,
`monitors/scheduler.py`) runs inside the manager process, ticks every **30 s**
(`TICK_INTERVAL`), reloads the monitor registry each tick (runtime adds take effect
without a restart), and runs any monitor that is **due**.

A **monitor** (`monitors/schema.py:Monitor`) is a YAML record with these
schedule-relevant fields:

| field | meaning |
|---|---|
| `interval:` | run every N (`30s`, `15m`, `1h`, `2d`) — anchored to `last_run` |
| `at:` | wall-clock times, `"06:00"` or `["06:00","18:00"]` |
| `tz:` | IANA timezone for `at:` (e.g. `America/Los_Angeles`); falls back to host local |

**Due logic** (`scheduler.py`):
- `interval` monitors (`_due`): fire when `now - last_run >= interval_seconds`;
  fire immediately on first sight (no `last_run`).
- `at` monitors (`_due_at` / `_last_scheduled`): compute the most recent scheduled
  wall-clock time at/before `now` **in the monitor's tz**; fire once when that time
  is newer than `last_run`. **Do *not* fire on first sight** — the first tick records
  a baseline so starting the manager at 2pm doesn't retro-fire the 6am slot. Today,
  missed slots during downtime fire **once, late** on the next tick. The new weekly
  filter deliberately does **not** carry that late-fire forward (no catch-up — see §4.2
  and **D8**).

**Monitor flavors** — today all are *condition detectors*; dedup + publish is one shared
path (`_reconcile` → `_fire` → `post_event`). Findings travel through the event
server's topic routing like any other event:
- **`notify: true`** — emits a single condition keyed to the due time (so dedup never
  suppresses it). For scheduled *nudges* — and, under this spec, for scheduled tasks
  whose **consuming agent generates content** (the monitor fires the event; the agent
  does the work). Publishes `{"description": …}` on the monitor's `event` topic.
- **`command:`** — runs a shell command, parses JSON stdout into conditions.
- **`check:`** — a native Python runner in `*_checks.py` (`pr_conflicts`,
  `stale_prs`, `disk_free`).
- **description-only** — launches a short-lived, **non-interactive check agent**
  out-of-band (`bobi agent <name> subagents launch --wait`) that *observes and returns a
  verdict only*; the scheduler converts the verdict to conditions.

> **Note on "monitors generate content":** the monitor record itself still only *fires an
> event* — we are **not** changing the scheduler's internals or adding a new flavor. What
> changes is the contract: the **consuming agent** reacting to a scheduled `notify` event
> may *produce an artifact* (run a skill, write a doc, deliver it), not just record a
> condition. This keeps core task-agnostic while supporting content generation.

### 2.2 Event routing — how a scheduled fire reaches an agent
The scheduler never injects in-process. It `post_event(monitor.event, data)` to the
event server, which routes to subscribers. The manager subscribes to **every
effective monitor's event topic** at startup (`cli.py` →
`monitor_subscription_keys`, `events/subscriptions.py`), so a monitor firing
`monitor/<name>` is delivered to the manager like any external webhook. The agent's
**role prompt** (plus the monitor `description` and the skill it references) decides
what to do with it.

### 2.3 Closest prior art — the shipped `team-status-roundup` default
`run/package/monitors/defaults.yaml` already ships a scheduled-notification monitor
that is *structurally identical* to what we need, minus weekly recurrence:

```yaml
- name: team-status-roundup
  description: >
    Twice-daily status roundup. The director pings every project lead …
  at: ["06:00", "18:00"]
  tz: America/Los_Angeles
  event: monitor/status.roundup_due
  notify: true
```

This is the exact shape of the weekly example: **a `notify` monitor on a wall-clock
schedule whose event an agent reacts to.** The only thing it cannot yet express is
"only on Sundays."

### 2.4 The gap (framework)
- **No day-of-week recurrence.** `at:` fires *every* day at the given time. There is
  no way to say "Sundays only." `interval: 7d` is **not** a substitute: it anchors to
  `last_run` (drifts off "Sunday night"), fires immediately on first sight, and a
  manager restart re-anchors it.
- **CLI can't express wall-clock/weekly scheduling.** `bobi agent <name> monitors add` only
  exposes `--interval`/`--description`/`--event`/`--check`/`--url`. `at:`, `tz:`,
  `notify:`, and (proposed) weekday gating are YAML-edit-only today.

> The "no artifact-producing scheduled job" item from the prior draft is intentionally
> **removed** as a framework gap — producing an artifact is the *agent's* job, driven by a
> skill in the agent package, not a missing framework primitive.

---

## 3. Scope

### In scope
1. **Framework:** a generic, task-agnostic **day-of-week (`days:`) filter** on the
   existing `at:`/`tz:` scheduler. This is the only bobi-core change.
2. **Framework:** **CLI** support (`monitors add --at/--tz/--days/--notify`) to create a
   weekly scheduled monitor without hand-editing YAML.
3. **Framework docs:** monitor-schema reference for `days:` + a short, *use-case-neutral*
   "schedule a weekly job" guide describing the monitor→description→skill pattern.
4. **Agent package (worked example):** the **prep-doc use case** — a `notify` monitor whose
   `description` references a **prep-doc skill markdown shipped in the agent package**. The
   skill defines the task, sources, outputs, and delivery (§5). Nothing prep-doc-specific
   ships in bobi core.
5. **Tests:** unit tests for weekday due-logic (incl. tz/DST and no-catch-up), schema
   parsing, and CLI; a routing test that a scheduled event reaches a subscribed handler.

### Out of scope
- Full crontab expression support (`* * * * *`). See **D1**.
- A new first-class "scheduled-job" monitor flavor. Held the line at the prose-skill
  abstraction instead. See **D2**.
- Sub-day arbitrary recurrence beyond weekday-gated `at:` (e.g. "every other Tuesday",
  "last Friday of the month").
- Per-end-user multi-tenant scheduling (each human scheduling their own private doc). v1 =
  **one configured monitor per project**. See **D6**.
- Catch-up / late-fire for missed weekly runs. v1 ships **no catch-up policy**. See **D8**.
- Any change to the `interval`/`command`/`check`/description-only flavors' semantics, or to
  the scheduler's dedup/publish internals.

---

## 4. Technical approach — framework scheduling layer

### 4.1 Schema: add a generic `days:` field (**D1 = Option A, confirmed**)
Add an optional, **task-agnostic** `days:` field to `Monitor` that **gates** `at:` firing
to specific weekdays. `days:` only has meaning alongside `at:`.

```yaml
- name: weekly-prep-doc
  description: >
    Every Sunday night, run the prep-doc skill (see the prep-doc skill in this
    agent package) to assemble and deliver next week's prep doc.
  at: ["21:00"]
  days: [sun]            # NEW — generic weekday gate; Sunday only
  tz: America/Los_Angeles
  event: monitor/prep.weekly_due
  notify: true
```

- Accepts weekday **names** (`sun`..`sat`, case-insensitive, 3-letter or full) and/or
  **numbers**. Per **D3**, accept *both* numbering conventions — `0` **and** `7` mean
  Sunday — and prefer names (`sun`) in docs/CLI to avoid ambiguity. Parsed by a new
  `parse_days()` in `schema.py`, mirroring `parse_at()`/`parse_interval()` (raises
  `ValueError` on garbage; empty/absent = every day, preserving current behavior).
- Add `days: list` to the `Monitor` dataclass, `_RESERVED`, `from_dict`, `to_dict`,
  and a `weekdays` property returning a `set[int]`.

### 4.2 Scheduler: gate `_due_at` on weekday — **no catch-up** (**D8 = no catch-up**)
`_last_scheduled` already computes the most recent scheduled fire time **in the
monitor's tz**. Extend the due check so a fire only counts when its *local* weekday
is in `days:`:

- In `_last_scheduled` (or a thin wrapper), restrict eligible instants to those whose
  *local* weekday is in `days:` — weekly is a **filter** on which wall-clock instants are
  eligible, reusing all existing tz logic.
- **No catch-up (start simple).** If the manager is down across the scheduled weekly
  instant, that run is **skipped** — there is no late/Monday-morning fire. On first sight
  (and after downtime) the gated monitor records a baseline and fires only at the *next*
  scheduled occurrence. This deliberately does **not** inherit the daily `at:` "fire missed
  slot once, late" behavior; engineering a catch-up policy is deferred (**D8**).
- Empty `days:` ⇒ all weekdays eligible ⇒ behavior identical to today (full
  backward-compat; the shipped daily `team-status-roundup` is **unaffected**, including its
  existing catch-up).
- **DST correctness:** weekday is read from the tz-aware `local = now.astimezone(tz)`,
  so "Sunday 21:00 LA" stays correct across DST. Unit tests pin a fixed `now` across a DST
  boundary.

This is intentionally the **smallest** framework change that satisfies the ticket: weekly =
"`at:` + a weekday filter," reusing existing tz/dedup logic, adding no catch-up.

### 4.3 CLI: expose wall-clock + weekly scheduling
Extend `bobi agent <name> monitors add` with `--at`, `--tz`, `--days`, and `--notify` so a
user can do:

```bash
bobi agent <name> monitors add weekly-prep-doc \
  --at 21:00 --days sun --tz America/Los_Angeles \
  --notify --event monitor/prep.weekly_due \
  --description "Every Sunday night, run the prep-doc skill to assemble and deliver next week's prep doc"
```

Validation reuses `parse_at`/`parse_days`. (`--interval` and `--at` are mutually
exclusive — error if both given.)

---

## 5. Technical approach — the skill-based task boundary (use-case)

This layer ships **in the agent package, not in bobi**. The prep-doc is the worked
**example** of a general pattern.

### 5.1 The pattern (**D2 = skill-based, confirmed; monitors may generate content**)
Hold the line at a **prose-based skill** as the task boundary — *not* a new framework job
flavor. Reuse the `team-status-roundup` path exactly:

1. A `notify` monitor (e.g. `weekly-prep-doc`) fires `monitor/prep.weekly_due` on its
   schedule (here Sunday 21:00 via §4 — but the pattern works for *any* schedule trigger).
2. The manager is already subscribed to every monitor event topic, so it receives the
   event like any webhook.
3. The **monitor `description` references a skill markdown file** in the agent package
   (e.g. *"run the prep-doc skill"*). The agent reads that skill.
4. The agent **executes the skill**, which both *defines* and *performs* the task —
   including generating and delivering content.

**Why this over a first-class job flavor (the rejected D2 Option B):** it keeps bobi
core entirely task-agnostic; the task *logic* lives in a skill + monitor description (easy
to evolve, test, and reuse, and portable with the agent package) rather than in framework
internals; and it matches how scheduled agent work already happens here. The earlier worry
— "a monitor that isn't really monitoring" — is explicitly **accepted**: monitors may
generate content. No new scheduler machinery, no new flavor.

### 5.2 The prep-doc skill (worked example — ships in the agent package)
A skill markdown file packaged with the agent team (**not** bobi core). It is the
single source of truth for the task and owns every use-case decision:

- **Task:** assemble a markdown **prep doc for the week ahead** (the worked example; the
  same pattern hosts any weekly roundup or other scheduled task).
- **Sources (D4 — defined in the skill):** the skill enumerates its own sources — e.g. open
  PRs & issues assigned to the user, upcoming calendar events (via `venn`), Linear tickets
  in progress, recent Slack threads — and which are on by default vs. opt-in. The framework
  has no opinion; **the skill decides.**
- **Outputs & delivery (D5 — defined in the skill):** the skill decides **where the output
  lands and how it is delivered** — e.g. write to a workspace file
  (`workspace/prep-docs/<date>.md`) and/or post to Slack and/or email via `venn`. Per the
  team's rendered-markdown convention, any human-facing markdown ships with a link to the
  rendered version, not a raw blob. **The skill decides; the framework does not.**

### 5.3 Where it ships (**D6 = one per project; D7 = skill-based**)
- The **framework** ships only the generic `days:` capability + CLI + use-case-neutral docs.
- The **skill markdown and the example monitor ship in the agent package** (e.g. the
  eng-team package), installed under `run/package/` like other package
  content. v1 targets **one configured prep-doc monitor per named agent** (agent-level
  monitors, not per-end-user). The use case is delivered **via the skill-based approach** —
  not as a forced framework default and not via a special recipe mechanism: it is simply a
  monitor + skill that the agent package provides and the user can adopt/edit.

> **Open — needs confirmation (new convention introduced by this re-architecture):** agent
> packages today resolve `roles/`, `tools/`, `context/`, `workflows/`, `monitors/`, and
> `workspace/` (see CLAUDE.md), but there is **no established home for a task skill inside
> an agent package**. Proposed: ship it at `agents/<team>/skills/<name>.md`, install it
> under `run/package/skills/`, and reference it by that path from the monitor `description`. The
> exact location and the discovery/resolution mechanism (how the agent locates and invokes
> the referenced skill — plain prose path vs. an installed Claude Code `/skill`) are the two
> genuinely-open points (§7). Flagging rather than silently picking.

---

## 6. Verification plan

**Unit (fast, `tests/`, no live agents):**
- `parse_days()`: names, numbers, mixed, case, whitespace, garbage→`ValueError`,
  empty→every day; **both** Sunday numbers (`0` and `7`) accepted (**D3**).
- `Monitor` round-trip: `from_dict`/`to_dict` preserve `days:`; `weekdays` property.
- `_due_at` weekday gating:
  - fires on the configured weekday at/after the at-time; not on other weekdays;
  - **no catch-up:** manager down across the Sunday slot → the run is **skipped**; fires
    only at the next scheduled occurrence, **not** late (**D8** regression guard);
  - does not double-fire within the same scheduled instant;
  - **DST:** "Sunday 21:00 LA" correct across both DST transitions (fixed `now`);
  - `days: []` ⇒ identical to current daily behavior, **including** existing catch-up
    (regression guard for `team-status-roundup`).
- CLI `monitors add --at/--days/--tz/--notify`: writes expected YAML;
  `--interval`+`--at` together → error.

**Routing / integration:**
- Publishing `monitor/prep.weekly_due` is delivered to a subscribed handler
  (subscription-key + event-routing test, mirroring existing monitor-event tests). The
  example skill's task logic is exercised as the agent-package example, not as a framework
  unit test.

**Manual smoke:**
- Add the weekly monitor, temporarily set `at:` to ~2 min ahead on today's weekday,
  start the manager, confirm one fire + the agent reads the referenced skill and produces
  output.

Run `pytest tests/ --ignore=tests/integration/` before PR.

---

## 7. Design decisions — resolved in review (Zach, PR #424, 2026-06-22)

All eight are resolved. The throughline: **separate framework from use-case** — core gets a
generic day-of-week filter; the task lives in a prose skill in the agent package.

- **D1 — Weekly representation → (A) `days:` weekday filter.** Add a `days:` filter on the
  existing `at:`/`tz:` mechanism — smallest, most readable change; reuses tz logic. Full
  crontab (B) and a `weekly:` sugar field (C) are **rejected** for now.

- **D2 — Job execution model → skill-based (Option A); monitors may generate content.**
  Hold the line at a **prose-based skill** as the task boundary. **No** new first-class
  "scheduled-job" flavor (Option B rejected). The "monitor that produces an artifact"
  concern is explicitly accepted: **monitors may generate content**, not only detect
  conditions.

- **D3 — Weekday numbering → support both.** Accept **both** `0` and `7` for Sunday
  (cron-style and ISO) numerically; prefer names (`sun`) in docs/CLI to avoid ambiguity.

- **D4 — Default sources → defined in the skill.** Source list (open PRs/issues, calendar
  via `venn`, Linear in-progress, recent Slack, etc.) and on-by-default vs. opt-in are
  specified **in the skill**, not the framework.

- **D5 — Delivery surface → the skill decides.** Where the output lands and how it is
  delivered (workspace file, Slack, email via `venn`) is decided **in the skill**.

- **D6 — Single vs. multi-user → one per project.** v1 = **one configured monitor per
  project** (project-level). True per-user weekly docs are deferred.

- **D7 — Default vs. recipe → the skill-based approach.** Ship the generic `days:`
  capability in-framework; ship the task as a **skill + monitor in the agent package**. Not
  a forced framework default and not a bespoke recipe mechanism — the skill-based pattern is
  the delivery mechanism.

- **D8 — Catch-up → none (start simple).** A missed weekly run is **skipped**; the next fire
  is the next scheduled occurrence. No late/Monday-morning catch-up in v1.

### Still open — needs confirmation
These were *created* by the re-architecture and are not yet pinned (flagged, not guessed):

1. **Skill location in the agent package.** Agent packages have no established `skills/`
   home today. Proposed: `agents/<team>/skills/<name>.md` → installed to
   `run/package/skills/`. Needs a yes/where.
2. **Skill discovery / invocation mechanism.** How the consuming agent locates and runs the
   referenced skill — a plain prose path in the monitor `description` that the agent reads,
   vs. an installed Claude Code `/skill` the agent invokes. Affects packaging and the
   monitor `description` wording.

---

## 8. Implementation plan (HELD until spec approval)

**Framework (bobi core) — the only core changes:**
1. **Schema** — `parse_days()` + `days` field + `weekdays` property +
   `from_dict`/`to_dict`/`_RESERVED` (`monitors/schema.py`). Tests first. Accept both
   Sunday numbers (D3).
2. **Scheduler** — weekday gating in `_due_at`/`_last_scheduled`
   (`monitors/scheduler.py`), **no catch-up** for gated monitors (D8). Tests first (incl.
   DST + no-catch-up + `days: []` regression guard).
3. **CLI** — `--at/--tz/--days/--notify` on `monitors add` (`cli.py`) + validation. Tests.
4. **Framework docs** — use-case-neutral "schedule a weekly job" guide + monitor-schema doc
   update for `days:`.

**Agent package (worked example) — no framework coupling:**
5. **Prep-doc skill** — task/sources/outputs/delivery markdown in the agent package
   (location per the §7 open point); sources per D4, delivery per D5.
6. **Example monitor** — a `weekly-prep-doc` `notify` monitor in the agent package whose
   `description` references the skill; routing test for `prep.weekly_due`.

7. `/review`, run unit suite, then open the implementation PR against `main`.

*No code is written until this spec PR is approved. Resolve the two §7 open points before
step 5.*
