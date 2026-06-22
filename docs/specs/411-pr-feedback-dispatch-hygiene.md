# Spec — #411: pr-feedback dispatch hygiene (self-author skip, draft skip, per-comment dedup)

- **Issue:** [moda-labs/modastack#411](https://github.com/moda-labs/modastack/issues/411)
- **Type:** bug (event-reactor / auto-dispatch)
- **Status:** SPEC — held for Zach's approval. Implementation is gated on sign-off; this PR must not auto-build past the spec gate.
- **Author:** engineer (spec phase)
- **Related:** #326 (reactor dedup key — merged, commit `ded0375`), #321 (duplicate-comment dispatch — merged), #412 (lifecycle auto-advance past spec gate — open, adjacent)
- **Concrete harm:** #416 / #417 / #418 — three near-identical "Reusable tool library" tickets the bot filed from a **single** trigger; #417 and #418 are now closed as duplicates of #416 (see §1(c))

> This spec is a strict superset of the issue (summary + the follow-up comment that added part (c)). Nothing in the issue is dropped.

---

## 1. Problem

The deterministic event reactor (`modastack/events/reactor.py`) auto-dispatches a `pr-feedback`
engineer in three situations where it must not. Each wastes an agent launch and — worse — risks an
engineer **editing a spec PR that is explicitly held for human approval**, directly defeating the
spec-approval gate.

### (a) Dispatch on the bot's OWN comments

Per the spec-PR / markdown policy, the lead posts a "📄 Rendered spec" link comment on every spec PR.
That self-authored `issue_comment` re-enters the event stream and matches the
`github.issue_comment` → `pr-feedback` auto-dispatch rule, spinning up an engineer even though there
is no human feedback to act on.

Observed repeatedly on spec PRs **#405, #407, #410**, and again on impl PRs **#413, #414** (it fires
on the bot's own "Ready for review" / "held pending approval" comments too — not only spec/draft PRs).
The lead has had to recognize and no-op these by hand each time.

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

---

## 2. Solution (overview)

Three independent, composable changes in the reactor + adapter, each guarded by a test that fails first:

| Part | Change | Primary file |
|------|--------|--------------|
| (a) | Skip auto-dispatch for events whose `sender` is the bot's own GitHub identity. | `modastack/events/reactor.py` |
| (b) | Skip `pr-feedback` dispatch when the target PR is a **draft**. | `reactor.py` (+ adapter enrichment) |
| (c) | Anchor dedup on the **stable comment/review id** and pass a **deterministic `run_key`** so the active-run guard prevents fan-out. | `reactor.py` (+ adapter field) |

All three are scoped to the dispatch path. No change to workflow DAGs, role prompts, or the
`pr-feedback` workflow body. Blast radius is the reactor + one adapter field; both are unit- and
integration-tested.

---

## 3. Technical approach

### (a) Self-author skip

The adapter already emits `fields.sender` (`payload.sender.login`). The reactor must learn its **own**
GitHub login to compare. The bot's identity is the authenticated `gh` token's user — today that is
`modastack` (verified via `gh api user --jq .login`).

- Resolve the bot login **once** at reactor construction (or first use) via `gh api user --jq .login`,
  cache it on the `EventReactor`. No new config key — the token is the single source of truth, which
  also survives token rotation (a documented operational event).
- In `process()`, before dispatching, if the bot login is known and `fields.sender == bot_login`,
  **skip** (return `None`, log `Auto-dispatch skipped (self-authored): <key>`).
- Scope: applies to **all** dispatch rules, not just `pr-feedback`. A bot auto-reacting to its own
  action is never desired. `suppress` rules are unaffected (they already return without launching).
- Fail-open: if the login can't be resolved (network/auth blip), do **not** skip — preserve today's
  behavior rather than silently dropping real events.

> **Decision point D1 (bot identity source).** Recommended: resolve from `gh api user` and cache.
> Alternative: add an explicit `services.github.identity` config field. Recommend the token-derived
> approach (no duplication, rotation-safe).

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

---

## 4. Verification plan

Per `CLAUDE.md`: **"production bug = integration test gap."** Each part gets a test that **fails on
current `main` first**, then passes after the fix.

### Unit tests (`tests/test_reactor.py`)

- (a) `test_skips_dispatch_when_sender_is_bot` — event with `fields.sender == bot_login` → `process()`
  returns `None`, no launch. And `test_dispatches_when_sender_is_human` (regression guard — human
  comments still dispatch).
- (a) `test_self_author_skip_fails_open_when_login_unknown` — login unresolved → dispatch proceeds.
- (b) `test_skips_pr_feedback_on_draft_review_event` — `fields.draft == True` → no dispatch.
  `test_dispatches_pr_feedback_on_ready_pr` — `draft == False` → dispatch (regression guard).
- (b) `test_draft_lookup_fails_open` — lookup raises → dispatch proceeds.
- (c) `test_dedup_key_uses_stable_comment_id` — two events, same `comment_id`, different `event.id` →
  one dispatch. `test_distinct_comments_dispatch_independently` — different `comment_id` → two
  dispatches (preserves #326).
- (c) `test_dispatch_passes_deterministic_run_key` — `_dispatch` calls `launch_agent` with
  `run_key` derived from the comment id.

### Adapter tests (`event-server` test suite)

- `fields.draft` set from `pull_request.draft` on review / review_comment events.
- `fields.comment_id` / `fields.review_id` set from the respective payload objects.

### Integration test (`tests/test_drain_dispatch.py`)

Drive the real drain → reactor pipeline against the shipped `eng-team` `auto_dispatch` config
(extends the harness #326 added):

1. Bot-authored `issue_comment` on a PR → **zero** dispatches (part a).
2. Human `issue_comment` on a **draft** PR → zero dispatches (part b).
3. The **same** human comment (same `comment_id`) delivered **twice** → exactly **one** dispatch
   (part c, redelivery).
4. Human comment on a **ready** PR → exactly one dispatch (regression — the happy path still works).

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
- `modastack/events/reactor.py`: self-author skip, draft skip, stable-comment-id dedup key,
  deterministic `run_key`.
- `event-server/src/adapters/github.ts`: emit `fields.draft`, `fields.comment_id`, `fields.review_id`.
- `agents/eng-team/agent.yaml`: `skip_draft: true` on the `pr-feedback` rules.
- Unit + adapter + integration tests above.

### Out of scope
- A durable/shared cross-process dedup store (only if D3-A proves insufficient — separate ticket).
- #412 (lifecycle auto-advancing past the spec-approval gate) — related but distinct.
- Any change to the `pr-feedback` workflow body, role prompts, or other workflows.
- Slack / Linear dispatch paths (GitHub reactor only).

---

## 6. Implementation plan (post-approval — do NOT start until Zach signs off)

1. Write the failing unit tests (a/b/c) in `tests/test_reactor.py` — confirm red on `main`.
2. Adapter: add `fields.draft`, `fields.comment_id`, `fields.review_id` + adapter tests.
3. Reactor (a): resolve + cache bot login; self-author skip in `process()`.
4. Reactor (b): `skip_draft` rule flag; field-based skip for review events, off-thread `gh pr view`
   lookup for `issue_comment`.
5. Reactor (c): stable-id dedup key; deterministic `run_key` into `launch_agent`.
6. `agent.yaml`: add `skip_draft: true` to the two `pr-feedback` rules.
7. Extend the integration test; run full suite + `modastack workflows validate`.
8. `/review`; fix everything it finds.
9. Open the impl PR against `main` (it — not this spec PR — carries `Fixes #411`).

---

## 7. Open questions for Zach

- **D1:** bot identity from `gh api user` (recommended) vs explicit `agent.yaml` config field?
- **D2:** draft source for `issue_comment` — reactor-side `gh pr view` off-thread (recommended) vs
  worker adapter REST call (needs a GH token in the worker)?
- **D3:** accept deterministic-`run_key` + persisted active-run guard for cross-process dedup
  (recommended) vs invest now in a durable shared dedup store?
- **Scope of (a):** confirm the self-author skip should be **global** (all dispatch rules), not just
  `pr-feedback`. (Recommended: global — a bot should never auto-react to its own action.)
