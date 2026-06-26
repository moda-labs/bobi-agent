# Ticket State

Living overview of all open GitHub issues — what tracks they belong to, what's
blocked vs. ready, and which one-offs are ready to hand to the `bobi` bot.

**Keep this current.** When an issue is opened, closed, assigned, unblocked, or
moves tracks, update it here in the same session. This file is the single place
to get the lay of the land without re-reading every issue.

- **Last reviewed:** 2026-06-26 (**17 open issues, 0 open PRs** — reconciled against live GitHub after the bobi rename PR merged. Active clusters: **Unified agent dashboard** (#525 design + #526/#527/#528/#529 prep), **runtime/fleet bugs** (#518/#520), **brain/runtime portability** (#485/#484/#481), **Tool library** (#515 with #428/#398 ready), **User accounts** (#239), **Containerization** (#395), **Chat SDK** (#190), and release protocol compatibility (#427). Since the prior review, #479/#489/#496/#498/#507/#513/#519/#521/#530 closed; #526-#529 remain dashboard-independent prep tickets.)
- **Prev reviewed:** 2026-06-26 (**28 open issues** — filed the **Unified agent dashboard** track: design record **#525** (merge `bobi setup` + `bobi ui` into one dashboard → onboarding → monitor app) plus four **do-now prep carve-outs** that are good regardless of the dashboard and shrink the eventual merge PR: **#526** canonical `~/.bobi/agents/<team>/` directory layout, **#527** shared web-server harness, **#528** consolidated design tokens, **#529** service-core extraction (CLI + web as thin adapters). The frontend-framework decision (vanilla vs lightweight framework) gates the *merge*, not the prep. New track added to "Tracks at a glance".)
- **Prev reviewed:** 2026-06-25 (**16 open issues** — filed #515 as the Tool library epic and cleaned #428/#398 into shovel-ready implementation tickets; #493 and #499 closed while reconciling. Earlier today: consolidated containerization into #395 and closed #378/#215/#394 as duplicate tracking tickets; split #239 out as **User accounts**; collapsed Chat SDK into #190 and closed #201/#202/#203/#204; #488 closed after PR #491. Active clusters: Codex-brain rollout (#485 plus #479/#484/#481/#498/#507/#513), Tool library (#515 with #428/#398 ready), user accounts (#239), shared event-server hardening (#489), and Slack event polish (#496). **PR state:** #489/#496/#498/#507 have PRs up; #513/#428/#398 are status:todo; #481 is assigned and moving. **Closed/stale table refs corrected:** #416, #456, #327, #378, #394, #215, #488, #493, #499 are closed; #501/#502 remain closed in 0.34.1.)
- **Prev reviewed:** 2026-06-25 (**23 open issues after closing #501/#502** — 0.34.1 bugfix release cut from #500/#503. **Closed:** #501 stale persisted `.bobi/.env` secrets on Fly volumes, and #502 Slack workspace-level DM cross-delivery between bots. Both shipped in PR #503; #500 shipped the Codex shell PATH fix.)
- **Prev reviewed:** 2026-06-25 (**25 open issues** — filed live Codex fleet incident follow-ups: **#501** stale persisted `.bobi/.env` secrets on Fly volumes can make tool shells use the wrong Slack bot, and **#502** workspace-level Slack DM routing can cross-deliver inbound events between bots. #501 is outbound credential drift; #502 is inbound webhook routing isolation.)
- **Prev reviewed:** 2026-06-24 (**17 open issues** — filed **epic #485 "Pluggable agent brain"** (Claude/Codex/Gemini/Grok behind one `BrainClient`; spec `pluggable-brain.md`; Phase 0 Codex spike done). Self-contained epic — work breakdown is in-issue checkboxes, no child tickets. New track added to "Tracks at a glance".)
- **Prev reviewed:** 2026-06-24 (**16 open issues** — full table reconciliation against `gh issue list` + Linear. Header had been bumped for the #453 cutover but the body tables still showed the 2026-06-22 state; this pass fixes that. **Closed since last real table refresh, moved to "Recently closed":** #285, #397, #403, #411, #412, #417, #418, #425, #426, #433, #443, #454 — i.e. the **CLI-first cleanup track, the Codex track, and the Reliability track are all DONE**. **New tracks added:** Tool/Capability library (#416 cli, #428 skill, #398 mcp) and a curator-monitor one-off (#456, assigned to `bobi`). **Linear sync gap flagged:** MDS-47/48/49 still Backlog but their GitHub twins (#285, #363) are closed — they describe the retired gateway-harness/MCP-shim architecture; epic MDS-42 likely needs re-scoping.)
- **Prev reviewed:** 2026-06-24 (15 open issues. **Epic #453 "Team distribution & composition" — ✅ DONE + CUTOVER LIVE**; #440/#446/#451/#452/#453 all CLOSED. bobi 0.31.0 ships `from:` composition; live `moda-eng-team` runs the composed team. _(prior:_ **PR [#457](https://github.com/moda-labs/bobi-agent/pull/457)** (`epic-453-team-compose`). #446 + #451 = the `from:` compose mechanism (`bobi/compose.py`: resolution local-always-wins + fail-fast + `--pinned`; merge prose-concat + structured-deep-merge; deploy flatten; publish guard; 34 tests). #452 = pristine `agents/eng-team-core` extracted + monolith `agents/eng-team` DELETED + tests re-pointed; **regression bar met** (composed moda-eng-team ≡ today's eng-team). Overlay `moda-eng-team` (`from: eng-team-core`) committed to **private moda-agent-teams** branch `moda-eng-team-overlay` (additive; live app untouched). Full unit suite green (2116). **Remaining = the release-gated CUTOVER**: merge #457 → cut a bobi release (publishes `eng-team-core@1.0.0` to the registry) → merge the moda-agent-teams overlay PR → flip `deployments/eng-team.yaml` to `team: moda-eng-team` + delete the private monolith + deploy. Specs: `team-from-resolution.md`, `team-compose-merge.md`, `eng-team-core-split.md`.)
- **Prev reviewed:** 2026-06-22 (after **v0.28.0 released** — #409/#326/#321/#329/#323/#325 shipped + CLOSED; 23 open issues; **#425 filed** = resume-wedge bug; **#426/#427 filed** from the release.yml review (#426 = deploy-event-server concurrency tweak, #427 = lib↔event-server protocol compat); **#433 filed + PR #434** = context-rotation metric bug (measured input_tokens only → never fired under prompt caching; manager ran to ~424K) + new `bobi compact`; **#440 filed + ASSIGNED to `bobi`** = registry-based team install/deploy via versioned `name@version` packages (single-source-of-truth enabler for eng-team); assigned to `bobi`: #285/#397/#426/#440)
- **How to refresh:** `gh issue list --state open --limit 200` → reconcile the
  tables below; update "Last reviewed" and the open-count.

---

## Tracks at a glance

| Track | Type | Issues | Status |
|---|---|---|---|
| Unified agent dashboard | **NEW (#525)** | #525 design; #526, #527, #528, #529 prep | 🟡 **PROPOSED 2026-06-26** — merge the create (`setup`) + monitor (`ui`) UIs into one dashboard → onboarding → launch → monitor app. **#526–#529 are do-now, dashboard-independent cleanup** (good regardless). Frontend-framework decision gates the *merge*, not the prep. |
| Runtime / fleet bugs | Bugs | #518, #520 | 🔴 Open production-safety bugs: session crash bursts tripping the loop breaker, and verify agents mutating the shared editable install to PR worktrees. |
| Codex brain rollout / runtime portability | Epic + incidents (#485) | #485, #484, #481 | 🔴 Active rollout risk. #479/#498/#507/#513 closed; remaining live items are Claude initialize timeouts under contention (#484) and memory-aware admission (#481), with #485 tracking the broader brain epic. |
| User accounts | Epic (#239) | account model + bubble registration binding | 🔴 Separate from containerization. Add user accounts, then bind auth bubbles to accounts during registration. |
| Containerization | Epic (#395) | collapsed #378/#215/#394 | ⏸️ Single epic only. Former child tickets are closed as duplicate tracking tickets and preserved as checklists in #395. User accounts moved to #239. |
| Chat SDK / ChannelAdapter | Epic (#190) | collapsed #201/#202/#203/#204 | ⏸️ Single epic only. Former child tickets are closed as duplicate tracking tickets and preserved as a checklist in #190. |
| Tool library | Epic (#515) | #416 done; #428, #398 | 🟢 #428 and #398 are shovel-ready implementation tickets under #515. #416 CLI catalog is CLOSED. |
| Release pipeline / packaging | — | #427 | 🔴 #493 is closed; #427 remains design-first for library↔event-server protocol compatibility. |
| Live fleet incidents | NEW | #501, #502 | ✅ **FIXED in PR #503 / release 0.34.1** — Codex-switched Fly bots exposed stale volume secrets and Slack cross-delivery between apps in one workspace |
| Team distribution & composition | **Epic (#453)** | #440 → #446 → #451 → #452 | ✅ **DONE — CUTOVER COMPLETE & LIVE 2026-06-24.** PR #457 (`7709b76`) + **bobi 0.31.0 released** (compose on PyPI). moda-agent-teams **PR #3 merged**, `BOBI_VERSION→0.31.0`, deploy dispatched (`only=eng-team`, `rebuild=true`) → live `moda-eng-team` reconciled onto the composed team. Behavioral identity confirmed live. |
| CLI-first connection cleanup | Sequence | #397 → #403 | ✅ **DONE** — #397 (image→CLI) + #403 (inject.py shim deleted) both CLOSED 2026-06-22 |
| Codex integration | MDS-42 B | #285 | ✅ **DONE** — #285 shipped CLI-first (`codex exec` + `tools/codex.md`), CLOSED |
| Reliability (post-#409) | — | #425, #433, #443, #454 | ✅ **DONE** — all CLOSED (#433/#454 rotation-metric + `compact`; #443/#425 wedge fixes) |
| Pluggable agent brain | **Epic (#485)** | self-contained + follow-ups above | 🟡 Phase 0 + Claude adapter landed; Codex stateless/manager parity is now being driven through #484/#481 and the remaining #485 checklist. |

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
| #501 | Deploy reconcile leaves stale `.bobi/.env` secrets on Fly volume | ✅ **CLOSED — fixed in PR #503 / release 0.34.1.** Existing-app deploy now syncs reconciled Fly secrets into `/data/project/.bobi/.env` and removes pruned keys from that file. |
| #502 | Slack workspace-level DM routing can cross-deliver events between bots | ✅ **CLOSED — fixed in PR #503 / release 0.34.1.** Slack events and subscriptions now use app-qualified topics (`slack:<team>:app:<app>`) so DMs route to the matching Slack app. |

## Codex brain rollout / runtime portability — epic #485

The 0.34 pluggable-brain work made Codex real enough to expose rollout
footguns. Treat this as the active reliability lane until Codex-backed teams can
launch children, run gates, authenticate helper reviews, and survive contention
without special-case operator intervention.

| Issue | What | Status |
|---|---|---|
| #485 | Pluggable brain epic | 🟡 **OPEN.** Phase 0 and Claude adapter are done; remaining work is Codex stateless paths, manager loop strategy, and other brain adapters. |
| #479 | Codex CLI unauthenticated fleet-wide for adversarial review | ✅ **CLOSED 2026-06-26.** `OPENAI_API_KEY` now materializes Codex auth before launch. |
| #498 | Auto-bake Codex CLI when `brain.kind=codex` | ✅ **CLOSED 2026-06-25.** Removes the config footgun where the brain says Codex but the image lacks `codex`. |
| #507 | Codex skill/gate compatibility for issue-lifecycle | ✅ **CLOSED 2026-06-25.** Makes `/review`, `/qa`, `/browse` available to Codex-backed sessions. |
| #513 | General parent→child environment propagation contract | ✅ **CLOSED 2026-06-25.** Contract landed for inherited brain/tool/service credentials. |
| #484 | Claude CLI `initialize` timeout under CPU/IO contention | 🔴 **OPEN — blocks fresh engines.** Not OOM; needs configurable/retried initialize and possibly launch admission under high load. |
| #481 | Memory-aware concurrency cap | 🟡 **OPEN, assigned to `bobi`, status:in-progress.** Replace static `max_concurrent_agents` with dispatch-time memory headroom gating plus static hard ceiling fallback. |

## Runtime / fleet bugs

| Issue | What | Status |
|---|---|---|
| #518 | Agent sessions crashed in burst and tripped loop breaker | 🔴 **OPEN.** A burst of session.failed events tripped the loop breaker; needs root-cause recovery/reporting so crashes do not create control-plane ambiguity. |
| #520 | Verify agents can re-root shared editable install to PR worktrees | 🔴 **OPEN.** `pip install -e` from task worktrees can mutate the shared user-level editable install; needs isolation/guardrails before cleanup removes those worktrees. |

## Shared event-server security

These are the near-term blockers before treating the Cloudflare Worker as a
shared or public event server. The broader account model now lives in the
separate User accounts epic (#239).

| Issue | What | Status |
|---|---|---|
| #488 | Pluggable upstream resource grants for webhook topic subscriptions | ✅ **CLOSED 2026-06-25 after PR #491.** |
| #489 | Internal Worker-to-Durable-Object auth for `DeploymentSession` | ✅ **CLOSED 2026-06-25.** Defense-in-depth plus stops forwarding client bearer auth to the DO. |
| #239 | User accounts + bind auth bubbles during registration | 🔴 **SEPARATE EPIC.** Not part of containerization; account-level authorization builds on top of bubble/resource-grant hardening. |

## Slack UX / delivery polish

| Issue | What | Status |
|---|---|---|
| #496 | Duplicate `Thinking...` placeholders on @mentions | ✅ **CLOSED 2026-06-25.** Dedup Slack `app_mention` + `message.channels` double-events. |
| #499 | User-friendly Slack DM `login_channel` values | ✅ **CLOSED 2026-06-25.** Resolved `@handle`/structured user refs against the configured bot token workspace. |

## Team distribution & composition — epic #453 ✅ DONE (2026-06-24)

Agent teams as a versioned, composable package ecosystem (reuse across orgs without
forking). Same arc as a package manager: package → resolve → compose → std-lib.
Supersedes the old "workspace/config/eject, no overlay" stance (composition over forking).

**Shipped:** bobi 0.31.0 (`bobi/compose.py` `from:` resolution + merge);
`eng-team@1.0.0` published as the public base; live `moda-eng-team`
(`from: eng-team` + Moda overlay, in private `moda-agent-teams`) reconciled onto
the composed team and **behaviorally identical** to the old monolith. All five issues
CLOSED. Specs (historical): `docs/specs/team-from-resolution.md` (#446),
`team-compose-merge.md` (#451), `eng-team-split.md` (#452).

| Issue | Stage | What | Status |
|---|---|---|---|
| #440 | Package | Versioned per-team tarballs (`name@version`) registry install + deploy | ✅ **Phase 1 (#442) + Phase 2 (#448) MERGED to main** — foundation landed. Interface: `registry.fetch(name, version=)` + `_split_ref` (exact `name@version` or "latest", **not semver ranges**) |
| #446 | Resolve | `from:` resolution: local-always-wins + fail-fast + `--pinned` | ✅ **MERGED (PR #457)** — `bobi/compose.py` `resolve_chain`/`resolve_team_ref`; Cargo-quality fail-fast; cycle/depth guards; compose-lock; deploy `resolve_team_dir` flattens on the host; publish guard `scripts/check-publishable.py` |
| #451 | Compose | Merge semantics: prose concat-in-order; structured deep-merge by key; `replace:` hatch; `prune:` | ✅ **MERGED (PR #457)** — `compose.compose()`; prose concat + `replace:`; agent.yaml deep-merge (services/requires by name, build append+dedupe, auto_dispatch id-keyed); `prune:`; provenance; install clears only contributed surfaces; 38 tests |
| #452 | Std lib | Extract pristine `eng-team` (→ public bobi) + `moda-eng-team` overlay (→ private moda-agent-teams) | ✅ **CLOSED + CUTOVER LIVE.** `agents/eng-team` extracted, monolith deleted, overlay PR merged, `deployments/eng-team.yaml` flipped to `team: moda-eng-team`, and live behavior verified. |

## User accounts — epic #239 🔴

Add the user/account identity layer, then bind runtime auth bubbles to accounts
during deployment registration. This is not a containerization ticket.

| Issue | What | Status |
|---|---|---|
| #239 | User accounts + bind auth bubbles during registration | 🔴 **OPEN EPIC.** Define persisted accounts/connections, verify account proof during deployment registration, store `bubble_id -> account_id`, and use that account binding for future resource/outbound authorization. |

## Containerization — epic #395 ⏸️

The single-tenant containerization MVP is shipped (#344 closed; eng-team live on
Fly; EC2 retired). Remaining deployed-instance work is now consolidated into one
epic: image reuse, remote debug/smoke tooling, and operational safeguards.
Account/user identity is tracked separately in #239.

| Issue | What | Status |
|---|---|---|
| #395 | Containerization: deployed instances and scale | 🟡 **OPEN EPIC.** Owns build-once team images, `bobi remote`/attach tooling, loop circuit breaker, spend governor, and per-deployment identity follow-ups. Deferred until there is a real scale/debug/safety driver. |
| #378 / #215 / #394 | Former child tickets for build-once images, loop-safety, and remote attach | ✅ **CLOSED 2026-06-25 as duplicate tracking tickets.** Scope preserved in #395. |

## Chat SDK / ChannelAdapter — epic #190 ⏸️

Replace hand-rolled channel plumbing with a `ChannelAdapter` interface + adapters.
Collapsed into a single epic on 2026-06-25: #201/#202/#203/#204 were closed as
duplicate tracking tickets, and their scope now lives as a checklist in #190.

| Issue | What | Status |
|---|---|---|
| #190 | Adopt a channel library or minimal `ChannelAdapter`; includes interface, registry, Slack migration, Telegram adapter, WhatsApp adapter, and capability docs | 🟡 **OPEN EPIC.** Work is intentionally not split until there is a renewed product driver. |
| #201 / #202 / #203 / #204 | Former child tickets for interface + Slack/Telegram/WhatsApp adapters | ✅ **CLOSED 2026-06-25 as duplicate tracking tickets.** Scope preserved in #190. |

## Tool library — epic #515 🟢

A curated, **opt-in catalog** where a team pulls in a capability **by name**
instead of hand-coordinating install/build surfaces, runtime checks, MCP config,
skill files, and agent-facing guides across teams. #416 shipped the CLI
foundation; #515 now tracks the full CLI/skill/MCP catalog.

| Issue | Kind | What | Status |
|---|---|---|---|
| #515 | epic | Tool library across CLI, skill libraries, and MCP servers | 🟡 **OPEN EPIC.** Keeps the remaining spokes coherent without collapsing them. |
| #416 | `cli` | Catalog of baked CLI tools (binary+version+`requires`+guide); resolver expands `tool_library: [..]` → `build`+`requires`+guide at build time; **migrate aichat/codex/openai/venn/gstack** into it | ✅ **CLOSED 2026-06-24.** Foundation landed; remaining work is in the skill/MCP spokes. |
| #428 | `skill` | Install pinned third-party skill libraries | 🟢 **STATUS:TODO / SHOVEL READY.** Register `_expand_skill`, install selected skills from pinned refs, add gstack entry, pin lint, override tests, and runtime-compat docs. |
| #398 | `mcp` | First-class third-party MCP servers through `tool_library:` | 🟢 **STATUS:TODO / SHOVEL READY.** Register `_expand_mcp`, merge `mcp_servers`, preserve local overrides, probe final config, and document per-brain support. |

## Knowledge / curation — NEW one-off 🟢

| Issue | What | Status |
|---|---|---|
| #456 | Replace the append-only **decision log** with a **curator-monitor** that distills transcripts → a rewritten-in-place, size-capped `policy.md` (injected read-only), publishing `policy.updated` so agents re-read | ✅ **CLOSED 2026-06-24.** Filed off the 2026-06-23 director wedge; root-cause of the false over-cap was the rotation metric (#454, closed separately). |

## Unified agent dashboard — NEW track 🟡

Close the gap between the two existing local web apps — the creation/onboarding UI
(`bobi setup`, `setup/webui/`) and the monitoring UI (`bobi ui`,
`agentui/`) — into **one app**: opens as a dashboard of your teams, leads into the
existing onboarding flow, **installs + launches + returns home**, and click-through
to the existing monitor. Goal: never need the CLI except to start the app; keep the
UI a static client over a relocatable HTTP API so it can be hosted later.

**#525 is a design record (not a bot task).** **#526–#529 are independent, do-now
cleanup** that's valuable regardless and shrinks the eventual merge PR. The one thing
to settle *before the merge* (but after the prep) is the **frontend-framework
decision** (stay vanilla vs a lightweight component framework) — not yet ticketed.

| Issue | Role | What | Depends on / Status |
|---|---|---|---|
| #525 | Design | Unified dashboard decision record: merge, MVP cut (Dashboard + Onboarding-with-launch-and-return + Agents-&-Chat), screen inventory, distribution strategy | 🟡 **OPEN — awaiting approval.** No impl until approved. |
| #526 | Prep | Canonical `~/.bobi/{config.yaml,sources/,agents/<team>/{.bobi,workspace}}` layout; `--team` selector; sources configurable, runtime fixed; path-only global config | 🟢 **OPEN — do-now.** Independent. Restores `paths.py` single-chokepoint (consolidates the `~/bobi-agents` literal in `setup/webui/server.py`). |
| #527 | Prep | Shared local web-server harness (`webui_common`): bind/secret/host-guard/static-serving/browser-open + 6PN container mode; both servers adopt it | 🟢 **OPEN — do-now.** Independent; framework-agnostic (serves a static dir). Pairs with #528. |
| #528 | Prep | Consolidate design-system tokens into one `tokens.css` from DESIGN.md (the two app.css have **drifted** — two ambers, two papers) | 🟢 **OPEN — do-now.** Pairs with #527. Survives any framework choice (CSS custom props). |
| #529 | Prep | Service-core extraction: CLI + web as thin adapters over one engine; `launch_team()` the first brick (unblocks `/api/launch`) | 🟢 **OPEN — do-now.** Composes with #526. Pulls domain logic out of Click command bodies in `cli.py`. |

## DONE tracks (kept for context, all CLOSED)

- **CLI-first connection cleanup** — #397 (image-gen → baked OpenAI CLI) + #403 (deleted `inject.py`/`codex_server.py`/`ConnectionEntry`/the `connections:` block). The middle MCP-shim layer is gone; two clean layers remain (baked CLIs + team-brought MCP). Both CLOSED 2026-06-22.
- **Codex integration (MDS-42 B)** — #285 shipped CLI-first (`codex exec` shell-out + `tools/codex.md`, NOT the `codex_exec` MCP tool). CLOSED. ⚠️ **Linear MDS-47 still Backlog** with the stale MCP-tool spec — needs closing/re-scoping.
- **Reliability (post-#409)** — #433/#454 (context-rotation metric: sum cache_read+cache_creation+input, not input_tokens; + `bobi compact`), #443 (transient 529 no longer wedges a session), #425 (resume-wedge). All CLOSED.

---

## One-offs — bot-readiness

Grading for the `bobi` bot: **bounded scope, clear acceptance criteria, no
unresolved design decision, verifiable without infra/credentials the bot lacks.**

### 🟢 Assigned to `bobi`

| Issue | What | Note |
|---|---|---|
| #481 | Memory-aware concurrency cap | Assigned and actively relevant; acceptance is mostly unit-testable, with live behavior verified through queue/admission logs. |

### 🟡 Ready with a prep step / caveat

| Issue | What | Caveat |
|---|---|---|
| #527 | Shared web-server harness (`webui_common`) | Bounded refactor; clear acceptance + unit tests in-issue. Pairs with #528. Verify both UIs launch unchanged. |
| #528 | Consolidate design tokens → `tokens.css` | Bounded; pairs with #527 (shared static path). Reconcile drift to DESIGN.md values. |
| #526 | Canonical `~/.bobi/` directory layout + `--team` | Bounded but touches `paths.py` (the path chokepoint) broadly; back-compat (cwd walk-up) is spelled out. Good first dashboard-prep brick. |
| #529 | Service-core extraction (`launch_team`, …) | Bounded; **acceptance wants an integration test driving a real session** (dogfood) for the launch path. |
| #428 | Tool library `kind: skill` spoke | `status:todo`; implementation is scoped by the issue body and #515. |
| #398 | Tool library `kind: mcp` spoke | `status:todo`; implementation is scoped by the issue body and #515. |

### 🔴 Not ready — needs decision/investigation first

| Issue | What | Blocker to autonomy |
|---|---|---|
| #427 | library ↔ event-server protocol compat | Design-first; N-1 compat + canary-against-new-server |
| #484 | Engine initialize timeout under contention | Needs root-cause-preserving fix: SDK timeout/retry and maybe launch admission, not just relaunch loops. |
| #518 | Session crash burst / loop breaker | Needs root-cause investigation before a fix; likely spans engine lifecycle and breaker policy. |
| #520 | Verify agents re-root shared editable install | Needs a careful isolation design so task verification cannot mutate the live install. |

---

## Recently closed (for context)

| Issue | Resolution |
|---|---|
| #453 / #440 / #446 / #451 / #452 | **Epic: Team distribution & composition** — `from:` inheritance + compose merge + `eng-team` split. bobi 0.31.0; cutover LIVE. All CLOSED 2026-06-24. |
| #454 | Rotation metric over-counted (summed cache_read across a turn) → false "rotation pending" + wedge — fixed in v0.30.0, prod rolled. CLOSED 2026-06-23. |
| #443 | Transient 529/turn error permanently wedged a session (deaf until restart) — CLOSED 2026-06-23. |
| #425 | Resumed manager session could wedge (deaf to inbox while reporting "ready") — CLOSED 2026-06-22. |
| #433 | Context rotation never fired under prompt caching (measured input_tokens) + new `bobi compact` — CLOSED 2026-06-22. |
| #426 | Decoupled `deploy-event-server` from PyPI publish in release.yml — CLOSED 2026-06-22. |
| #412 | issue-lifecycle auto-advanced past the spec-approval gate — CLOSED 2026-06-22. |
| #411 | pr-feedback auto-dispatched on bot comments / draft spec PRs — CLOSED 2026-06-22. |
| #403 | Dismantled the `inject.py` / `ConnectionEntry` connection-kind shim — CLOSED 2026-06-22. |
| #397 | Moved image generation from MCP server → baked CLI — CLOSED 2026-06-22. |
| #418 / #417 | Reusable tool library: define-once catalog foundation (binary + guide) — CLOSED 2026-06-22 (track now lives in #515; CLI foundation #416 closed, skill/MCP spokes #428/#398 open). |
| #285 | [MDS-47] Codex adversarial-review step — shipped CLI-first (`codex exec` + `tools/codex.md`). CLOSED. ⚠️ Linear MDS-47 still Backlog. |
| #409 | Event-server registration non-fatal at startup + background retry — **shipped v0.28.0** (PR #413). The headline stability fix. |
| #326 | Reactor dedup key now includes per-delivery event id (reviewer follow-up comments no longer dropped) — **shipped v0.28.0** (PR #408). |
| #321 | pr-feedback posts one resolution comment via the lead — **shipped v0.28.0** (PR #402). |
| #329 | Graceful preflight degradation for non-required services (`required:` flag) — **shipped v0.28.0** (PR #405). |
| #323 | Auto-fix CI failures on any open PR — **shipped v0.28.0** (PR #400). |
| #325 | Convention: changelog/version only at release (docs + CI guard) — **shipped** (PR #404). |
| #363 | [MDS-48/MDS-49] Gateway harness — closed not-planned 2026-06-21. Shipped via a **different approach** in #396 (aichat baked as CLI-first; gateway/chat/embedding connection kinds retired). |
