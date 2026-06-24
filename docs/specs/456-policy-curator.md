> **Agent-authored design spec — pending human approval.** Written for #456
> by the engineer agent and reviewed via the plan-eng / plan-design / plan-ceo
> lenses. Not self-approved: routing to the director for human (project-lead →
> director) sign-off before implementation. This spec is a **superset** of the
> original issue; the original issue text is preserved verbatim at the bottom.
>
> This is a **self-modifying** change — it rewrites how modastack itself
> persists durable knowledge across every agent team. Treat the removal of the
> decision log and its rotation flush as the riskiest part: it touches the
> session rotation path that wedged the director live on 2026-06-23.

---

# Spec: replace the append-only decision log with a curator-monitor → `policy.md` (#456)

## Problem

The team is supposed to **get smarter as it runs** — accumulate durable,
reusable knowledge — *without* the per-prompt context growing unbounded. The
current decision log does the opposite.

Today every agent gets a **per-session, append-only journal** injected into its
prompt:

- It lives at `.modastack/state/memory/<session>/INDEX.md`
  (`memory.py:memory_dir_for_session`, line 22) and is read +
  formatted into the prompt under a `## Decision Log` heading
  (`memory.py:load_memory`/`format_memory_prompt`, lines 27–88), injected at
  three sites: `prompts/resolver.py:build_startup_prompt` (line 168),
  `session.py:_rebuild_system_prompt` (line 236), and
  `subagent.py:run_phase_blocking`/`spawn_adhoc` (lines 358, 491).
- It **accumulates** — on every context rotation the session injects a flush
  prompt asking the agent to *write more* to `INDEX.md`
  (`session.py:_do_flush_and_rotate`, lines 384–418), gated on the file
  actually changing (`_verify_flush`, lines 109–135). Nothing ever prunes it.
- It grew to **127KB live** and now bloats every prompt for every session.
- It is **per-session and dies with the agent**: an ephemeral agent's
  `memory/<session>/INDEX.md` never compounds into anything the next agent or a
  different role can read. There is no team-scoped learning.
- It is **redundant** with the transcripts we already keep as the system of
  record.

