> **Agent-authored design spec — pending human approval.** Written for #456
> by the engineer agent and reviewed via the plan-eng / plan-design / plan-ceo
> lenses. Not self-approved: routing to the director for human (project-lead →
> director) sign-off before implementation. This spec is a **superset** of the
> original issue; the original issue text is preserved verbatim at the bottom.
>
> This is a **self-modifying** change — it rewrites how bobi itself
> persists durable knowledge across every agent team. Treat the removal of the
> decision log and its rotation flush as the riskiest part: it touches the
> session rotation path that wedged the director live on 2026-06-23.

> **Revision 2 (2026-06-24)** — folds in the review on the spec PR. Changes:
> (1) **blocking watermark fix** — the curator gets a *dedicated cursor advanced
> on success*, because the scheduler clobbers `last_run` at dispatch (§3, Q4);
> (2) the durable doc is **decomposed by retention semantics** into `## Facts`
> (refreshable) and `## Decisions` (sticky) — two sections in the one capped
> file, with a new "decisions survive rewrite" invariant + test;
> (3) the cap is treated as **lossless-by-design** — the curator separates
> lossless from lossy ops and **surfaces any lossy drop loudly** in the change
> summary; (4) completion delivery is **passive by default** (active inbox push
> reserved for urgent changes); (5) a **curator input cap** bounds what it
> ingests per run (the hard cap was output-only); (6) all five open questions are
> **resolved** from the review's leans. Diff is in the spec body below.

> **Revision 3 (2026-06-24)** — folds in the R2 review (three downstream issues
> the now-concrete mechanics exposed). Changes:
> (1) **blocking windowing fix** — the delta is windowed on **`messages.timestamp
> > cursor` across all sessions**, not `conversations.started_at > cursor`.
> `started_at` is write-once (`history.py:137–141`), so the long-lived
> director/manager — the highest-signal transcript — is selected once and then
> *never re-selected* no matter how many messages it accrues. Windowing on
> per-message timestamp captures ongoing sessions (§3, new `messages_since`
> reader, in-scope item 2, tests 8/9);
> (2) **input-cap / cursor silent-skip fix** — the curator now ingests
> **oldest-first up to budget** and advances the cursor only to the **newest
> message in the oldest contiguous block actually ingested**, deferring the newest
> overflow to the next run. The R2 combination (newest-first-drop + advance-to-max)
> skipped the dropped older window *forever*; the spec now picks the direction
> consciously, trading "recent is higher-signal" for cursor monotonicity /
> no-silent-loss (§3, JSON contract, test 9);
> (3) **passive-vs-active push gets a mechanism** — agents stay subscribed to
> `policy.updated`; `events/drain.py` gains a **net-new drain-side filter** that
> suppresses the inbox push for non-urgent `policy.updated` (the event still
> publishes for observability; passive re-read is the next-prompt rebuild) and
> pushes only when `urgent: true` (§4, in-scope item 5, new test 11);
> (4) **seed bypasses the input cap** — the one-time INDEX.md seed uses a
> one-shot budget above `MAX_CURATOR_INPUT_CHARS` so it doesn't truncate the very
> content it exists to preserve (in-scope item 7, impl step 7). Diff in the body.

