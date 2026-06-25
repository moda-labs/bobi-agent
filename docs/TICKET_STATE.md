# Ticket State

Living overview of all open GitHub issues — what tracks they belong to, what's
blocked vs. ready, and which one-offs are ready to hand to the `modastack` bot.

**Keep this current.** When an issue is opened, closed, assigned, unblocked, or
moves tracks, update it here in the same session. This file is the single place
to get the lay of the land without re-reading every issue.

- **Last reviewed:** 2026-06-25 (**19 open issues** — filed live Codex fleet incident follow-ups: **#501** stale persisted `.modastack/.env` secrets on Fly volumes can make tool shells use the wrong Slack bot, and **#502** workspace-level Slack DM routing can cross-deliver inbound events between bots. #501 is outbound credential drift; #502 is inbound webhook routing isolation.)
- **Prev reviewed:** 2026-06-24 (**17 open issues** — filed **epic #485 "Pluggable agent brain"** (Claude/Codex/Gemini/Grok behind one `BrainClient`; spec `pluggable-brain.md`; Phase 0 Codex spike done). Self-contained epic — work breakdown is in-issue checkboxes, no child tickets. New track added to "Tracks at a glance".)
- **Prev reviewed:** 2026-06-24 (**16 open issues** — full table reconciliation against `gh issue list` + Linear. Header had been bumped for the #453 cutover but the body tables still showed the 2026-06-22 state; this pass fixes that. **Closed since last real table refresh, moved to "Recently closed":** #285, #397, #403, #411, #412, #417, #418, #425, #426, #433, #443, #454 — i.e. the **CLI-first cleanup track, the Codex track, and the Reliability track are all DONE**. **New tracks added:** Tool/Capability library (#416 cli, #428 skill, #398 mcp) and a curator-monitor one-off (#456, assigned to `modastack`). **Linear sync gap flagged:** MDS-47/48/49 still Backlog but their GitHub twins (#285, #363) are closed — they describe the retired gateway-harness/MCP-shim architecture; epic MDS-42 likely needs re-scoping.)
- **Prev reviewed:** 2026-06-24 (15 open issues. **Epic #453 "Team distribution & composition" — ✅ DONE + CUTOVER LIVE**; #440/#446/#451/#452/#453 all CLOSED. modastack 0.31.0 ships `from:` composition; live `moda-eng-team` runs the composed team. _(prior:_ **PR [#457](https://github.com/moda-labs/modastack/pull/457)** (`epic-453-team-compose`). #446 + #451 = the `from:` compose mechanism (`modastack/compose.py`: resolution local-always-wins + fail-fast + `--pinned`; merge prose-concat + structured-deep-merge; deploy flatten; publish guard; 34 tests). #452 = pristine `agents/eng-team-core` extracted + monolith `agents/eng-team` DELETED + tests re-pointed; **regression bar met** (composed moda-eng-team ≡ today's eng-team). Overlay `moda-eng-team` (`from: eng-team-core`) committed to **private moda-agent-teams** branch `moda-eng-team-overlay` (additive; live app untouched). Full unit suite green (2116). **Remaining = the release-gated CUTOVER**: merge #457 → cut a modastack release (publishes `eng-team-core@1.0.0` to the registry) → merge the moda-agent-teams overlay PR → flip `deployments/eng-team.yaml` to `team: moda-eng-team` + delete the private monolith + deploy. Specs: `team-from-resolution.md`, `team-compose-merge.md`, `eng-team-core-split.md`.)
- **Prev reviewed:** 2026-06-22 (after **v0.28.0 released** — #409/#326/#321/#329/#323/#325 shipped + CLOSED; 23 open issues; **#425 filed** = resume-wedge bug; **#426/#427 filed** from the release.yml review (#426 = deploy-event-server concurrency tweak, #427 = lib↔event-server protocol compat); **#433 filed + PR #434** = context-rotation metric bug (measured input_tokens only → never fired under prompt caching; manager ran to ~424K) + new `modastack compact`; **#440 filed + ASSIGNED to `modastack`** = registry-based team install/deploy via versioned `name@version` packages (single-source-of-truth enabler for eng-team); assigned to `modastack`: #285/#397/#426/#440)
- **How to refresh:** `gh issue list --state open --limit 200` → reconcile the
  tables below; update "Last reviewed" and the open-count.

---

## Tracks at a glance

| Track | Type | Issues | Status |
|---|---|---|---|
| Multitenant | Epic (#395) | #378, #239, #215, +#394 (tooling) | ⏸️ Deferred — no forcing function |
| Chat SDK / ChannelAdapter | Multi-issue (#190) | #201 → #202, #203, #204 | ⛔ Blocked on spike #191; #201 is the gate |
| Tool / Capability library | NEW | #416 (cli) → #428 (skill); #398 (mcp) | 🟡 #416 is the foundation (catalog format + resolver); #428/#398 are the skill/mcp spokes. **Successor to the now-closed CLI-first track.** |
| Release pipeline (release.yml) | — | #427 | 🔴 design (library↔event-server protocol compat); #426 closed |
| Live fleet incidents | NEW | #501, #502 | 🔴 investigation/fix design — Codex-switched Fly bots exposed stale volume secrets and Slack cross-delivery between apps in one workspace |
| Knowledge / curation | NEW | #456 | 🟢 **Assigned to `modastack`** — replace append-only decision log with curator-monitor → `policy.md` (filed off the 2026-06-23 director wedge) |
| Team distribution & composition | **Epic (#453)** | #440 → #446 → #451 → #452 | ✅ **DONE — CUTOVER COMPLETE & LIVE 2026-06-24.** PR #457 (`7709b76`) + **modastack 0.31.0 released** (compose on PyPI). moda-agent-teams **PR #3 merged**, `MODASTACK_VERSION→0.31.0`, deploy dispatched (`only=eng-team`, `rebuild=true`) → live `moda-eng-team` reconciled onto the composed team. Behavioral identity confirmed live. |
| CLI-first connection cleanup | Sequence | #397 → #403 | ✅ **DONE** — #397 (image→CLI) + #403 (inject.py shim deleted) both CLOSED 2026-06-22 |
| Codex integration | MDS-42 B | #285 | ✅ **DONE** — #285 shipped CLI-first (`codex exec` + `tools/codex.md`), CLOSED |
| Reliability (post-#409) | — | #425, #433, #443, #454 | ✅ **DONE** — all CLOSED (#433/#454 rotation-metric + `compact`; #443/#425 wedge fixes) |
| Pluggable agent brain | **Epic (#485)** | self-contained (work breakdown in-issue, no child tickets) | 🟡 **PROPOSED 2026-06-24** — Claude/Codex/Gemini/Grok behind one `BrainClient`. **Phase 0 Codex spike DONE** (exec/NDJSON/MCP/resume validated). Spec: `pluggable-brain.md`. Phases 1–4 tracked as checkboxes in the issue. |
| Standalone one-offs | — | #327 | 🔴 Needs design |

---

## Release pipeline — `release.yml` (unified in PR #401, merged)

Single gated pipeline on `release: published`:
`subscription-login-smoke + build-wheel → build-canary (smoke vs PROD event server) → {publish→update-homebrew, roll-fleet→deploy-teams, deploy-event-server}`.

| Issue | What | Status |
|---|---|---|
| #426 | Decouple `deploy-event-server` from PyPI `publish` (run concurrently) | ✅ **CLOSED 2026-06-22** — one-line `needs:` change shipped. |
| #427 | Define library ↔ event-server **protocol compatibility contract** | 🔴 **OPEN — design-first.** Version skew is unhandled: roll is non-atomic, and the canary smokes against the *old/PROD* event server so it can't catch event-server breaks. Needs N-1 compat + expand/contract + canary-against-new-server + registration version check. Not a one-shot bot ticket. |

## Live fleet incidents — Codex-switched Fly bots

Filed from the 2026-06-25 `eng-team` / Bobbers / basketbot production
investigation. Keep these separate: #501 is credential materialization drift
on the Fly volume; #502 is inbound Slack event routing across multiple Slack
apps in one workspace.

| Issue | What | Status |
|---|---|---|
| #501 | Deploy reconcile leaves stale `.modastack/.env` secrets on Fly volume | 🟡 **OPEN — fix in progress.** Existing-app deploy now syncs reconciled Fly secrets into `/data/project/.modastack/.env` and removes pruned keys from that file. |
| #502 | Slack workspace-level DM routing can cross-deliver events between bots | 🟡 **OPEN — fix in progress.** Slack events and subscriptions now use app-qualified topics (`slack:<team>:app:<app>`) so DMs route to the matching Slack app. |

## Team distribution & composition — epic #453 ✅ DONE (2026-06-24)

Agent teams as a versioned, composable package ecosystem (reuse across orgs without
forking). Same arc as a package manager: package → resolve → compose → std-lib.
Supersedes the old "workspace/config/eject, no overlay" stance (composition over forking).

**Shipped:** modastack 0.31.0 (`modastack/compose.py` `from:` resolution + merge);
`eng-team@1.0.0` published as the public base; live `moda-eng-team`
(`from: eng-team` + Moda overlay, in private `moda-agent-teams`) reconciled onto
the composed team and **behaviorally identical** to the old monolith. All five issues
CLOSED. Specs (historical): `docs/specs/team-from-resolution.md` (#446),
`team-compose-merge.md` (#451), `eng-team-split.md` (#452).

| Issue | Stage | What | Status |
|---|---|---|---|
| #440 | Package | Versioned per-team tarballs (`name@version`) registry install + deploy | ✅ **Phase 1 (#442) + Phase 2 (#448) MERGED to main** — foundation landed. Interface: `registry.fetch(name, version=)` + `_split_ref` (exact `name@version` or "latest", **not semver ranges**) |
| #446 | Resolve | `from:` resolution: local-always-wins + fail-fast + `--pinned` | ✅ **MERGED (PR #457)** — `modastack/compose.py` `resolve_chain`/`resolve_team_ref`; Cargo-quality fail-fast; cycle/depth guards; compose-lock; deploy `resolve_team_dir` flattens on the host; publish guard `scripts/check-publishable.py` |
| #451 | Compose | Merge semantics: prose concat-in-order; structured deep-merge by key; `replace:` hatch; `prune:` | ✅ **MERGED (PR #457)** — `compose.compose()`; prose concat + `replace:`; agent.yaml deep-merge (services/requires by name, build append+dedupe, auto_dispatch id-keyed); `prune:`; provenance; install clears only contributed surfaces; 38 tests |
| #452 | Std lib | Extract pristine `eng-team` (→ public modastack) + `moda-eng-team` overlay (→ private moda-agent-teams) | 🟡 **modastack side MERGED (PR #457) — cutover pending** — `agents/eng-team` extracted, monolith deleted, tests re-pointed, teams de-bundled from the wheel, regression bar met; overlay on private moda-agent-teams **PR #3**. Live flip release-gated: cut a modastack release (publishes eng-team@1.0.0) → merge PR #3 → flip `deployments/eng-team.yaml` to `team: moda-eng-team` + delete private monolith + deploy |

## Multitenant — epic #395 ⏸️

Post-MVP phase of containerization (#344, closed; MVP shipped v0.24.0, eng-team
live on Fly, EC2 retired). **Explicitly deferred — don't build speculatively.**
Pick up only when there's a real driver: a 2nd instance of one team, or an
external/untrusted tenant.

| Issue | What | Blocker |
|---|---|---|
| #378 | Build-once team images → Fly registry → deploy-many by digest | First ticket; only pays off at N instances of one team. Engine already consumes `image: <ref>`. |
| #239 | auth-v2: bind bubbles to accounts + authorize webhook subscriptions | Hard-blocked on the accounts model — no-op until it exists. Closes the accepted single-tenant webhook-fan-out hole. |
| #215 | loop-safety: circuit breaker, spend governor, per-deployment identities | Phase 1 (delivery-path breaker) is independent and could land alone. |
| #394 | Remote attach (debug/smoke tooling) | Adjacent, not in the epic. **Form A** (run-on-box SSH wrapper) ships independently; **Form B** (attach-from-local) hardened form depends on #239. |

## Chat SDK / ChannelAdapter — parent #190 ⛔

Replace hand-rolled channel plumbing with a `ChannelAdapter` interface + adapters.
Blocked on spike #191 (PR #198) results. Strict dependency order.

| Issue | What | Depends on |
|---|---|---|
| #201 | Define `ChannelAdapter` interface + adapter registry | — (**foundational — gates the rest**) |
| #202 | Migrate Slack adapter to the interface | #201 (~1 day, mostly wiring) |
| #203 | Telegram adapter | #201 |
| #204 | WhatsApp adapter | #201 (most friction — Meta Business acct, no edit/typing APIs) |
| #190 | Umbrella: adopt a channel library (e.g. Vercel Chat SDK) | Spike #191 |

## Tool / Capability library — NEW track 🟡

A curated, **opt-in catalog** where a team pulls in a capability **by name** (one
pinned definition + one guide) instead of hand-coordinating binary install +
version pin + guide across three places. Three delivery **kinds**, same opt-in /
pinning / guide model. **Successor to the now-closed CLI-first cleanup** (#397/#403):
those tore the runtime MCP-shim down to bare CLIs; this makes the bake/guide
opt-in and reusable across teams. #417/#418 (the define-once foundation) already CLOSED.

| Issue | Kind | What | Status |
|---|---|---|---|
| #416 | `cli` | Catalog of baked CLI tools (binary+version+`requires`+guide); resolver expands `tool_library: [..]` → `build`+`requires`+guide at build time; **migrate aichat/codex/openai/venn/gstack** into it | 🟡 **OPEN, unassigned** — foundation of the track. Build/config-time sugar over primitives we already have; does NOT reintroduce runtime MCP indirection. |
| #428 | `skill` | Install third-party Claude Code **skill libraries** from GitHub (gstack, superpowers) by pinned SHA/tag | 🟡 **OPEN** — the skill spoke. Runtime-coupled (Claude-only today); supply-chain: pin by SHA. |
| #398 | `mcp` | First-class third-party **MCP server** support (declare/probe/surface) | 🔴 **OPEN — design-heavy.** The mcp spoke + the only legit MCP path now that built-in shims are retired. Needs a plan (coherent declare/probe/surface + multi-runtime portability). |

## Knowledge / curation — NEW one-off 🟢

| Issue | What | Status |
|---|---|---|
| #456 | Replace the append-only **decision log** with a **curator-monitor** that distills transcripts → a rewritten-in-place, size-capped `policy.md` (injected read-only), publishing `policy.updated` so agents re-read | 🟢 **OPEN — assigned to `modastack`.** Filed off the 2026-06-23 director wedge (the decision log grew to 127KB and aggravated the wedge). Rides existing monitor infra (out-of-band curator agent). Root-cause of the false over-cap was the rotation metric (#454, closed separately). |

## DONE tracks (kept for context, all CLOSED)

- **CLI-first connection cleanup** — #397 (image-gen → baked OpenAI CLI) + #403 (deleted `inject.py`/`codex_server.py`/`ConnectionEntry`/the `connections:` block). The middle MCP-shim layer is gone; two clean layers remain (baked CLIs + team-brought MCP). Both CLOSED 2026-06-22.
- **Codex integration (MDS-42 B)** — #285 shipped CLI-first (`codex exec` shell-out + `tools/codex.md`, NOT the `codex_exec` MCP tool). CLOSED. ⚠️ **Linear MDS-47 still Backlog** with the stale MCP-tool spec — needs closing/re-scoping.
- **Reliability (post-#409)** — #433/#454 (context-rotation metric: sum cache_read+cache_creation+input, not input_tokens; + `modastack compact`), #443 (transient 529 no longer wedges a session), #425 (resume-wedge). All CLOSED.

---

## One-offs — bot-readiness

Grading for the `modastack` bot: **bounded scope, clear acceptance criteria, no
unresolved design decision, verifiable without infra/credentials the bot lacks.**

### 🟢 Assigned to `modastack`

| Issue | What | Note |
|---|---|---|
| #456 | Curator-monitor → `policy.md` (replaces decision log) | Bounded; rides existing monitor infra. Spec on the GH issue body. |

### 🟡 Ready with a prep step / caveat

| Issue | What | Caveat |
|---|---|---|
| #394 (form A) | `modastack remote <app>` SSH wrapper | Code is unit-testable; **acceptance needs live Fly** against moda-canary — human verifies |

### 🔴 Not ready — needs decision/investigation first

| Issue | What | Blocker to autonomy |
|---|---|---|
| #416 | Tool/Capability library (cli catalog) | Catalog format + where-it-lives undecided; compose semantics vs explicit `build:`/`requires:` + #380 pinning need a quick design pass first |
| #428 | Tool library `kind: skill` spoke | Depends on #416's catalog shape; supply-chain (SHA-pin + scanning) open |
| #398 | First-class MCP support (`kind: mcp`) | Design-heavy; needs a plan |
| #427 | library ↔ event-server protocol compat | Design-first; N-1 compat + canary-against-new-server |
| #501 | Stale `.modastack/.env` secrets on Fly volumes | Fix in progress; verify live deploy refreshes volume env after release |
| #502 | Slack app cross-delivery on workspace-level DM topics | Fix in progress; verify Bobbers and eng-team only receive their own app events after redeploy |
| #327 | Self-learning script-cache monitor | Large feature, unresolved design (sandboxing, cache invalidation, retry budgets); must define its own Axis-1 mechanism (per #363/#396) |

---

## Recently closed (for context)

| Issue | Resolution |
|---|---|
| #453 / #440 / #446 / #451 / #452 | **Epic: Team distribution & composition** — `from:` inheritance + compose merge + `eng-team` split. modastack 0.31.0; cutover LIVE. All CLOSED 2026-06-24. |
| #454 | Rotation metric over-counted (summed cache_read across a turn) → false "rotation pending" + wedge — fixed in v0.30.0, prod rolled. CLOSED 2026-06-23. |
| #443 | Transient 529/turn error permanently wedged a session (deaf until restart) — CLOSED 2026-06-23. |
| #425 | Resumed manager session could wedge (deaf to inbox while reporting "ready") — CLOSED 2026-06-22. |
| #433 | Context rotation never fired under prompt caching (measured input_tokens) + new `modastack compact` — CLOSED 2026-06-22. |
| #426 | Decoupled `deploy-event-server` from PyPI publish in release.yml — CLOSED 2026-06-22. |
| #412 | issue-lifecycle auto-advanced past the spec-approval gate — CLOSED 2026-06-22. |
| #411 | pr-feedback auto-dispatched on bot comments / draft spec PRs — CLOSED 2026-06-22. |
| #403 | Dismantled the `inject.py` / `ConnectionEntry` connection-kind shim — CLOSED 2026-06-22. |
| #397 | Moved image generation from MCP server → baked CLI — CLOSED 2026-06-22. |
| #418 / #417 | Reusable tool library: define-once catalog foundation (binary + guide) — CLOSED 2026-06-22 (track continues in #416/#428). |
| #285 | [MDS-47] Codex adversarial-review step — shipped CLI-first (`codex exec` + `tools/codex.md`). CLOSED. ⚠️ Linear MDS-47 still Backlog. |
| #409 | Event-server registration non-fatal at startup + background retry — **shipped v0.28.0** (PR #413). The headline stability fix. |
| #326 | Reactor dedup key now includes per-delivery event id (reviewer follow-up comments no longer dropped) — **shipped v0.28.0** (PR #408). |
| #321 | pr-feedback posts one resolution comment via the lead — **shipped v0.28.0** (PR #402). |
| #329 | Graceful preflight degradation for non-required services (`required:` flag) — **shipped v0.28.0** (PR #405). |
| #323 | Auto-fix CI failures on any open PR — **shipped v0.28.0** (PR #400). |
| #325 | Convention: changelog/version only at release (docs + CI guard) — **shipped** (PR #404). |
| #363 | [MDS-48/MDS-49] Gateway harness — closed not-planned 2026-06-21. Shipped via a **different approach** in #396 (aichat baked as CLI-first; gateway/chat/embedding connection kinds retired). |
