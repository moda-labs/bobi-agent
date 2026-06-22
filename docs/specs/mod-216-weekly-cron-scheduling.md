# MOD-216 — Weekly recurring scheduled jobs (weekly cron) in modastack

**Status:** DRAFT spec — awaiting human approval. Implementation is HELD until this PR is approved.
**Ticket:** [MOD-216](https://linear.app/moda-labs/issue/MOD-216) — *Enable scheduling of weekly cron jobs in modastack/bobi*
**Type:** Feature · MEDIUM · spec-first

---

## 1. Problem & Solution

### Problem (from the ticket)
> As a user, I want to schedule a weekly Sunday-night job that generates my prep
> doc for the week. I want the scheduler to be able to accommodate this.

The driving use case is a **recurring weekly job** — "every Sunday at 9pm, generate
my prep doc for the upcoming week." Today modastack has a scheduler, but it cannot
express *weekly* (day-of-week) recurrence, and it has no notion of a scheduled job
that *produces an artifact* (as opposed to *detecting a condition*). Both gaps must
be closed.

### Solution (summary)
Two focused additions, built on the **existing monitor scheduler** rather than a new
subsystem:

1. **Scheduling layer:** add **day-of-week gating** to the existing `at:`/`tz:`
   wall-clock scheduling so a monitor can fire on specific weekdays (e.g. Sunday
   21:00 America/Los_Angeles). This is a small, additive change to
   `monitors/schema.py` + `monitors/scheduler.py` + the `monitors add` CLI.
2. **Job execution:** deliver the **weekly prep-doc generator** as a `notify:`
   monitor that publishes a weekly event, consumed by an agent that runs a new
   `prep-doc` skill to assemble and deliver the doc. This reuses the framework's
   existing "monitor fires an event → subscribed agent reacts" path (the same path
   the shipped `team-status-roundup` monitor already uses) — **no new execution
   machinery in the scheduler**.

> **The spec author's recommendation is laid out below; every fork is also listed
> as an explicit Open Decision (§7) for the reviewer.** The biggest fork — "extend
> monitors" vs. "build a first-class job subsystem" — is **D2**.

---

## 2. Existing scheduling primitives (survey)

A survey of what already exists, so we design around it rather than reinventing.

### 2.1 The monitor scheduler — `modastack/monitors/`
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
  a baseline so starting the manager at 2pm doesn't retro-fire the 6am slot.
  Missed slots during downtime fire **once, late** on the next tick (catch-up).

**Monitor flavors** — all are *condition detectors*; dedup + publish is one shared
path (`_reconcile` → `_fire` → `post_event`). Findings travel through the event
server's topic routing like any other event:
- **`notify: true`** — emits a single condition keyed to the due time (so dedup never
  suppresses it). For scheduled *nudges*. Publishes `{"description": …}` on the
  monitor's `event` topic.
- **`command:`** — runs a shell command, parses JSON stdout into conditions.
- **`check:`** — a native Python runner in `*_checks.py` (`pr_conflicts`,
  `stale_prs`, `disk_free`).
- **description-only** — launches a short-lived, **non-interactive check agent**
  out-of-band (`modastack agents launch --wait`) that *observes and returns a
  verdict only*; the scheduler converts the verdict to conditions. **Observe-only by
  contract — not a "do work / produce an artifact" path.**

### 2.2 Event routing — how a scheduled fire reaches an agent
The scheduler never injects in-process. It `post_event(monitor.event, data)` to the
event server, which routes to subscribers. The manager subscribes to **every
effective monitor's event topic** at startup (`cli.py` →
`monitor_subscription_keys`, `events/subscriptions.py`), so a monitor firing
`monitor/<name>` is delivered to the manager like any external webhook. The agent's
**role prompt** decides what to do with it.

### 2.3 Closest prior art — the shipped `team-status-roundup` default
`.modastack/monitors/defaults.yaml` already ships a scheduled-notification monitor
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