> **Revision 4 (2026-06-24)** — folds in the R3 review (all three R2 fixes
> confirmed closed; one fresh blocking hole + one hardening). Changes:
> (1) **blocking cursor-key fix** — the R3 cursor was a message **timestamp**,
> which is *not* a unique consumption key: tool-call turns write multiple
> `messages` rows at the **identical** `timestamp` (and `line_number`)
> (`history.py:149–161`), empty/absent timestamps are stored as `""`
> (`history.py:132`) and sort below any real ts, and late-indexed old rows can
> land below an advanced cursor — so a budget cut between tie-rows, a `""`-ts row,
> or a late-indexed row gets **silently skipped forever** under `WHERE timestamp >
> cursor`, the exact silent-skip class R3 just closed, reappearing one level
> finer. Fix: make the cursor the table's **unique autoincrement `messages.id`**
> (`INTEGER PRIMARY KEY AUTOINCREMENT`, `history.py:33`). Window `WHERE id >
> cursor ORDER BY id`, ingest oldest-first by id, advance to the **last ingested
> id**. Unique id → no tie; assigned regardless of ts value → empty-ts rows still
> consumed; late-indexed old rows get a *high* id → still caught. The R3
> oldest-first / defer-overflow semantics are **unchanged** — only the cursor KEY
> moves from timestamp to id (§3, `messages_since(cursor)` signature, tests 8a/9);
> (2) **oversized-single-message hardening** — a single message larger than
> `MAX_CURATOR_INPUT_CHARS` would never fit and would stall the cursor forever
> (the budget can't advance past it). The curator now **truncates** an oversized
> single message (head+tail, with an elision marker) to fit, ingests it, advances
> the cursor past it, and flags the truncation loudly (`oversized_truncated`) so
> the stall is impossible and the lossy edit is visible (§3, JSON contract, test
> 9a). Diff in the body.

---

# Spec: replace the append-only decision log with a curator-monitor → `policy.md` (#456)

## Problem

The team is supposed to **get smarter as it runs** — accumulate durable,
reusable knowledge — *without* the per-prompt context growing unbounded. The
current decision log does the opposite.

Today every agent gets a **per-session, append-only journal** injected into its
prompt:

- It lives at `run/state/memory/<session>/INDEX.md`
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

**The wedge has three independent mechanisms — this spec must close the one it
straddles, not just the one it removes (2026-06-24 recurrence).** The director
wedged *again* on **2026-06-24** and took another manual `fly machine restart` —
this time on prod **already running 0.31.0** (i.e. *with* the #454 metric fix).
That rules out the metric as the sole cause and separates three distinct
mechanisms:

| # | Mechanism | Disposition |
|---|-----------|-------------|
| 1 | Rotation **metric** over-count (#454) — false "rotation pending" | Fixed in v0.30.0; orthogonal — this spec correctly leaves it alone. |
| 2 | Decision-log **bloat** aggravating the rotation reconnect | **Removed here** — the rebuilt prompt at rotation becomes a capped `policy.md`. |
| 3 | **Unbounded / unrecoverable rotation reconnect** — the actual hang | **Must be closed here too** (in scope below) — it lives inside the very `_rotate()` §6 already edits. |

Mechanism #3 is the structural defect. `_rotate()` does
`disconnect() → _rebuild_system_prompt() → await connect() → await _drain_turn()`
(`session.py` ~`_rotate`, lines 236–270), and **both `connect()` and the
connect-turn `_drain_turn()` are unbounded** — no timeout, no recovery.
`_drain_turn` loops `async for msg in receive_response()`; if the fresh `claude`
subprocess never yields a `ResultMessage`, it blocks forever — `_rotation_count`
never increments (it sits at `0`), the run loop never returns to `inbox.recv`,
and every event (including a human's Slack message) queues unanswered while the
"thinking…" indicator keeps refreshing. Removing the bloat (mechanism #2) only
lowers the *probability* that the connect stalls; it does not bound or recover
the stall. Notes on adjacent fixes that do **not** cover #3:

- **#443** (`session.py:393`) clears `is_error` only when a `ResultMessage`
  *arrives* carrying an error (e.g. a 529 on a steady-state turn). It does
  nothing for the *never-receives-a-message* hang, and does not cover
  `connect()` itself — so a 529 / network / subprocess stall *during the
  rotation connect turn* still wedges post-#456 unless we bound it.
- **No manager self-heal.** `stall-recovery` is director→engineer, so it cannot
  fire when the *director* is the wedged party. Every residual wedge today =
  manual restart + a human noticing. (A watchdog that covers *unknown* wedge
  classes is real defense-in-depth but is **out of scope** here — see below.)

## Solution

Replace the append-only decision log with a single, small, **rewritten-in-place**
`policy.md`, maintained **out-of-band by a curator that runs as a monitor**.

bobi agent <name> monitors already run an agent out-of-band on a schedule and treat its
output as data ("a description-only monitor's check agent runs out-of-band, only
observes, and returns a verdict" — CLAUDE.md, Monitors). The curator is the same
pattern, with exactly **one** new seam: its check agent **writes an artifact**
(`policy.md` via its `Write` tool) instead of returning a verdict.

- **New default monitor `policy-curator`**, fires on an interval.
- On fire → dispatch an **out-of-band curator agent** (subagent executor) that:
  1. reads **new transcript messages since its last successful run** — windowed
     on **`messages.id > cursor` across *all* sessions** (the cursor is the
     unique autoincrement row id, *not* a message `timestamp`, which is non-unique
     across tie-rows and sometimes empty; and *not* `conversations.started_at`,
     which is write-once and would never re-select the persistent manager — see
     §3) — against a **dedicated curator cursor advanced only on success**, *not*
     the monitor's `last_run`, which the scheduler clobbers at dispatch; bounded
     by a **per-run input cap** (ingest **oldest-first by id up to budget**, defer
     the higher-id overflow to the next run) so a busy team can't blow up the
     curator's ingest cost and no row is silently skipped,
  2. reconciles them against the **current `policy.md`**,
  3. **rewrites `policy.md` in place** (a full new document, never append) with
     durable learnings **that aren't already captured in the agent-team prose**
     (role prompts / tool guides / `agent.md`), under a **hard size cap** so it
     stays injectable.
- **`policy.md` is decomposed by retention semantics, not by document type.**
  One capped file, two sections the curator maintains under distinct rules:
  - **`## Facts`** — *refreshable* state of the world (this repo uses GitHub not
    Linear, the deploy command, user preferences). Falsifiable; **overwritten**
    when reality changes.
  - **`## Decisions`** — *sticky* choices ("chose A over B, don't re-litigate").
    **Retained across rewrites unless explicitly reversed** in a later transcript.

  This takes the facts-vs-decisions distinction (different retention rules) while
  deliberately **not** importing a per-type-document model (no SNAPSHOT / TASKS /
  LOG files): volatile state is re-derived from source, and a "log" is just the
  append-only decision log under a new name. Sections promote to their own files
  only if one outgrows the shared cap (not in v1).
- The cap is **lossless by design at the expected operating point** (see §3 and
  the Verification Plan): durable, deduped, generalized knowledge **plateaus**,
  so the cap is a tuning number above that plateau, not a knowledge-shredder. The
  curator separates **lossless** ops (dedup, generalize, evict falsified facts,
  evict superseded decisions) from **lossy** ops (drop a *still-valid* decision
  for space) and **never drops silently** — a lossy drop is surfaced in the
  change summary so we see the day the cap needs raising.
- On completion → **publish a `policy.updated` event** through the event server
  carrying a short change summary. Delivery is **passive by default**: working
  agents already re-read `policy.md` on their next injected prompt (rebuilt every
  rotation), so the common case needs no interruption. **Active inbox push**
  ("re-read and reconcile any in-flight plan now") is **reserved for changes the
  curator marks urgent** — it interrupts every working agent mid-task, which is
  the wrong default for a routine distillation.
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
   set, interval-configurable, dispatching an out-of-band curator agent. The
   *mechanism* is framework-level; the **curator prompt is team-overridable**
   (what counts as "durable" is domain-flavored — Q1).
2. **Curator agent** path: read new transcript **messages** (`messages.id >
   cursor` across *all* sessions, ongoing ones included — not
   `conversations.started_at`; the cursor is the unique row id, not a timestamp)
   since the **curator's own success-advanced cursor** (not the scheduler's
   `last_run`), ingesting **oldest-first by id under a per-run input cap** and
   deferring the higher-id overflow (truncating an oversized single message that
   can't fit), reconcile vs current `policy.md`, rewrite it in place under a hard
   output size cap.
3. **Two-section `policy.md` with distinct retention rules**: `## Facts`
   (refreshable/overwritten) and `## Decisions` (sticky/retained-unless-reversed),
   in one capped file. Curator contract distinguishes **lossless** ops from
   **lossy** ops and surfaces any lossy drop in the change summary (no silent
   loss).
4. **Curator writes an artifact** — the one genuinely-new monitor seam. The
   scheduler must accept a check-agent that produces a file + an optional change
   summary, not just a finding verdict.
5. **`policy.updated` completion event** published through the event server with
   a change summary. **Passive delivery by default** (agents re-read on next
   prompt); active inbox push only for changes the curator flags urgent. The
   passive/active split is enforced by a **net-new drain-side filter** in
   `events/drain.py`: agents stay subscribed to `policy.updated`, but the drain
   suppresses the inbox push for a non-urgent `policy.updated` and pushes only
   when `urgent: true` (§4).
6. **Inject `policy.md` read-only** into agent prompts at the three current
   memory-injection sites, replacing the `## Decision Log` section.
7. **One-time seed** of the first `policy.md` by distilling the existing
   `memory/<session>/INDEX.md` journal(s) (Q3 — resolved *seed once*; starting
   empty discards real, not-all-re-derivable knowledge). The seed **bypasses the
   per-run `MAX_CURATOR_INPUT_CHARS`** (it distills the full ~127KB `INDEX.md` in
   one shot) — capping the seed would truncate the very knowledge it exists to
   preserve.
8. **Rework the rotation path** (`session.py`) — both halves touch the same
   `_rotate()` surface:
   - **Remove the append-only decision log + rotation flush**: the
     `## Decision Log` injection, `memory.load_memory`/`format_memory_prompt` as a
     journal reader, and `session.py`'s `_do_flush_and_rotate` / `_verify_flush` /
     `_snapshot_index` flush machinery. Keep rotation itself (the client cycle);
     only drop the append-on-rotation behavior.
   - **Bound and recover the rotation reconnect** (mechanism #3, §6): wrap
     `_rotate()`'s `connect()` + connect-turn `_drain_turn()` in
     `asyncio.wait_for(ROTATION_RECONNECT_TIMEOUT)` (the flush turn already had a
     timeout; the reconnect must too), bounded-retry on timeout/error, then fail
     **loudly into a recoverable state** rather than a silent park. This is the
     *only* in-scope half that touches the actual hang; removing the bloat alone
     does not bound it.
9. **Single-writer invariant**: only the curator writes `policy.md`.
10. **Tests** (see Verification Plan), using real message/transcript shapes.

### Out of scope (explicit MVP guardrails)

- **No** index/retrieval/KB machinery, **no** per-type schema, **no** embeddings.
  One markdown file, rewritten in place, capped, injected. (The existing
  `bobi agent <name> kb` subsystem is *not* involved.)
- **No** change to the rotation **metric** — that is #454. This spec removes the
  bloat source; #454 fixes why rotation falsely fired. They ship independently.
  (Bounding/recovering the rotation **reconnect** — mechanism #3 — *is* in scope,
  item 8; it is a different surface from the metric.)
- **No** manager self-heal watchdog in this ticket. A watchdog on the existing
  health endpoint (`127.0.0.1:45985`) that restarts a *director* whose
  `last_activity` stalls is the only thing that covers **unknown** wedge classes
  (not just the three enumerated above), and is worth its own ticket as
  defense-in-depth — but it is **separate follow-up, not blocking #456**
  (tracked in **#464**). Scoping it here would widen this PR past the curator +
  the rotation surface it already owns.
- **No** *ongoing* migration machinery for `INDEX.md` journals. There is a
  **one-time seed** at rollout (in scope, item 7) — a single distill of the
  current journal(s) into the first `policy.md` — but the old per-session journals
  are not read again after that; thereafter the curator fills `policy.md` from
  transcripts. (Reverses R1's earlier "start empty" stance per the review.)
- **No** per-type *document* model (no SNAPSHOT / TASKS / LOG files). The
  facts-vs-decisions split is **two sections in one capped file**, not separate
  files — separate files reintroduce per-type schema, cap-splitting, and
  injection multiplication, all explicit MVP non-goals. Sections graduate to
  files only if one outgrows the shared cap (not v1).
- **No** decisions-spill archive in v1. The curator *signals* (in the change
  summary) the day it is forced into a lossy drop; building the read-on-demand
  spill is deferred until that signal actually fires.
- **No** per-agent or per-role policy files. One **team-scoped** `policy.md`.

## Technical Approach

All line references are against `main` at spec time (`1a58745`,
release v0.31.0).

### 1. Where `policy.md` lives

- Path: **`run/state/policy.md`** (single, team-scoped, not under
  `memory/<session>/`). Add `policy_path()` to `bobi/paths.py` returning
  `state_dir() / "policy.md"`.
- **Structure: two fixed sections in the one file**, written and parsed as plain
  markdown headings (no schema, no frontmatter):

  ```markdown
  ## Facts
  <refreshable, falsifiable state of the world — overwritten when reality changes>

  ## Decisions
  <sticky choices — retained across rewrites unless explicitly reversed>
  ```

  The curator owns both. The whole file is still capped (`MAX_POLICY_CHARS`)
  and injected as one block; the sections only carry *different curator rules*
  (below), not different files or schemas.
- The existing `run/state/memory/` tree is **no longer read** for prompt
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
  166–234), which shells out to `bobi agent <name> subagents launch -w adhoc --non-interactive
  --wait --task <…>`. The curator runs with `permission_mode="bypassPermissions"`
  (`session.py:420–432`), so its `Write` tool is available — no new permission
  plumbing.
- The curator's working instructions come from a **dedicated curator prompt**
  (new `bobi/prompts/curator.md`, or a framework role), not from the
  monitor `description`. **The prompt is team-overridable** (Q1): the framework
  ships a default `curator.md`, and a team may replace it via the normal
  prompt-override path — "what counts as durable" is domain-flavored and a
  framework with no topology opinions shouldn't hard-bake one team's notion of
  policy. The prompt instructs the agent to:
  1. Read the **curator cursor** (see watermark subsection) and enumerate
     transcript **messages with `id > cursor` across all sessions**
     (ongoing long-lived sessions included — *not* conversations whose
     `started_at > cursor`; the cursor is the unique autoincrement row id, not a
     timestamp), grouped by `session_id` for per-session context.
     **Trim the ingest to the per-run input cap by ingesting oldest-first by id up
     to budget** and deferring the higher-id over-budget messages to the next run
     (noted in the summary) — see the watermark subsection for why this direction
     (not highest-id-first-drop), and how an oversized single message is truncated
     rather than allowed to stall the cursor.
  2. Read the **current `run/state/policy.md`** (both sections).
  3. Distill durable, reusable learnings **not already in** the team prose
     (role prompts, `tools/*.md`, `agent.md`) — promote only patterns seen
     across runs, never one-off operational details — and **file each into the
     right section** under its retention rule:
     - **`## Facts`** — refreshable. **Overwrite** a fact when the transcript
       delta shows reality changed; **evict** a fact the delta falsifies. Facts
       are not accreted; the latest true value replaces the old one.
     - **`## Decisions`** — sticky. **Carry every existing decision forward**
       into the rewrite **unless** the delta explicitly reverses/supersedes it.
       A decision absent from the recent window is **retained, not dropped** —
       this is the whole point of the bucket (an old "chose A over B" that's not
       re-mentioned must not silently vanish and get re-litigated).
  4. **Rewrite `policy.md` in full** via `Write` (never append), staying under
     `MAX_POLICY_CHARS`. Classify every removal:
     - **Lossless** (always allowed): dedup duplicates, generalize N specifics
       into one principle, evict a **falsified** fact, evict a **superseded**
       decision. These are compression toward the information-theoretic minimum,
       not loss.
     - **Lossy** (last resort only): drop a **still-valid** decision purely for
       space. Only when lossless compression still exceeds the cap.
  5. Emit a final JSON line with a short **change summary**:
     `{"success": true, "updated": true, "summary": "…", "bytes": N,
     "urgent": false, "lossy_drops": 0, "input_truncated": false,
     "oversized_truncated": 0}`.
     - `updated: false` when nothing durable changed (publishes nothing).
     - `urgent: true` only for changes worth interrupting in-flight agents →
       gates the **active inbox push** (passive re-read otherwise; see §4).
     - `lossy_drops: N` (> 0) means the curator was forced to drop still-valid
       items for space; the summary must name them
       (`"dropped N still-valid decisions for space"`). **This is the trigger to
       raise the cap / build the decisions-spill** — v1 degrades *loudly*.
     - `input_truncated: true` when the per-run input cap **deferred** the
       higher-id over-budget messages to the next run (cursor advanced only past
       the contiguous ingested block of ids, so nothing is skipped — see §3
       watermark). The summary names the deferred window (id range) so a busy
       interval is visible, not silently under-distilled.
     - `oversized_truncated: N` (> 0) means N single messages each exceeded
       `MAX_CURATOR_INPUT_CHARS` on their own and were **head+tail truncated**
       (with an elision marker) to fit, then ingested and passed by the cursor —
       so an oversized message can never stall the watermark. The summary names
       each truncated message (session + id) so the lossy edit is visible (see §3
       input cap).

#### Reading transcripts since the watermark — and bounding the input

Transcripts are indexed in SQLite at `run/state/history.db`
(`history.py:16–18`) from the raw JSONL under the Claude projects dir. The
`messages` table carries a unique **`id INTEGER PRIMARY KEY AUTOINCREMENT`**
(`history.py:33`) plus a (non-unique, sometimes empty) per-message `timestamp`
column (`history.py:40`). The curator uses:

- `history.index()` — incremental re-index (only new lines;
  `history.py:177–213`).
- **New `history.messages_since(cursor)`** — the delta the curator actually
  needs, keyed on the **row id** (an integer cursor, not an ISO timestamp):
  `SELECT * FROM messages WHERE id > ? ORDER BY id` (small net-new query, mirrors
  `session_messages`/`conversations`), returning message rows across **all**
  sessions, oldest-first by id. The curator groups the rows by `session_id` to
  reconstruct per-session context.
- `history.session_messages(session_id)` — full message list per session
  (`history.py:281–294`), for pulling surrounding context once a session is in
  the window.

**Window on `messages` (not `conversations.started_at`), keyed on `messages.id`
(not `timestamp`) — two distinct fixes.**

*(R2 fix — window on messages, not conversations.)* R2 windowed the delta with
`conversations(...)` filtered on `started_at > cursor`. But `started_at` is
written **once** — the `INSERT OR REPLACE … started_at` fires only
`if not conv_exists` (`history.py:130, 137–141`) and is **never updated**. The
director/manager runs as **one persistent, long-lived session**: it appears in
`conversations` exactly once, with an old `started_at`, and `started_at > cursor`
would **never re-select it** no matter how many thousands of messages it accrues
— so the curator would distill ephemeral subagent sessions and **systematically
miss the single session where most durable learning happens**. Fix: window on the
**`messages` table across all sessions** via `messages_since(cursor)` — "messages
since cursor," not "conversations started since cursor."

*(R3→R4 fix — key the cursor on `messages.id`, not `messages.timestamp`.)* R3
made that cursor a **message timestamp**. But `timestamp` is **not a unique
consumption key**, so `WHERE timestamp > cursor` reintroduces the silent-skip
class one level finer:

1. **Tie-rows at an identical timestamp.** A tool-using assistant turn writes
   the text row **plus one row per tool call**, all with the **same `timestamp`
   *and* `line_number`** (`history.py:149–161`). Ties are guaranteed for most
   turns. If the oldest-first input cap cuts **between two rows sharing timestamp
   T** (text ingested, sibling tool-call row deferred), advancing the cursor to T
   makes the next run's `WHERE timestamp > T` **skip the deferred sibling
   forever** — at exactly the budget boundary the cap exists to handle.
2. **Empty timestamps permanently excluded.** `timestamp = msg.get("timestamp",
   "")` (`history.py:132`) stores `""` when absent. `""` sorts below any real ts,
   so once the cursor is non-empty a `""`-ts row never satisfies `> cursor` and is
   dropped from every future window.
3. **Late-indexed old rows slip below the watermark.** With concurrent sessions
   indexed incrementally across files, a session flushed late can land messages
   with timestamps *below* an already-advanced cursor → never selected by
   `timestamp > cursor`.

A `(timestamp, line_number)` composite does **not** fix this — the tie-rows share
**both** columns. Fix: key the cursor on the table's unique monotonic
**`messages.id`** (`INTEGER PRIMARY KEY AUTOINCREMENT`, `history.py:33`): window
`WHERE id > cursor ORDER BY id`, advance to the **last id ingested**. This kills
all three at once — unique id ⇒ no tie (the cap can cut anywhere and `id > cursor`
still picks up the deferred row next run); id is assigned regardless of ts value
⇒ empty/`""`-ts rows are still consumed; a late-indexed old row gets a *high* id
(inserted later) ⇒ `id > cursor` still catches it. The cursor is a **consumption
watermark** ("what have I read?"), which is what the curator needs — versus
`timestamp`, a *content* attribute that is non-unique and sometimes absent. The
only property lost is strict global chronological order **within** a window; since
the curator groups by `session_id` and distills, completeness matters far more
than cross-session time-ordering. This redefines what the cursor *is*: a
**message row id**, not a timestamp or a conversation `started_at`.

**Watermark — dedicated curator cursor, advanced on success (resolves Q4, and
fixes a blocking bug in R1).** R1 proposed reusing the monitor's persisted
`last_run`. That **does not work** against the real scheduler: `run_monitor`
dispatches the curator async via `_spawn_check` (`scheduler.py:405`) and then
writes `last_run = now` **synchronously, at dispatch** (`scheduler.py:411–412`),
*before* the curator subprocess has started. So a curator that read `last_run`
to compute "transcripts since last run" would read **its own dispatch time** —
the delta collapses to ≈empty and it distills nothing. The watermark it actually
needs (the *previous* run's time) has already been clobbered.

Fix: the curator maintains its **own cursor** at
`run/state/policy_cursor` (the integer **`messages.id`** of the last row
it ingested, not an ISO timestamp), and **advances it only after a successful
rewrite** — to the **highest id it actually ingested** (which, because it ingests
oldest-first by id and contiguously from the cursor, is the upper edge of an
unbroken block of ids — see the input cap). Properties:

- Reads the *previous* successful boundary, not its own dispatch time → the
  delta is real.
- A run that **dies mid-distillation** does not advance the cursor, so the next
  run re-reads that window → **no transcripts skipped forever** (the other half
  of Q4).
- Independent of how/when the scheduler touches `last_run`.

**Per-run input cap — ingest oldest-first by id (resolves an R2 silent-skip
collision).** The output (`policy.md`) is capped, but the curator *reads all new
messages across all sessions* each interval, and that input is large and
variable-cost (the director alone is huge). Add `MAX_CURATOR_INPUT_CHARS` (a
char/message budget). **Direction matters**, and R2's was wrong: R2 ingested
**newest-first**, dropped the **oldest** overflow, *and* advanced the cursor to
the max ingested timestamp — so on a busy interval the dropped older window fell
**below** the advanced cursor and was **never re-read**. `input_truncated: true`
was loud about *truncating* but silent about *skipping*, defeating the
"dropped/failed window gets re-read next run" property the success-advanced
cursor exists to provide.

Fix — **ingest oldest-first by id up to the budget, and advance the cursor only
to the highest id actually ingested**, leaving the higher-id over-budget messages
for the next run:

- The ingested block is contiguous from the cursor upward (a run of ids), so
  advancing to its top id **cannot skip** anything — the deferred higher-id
  overflow sits *above* the new cursor and is re-read next run.
- Set `input_truncated: true` and name the deferred id range in the summary (so a
  busy interval is visible, not silently under-distilled).

**Oversized single message (hardening — never stall the cursor).** The
oldest-first rule assumes each message *eventually fits* in some run's budget. A
**single** message larger than `MAX_CURATOR_INPUT_CHARS` breaks that assumption:
it never fits, so a budget that "ingests until the next message would overflow"
would ingest **zero** rows, advance the cursor **nowhere**, and re-hit the same
oversized row every run — a permanent stall (and it is the *oldest* unread row,
so everything behind it stalls too). Rule: when the **oldest unread** message
alone exceeds the budget, **truncate it** to fit — keep a head+tail slice with an
explicit elision marker (`… [truncated N chars] …`) — ingest the truncated form,
**advance the cursor past its id**, and set `oversized_truncated += 1` naming the
message (session + id) in the summary. Truncation is **lossy and loud** (it is
the one place the input cap drops content rather than deferring it), but it
guarantees forward progress; deferral is still preferred for every message that
*can* fit in a future run. Only the single oldest message is force-truncated per
run — normal overflow above it still defers.

**Conscious tradeoff (the tension R2 flagged):** recent learnings are
higher-signal (argues newest-first), but cursor monotonicity / no-silent-loss
requires oldest-first (you can only safely advance a single watermark past a
*contiguous* ingested block of ids). We choose **oldest-first** — never losing a
window beats distilling the newest one a few hours sooner, and a shorter interval
(Q2, 6h) keeps the deferred lag small. We explicitly **do not** combine
newest-first-drop with advance-to-max (the silent-skip combination).

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
- **Delivery is passive by default; active push is opt-in per update.** Working
  agents already re-read `policy.md` on their next injected prompt (the system
  prompt is rebuilt every rotation, §5/§6), so a routine distillation needs **no
  interruption** — pushing *"re-read and reconcile your in-flight plan now"* into
  every working agent's inbox every 6h interrupts everyone mid-task for a change
  most of them don't need yet.

  **The mechanism (R2 fix — the policy needed a seam).** The existing path is
  event → subscription → `events/drain.py` → `inbox.push`: a *subscribed* agent
  receives an event in its inbox whenever it publishes, and subscriptions are
  per-topic, not per-event (`drain_loop` pushes **every** external event in a
  batch to `inbox.push`, with no per-event field check — `drain.py`
  `for e in external: … inbox.push(...)`). So "publish but don't push" has no
  mechanism as written. We add one **net-new drain-side filter**:
  - Agents **stay subscribed** to `policy.updated` (no change to
    `monitor_subscription_keys` — the manager's monitor subscriptions already
    cover it).
  - `events/drain.py` gains a small filter: an external event of type
    `policy.updated` (or `system/policy.updated`) whose payload `urgent` is not
    truthy is **dropped before `inbox.push`** — it does not become an inbox
    message. (It remains observable in the events feed / `bobi agent <name> events`, so
    the publish-for-observability property holds.) Passive pickup is the
    **next-prompt rebuild** (§5/§6), independent of this event.
  - This drain-side filter is **net-new** (currently unmentioned) and is the only
    place the `urgent` policy is enforced. Specified here so implementation
    doesn't fall back to the naive reading (publish the event → it pushes to
    every subscribed inbox regardless of `urgent`, i.e. active-always).

  So:
  - **`urgent: false` (default):** the curator publishes `policy.updated` for
    observability/logging; the drain filter suppresses the inbox push — agents
    pick the change up passively on their next prompt.
  - **`urgent: true`:** the drain passes it through and delivers the inbox message
    via the existing path (WS client → `events/drain.py` batches →
    `inbox.push(Message(...))` → `_inbox_loop`): *"policy.md updated — <summary>.
    Re-read run/state/policy.md and reconcile any in-flight plan."* The
    curator sets `urgent` only for changes worth the interruption (e.g. a reversed
    decision that invalidates work in flight).
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

### 6. Reworking the rotation path: remove the flush, bound the reconnect

Two changes on the same `session.py` rotation surface. The first removes the
bloat-flush (mechanism #2); the second bounds and recovers the reconnect
(mechanism #3 — the actual 2026-06-24 hang). Both ship in this PR because they
edit the same `_rotate()` / idle-rotation code.

**6a. Remove the append-on-rotation flush.** Delete the machinery and keep the
lightweight client cycle:

- Remove `_do_flush_and_rotate` (384–418), `_verify_flush` (109–135),
  `_snapshot_index` (137–151), and the flush-related rotation state
  (`_flush_snapshot_mtime` / `_flush_snapshot_hash`, 91–94).
- In the idle-rotation path (377–379), call `_rotate()` directly when
  `_rotate_pending` (no flush prompt). `_rotate` stays — it cycles the
  SDK client and **rebuilds the system prompt**, which now re-reads `policy.md`.
- Leave the rotation **trigger/metric** (302–308) alone — that's #454.

**6b. Bound and recover the rotation reconnect.** The flush turn is already
wrapped in `asyncio.wait_for(ROTATION_FLUSH_TIMEOUT)` (`session.py:585`), but the
reconnect inside `_rotate()` is not: `await self._client.connect()` and the
connect-turn `await self._drain_turn()` (`session.py` ~267–270) are both
unbounded, and the `try/except` guarding the `_rotate()` call (`session.py:610`)
catches *exceptions* but **cannot catch a hang** — an `await` that never returns
simply never returns. Close it:

1. **Bound the reconnect.** Wrap the reconnect work — `connect()` **and** the
   first `_drain_turn()` (the system-prompt acknowledgment) — in
   `asyncio.wait_for(..., timeout=ROTATION_RECONNECT_TIMEOUT)`. Add the constant
   next to the existing rotation constants (`ROTATION_FLUSH_TIMEOUT = 180.0`,
   `ROTATION_MAX_FLUSH_ATTEMPTS = 3`); a connect + single-ack turn is lighter
   than a flush, so a smaller default (e.g. `ROTATION_RECONNECT_TIMEOUT = 120.0`)
   is reasonable — it is a tuning number, not load-bearing.
2. **Define on-timeout / on-error recovery.** On `asyncio.TimeoutError` or a
   connect exception: **discard the timed-out client first** — `wait_for` cancels
   the hung `connect()`/`receive_response()` await, but the partially-connected
   `ClaudeSDKClient` (and its `claude` subprocess) must be `disconnect()`ed /
   dropped before the next attempt so a stalled reconnect can't leak a subprocess
   per retry — then **bounded retry** of the reconnect
   (`ROTATION_MAX_RECONNECT_ATTEMPTS`, mirroring the flush attempt budget, with
   backoff). On exhaustion, do **not** leave the session in the disconnected
   state `_rotate()` created (`self._client = None` after the `disconnect()` at
   ~252) and do **not** drop into the terminal `"error"` state (which deafens the
   session — `_process_message` rejects every future message while state is
   `"error"` and `is_alive()` reports it dead, the same trap #443 fixed). Instead
   fail **loudly** (`log.error` + a `session.rotation_failed` activity/event) and
   recover into a **usable** state: re-establish a connected client via the same
   fresh-connect path `_run` uses (reconnect from the saved session id, falling
   back to a fresh `resume=None` connect), so the session is addressable again.
   Only if even that final recovery fails does the session surface terminally —
   loudly, never a silent hang.
3. **Confirm #443's error clearing reaches the reconnect drain.** The reconnect
   reuses `_drain_turn`, so a `ResultMessage` that *arrives* with
   `is_error=True` (e.g. a 529 on the connect turn) already routes through the
   `session.py:393` branch that returns to `waiting_input` instead of `"error"`.
   Make that explicit (it is the arrives-with-error case); the *never-arrives*
   case is what step 1's timeout covers. The two together close both failure
   shapes of the connect turn.

The recovery path lives **inside** `_rotate()` / its caller so it cannot starve
the run loop the way the unbounded `await` does today.

### 7. Single-writer invariant

- Only the curator agent writes `run/state/policy.md`. Working agents get
  it injected as **read-only** (prompt wording + it is not in any working
  agent's task instructions to edit it).
- **Curator-vs-curator concurrency is already guarded by the interval, not the
  doctor check.** `last_run` advances at dispatch (`scheduler.py:411`), so a
  second `policy-curator` cannot fire until the interval elapses — two curators
  overlap only if one run *exceeds* its own interval. The doctor check below is
  therefore about a *foreign* writer (some other process touching `policy.md`),
  which is a smaller risk.
- Enforce in code where cheap: the policy loader is read-only; no framework code
  path other than the curator dispatch writes the file. Add a doctor check that
  flags a `policy.md` mtime newer than the **cursor file's** mtime (the cursor is
  now an integer id, so compare the two files' modification times, not the cursor
  *value*; the curator rewrites `policy.md` and advances `policy_cursor` together,
  so a `policy.md` written *after* the last cursor advance is a write not
  attributable to the last curator run) as an invariant violation. **A soft
  doctor check is enough for v1 (resolves Q5)** — given the interval guard above,
  a hard write-guard is not worth the plumbing yet.

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
   the one-off does not. **Caveat — in unit form this tests plumbing, not
   judgment:** with a stubbed model response (real verdict shape) it only proves
   the curator wires its output through correctly; it asserts the *stub*, not the
   curation decision (the #454 trap). **The real assertion is the live-model
   integration variant** (below); the unit test is explicitly the plumbing check.
2a. **Decisions survive a rewrite over a window that doesn't mention them.**
   Seed `policy.md` with an old decision ("chose A over B"). Run a curator pass
   over a transcript delta that **never mentions** that decision. Assert the
   decision is **still present** in the rewritten `## Decisions` section
   (retained-unless-reversed). Then run a pass over a delta that **explicitly
   reverses** it and assert it is removed. This is the regression test for the
   "rewrite silently drops an un-mentioned old decision → it gets re-litigated"
   failure the decisions bucket exists to prevent.
2b. **Facts are refreshed, not accreted.** Seed a fact ("deploy via X"); feed a
   delta showing it changed ("deploy via Y"). Assert `## Facts` holds **only** Y
   (overwrite), not both.
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
6a. **Bounded, recoverable rotation reconnect (mechanism #3 — the 2026-06-24
   wedge).** The regression test for the actual hang, in two shapes against a real
   reconnect (no `MagicMock` that bypasses the await — the #454 lesson):
   (a) *never-yields* — drive a rotation whose fresh connect turn **never yields a
   `ResultMessage`** (a real client/transport stub whose `receive_response()`
   blocks). Assert `_rotate()` **bounds the wait** at `ROTATION_RECONNECT_TIMEOUT`
   (does not block forever), retries within `ROTATION_MAX_RECONNECT_ATTEMPTS`,
   then **recovers** to an addressable, connected session — `is_alive()` stays
   true, state is **not** terminal `"error"`, and a follow-up `inbox` message is
   served — rather than wedging with `_rotation_count` stuck and the run loop off
   `inbox.recv`.
   (b) *arrives-with-529* — variant where the connect turn's `ResultMessage`
   returns `is_error=True` / `api_error_status=529`. Assert the session clears the
   error (the #443 path) and returns to ready, **not** terminal `"error"`.
   Both assert the session ends **addressable and recoverable**, never silently
   parked.
7. **Single-writer.** Assert no framework path other than the curator dispatch
   writes `policy.md` (doctor invariant check returns ok for a curator-written
   file, flags a foreign write).
8. **Success-advanced cursor.** Drive a curator run; assert the cursor advances
   to the **highest ingested `messages.id`** **only on success**, and that a
   run which raises mid-distillation leaves the cursor unmoved so the next run
   re-reads the same window (no skipped transcripts). Assert the curator reads the
   cursor, not the scheduler's `last_run` (which the scheduler clobbers at
   dispatch).
8a. **Windowing — id cursor, no tie-skip / no empty-ts-skip (blocking R2/R3
   regression).** Two parts on the same `messages_since(cursor)` reader:
   (a) *long-lived session* — seed `history.db` with one **persistent** session
   whose `conversations.started_at` is **older than the cursor** but which has
   **new `messages` after the cursor**, plus a fresh ephemeral session; assert the
   delta **includes the persistent session's new messages** (windowing on
   `messages`, not `conversations.started_at`, which would exclude the manager).
   (b) *id is skip-free where timestamp was not* — seed a tool-using turn that
   writes **multiple rows at the identical `timestamp`** (text + tool-call
   siblings, as `index()` does) **and** at least one row with an **empty `""`
   timestamp**; advance the cursor to a point that, under a `timestamp > cursor`
   window, would drop a tie-sibling and the `""`-ts row. Assert that with the
   **`id > cursor`** window **every** such row is selected — no tie-sibling and no
   empty-timestamp row is skipped. This is the regression test for the R3
   timestamp cursor the R4 review flagged.
9. **Per-run input cap — oldest-first by id, no skip.** Seed more new-message
   volume than `MAX_CURATOR_INPUT_CHARS` across the window; assert the curator
   ingests **oldest-first by id** up to the budget, **defers** the higher-id
   overflow, sets `input_truncated: true` with the deferred id range named in the
   summary, and advances the cursor **only to the highest ingested id** — then
   assert a second run **re-reads the deferred overflow** (the silent-skip the R2
   newest-first-drop + advance-to-max combination would have caused never
   happens). Include a tie case: seed sibling rows sharing one timestamp straddling
   the budget boundary and assert the deferred sibling **is re-read** next run
   (impossible to guarantee under a timestamp cursor).
9a. **Oversized single message — truncate, don't stall.** Seed one message whose
   content alone exceeds `MAX_CURATOR_INPUT_CHARS`, as the **oldest unread** row.
   Assert the curator does **not** stall (it ingests a **head+tail-truncated**
   form with the elision marker rather than ingesting zero rows), **advances the
   cursor past that message's id**, and sets `oversized_truncated: 1` naming the
   message (session + id) in the summary. Assert a second run does **not** re-hit
   the same oversized row (the cursor moved past it) — i.e. the permanent-stall is
   impossible.
10. **No silent lossy drop.** Force the curator over-cap with all-still-valid
   decisions; assert any drop of a still-valid item sets `lossy_drops > 0` and is
   named in the change summary (loud degradation), never a silent deletion.
11. **Passive vs active delivery (drain-side filter).** Drive `events/drain.py`
   with a real `policy.updated` envelope. Assert that with `urgent` absent/false
   the drain **does not** call `inbox.push` for it (passive — suppressed before
   delivery, event still observable), and that with `urgent: true` the same path
   **does** push the "re-read policy.md" message to the inbox. This is the
   regression test for the naive "publish → pushes to every subscribed inbox
   regardless of urgent" reading the R2 review flagged.

Integration (`tests/integration/`, real Claude session): a live curator run over
a seeded transcript fixture produces a sane, capped `policy.md` with correctly
sorted `## Facts` / `## Decisions`, retains an un-mentioned old decision, and the
`policy.updated` event reaches a second running agent's inbox. This live variant
is the **real** assertion behind unit test 2 (judgment, not plumbing). A second
integration variant drives a **real rotation against a stalled reconnect** (the
fresh connect turn never completes) and asserts the session bounds the wait and
recovers to addressable rather than wedging — the live counterpart to unit test
6a, since the 2026-06-24 hang was in a real subprocess reconnect, not a mock.

## Implementation Plan

Build inside-out; each step builds + type-checks + passes tests on its own.

1. **Paths + policy doc primitives.** `paths.policy_path()`,
   `paths.policy_cursor_path()`; `memory.py` `load_policy` /
   `format_policy_prompt` + `MAX_POLICY_CHARS`. Load reads the two-section file
   as one capped block. Tests for load + truncation. *(No behavior change yet —
   old path still injected.)*
2. **Injection swap.** Repoint the three injection sites + `doctor` at
   `policy.md`; rename the prompt section to `## Team Policy` (read-only). Test 5.
3. **Rework the rotation path (§6).** (a) Remove the flush: delete
   `_do_flush_and_rotate` / `_verify_flush` / `_snapshot_index`; idle-rotation
   calls `_rotate()` directly; delete the now-dead `load_memory` journal reader +
   its tests. Test 6. (b) Bound the reconnect: wrap `_rotate()`'s `connect()` +
   connect-turn `_drain_turn()` in `asyncio.wait_for(ROTATION_RECONNECT_TIMEOUT)`,
   add `ROTATION_MAX_RECONNECT_ATTEMPTS` bounded retry, and recover into an
   addressable connected client on exhaustion (never terminal `"error"`, never a
   silent park); confirm the #443 `is_error` clearing covers the connect-turn
   drain. Test 6a + the reconnect integration variant.
4. **Curator monitor declaration + flavor.** Add `policy-curator` default
   monitor + the `curator: true` marker; scheduler routes it to the curator
   dispatch (reusing `_default_spawn_check`). Default curator prompt
   (`prompts/curator.md`), **team-overridable** via the prompt-override path.
5. **Curator completion publish + delivery gate.** On success+`updated`, publish
   `system/policy.updated` with summary (bypassing `_reconcile` dedup); add the
   **net-new `events/drain.py` filter** that suppresses the inbox push for a
   non-urgent `policy.updated` and pushes only on `urgent: true` (passive
   otherwise). Tests 3, 4, 11.
6. **Distillation contract: cursor + windowing + caps.** Add
   `history.messages_since(cursor)` (keyed on **`messages.id`**: `WHERE id > ?
   ORDER BY id`); the curator reads the **success-advanced cursor** (a
   `messages.id`, not a timestamp and not `last_run`), enumerates **messages
   across all sessions** since the cursor **oldest-first by id under
   `MAX_CURATOR_INPUT_CHARS`** (deferring the higher-id overflow; truncating an
   oversized oldest message that can't fit, `oversized_truncated`), rewrites the
   two-section `policy.md` under the retention + lossless/lossy rules, and advances
   the cursor only on success to the highest ingested id. Tests 1, 2, 2a, 2b, 7,
   8, 8a, 9, 9a, 10.
7. **One-time seed.** A guarded one-shot that distills the existing
   `memory/<session>/INDEX.md` journal(s) into the first `policy.md` (idempotent:
   no-op if `policy.md` already exists). Q3 = seed once.
8. **Integration test + docs.** Live curator run; update CLAUDE.md (Monitors +
   the removed Decision Log mention) and `DESIGN.md`/skills references to the
   memory model.

## Open Questions — resolved in the review (R2)

All five are now decided from the review's leans; recorded here with rationale.

- **Q1 — framework default vs eng-team default → RESOLVED: framework mechanism,
  team-overridable prompt.** Ship the *mechanism* (curator flavor, injection,
  completion event) framework-level so any team compounds, but make the
  **curator prompt team-overridable** — "what counts as durable" is
  domain-flavored, and a framework that prides itself on no topology opinions
  shouldn't hard-bake one team's notion of policy. (§3, in-scope item 1.)
- **Q2 — default interval → RESOLVED: 6h to start.** Cheap, flat output cost,
  distills a few times a day. Note it interacts with **input** cost — see the
  per-run input cap (§3); a shorter interval means smaller deltas per run.
- **Q3 — one-time seed → RESOLVED: yes, seed once.** Distill the existing
  `INDEX.md` journal(s) into the first `policy.md` rather than starting empty —
  starting empty discards real knowledge, and transcripts age/rotate so it is not
  all re-derivable. (In-scope item 7; impl step 7.)
- **Q4 — watermark → RESOLVED by the §3 fix: dedicated curator cursor advanced
  on success, keyed on `messages.id`.** This was not a nicety — reusing `last_run`
  is *non-functional* (the scheduler clobbers it at dispatch). The
  success-advanced cursor fixes both the dispatch-time race and the
  mid-distillation-skip problem in one move; keying it on the unique `messages.id`
  (R4, not a message timestamp) closes the finer tie-row / empty-timestamp /
  late-index skip class on top.
- **Q5 — single-writer enforcement → RESOLVED: soft doctor check for v1.**
  Curator-vs-curator overlap is already prevented by the interval; the doctor
  check only guards *foreign* writes, a smaller risk. (§7.)

### One deferred lever (not a blocker)
- **Decisions-spill archive.** The only bucket that can genuinely outgrow a cap
  is accreting **still-valid** decisions. v1 does **not** build the read-on-demand
  spill — it builds the **signal** (`lossy_drops` in the change summary). When
  that signal first fires, spill the oldest/least-relevant decisions to an indexed
  archive (still retrievable) rather than deleting them. Build-the-signal-now,
  build-the-spill-when-it-fires.

## Related

- **#454** — rotation metric over-count (wedge mechanism #1). Complementary:
  #454 fixes *why rotation falsely fired*; this removes the *bloat source*
  (mechanism #2) and bounds the *reconnect hang* (mechanism #3). Ship
  independently of #454.
- **#443** — turn-level API-error (529) clearing at `session.py:393`. This spec's
  §6b reconnect hardening builds on it (confirms it covers the connect-turn
  drain) and adds the orthogonal *never-receives-a-message* timeout it does not
  cover.
- **Manager self-heal watchdog** ([#464](https://github.com/moda-labs/bobi-agent/issues/464)) — a follow-up ticket (out of scope here) for a
  health-endpoint watchdog that restarts a wedged *director* on a stalled
  `last_activity`, covering **unknown** wedge classes beyond the three enumerated.
- Mirrors the framework's own context-files pattern (index + read-on-demand) and
  Claude Code's memory model (curated, deduped, not a log).

---

## Original issue (#456) — preserved verbatim

### North star
The agent team should **get smarter as it's used** — accumulate durable, reusable knowledge — *without* the per-prompt context growing unbounded. The current decision log does the opposite: it's an append-only journal that **accumulates** (it grew to 127KB live), which both bloats every prompt and isn't actually learning. Replace it with a small, **rewritten-in-place** `policy.md`, maintained out-of-band by a curator that distills the team's transcripts.

### Background (live incident, 2026-06-23, moda-eng-team)
The director wedged for ~2h40m and required a manual nuke (clear session + delete the decision-log `INDEX.md`). The append-only decision log was a contributing aggravator: it grew unbounded (the agent appends a RESTART/RESUME block every restart and **never prunes** — asked to prune while over-cap, it *grew* the log instead), and it's redundant with the transcripts we already keep. Root-cause of the false "over-cap" that triggered the wedge is the rotation metric (#454, separate). This ticket removes the decision log entirely and replaces it with a curated, bounded knowledge doc.

### Design (rides existing infra — small net new work)
A **curator runs as a monitor**. bobi agent <name> monitors already run an agent **out-of-band on a schedule** and treat its output as data ("a description-only monitor's check agent runs out-of-band, only observes, returns a verdict"). The curator is the same pattern, except its output is a rewritten document instead of a verdict.

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
- The append-only **decision log** (`run/state/memory/<session>/INDEX.md` as a journal) and its **rotation flush** step. `policy.md` replaces it. (Keep the memory dir mechanics if reused for `policy.md`; drop the append-on-rotation behavior.)

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
