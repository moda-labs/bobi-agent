# Spec — #411: pr-feedback dispatch hygiene (self-author skip on ALL event types, human-author hard-skip, draft skip, per-comment dedup)

- **Issue:** [moda-labs/modastack#411](https://github.com/moda-labs/modastack/issues/411)
- **Type:** bug (event-reactor / auto-dispatch)
- **Status:** SPEC — held for Zach's approval. Implementation is gated on sign-off; this PR must not auto-build past the spec gate.
- **Author:** engineer (spec phase)
- **Related:** #326 (reactor dedup key — merged, commit `ded0375`), #321 (duplicate-comment dispatch — merged), #412 (lifecycle auto-advance past spec gate — open, adjacent)
- **Concrete harm:** #416 / #417 / #418 — three near-identical "Reusable tool library" tickets the bot filed from a **single** trigger; #417 and #418 are now closed as duplicates of #416 (see §1(c))
- **Most severe harm (live, 2026-06-22):** #423 — the auto-dispatched `pr-feedback` engine acted on a **human-authored** PR (lukelin10, branch `luke/setup-home-nav`) and **pushed a bot revert commit (`15a7fb5`, Co-Authored-By Claude) to the human's branch**, then self-cascaded off its own push/title-edit `synchronize` events. None of (a)/(b)/(c) would have stopped it — it was a genuine human comment on a human-owned PR. This is the motivation for the new part (d) (see §1(d)).

> This spec is a strict superset of the issue (summary + the follow-up comment that added part (c)). Nothing in the issue is dropped.

---

## 1. Problem

The deterministic event reactor (`modastack/events/reactor.py`) auto-dispatches a `pr-feedback`
engineer in four situations where it must not. Each wastes an agent launch and — worse — risks an
engineer **editing a PR it has no business touching**: a spec PR explicitly held for human approval
(parts a/b), or — most severely — a **human-authored** PR the bot pushed an unrequested commit onto
(part d, observed live in #423). Both directly defeat the boundary that auto-dispatch is supposed to
respect.

### (a) Dispatch on the bot's OWN events — comments **and** pushes / synchronizes / reviews / edits

`pr-feedback` (and every dispatch rule) must never re-trigger off the bot's **own** activity, on **any**
event type — not just comments. A bot auto-reacting to an event it itself emitted is never the intent.
Two distinct self-trigger vectors have been observed:

**Self-authored comments (original report).** Per the spec-PR / markdown policy, the lead posts a
"📄 Rendered spec" link comment on every spec PR. That self-authored `issue_comment` re-enters the
event stream and matches the `github.issue_comment` → `pr-feedback` auto-dispatch rule, spinning up an
engineer even though there is no human feedback to act on. Observed repeatedly on spec PRs
**#405, #407, #410**, and again on impl PRs **#413, #414** (it fires on the bot's own "Ready for
review" / "held pending approval" comments too — not only spec/draft PRs). The lead has had to
recognize and no-op these by hand each time.

**Self-authored pushes / title-edits → `synchronize` self-cascade (live, #423, 2026-06-22).** The
self-trigger is not limited to comments. On **#423** the `pr-feedback` engineer pushed its own revert
commit (`15a7fb5`) and then **edited the PR title**; both the bot's `push` and the title-edit emitted
GitHub `pull_request` **`synchronize`** events whose `sender` was the **bot itself**. Those events
re-entered the reactor and **re-triggered `pr-feedback` ≥2 more times** (sessions **`d8e2aa12`** and
**`73da8106`**) even though **nothing had changed** — `HEAD` stayed at `15a7fb5` across all of them.
The loop compounds purely off the bot's own events: each dispatch's push/edit feeds the next.

The original part-(a) fix was framed narrowly around `issue_comment`s. **This spec broadens it: the
self-author skip must apply to every event type the reactor dispatches on — `issue_comment`,
`pull_request_review`, `pull_request_review_comment`, and crucially `push` / `pull_request`
(`synchronize`, edited) events** — keyed on the event `sender`, not on the event being a comment. See
the broadened §3(a). (This is distinct from part (d): (a) skips the bot's own *events* on **any** PR
including its own; (d) skips dispatch onto a *human-owned* PR regardless of who triggered. #423 is
caught by **both** — see the note at the end of §1(d).)

### (b) Dispatch on DRAFT PRs held for approval

Spec PRs go up as **draft** and are explicitly held: implementation is gated on a human sign-off.
A `pr-feedback` engineer dispatched against a held draft can edit the spec before the human has
approved it — the exact thing the spec-approval gate exists to prevent. Even a *genuine human comment*
on a held draft spec PR should open a discussion, not dispatch an editor.

### (c) No per-comment dedup — one comment fans out multiple engineers

A single bot-authored comment on held draft PR **#413** dispatched **two** `pr-feedback` engineers
simultaneously (a third on a later check); the lead cancelled all three before any touched the PR.

**Concrete harm — duplicate tickets, not just duplicate launches (#416/#417/#418).** The most visible
damage from the missing per-trigger dedup is not wasted launches that the lead catches in flight — it
is **duplicate work products that escape onto GitHub**. One logical trigger (Zach's "open a new ticket
to build out a reusable tool library" instruction on PR **#407**, 2026-06-22) fanned out into **three**
near-identical issues, all authored by `modastack` within **51 seconds**:

| Issue | Created (UTC) | State | Title |
|-------|---------------|-------|-------|
| #416 | 17:04:59 | OPEN | Reusable tool library: opt-in catalog of baked CLI tools … |
| #417 | 17:05:45 | CLOSED (dup) | Reusable tool library: opt-in catalog of (pinned binary + tools guide) … |
| #418 | 17:05:50 | CLOSED (dup) | Reusable tool library: define-once catalog of binary + guide … |

Three engines each independently created the same ticket because nothing deduped on the **stable
trigger identity**. #417 and #418 were later closed by hand as duplicates of #416 — the same manual
clean-up the lead performs for the in-flight engine fan-out above, except here the spam reached the
issue tracker first. This is the same root cause as the duplicate-engine fan-out (part c), and it is
the strongest motivation for anchoring dedup on a stable identity rather than a volatile per-delivery
id.

**Root cause (verified in code, current `origin/main`):**

1. `#326` (commit `ded0375`) changed `AutoDispatchRule.dedup_key` from PR-level
   (`{workflow}:{topic}:{number}`) to append the **per-delivery event id**:
   `f"{base}:{event_id}"` where `event_id = event.get("id")`. That fixed the *comment-drop* side of
   #326 (distinct comments now each dispatch), but the per-delivery id is **not a stable comment
   identity**:
   - The event id is the GitHub webhook *delivery* id, and the worker adapter falls back to
     `crypto.randomUUID()` when the delivery id is absent (`event-server/src/adapters/github.ts`:
     `id: deliveryId || crypto.randomUUID()`). The **same logical comment** redelivered without a
     delivery id gets a **fresh random key** → no dedup → re-dispatch.
2. `EventReactor._dispatched` is an **in-memory dict, per reactor instance**. With concurrent lead
   sessions (a documented multi-session / crash-relaunch race), each reactor has its own empty
   `_dispatched` → the same event dispatches once per process.
3. The launch-time "already active" guard cannot catch this. The reactor calls
   `launch_agent(...)` **without** a `run_key`, so `launch_agent` defaults to a **random**
   `run_key = f"adhoc-{uuid4().hex[:8]}"` → a unique `session_name` per dispatch
   (`make_session_name(workflow, project, run_key)`). Because the
   "A run is already active" guard (`subagent.py:752`) keys on `session_name`, two dispatches for the
   same comment have different session names and the guard **never fires**. Dedup is therefore the
   *sole* fan-out guard, and it is keyed on a volatile id.

So the fan-out has two layers: a volatile dedup key (in-process) and no cross-process / launch-level
guard. The fix must anchor dedup to the **stable comment identity** and (recommended) make the
dispatch `run_key` deterministic so the existing active-run guard becomes a real second line of defense.

### (d) Dispatch on HUMAN-authored PRs — the bot pushed a commit to a human's branch

`pr-feedback` exists to let the bot iterate on **its own** PRs in response to review. It has no
business dispatching an editor against a PR a **human** owns: doing so means the bot pushes commits to
someone else's branch without being asked.

**Live incident — #423 (2026-06-22).** This is the most severe observed harm in the whole ticket,
and it is qualitatively worse than the spec-PR cases above because the bot mutated **work it did not
author**:

- **#423 = a human PR.** Authored by **lukelin10**, branch **`luke/setup-home-nav`** (the v0.28.1
  "setup home-nav" work). Not a bot PR, not a draft spec PR.
- Zach left a genuine **human** review comment on it ("version and changelog should only be updated
  during releases").
- The reactor auto-dispatched a `pr-feedback` engineer on that review. The engineer **pushed a bot
  commit — `15a7fb5` ("drop version bump and changelog", `Co-Authored-By: Claude`) — to Luke's branch
  at 19:04:48Z**, rewriting a human contributor's PR.
- It then **self-cascaded**: its own `push` and a subsequent PR-title edit each emitted GitHub
  `synchronize` events that re-entered the reactor and re-triggered dispatch multiple times off the
  bot's own activity.

**Why (a), (b), and (c) do not catch this.** The incident slips through every existing guard in this
spec:

- **(a) self-author skip** keys on the event `sender`. For the **initial** trigger this does not help —
  the sender was Zach (a human), so his review comment is a legitimate dispatch trigger; (a) is about
  *who sent the event*, not *whose PR it is*. (Note: broadened (a) — see §1(a)/§3(a) — **does** catch
  the **subsequent self-cascade**, where the bot's own push/title-edit `synchronize` events have
  `sender == bot`. So (a) and (d) split the #423 incident: (d) blocks the *first* unwanted dispatch off
  Zach's comment; broadened (a) blocks the *self-cascade* that followed. Both are needed.)
- **(b) draft skip** does not apply — #423 was a ready (non-draft) PR.
- **(c) per-comment dedup** would at most collapse the cascade to *one* unwanted push; it does not
  stop the bot from touching a human PR at all.

The missing guard is orthogonal to all three: dispatch must be gated on **PR authorship**, not just
comment authorship or draft state. `pr-feedback` must **hard-skip any PR whose author is not the bot
identity** — i.e. dispatch only when `pr.author == bot_login`. This is the direct, narrowly-scoped
fix for #423 and a clean sibling to (a)/(b)/(c).

> Note the symmetry with (a): (a) checks the **event `sender`** (`fields.sender`, now across all event
> types — comments, pushes, synchronizes, edits); (d) checks the **PR owner**
> (`pull_request.user.login`). Both resolve against the same cached bot login from §3(a), so (d) adds a
> guard, not a new identity source. They are complementary: on #423 the **initial** human-comment
> trigger passes (a) and is stopped only by (d); the **self-cascade** that follows (bot's own
> push/title-edit) is stopped by broadened (a) regardless of PR ownership. Defense-in-depth, not
> redundancy.

---

## 2. Solution (overview)

Four independent, composable changes in the reactor + adapter, each guarded by a test that fails first:

| Part | Change | Primary file |
|------|--------|--------------|
| (a) | Skip auto-dispatch for **any event type** whose `sender` is the bot's own GitHub identity — comments **and** `push` / `synchronize` / review / edit events (closes the #423 self-cascade) — **default-on**, no enable flag, with an `allow_self_authored: true` opt-in escape hatch. | `modastack/events/reactor.py` (+ adapter `fields.sender` on push/synchronize) |
| (b) | Skip `pr-feedback` dispatch when the target PR is a **draft**. | `reactor.py` (+ adapter enrichment) |
| (c) | Anchor dedup on the **stable comment/review id** and pass a **deterministic `run_key`** so the active-run guard prevents fan-out. | `reactor.py` (+ adapter field) |
| (d) | **Hard-skip `pr-feedback` on human-authored PRs** — dispatch only when the PR author == bot identity. | `reactor.py` (+ adapter field) |

All four are scoped to the dispatch path. No change to workflow DAGs, role prompts, or the
`pr-feedback` workflow body. Blast radius is the reactor + one adapter field; both are unit- and
integration-tested.

---

## 3. Technical approach

### (a) Self-author skip

The adapter already emits `fields.sender` (`payload.sender.login`). The reactor must learn its **own**
GitHub login to compare. The bot's identity is the authenticated `gh` token's user — today that is
`modastack` (verified via `gh api user --jq .login`).

- Resolve the bot login **once** at reactor construction (or first use) via `gh api user --jq .login`,
  cache it on the `EventReactor`. No config key is needed to *resolve identity* — the token is the
  single source of truth, which also survives token rotation (a documented operational event).
- **Skip is the default and is always on — no flag enables it.** In `process()`, before dispatching,
  if the bot login is known and `fields.sender == bot_login`, **skip** (return `None`, log
  `Auto-dispatch skipped (self-authored): <key>`). This applies to **all** dispatch rules. A bot
  auto-reacting to its own action is never the intent, so it requires no opt-in to turn on.
- **The skip is keyed on the event `sender` for EVERY event type the reactor dispatches on — not just
  comments.** This is the broadening over the original part-(a) framing. Concretely it must cover:
  - `issue_comment`, `pull_request_review`, `pull_request_review_comment` (the original comment/review
    vectors), **and**
  - `push` and `pull_request` **`synchronize`** / **`edited`** events — the #423 self-cascade vector,
    where the bot's own commit-push and PR-title edit each emit a `synchronize` whose `sender` is the
    bot. These must be skipped on `sender` even though no comment is involved and even on the bot's own
    PR (so the loop is closed before part (d)'s author check is reached).
- **Adapter prerequisite.** Because the skip now gates push/synchronize/edited events, the worker
  adapter must emit `fields.sender = payload.sender.login` for **those** event types too (it already
  does for comment/review events). Verify `event-server/src/adapters/github.ts` populates `sender` on
  the `push` and `pull_request` event paths; add it where missing. Without `fields.sender` on these
  events the reactor cannot recognize them as self-authored and the cascade persists.
- **Opt-in escape hatch for the rare reverse case.** The only realistic situation where you'd *want*
  the bot to react to its own event is a rule that deliberately self-chains — e.g. a future rule that
  triggers off a structured command comment the bot posts to itself as a work queue. For that, a rule
  may set an explicit `allow_self_authored: true`; absent the flag, self-authored events are skipped.
  **None of the shipped `eng-team` rules set it**, and the framework default stays skip-on.
- `suppress` rules are unaffected (they already return without launching).
- Fail-open: if the login can't be resolved (network/auth blip), do **not** skip — preserve today's
  behavior rather than silently dropping real events.

> **Resolved (was "scope of (a)" open question) — per review (underminedsk, 2026-06-22).** The
> self-author skip is **default-on for every rule with no config field to enable it**, plus an
> `allow_self_authored: true` per-rule opt-in for the rare deliberate self-trigger. This is exactly the
> "default to skip, opt-in to receive your own events" shape the reviewer asked for.

> ⚠️ **For Zach's review — broadening (a) to push/synchronize/edit likely shifts earlier
> design points/decisions.** Part (a) was originally scoped, reviewed, and resolved as a *comment*
> skip. Extending it to all event types (`push`, `synchronize`, `edited`) changes assumptions baked
> into the existing decisions, and these knock-on effects need explicit sign-off:
> - **`allow_self_authored` opt-in semantics widen.** The resolved decision notes `pr-closed` /
>   `issues.assigned` carry `allow_self_authored: true` because they legitimately act on the bot's own
>   merges/assigns. With (a) now covering push/synchronize, **re-confirm which rules need the opt-in** so
>   broadening the skip doesn't silently suppress a self-chain a rule actually depends on (e.g. any rule
>   meant to react to the bot's own push). New open question **D5** below.
> - **Overlap with part (d) on #423.** #423 is now caught by *both* (a) (bot is the `synchronize`
>   sender) and (d) (PR author is human). That redundancy is deliberate defense-in-depth, but it means
>   the #423 self-cascade is closed by (a) **independent of PR ownership** — so the cascade is also
>   stopped on the bot's *own* PRs, which (d) alone would not do. Confirm this is the intended layering.
> - **Adapter scope grows.** (a) now depends on `fields.sender` being present on push/synchronize
>   events (see adapter prerequisite above), adding an adapter test surface that the comment-only
>   framing did not have.

> **Decision point D1 (bot identity *source*).** Separate from the skip default above: how the reactor
> learns *which* login is its own. Recommended: resolve from `gh api user` and cache. Alternative: add
> an explicit `services.github.identity` config field. Recommend the token-derived approach (no
> duplication, rotation-safe).

### (b) Draft skip

`pr-feedback` should not dispatch against a draft PR.

- **Review events** (`github.pull_request_review`, `github.pull_request_review_comment`) carry
  `payload.pull_request.draft`. Enrich the adapter to set `fields.draft = pr.draft` (boolean) for
  these — free, no API call.
- **`issue_comment` events** (the dominant observed case — the rendered-spec link comment) do **not**
  include the PR's draft state in the webhook payload (`issue.pull_request` is a bare ref). The reactor
  resolves it via an authenticated `gh pr view <number> --repo <repo> --json isDraft` lookup.
- To preserve the invariant that **the drain thread never blocks on the network** (the documented
  reason `_dispatch` already runs off-thread), the draft lookup for `issue_comment` runs in the
  **off-thread launch path**, immediately before `launch_agent`. If the PR is a draft, log
  `Auto-dispatch skipped (draft PR): <key>` and do not launch. (Wasted-dispatch elimination for
  `issue_comment` is therefore best-effort but reliably prevents the engineer from *running*; review
  events skip synchronously via the `fields.draft` field with zero added latency.)
- Mechanism in the rule: add a `skip_draft: true` flag on the `pr-feedback` rules in `agent.yaml`
  (explicit, opt-in, leaves non-PR rules untouched).
- Fail-open: if the draft lookup errors, do **not** skip — dispatch and let the workflow's
  verify-live step decide (matches today's "verify review state before acting" policy).

> **Decision point D2 (draft source for `issue_comment`).** Recommended: reactor-side
> `gh pr view --json isDraft`, off the drain thread (self-contained — the Python side always has
> `GH_TOKEN`). Alternative: enrich draft in the worker adapter via a REST call, which would require a
> GH token in the Cloudflare worker env (an infra dependency we don't currently have). Recommend the
> reactor-side lookup.

### (c) Per-comment dedup + deterministic run_key

Two coordinated changes, both reusing existing machinery:

1. **Anchor the dedup key on stable comment identity.** Enrich the adapter to emit a stable
   `fields.comment_id` (from `payload.comment.id` for `issue_comment` / `pull_request_review_comment`)
   and `fields.review_id` (from `payload.review.id` for `pull_request_review`). Change `dedup_key` to
   prefer this stable id over the volatile per-delivery `event.id`:
   `f"{workflow}:{topic}:{number}:{comment_id or review_id or event_id}"`.
   This keeps #326's intent (distinct comments each dispatch) while making genuine redelivery of the
   *same* comment dedup correctly — even when the delivery id is absent and the worker would otherwise
   mint a fresh random id.
2. **Deterministic `run_key`.** Pass `run_key=f"{number}-{comment_id or review_id}"` (sanitized) into
   `launch_agent` from `_dispatch`. The resulting `session_name` is then identical for two dispatches
   of the same comment, so the existing **"A run is already active" guard** (`subagent.py:752`) rejects
   the duplicate — the second dispatch is caught by the `RuntimeError` handler `_dispatch` already has.
   Because the run registry is backed by persisted session state, this guard also covers the
   **cross-process** (concurrent-session) race, not just the in-process one.

Together: the dedup key is the fast path (skip before launch); the deterministic `run_key` is the
authoritative guard (one engineer per comment, process-independent).

> **Decision point D3 (cross-process dedup scope).** Recommended: the deterministic-`run_key` +
> persisted-active-run-guard approach above — it fixes both in- and cross-process fan-out by reusing
> machinery that already exists, with no new persistent store. Alternative (heavier, out of scope
> here): a durable shared dedup store for `_dispatched`. Recommend D3-A; if the run registry turns out
> not to be cross-process-durable, the integration test in §4 will catch it and we escalate to the
> durable store as a follow-up.

### (d) Human-author hard-skip

`pr-feedback` must dispatch **only** when the target PR's author is the bot identity. Any PR authored
by a human is hard-skipped — the bot never pushes to a branch it does not own.

- Reuse the cached bot login from §3(a) (`gh api user --jq .login`, today `modastack`). No new
  identity source.
- **Resolve the PR author per event type, mirroring (b)'s draft sourcing:**
  - **Review events** (`pull_request_review`, `pull_request_review_comment`) carry
    `payload.pull_request.user.login`. Enrich the adapter to set `fields.pr_author` (string) for these
    — free, no API call. Skip synchronously when `fields.pr_author != bot_login`.
  - **`issue_comment` events** do not include PR authorship in the webhook payload. Resolve it in the
    **off-thread launch path** (same place as the draft lookup) via
    `gh pr view <number> --repo <repo> --json author --jq .author.login`. Fold this into the **single**
    `gh pr view` call that (b) already makes (`--json isDraft,author`) so (b) and (d) cost **one**
    lookup, not two.
  - **`synchronize` / `push`-derived events** (the #423 self-cascade vector) carry
    `payload.pull_request.user.login` and are gated identically — the bot's own push to a human PR is
    skipped on author, not just on sender.
- Mechanism in the rule: gate behind a `require_bot_author: true` flag on the `pr-feedback` rules in
  `agent.yaml` (explicit, opt-in, leaves non-`pr-feedback` rules untouched). Combined with (b)'s
  `skip_draft`, the two PR-state guards are declared side by side on the same rules.
- On skip: log `Auto-dispatch skipped (human-authored PR): <key>` and do not launch.
- **Fail-CLOSED (deliberate asymmetry vs (a)/(b)).** If the bot login or the PR author cannot be
  resolved, **skip** rather than dispatch. Rationale: the failure mode this part prevents — the bot
  pushing a commit onto a human's branch (#423) — is far costlier than a missed dispatch on one of the
  bot's own PRs, which a human can re-trigger with a fresh comment. This is the one part of the spec
  that fails closed; (a) and (b) remain fail-open because their worst case is only a wasted launch.

> **Decision point D4 (fail-open vs fail-closed for (d)).** Recommended: **fail-closed** as above — a
> false skip on a bot PR is cheap and recoverable, a false dispatch onto a human PR is the exact harm
> #423 demonstrated. Confirm with Zach; if he prefers symmetry with (a)/(b), the alternative is
> fail-open, accepting that an unresolved author would let a human-PR dispatch through.

---

## 4. Verification plan

Per `CLAUDE.md`: **"production bug = integration test gap."** Each part gets a test that **fails on
current `main` first**, then passes after the fix.

### Unit tests (`tests/test_reactor.py`)

- (a) `test_skips_dispatch_when_sender_is_bot` — event with `fields.sender == bot_login` → `process()`
  returns `None`, no launch. And `test_dispatches_when_sender_is_human` (regression guard — human
  comments still dispatch).
- (a) `test_skips_bot_authored_synchronize_event` — `pull_request` `synchronize` event with
  `fields.sender == bot_login` → no dispatch (reproduces the #423 self-cascade; broadened (a) covers
  non-comment event types). Parametrize over `push` / `synchronize` / `edited`.
- (a) `test_self_author_skip_fails_open_when_login_unknown` — login unresolved → dispatch proceeds.
- (a) `test_allow_self_authored_opt_in_dispatches` — rule with `allow_self_authored: true` + bot
  `sender` → dispatch proceeds (escape hatch works, default-skip is overridable per rule).
- (b) `test_skips_pr_feedback_on_draft_review_event` — `fields.draft == True` → no dispatch.
  `test_dispatches_pr_feedback_on_ready_pr` — `draft == False` → dispatch (regression guard).
- (b) `test_draft_lookup_fails_open` — lookup raises → dispatch proceeds.
- (c) `test_dedup_key_uses_stable_comment_id` — two events, same `comment_id`, different `event.id` →
  one dispatch. `test_distinct_comments_dispatch_independently` — different `comment_id` → two
  dispatches (preserves #326).
- (c) `test_dispatch_passes_deterministic_run_key` — `_dispatch` calls `launch_agent` with
  `run_key` derived from the comment id.
- (d) `test_skips_pr_feedback_on_human_authored_pr` — `fields.pr_author != bot_login` → no dispatch
  (reproduces #423). `test_dispatches_pr_feedback_on_bot_authored_pr` — `pr_author == bot_login` →
  dispatch (regression guard — the bot still iterates on its own PRs).
- (d) `test_human_author_skip_fails_closed_when_author_unknown` — PR author/bot login unresolved →
  **skip** (asserts the deliberate fail-closed behavior, opposite of (a)/(b)).

### Adapter tests (`event-server` test suite)

- `fields.draft` set from `pull_request.draft` on review / review_comment events.
- `fields.comment_id` / `fields.review_id` set from the respective payload objects.
- `fields.pr_author` set from `pull_request.user.login` on review / review_comment / `synchronize`
  events (part d).
- `fields.sender` set from `payload.sender.login` on `push` and `pull_request`
  (`synchronize` / `edited`) events (part a — broadening; without it the self-cascade skip can't fire).

### Integration test (`tests/test_drain_dispatch.py`)

Drive the real drain → reactor pipeline against the shipped `eng-team` `auto_dispatch` config
(extends the harness #326 added):

1. Bot-authored `issue_comment` on a PR → **zero** dispatches (part a).
2. Human `issue_comment` on a **draft** PR → zero dispatches (part b).
3. The **same** human comment (same `comment_id`) delivered **twice** → exactly **one** dispatch
   (part c, redelivery).
4. **Human review comment on a ready, human-authored PR → zero dispatches (part d, reproduces #423).**
   The same fixture re-delivered as a bot `synchronize` self-cascade event (`sender == bot`) → still
   zero — closed by **broadened (a)** (self-sender skip on synchronize), and would also be closed by
   (d) on author. Assert zero so the #423 self-trigger loop is provably shut on both axes.
5. Human comment on a **bot-authored, ready** PR → exactly one dispatch (regression — the happy path,
   the bot iterating on its own PR, still works).

### Regression / non-goals to protect

- #326 behavior preserved: distinct human comments on the same ready PR each dispatch.
- `review_requested` suppress rule unchanged.
- `issues.assigned` → `issue-lifecycle` and `pull_request.closed` → `pr-closed` dispatch unchanged.

### Commands

```bash
pytest tests/test_reactor.py tests/test_drain_dispatch.py -q   # unit + integration
cd event-server && <adapter test cmd>                          # adapter field tests
```

---

## 5. Scope

### In scope
- `modastack/events/reactor.py`: self-author skip (default-on, `allow_self_authored` opt-in) **applied
  to all event types incl. `push` / `synchronize` / `edited` (part a broadening — closes #423
  self-cascade)**, draft skip, stable-comment-id dedup key, deterministic `run_key`, **human-author
  hard-skip (part d)**.
- `event-server/src/adapters/github.ts`: emit `fields.draft`, `fields.comment_id`, `fields.review_id`,
  **`fields.pr_author`**, and **`fields.sender` on `push` / `pull_request` (`synchronize`/`edited`)
  events** (part a broadening).
- `agents/eng-team/agent.yaml`: `skip_draft: true` **and `require_bot_author: true`** on the
  `pr-feedback` rules.
- Unit + adapter + integration tests above.

### Out of scope
- A durable/shared cross-process dedup store (only if D3-A proves insufficient — separate ticket).
- #412 (lifecycle auto-advancing past the spec-approval gate) — related but distinct.
- Any change to the `pr-feedback` workflow body, role prompts, or other workflows.
- Slack / Linear dispatch paths (GitHub reactor only).

---

## 6. Implementation plan (post-approval — do NOT start until Zach signs off)

1. Write the failing unit tests (a/b/c/d) in `tests/test_reactor.py` — confirm red on `main`.
2. Adapter: add `fields.draft`, `fields.comment_id`, `fields.review_id`, `fields.pr_author`, and
   `fields.sender` on `push` / `synchronize` / `edited` events + adapter tests.
3. Reactor (a): resolve + cache bot login; default-on self-author skip in `process()` **for all event
   types (comments, reviews, `push`, `synchronize`, `edited`)**, honoring a per-rule
   `allow_self_authored: true` opt-in. Re-audit which shipped rules need the opt-in given the broadened
   scope (see D5).
4. Reactor (b): `skip_draft` rule flag; field-based skip for review events, off-thread `gh pr view`
   lookup for `issue_comment`.
5. Reactor (c): stable-id dedup key; deterministic `run_key` into `launch_agent`.
6. Reactor (d): `require_bot_author` rule flag; field-based author skip for review/`synchronize`
   events, author resolved in the **same** off-thread `gh pr view --json isDraft,author` call as (b);
   **fail-closed** when author/bot login is unresolved.
7. `agent.yaml`: add `skip_draft: true` and `require_bot_author: true` to the two `pr-feedback` rules.
8. Extend the integration test (including the #423 / self-cascade case); run full suite +
   `modastack workflows validate`.
9. `/review`; fix everything it finds.
10. Open the impl PR against `main` (it — not this spec PR — carries `Fixes #411`).

---

## 7. Open questions for Zach

- **D1:** bot identity from `gh api user` (recommended) vs explicit `agent.yaml` config field?
- **D2:** draft source for `issue_comment` — reactor-side `gh pr view` off-thread (recommended) vs
  worker adapter REST call (needs a GH token in the worker)?
- **D3:** accept deterministic-`run_key` + persisted active-run guard for cross-process dedup
  (recommended) vs invest now in a durable shared dedup store?
- **D4:** for the human-author hard-skip (part d), **fail-closed** when the PR author can't be resolved
  (recommended — #423 showed a false dispatch onto a human PR is the costly direction) vs fail-open for
  symmetry with (a)/(b)?
- **Scope of (d):** confirm the human-author hard-skip is scoped to **`pr-feedback`** only (other
  workflows like `pr-closed` legitimately act on human PRs). (Recommended: `pr-feedback`-only via the
  `require_bot_author` rule flag.)
- **D5 (broadening (a) to push/synchronize/edit — NEW, needs review):** part (a) now skips the bot's
  own `push` / `synchronize` / `edited` events, not just comments (closes the #423 self-cascade). This
  shifts decisions already resolved under the comment-only framing — see the ⚠️ callout in §3(a).
  Confirm: (1) the broadened scope is wanted; (2) the `allow_self_authored: true` opt-in set is
  re-audited so no rule that legitimately reacts to the bot's own push is silently suppressed; (3) the
  deliberate (a)+(d) overlap on #423 (cascade closed independent of PR ownership) is the intended
  layering. (Recommended: ship the broadening — the #423 evidence shows the self-cascade is the live
  recurring harm and comment-only (a) does not stop it.)

**Resolved during review:**

- ~~**Scope of (a):**~~ **Decided (underminedsk, 2026-06-22):** self-author skip is **default-on for
  all dispatch rules with no config field to enable it**, plus an `allow_self_authored: true` per-rule
  opt-in for the rare deliberate self-trigger. See §3(a). (The shipped `pr-closed` / `issues.assigned`
  rules carry `allow_self_authored: true` because they legitimately act on the bot's own merges/assigns.)