This is the exact shape of the weekly prep-doc job: **a `notify` monitor on a
wall-clock schedule whose event an agent reacts to.** The only thing it cannot yet
express is "only on Sundays."

### 2.4 The gap
- **No day-of-week recurrence.** `at:` fires *every* day at the given time. There is
  no way to say "Sundays only." `interval: 7d` is **not** a substitute: it anchors to
  `last_run` (drifts off "Sunday night"), fires immediately on first sight, and a
  manager restart re-anchors it.
- **No artifact-producing scheduled job.** Every flavor is a detector. The
  description-only flavor launches an agent but contractually only *observes*. There
  is no first-class "scheduled job that does work and produces output."
- **CLI can't express any of this.** `modastack monitors add` only exposes
  `--interval`/`--description`/`--event`/`--check`/`--url`. `at:`, `tz:`, `notify:`,
  and (proposed) weekday gating are YAML-edit-only today.

---

## 3. Scope

### In scope
1. **Weekly (day-of-week) recurrence** on the existing `at:`/`tz:` scheduler.
2. **CLI** support to create a weekly scheduled job without hand-editing YAML.
3. **The weekly prep-doc generator**: a `notify` monitor (shipped or user-added) +
   a `prep-doc` skill/role-prompt the consuming agent runs to assemble & deliver the doc.
4. **Docs**: monitor schema reference + a short "schedule a weekly job" guide.
5. **Tests**: unit tests for weekday due-logic (incl. tz/DST and catch-up), schema
   parsing, and CLI; a test that the prep-doc event routes to a handler.