**Live incident (2026-06-23, moda-eng-team).** The director wedged for ~2h40m
and required a manual nuke (clear session + delete the decision-log `INDEX.md`).
The append-only log was a contributing aggravator: asked to *prune* while
over-cap, the agent *grew* the log instead (the agent-under-load is the worst
possible curator). The root cause of the false "over-cap" that triggered the
wedge is the rotation **metric** (#454, separate, complementary). This ticket
removes the **bloat source** and replaces it with a curated, bounded, team-scoped
learning substrate.

## Solution

Replace the append-only decision log with a single, small, **rewritten-in-place**
`policy.md`, maintained **out-of-band by a curator that runs as a monitor**.

modastack monitors already run an agent out-of-band on a schedule and treat its
output as data ("a description-only monitor's check agent runs out-of-band, only
observes, and returns a verdict" — CLAUDE.md, Monitors). The curator is the same
pattern, with exactly **one** new seam: its check agent **writes an artifact**
(`policy.md` via its `Write` tool) instead of returning a verdict.

- **New default monitor `policy-curator`**, fires on an interval.
- On fire → dispatch an **out-of-band curator agent** (subagent executor) that:
  1. reads **new agent transcripts since its last run** (incremental
     watermark; v1 approximates "since last run" by the monitor's persisted
     `last_run`),
  2. reconciles them against the **current `policy.md`**,
  3. **rewrites `policy.md` in place** (a full new document, never append) with
     durable learnings **that aren't already captured in the agent-team prose**
     (role prompts / tool guides / `agent.md`), under a **hard size cap** so it
     stays injectable.
- On completion → **publish a `policy.updated` event** through the event server
  → delivered to working agents' inboxes so they **re-read the policy** (and
  reconcile any in-flight plan against it), instead of only picking it up
  passively on their next injected prompt. The event carries a short summary of
  what changed.
- `policy.md` is **injected read-only into every agent's prompt**, exactly where
  the decision log used to go.

### Properties this buys (for free, from the chosen shape)

- **Single writer** = the curator monitor; all working agents are readers → no
  write contention.
- **Team-scoped** = it reads *all* agents' transcripts → one shared doc that
  compounds across roles.
- **Can't wedge a working agent** = curation is out-of-band by construction;
  working agents never pay for it.
- **Closes the transcript loop** = transcripts are the system of record
  (history, never injected); `policy.md` is the distilled knowledge (injected).
  Volatile operational state (live leads, in-flight tickets) is **re-derived
  from source** (GitHub/Linear/`agents list`), not stored.

## Scope

### In scope

1. **New default monitor `policy-curator`** in the framework's default monitor
   set, interval-configurable, dispatching an out-of-band curator agent.
2. **Curator agent** path: read new transcripts since `last_run`, reconcile vs
   current `policy.md`, rewrite it in place under a hard size cap.
3. **Curator writes an artifact** — the one genuinely-new monitor seam. The
   scheduler must accept a check-agent that produces a file + an optional change
   summary, not just a finding verdict.
4. **`policy.updated` completion event** published through the event server with
   a change summary, delivered to working agents' inboxes; agents re-read
   `policy.md` on receipt.
5. **Inject `policy.md` read-only** into agent prompts at the three current
   memory-injection sites, replacing the `## Decision Log` section.
6. **Remove the append-only decision log + rotation flush**: the
   `## Decision Log` injection, `memory.load_memory`/`format_memory_prompt` as a
   journal reader, and `session.py`'s `_do_flush_and_rotate` / `_verify_flush` /
   `_snapshot_index` flush machinery. Keep rotation itself (the client cycle);
   only drop the append-on-rotation behavior.
7. **Single-writer invariant**: only the curator writes `policy.md`.
8. **Tests** (see Verification Plan), using real message/transcript shapes.

### Out of scope (explicit MVP guardrails)

- **No** index/retrieval/KB machinery, **no** per-type schema, **no** embeddings.
  One markdown file, rewritten in place, capped, injected. (The existing
  `modastack kb` subsystem is *not* involved.)
- **No** change to the rotation **metric** — that is #454. This spec removes the
  bloat source; #454 fixes why rotation falsely fired. They ship independently.
- **No** migration of existing `INDEX.md` journals into `policy.md`. On rollout
  the old per-session journals are simply no longer injected (and may be deleted
  by ops); `policy.md` starts empty and the curator fills it from transcripts.
  *(Open question Q3 — confirm we don't want a one-time seed.)*
- **No** per-agent or per-role policy files. One **team-scoped** `policy.md`.

## Technical Approach

All line references are against `main` at spec time (`1a58745`,
release v0.31.0).

### 1. Where `policy.md` lives

- Path: **`.modastack/state/policy.md`** (single, team-scoped, not under
  `memory/<session>/`). Add `policy_path()` to `modastack/paths.py` returning
  `state_dir() / "policy.md"`.
- The existing `.modastack/state/memory/` tree is **no longer read** for prompt
  injection. The directory mechanics in `memory.py` may be reused for reading
  `policy.md`, but the per-session journal semantics are removed.

### 2. The `policy-curator` monitor (declaration)

The monitor schema already carries everything needed — no schema migration. A
new monitor only needs fields that already exist (`schema.py:65–88`), and any
extra keys land in `Monitor.extra` (reserved-key set at `schema.py:19–20`).

Add to the **framework default monitors** (the curator is a framework-level
default, present for every team, not eng-team-specific — see Q1):

```yaml
- name: policy-curator
  description: >
    Distill new agent transcripts since your last run into the team's
    policy.md. (The curator agent runs from a dedicated prompt; see
    Technical Approach §3 — this description is a human-readable label,
    not the agent's working instructions.)
  interval: 6h                       # configurable; see Q2 for default
  event: system/policy.updated
  curator: true                      # NEW marker → routes to the curator path
```

A new boolean flavor marker (`curator: true`, parsed into `Monitor.extra` or a
first-class field) distinguishes this from a description-only check agent.
Rationale: the description-only flavor expects a **verdict JSON** on stdout and
converts it to dedup conditions (`scheduler.py:_parse_verdict` 146–163,
`_verdict_conditions` 485–506). The curator instead produces a **file + change
summary**; conflating the two would force the curator to emit a fake "finding"
to publish. A distinct marker keeps the verdict path untouched.

### 3. The curator agent (dispatch + prompt)

- **Dispatch** reuses the existing out-of-band subprocess path the scheduler
  already uses for description-only monitors (`scheduler.py:_default_spawn_check`
  166–234), which shells out to `modastack agents launch -w adhoc --non-interactive
  --wait --task <…>`. The curator runs with `permission_mode="bypassPermissions"`
  (`session.py:420–432`), so its `Write` tool is available — no new permission
  plumbing.
- The curator's working instructions come from a **dedicated curator prompt**
  (new `modastack/prompts/curator.md`, or a framework role), not from the
  monitor `description`. The prompt instructs the agent to:
  1. Read the persisted watermark (`last_run` for `policy-curator` in
     `monitor_state.json`) and enumerate transcripts started/modified since
     then.
  2. Read the **current `.modastack/state/policy.md`**.
  3. Distill durable, reusable learnings **not already in** the team prose
     (role prompts, `tools/*.md`, `agent.md`) — promote only patterns seen
     across runs, never one-off operational details.
  4. **Rewrite `policy.md` in full** via `Write` (never append), staying under
     `MAX_POLICY_CHARS`.
  5. Emit a final JSON line with a short **change summary**:
     `{"success": true, "updated": true, "summary": "…", "bytes": N}`
     (`updated: false` when nothing durable changed).

#### Reading transcripts since the watermark

Transcripts are indexed in SQLite at `.modastack/state/history.db`
(`history.py:16–18`) from the raw JSONL under the Claude projects dir. The
curator uses the existing read API:

- `history.index()` — incremental re-index (only new lines;
  `history.py:177–213`).
- `history.conversations(limit=…)` — returns rows with `started_at`
  (`history.py:259–278`); filter `started_at > last_run` for the delta.
- `history.session_messages(session_id)` — full message list per session
  (`history.py:281–294`).

v1 watermark = the monitor's persisted `last_run` (ISO string) already written
by the scheduler (`scheduler.py:364`). The curator does **not** maintain its own
cursor file; it reads `last_run` and trusts the scheduler to advance it after
the run. *(Q4: should the curator advance the watermark only on a successful
rewrite, to avoid skipping transcripts when a run dies mid-distillation?)*

### 4. Publishing `policy.updated` on completion

The scheduler already owns the single publish chokepoint
(`scheduler.py:_reconcile` 367–395 → `_fire` 387–395 →
`events.publish.post_event` 102–116). For the curator flavor:

- On a successful curator run with `updated: true`, the scheduler publishes
  `post_event("system/policy.updated", {"monitor": "policy-curator",
  "summary": <change summary>, "bytes": N})`.
- Topic naming: `system/policy.updated`. The event server routes this onto both
  the bare topic `policy.updated` and the source-qualified `system/policy.updated`
  (`subscriptions.py:monitor_subscription_keys` 45–68), so the manager's monitor
  subscriptions already cover it via `monitor_subscription_keys([monitor.event])`.
- Delivery to inboxes is the existing path: WS client → `events/drain.py`
  batches → `inbox.push(Message(...))` → session `_inbox_loop` surfaces it to
  the agent (`session.py` inbox loop). Working agents see a message like
  *"policy.md updated — <summary>. Re-read .modastack/state/policy.md and
  reconcile any in-flight plan."*
- **Dedup caveat**: the scheduler's dedup keys on condition identity
  (`_reconcile` 367–395). A `policy.updated` with an identical summary two runs
  in a row must still deliver. Either give each curator event a unique key
  (e.g. `bytes`+run timestamp, threaded in via `args` since `Date.now` is fine
  in Python), or route the curator-completion publish around `_reconcile`
  (it is a completion signal, not a deduped finding). **Recommended: bypass
  `_reconcile` for the curator** and publish directly on completion, like a
  lifecycle event (`subagent.py:_emit_lifecycle_event` 96–176 is the precedent
  for a non-deduped system publish).

### 5. Injecting `policy.md` (replacing the Decision Log)

Replace the memory-load helpers so the three injection sites read `policy.md`
instead of the per-session journal:

- `memory.py`: replace `load_memory(state_dir, session_name)` /
  `format_memory_prompt` with `load_policy(state_dir)` /
  `format_policy_prompt(content)`. `load_policy` reads
  `state_dir/policy.md`, truncates at `MAX_POLICY_CHARS`, returns `""` when
  absent. `format_policy_prompt` wraps it under a `## Team Policy` heading
  marked **read-only** ("maintained out-of-band by the curator; do not edit").
- Update the three call sites to drop the per-session `session_name` argument:
  - `prompts/resolver.py:_load_memory_section` / `build_startup_prompt` (168, 173–181)
  - `subagent.py:_load_memory_for_session` (419–432) and its callers (358, 491)
  - `session.py:_rebuild_system_prompt` (223–239) — keep rebuilding the prompt
    on rotation so a rotated session re-reads `policy.md`; just point it at the
    policy loader and strip the `## Decision Log` split logic (replace the
    marker it splits on with `## Team Policy`).
- `doctor.py:_check_memory` currently walks `memory/<agent>/INDEX.md`; repoint
  it at `policy.md` (size vs cap) or drop it for a `_check_policy`.

### 6. Removing the rotation flush

In `session.py`, delete the append-on-rotation machinery and keep the
lightweight client cycle:

- Remove `_do_flush_and_rotate` (384–418), `_verify_flush` (109–135),
  `_snapshot_index` (137–151), and the flush-related rotation state
  (`_flush_snapshot_mtime` / `_flush_snapshot_hash`, 91–94).
- In the idle-rotation path (377–379), call `_rotate()` directly when
  `_rotate_pending` (no flush prompt). `_rotate` (153–221) stays — it cycles the
  SDK client and **rebuilds the system prompt**, which now re-reads `policy.md`.
- Leave the rotation **trigger/metric** (302–308) alone — that's #454.

### 7. Single-writer invariant

- Only the curator agent writes `.modastack/state/policy.md`. Working agents get
  it injected as **read-only** (prompt wording + it is not in any working
  agent's task instructions to edit it).
- Enforce in code where cheap: the policy loader is read-only; no framework code
  path other than the curator dispatch writes the file. Add a doctor check that
  flags a `policy.md` mtime newer than the last curator `last_run` as an
  invariant violation. *(Q5: is a soft doctor check enough, or do we want a hard
  guard?)*

## Verification Plan

Per CLAUDE.md: **a production bug = an integration-test gap; write the failing
test first.** Use **real** message/transcript and event shapes — no `MagicMock`
that bypasses the gate (the #454 lesson). Real shapes are documented in
`tests/test_history.py` (JSONL message records) and `tests/test_monitors.py`
(monitor event topics).

Unit tests (`pytest tests/ --ignore=tests/integration/`):

1. **Rewrite-not-append (no unbounded growth).** Seed `history.db` with two
   batches of real transcript JSONL across multiple sessions. Run the curator
   distillation twice. Assert `policy.md` is **rewritten** (content replaced, not
   concatenated) and stays under `MAX_POLICY_CHARS` — the multi-run coalesce
   does not grow without bound. This is the regression test for the bloat the
   decision log caused.
2. **One-off does NOT get promoted.** Feed a transcript containing a single
   ephemeral operational detail (one ticket number, one transient lead) plus a
   recurring durable pattern. Assert the durable pattern lands in `policy.md` and
   the one-off does not. *(Curator-agent behavior; in unit form this asserts the
   curator prompt/contract via a stubbed model response with the real verdict
   shape, with a live-model integration variant.)*
3. **Completion event fires and is delivered.** On a successful curator run with
   `updated: true`, assert `post_event("system/policy.updated", …)` is called
   with a change summary, and that a subscriber on `policy.updated` /
   `system/policy.updated` receives a delivered inbox `Message` (drive
   `events/drain.py` with a real event envelope, assert `inbox.push`).
4. **`updated: false` publishes nothing.** A curator run that finds nothing
   durable does not publish `policy.updated`.
5. **Injection swap.** `build_startup_prompt` / `_rebuild_system_prompt` /
   `spawn_adhoc` inject `## Team Policy` from `policy.md` and **no**
   `## Decision Log`. Empty/absent `policy.md` → no section injected.
6. **Rotation no longer flushes.** Drive a rotation; assert no flush prompt is
   queried and `policy.md` is untouched by the rotation, while the rotated
   session's rebuilt prompt re-reads `policy.md`.
7. **Single-writer.** Assert no framework path other than the curator dispatch
   writes `policy.md` (doctor invariant check returns ok for a curator-written
   file, flags a foreign write).

Integration (`tests/integration/`, real Claude session): a live curator run over
a seeded transcript fixture produces a sane, capped `policy.md` and the
`policy.updated` event reaches a second running agent's inbox.

## Implementation Plan

Build inside-out; each step builds + type-checks + passes tests on its own.

1. **Paths + policy doc primitives.** `paths.policy_path()`; `memory.py`
   `load_policy` / `format_policy_prompt` + `MAX_POLICY_CHARS`. Tests for load +
   truncation. *(No behavior change yet — old path still injected.)*
2. **Injection swap.** Repoint the three injection sites + `doctor` at
   `policy.md`; rename the prompt section to `## Team Policy` (read-only). Test 5.
3. **Remove rotation flush.** Delete `_do_flush_and_rotate` / `_verify_flush` /
   `_snapshot_index`; idle-rotation calls `_rotate()` directly. Test 6. Delete
   the now-dead `load_memory` journal reader + its tests.
4. **Curator monitor declaration + flavor.** Add `policy-curator` default
   monitor + the `curator: true` marker; scheduler routes it to the curator
   dispatch (reusing `_default_spawn_check`). Curator prompt
   (`prompts/curator.md`).
5. **Curator completion publish.** On success+`updated`, publish
   `system/policy.updated` with summary (bypassing `_reconcile` dedup). Tests
   3, 4.
6. **Distillation contract + watermark.** Curator reads `last_run`, enumerates
   transcripts via `history.*`, rewrites `policy.md`. Tests 1, 2, 7.
7. **Integration test + docs.** Live curator run; update CLAUDE.md (Monitors +
   the removed Decision Log mention) and `DESIGN.md`/skills references to the
   memory model.

## Open Questions (for human review)

- **Q1 — framework default vs eng-team default.** Should `policy-curator` ship
  as a *framework-level* default monitor (every team gets it) or as an
  eng-team default in `agents/eng-team/monitors/defaults.yaml`? The issue says
  "new **default** monitor"; recommended: framework-level so any team compounds,
  but it adds a curator-prompt dependency to every team. **Needs a call.**
- **Q2 — default interval.** Issue says "interval-configurable" without a
  default. Recommend **6h** (cheap, flat cost, distills a few times a day).
  Confirm.
- **Q3 — one-time seed from existing `INDEX.md`?** Out of scope as written
  (start empty). Confirm we don't want a one-time distill of the current 127KB
  journal into the first `policy.md`.
- **Q4 — watermark advance on failure.** Advance `last_run` always (scheduler
  default) or only on a successful rewrite (avoid skipping transcripts when a
  curator run dies mid-distillation)? Recommend advance-on-success.
- **Q5 — single-writer enforcement strength.** Soft doctor check vs a hard guard
  on `policy.md` writes. Recommend soft check for v1.

## Related

- **#454** — rotation metric over-count (the actual cause of the wedge's false
  over-cap). Complementary: #454 fixes *why rotation falsely fired*; this removes
  the *bloat source* and gives the team a real learning substrate. Ship
  independently.
- Mirrors the framework's own context-files pattern (index + read-on-demand) and
  Claude Code's memory model (curated, deduped, not a log).

---

## Original issue (#456) — preserved verbatim

### North star
The agent team should **get smarter as it's used** — accumulate durable, reusable knowledge — *without* the per-prompt context growing unbounded. The current decision log does the opposite: it's an append-only journal that **accumulates** (it grew to 127KB live), which both bloats every prompt and isn't actually learning. Replace it with a small, **rewritten-in-place** `policy.md`, maintained out-of-band by a curator that distills the team's transcripts.

### Background (live incident, 2026-06-23, moda-eng-team)
The director wedged for ~2h40m and required a manual nuke (clear session + delete the decision-log `INDEX.md`). The append-only decision log was a contributing aggravator: it grew unbounded (the agent appends a RESTART/RESUME block every restart and **never prunes** — asked to prune while over-cap, it *grew* the log instead), and it's redundant with the transcripts we already keep. Root-cause of the false "over-cap" that triggered the wedge is the rotation metric (#454, separate). This ticket removes the decision log entirely and replaces it with a curated, bounded knowledge doc.

### Design (rides existing infra — small net new work)
A **curator runs as a monitor**. modastack monitors already run an agent **out-of-band on a schedule** and treat its output as data ("a description-only monitor's check agent runs out-of-band, only observes, returns a verdict"). The curator is the same pattern, except its output is a rewritten document instead of a verdict.

- **New default monitor `policy-curator`**, fires on an interval.
- On fire → dispatch an **out-of-band curator agent** (subagent executor) that:
  1. reads **new agent transcripts since its last run** (incremental watermark/cursor — only the delta, so cost stays flat as the team runs for months; v1 may approximate "since last run" by mtime),
  2. reconciles them against the **current `policy.md`**,
  3. **rewrites `policy.md` in place** (full new document, never append) with durable learnings **that aren't already captured in the agent-team prose** (role prompts / tool guides / agent.md). Hard size cap so it stays injectable.
- On completion → **publish a `policy.updated` event** through the event server → delivered to the working agents' inboxes so they **re-read the policy** (and reconcile any in-flight plan against it), rather than only picking it up passively on their next injected prompt. Event should carry a short summary of what changed.
- `policy.md` is **injected read-only into every agent's prompt**, exactly where the decision log used to go.

#### Properties this buys (for free, from the chosen shape)
- **Single writer** = the curator monitor; all working agents are readers → no write contention.
- **Team-scoped** = it reads *all* agents' transcripts → one shared doc that compounds across roles (today's per-ephemeral-agent `memory/<agent>/INDEX.md` files die with the agent and never compound).
- **Can't wedge a working agent** = curation is out-of-band by construction; working agents never pay for it (the agent-under-load is the worst curator — proven live).
- **Closes the transcript loop** = transcripts are the system of record (history, never injected); `policy.md` is the distilled knowledge (injected). Volatile operational state (live leads, in-flight tickets) is **re-derived from source** (GitHub/Linear/`agents list`), not stored.

### Remove
- The append-only **decision log** (`.modastack/state/memory/<session>/INDEX.md` as a journal) and its **rotation flush** step. `policy.md` replaces it. (Keep the memory dir mechanics if reused for `policy.md`; drop the append-on-rotation behavior.)

### Scope guardrails (keep it MVP)
- **No** index/retrieval/KB machinery, **no** per-type schema, **no** embeddings. One markdown file, rewritten in place, capped, injected.
- The only genuinely-new seam vs. a normal monitor: the curator agent **writes an artifact** (`policy.md` via its Write tool) instead of returning a verdict.

### Acceptance criteria
- [ ] `policy-curator` default monitor exists (interval-configurable) and dispatches an out-of-band curator agent.
- [ ] Curator reads new transcripts since last run (watermark), reconciles vs current `policy.md`, and **rewrites** it in place under a hard size cap.
- [ ] On completion the monitor publishes a `policy.updated` event delivered to agents' inboxes (with a change summary); agents re-read on receipt.
- [ ] `policy.md` is injected read-only into agent prompts; the append-only decision log + rotation flush are removed.
- [ ] Single-writer invariant (only the curator writes `policy.md`).
- [ ] Tests (per CLAUDE.md): a multi-run curator coalesces without unbounded growth (rewrite-not-append is enforced); a one-off detail does NOT get promoted; the completion event fires and is delivered. Use real message/transcript shapes (no MagicMock that bypasses the gate — see #454's test-gap lesson).

### Related
- #454 — rotation metric over-count (the actual cause of the wedge's false over-cap). Complementary: that fixes *why rotation falsely fired*; this removes the *bloat source* and gives the team a real learning substrate.
- Mirrors the framework's own context-files pattern (index + read-on-demand) and Claude Code's memory model (curated, deduped, not a log).