### Out of scope
- Full crontab expression support (`* * * * *`). See **D1**.
- Sub-day arbitrary recurrence beyond weekday-gated `at:` (e.g. "every other
  Tuesday", "last Friday of the month").
- Per-end-user multi-tenant scheduling UI (each human scheduling their own private
  doc). See **D6**. v1 targets a single configured prep-doc job per project.
- The *content depth* of the prep doc beyond a documented, pluggable source list
  (§5.2). See **D4**.
- Any change to the `interval`/`command`/`check` flavors' semantics.

---

## 4. Technical approach — scheduling layer

### 4.1 Schema: add a `days:` field (recommended — **D1 Option A**)
Add an optional `days:` field to `Monitor` that **gates** `at:` firing to specific
weekdays. `days:` only has meaning alongside `at:`.

```yaml
- name: weekly-prep-doc
  description: Generate my prep doc for the upcoming week.
  at: ["21:00"]
  days: [sun]            # NEW — Sunday only
  tz: America/Los_Angeles
  event: monitor/prep.weekly_due
  notify: true
```

- Accepts weekday names (`sun`..`sat`, case-insensitive, 3-letter or full) and/or
  ISO numbers (`0`/`7`=Sunday … `6`=Saturday — exact mapping is **D3**). Parsed by a
  new `parse_days()` in `schema.py`, mirroring `parse_at()`/`parse_interval()`
  (raises `ValueError` on garbage; empty/absent = every day, preserving current
  behavior).
- Add `days: list` to the `Monitor` dataclass, `_RESERVED`, `from_dict`, `to_dict`,
  and a `weekdays` property returning a `set[int]`.

### 4.2 Scheduler: gate `_due_at` on weekday
`_last_scheduled` already computes the most recent scheduled fire time **in the
monitor's tz**. Extend the due check so a fire only counts when its *local* weekday
is in `days:`:

- In `_last_scheduled` (or a thin wrapper), walk back from `now` to the most recent
  `(weekday ∈ days, at-time ≤ now)` instant — not just the most recent at-time. This
  keeps the existing "fire once when scheduled > last_run" + catch-up semantics
  intact; weekly is just a *filter* on which wall-clock instants are eligible.
- Empty `days:` ⇒ all weekdays eligible ⇒ behavior identical to today (full
  backward-compat; the shipped daily `team-status-roundup` is unaffected).
- **DST correctness:** weekday is read from the tz-aware `local = now.astimezone(tz)`,
  so "Sunday 21:00 LA" stays correct across DST. Unit tests will pin a fixed `now`
  across a DST boundary.

This is intentionally the **smallest** change that satisfies the ticket: weekly =
"`at:` + a weekday filter," reusing all existing tz/catch-up/dedup logic.

### 4.3 CLI: expose wall-clock + weekly scheduling
Extend `modastack monitors add` with `--at`, `--tz`, `--days`, and `--notify` so a
user can do:

```bash
modastack monitors add weekly-prep-doc \
  --at 21:00 --days sun --tz America/Los_Angeles \
  --notify --event monitor/prep.weekly_due \
  --description "Generate my prep doc for the upcoming week"
```

Validation reuses `parse_at`/`parse_days`. (`--interval` and `--at` are mutually
exclusive — error if both given.)

---

## 5. Technical approach — the prep-doc job

### 5.1 Execution model (recommended — **D2 Option A: `notify` + agent handler**)
Reuse the `team-status-roundup` pattern exactly:

1. A `notify` monitor (`weekly-prep-doc`) fires `monitor/prep.weekly_due` every
   Sunday 21:00 (via §4).
2. The manager is already subscribed to every monitor event topic, so it receives
   the event like any webhook.
3. A **role-prompt instruction** (or a thin dedicated role) says: *on
   `prep.weekly_due`, run the `/prep-doc` skill and deliver the result.*
4. The agent runs the **`prep-doc` skill** (§5.2), which assembles and delivers the doc.

**Why this over alternatives:** zero new scheduler machinery; the prep-doc *logic*
lives in a skill + prompt (easy to evolve, test, and reuse) rather than in framework
internals; it is consistent with how scheduled agent work already happens in this
codebase. The trade-off — and the reason **D2** exists — is that this is "a monitor
that isn't really monitoring," which slightly stretches the monitor abstraction.
The alternative (a first-class scheduled-**job** flavor that launches an *acting*
agent) is cleaner conceptually but is a materially larger change. See **D2**.

### 5.2 The `prep-doc` skill
A new skill (`skills/prep-doc.md`, also packaged for the eng-team) that:
- Gathers the week's context from a **documented, pluggable source list** — e.g.
  open PRs & issues assigned to the user, upcoming calendar events (via `venn`),
  recent Slack threads, Linear tickets in progress. Exact default sources = **D4**.
- Renders a markdown prep doc for the week ahead.
- **Delivers** it (delivery surface = **D5**): write to a workspace file
  (`workspace/prep-docs/<date>.md`), and/or post to Slack, and/or email via `venn`.
  Per the team's rendered-markdown convention, any human-facing markdown ships with
  a link to the rendered version, not a raw blob.

### 5.3 Where it ships
- The **scheduling-layer** change (`days:`) ships in the framework — it benefits
  every team.
- The **prep-doc monitor + skill** can ship either as an **eng-team** default
  (opinionated, immediately useful for dogfood) or as a **documented recipe** a user
  adds with the CLI. Recommendation: ship the skill in the framework, ship the
  monitor as a **documented opt-in recipe** (not a forced default, since the prep-doc
  content is user-specific). = part of **D6**.

---

## 6. Verification plan

**Unit (fast, `tests/`, no live agents):**
- `parse_days()`: names, numbers, mixed, case, whitespace, garbage→`ValueError`,
  empty→every day; Sunday number mapping (**D3**).
- `Monitor` round-trip: `from_dict`/`to_dict` preserve `days:`; `weekdays` property.
- `_due_at` weekday gating:
  - fires on the configured weekday at/after the at-time; not on other weekdays;
  - **catch-up:** manager down over the Sunday slot → fires once, late, on next tick;
  - does not double-fire within the same scheduled instant;
  - **DST:** "Sunday 21:00 LA" correct across both DST transitions (fixed `now`);
  - `days: []` ⇒ identical to current daily behavior (regression guard for
    `team-status-roundup`).
- CLI `monitors add --at/--days/--tz/--notify`: writes expected YAML;
  `--interval`+`--at` together → error.

**Routing / integration:**
- Publishing `monitor/prep.weekly_due` is delivered to a subscribed handler
  (subscription-key + event-routing test, mirroring existing monitor-event tests).

**Manual smoke:**
- Add the weekly monitor, temporarily set `at:` to ~2 min ahead on today's weekday,
  start the manager, confirm one fire + the prep-doc handler runs and produces output.

Run `pytest tests/ --ignore=tests/integration/` before PR.

---

## 7. Open design decisions (for the reviewer)

- **D1 — Weekly representation.**
  **(A, recommended)** add a `days:` weekday filter on the existing `at:`/`tz:`
  mechanism — smallest change, reuses all tz/catch-up logic, can't express
  sub-weekly cron. **(B)** full crontab `cron: "0 21 * * 0"` — most expressive, but
  a new parser and a second scheduling model competing with `interval`/`at`.
  **(C)** a `weekly:`/`schedule:` sugar field. *Recommend A; adopt B only if we want
  general cron beyond this ticket.*

- **D2 — Job execution model (the big architectural fork).**
  **(A, recommended)** prep-doc as a `notify` monitor + agent handler running a skill
  — no new scheduler machinery, reuses the `team-status-roundup` path; downside: a
  "monitor" that produces an artifact instead of detecting a condition. **(B)** add a
  first-class **scheduled-job** flavor (e.g. `job: true` / `kind: job`) that launches
  an *acting* (not observing) agent and does not reconcile a condition — conceptually
  cleaner, names the thing the ticket actually asks for ("cron **jobs**"), but a
  bigger framework change. *Which abstraction do we want long-term?*

- **D3 — Weekday numbering.** Sunday = `0` (cron-style) or `7` (ISO)? Recommend
  accepting **both** numerically and preferring names (`sun`) in docs/CLI to avoid
  ambiguity.

- **D4 — Default prep-doc sources.** What goes in the doc by default? (open
  PRs/issues, calendar via `venn`, Linear in-progress, recent Slack.) Which are on
  by default vs opt-in?

- **D5 — Delivery surface.** Workspace file, Slack post/DM, and/or email (`venn`)?
  "Generates my prep doc" doesn't say where it lands. Recommend: workspace file +
  Slack link by default; email opt-in.

- **D6 — Single vs. multi-user.** Monitors are **project-level**, not per-end-user.
  v1 = one configured prep-doc job per project (recommended). True per-user weekly
  docs (each human schedules their own, scoped to their identity) is a larger design
  — defer unless required now.

- **D7 — Ship the prep-doc monitor as a default or a recipe?** Forced eng-team
  default vs. documented opt-in `monitors add` recipe. Recommend recipe (content is
  user-specific); ship the *skill* in-framework.

- **D8 — Catch-up policy for a missed weekly run.** Current `at` semantics fire a
  missed slot **once, late**, on the next tick. Confirm that's desired for a weekly
  doc (run Monday morning if the box was down Sunday night), or should a missed
  weekly run be **skipped** to a fresh-only schedule?

---

## 8. Implementation plan (HELD until spec approval)

1. **Schema** — `parse_days()` + `days` field + `weekdays` property +
   `from_dict`/`to_dict`/`_RESERVED` (`monitors/schema.py`). Tests first.
2. **Scheduler** — weekday gating in `_due_at`/`_last_scheduled`
   (`monitors/scheduler.py`). Tests first (incl. DST + catch-up).
3. **CLI** — `--at/--tz/--days/--notify` on `monitors add` (`cli.py`) + validation.
   Tests.
4. **`prep-doc` skill** — `skills/prep-doc.md` (+ package copy); source list per D4,
   delivery per D5.
5. **Handler wiring** — role-prompt instruction (or thin role) for
   `prep.weekly_due`; routing test.
6. **Recipe + docs** — "schedule a weekly job" guide; monitor-schema doc update for
   `days:`; sample `weekly-prep-doc` monitor.
7. `/review`, run unit suite, then open the implementation PR against `main`.

*No code is written until this spec PR is approved.*
