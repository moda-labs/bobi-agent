# Containerized Instances — Technical Design

Status: **design complete, ready to ticket (2026-06-11).** This doc is a
handoff: an agent should be able to file the §10 tickets in order without
re-deriving the analysis. Research session covered runtime inventory,
isolation options, sizing, auth handling, scale-to-zero, and an internal-first
scoping pass.

Amended 2026-06-12: Anthropic auth decision + subscription login bootstrap
(§6.1, C23), deploy/update automation (C22), MVP cut for the EC2 → Fly
migration (§10 "MVP cut"), C12 promoted into the MVP, open question 3
resolved.

Related designs:
- `docs/design/AUTH.md` (#142) — event-server auth. Complementary, not a
  dependency: this design deliberately keeps integration auth **outside** the
  instance boundary (§6), so the two efforts stay decoupled.
- `docs/design/EVENT_CONTRACT_V2.md` (#177–#181) — Worker adapter refactor.
  Any Worker-side changes here (routing verification C12, wake-on-event C15)
  sequence **after #177** so they're written against the stable adapter
  structure.

---

## 1. Goal and framing

Run each modastack instance in an isolated container/microVM — one instance
per "account." The near-term deployment is an **internal tool**: we operate a
handful of instances for ourselves, dogfooding the model's ability to become
a SaaS product later. That framing drives two hard constraints:

1. **Auth stays separate from the instance work.** Instances consume
   credentials only as env vars (already modastack's contract via `${VAR}`
   refs in `agent.yaml`). How those env vars get populated — a human running
   `fly secrets set` today, a token broker later — is invisible to the
   instance. (One scoped exception: subscription-mode Anthropic auth
   persists OAuth credentials on the volume rather than in env — see §6.1;
   the separation principle still holds because nothing inside the instance
   manages how they got there.)
2. **Control-plane work is minimized.** For the internal phase, the "control
   plane" is the existing Cloudflare Worker plus a provisioning script. No
   database, no dashboard, no new services. Slack (which agents already use)
   is the UI; `fly ssh console` is the admin door.

Non-goals for this design: billing, user-facing dashboard, multi-user account
management, integration OAuth flows. §9 records the seams those plug into
later so nothing built now gets thrown away.

## 2. The instance contract

The abstraction everything else hangs off:

> **A modastack instance is: one container image + one persistent volume
> (mounted as project root and `$HOME`) + a set of env vars + an outbound
> connection to one event-server deployment. Nothing reaches into it; it
> reaches out to nothing else.**

Consequences:
- Tenant identity lives entirely in volume + env. All instances share one
  image.
- No inbound networking, ever. Webhooks hit the shared Worker; instances
  connect outbound via WebSocket (`events/client.py`) and drain with their
  `last_seen` cursor. This is the strongest isolation property in the design
  and also what makes scale-to-zero possible (§7).
- Anything that respects the contract can be swapped independently: the
  provisioner (script → service), the credential source (human → broker),
  the UI (Slack → dashboard), the platform (Fly → anything that boots a
  container with a volume).

## 3. Runtime inventory (what actually runs in the box)

From the codebase audit. One instance's process tree, all localhost:

| Process | Source | Notes |
|---|---|---|
| Manager (Python) | `cli.py:248-341` | PID file `.modastack/state/manager.pid`; spawns the rest. In containers, runs as PID 1 via `--foreground` |
| Manager's persistent Claude session | `session.py:196` via `claude-agent-sdk` → `claude` CLI subprocess | `permission_mode="bypassPermissions"` (`session.py:189`) |
| Subagents | `subagent.py:522-591`, detached | one `claude` process each; **no concurrency cap today** (C3) |
| Per-session inboxes | `inbox.py`, `events/drain.py` | in-memory queues, **no HTTP server / no ports** — messages arrive as `inbox/<session>` events over the event-server subscription/drain path; `deliver(wait=True)` is pub/sub request-reply (#269) |
| Embedding sidecar | `kb/embedder.py:70-101`, `kb/sidecar.py` | lazy-loads fastembed/ONNX (~200 MB RSS); torch dependency removed in C4 |
| Local Node event server | `events/server.py:82-134`, port 8080 | **not run in deployed instances** — they point at the Worker via `event_server_url` (C6) |
| Monitor scheduler | `monitors/scheduler.py:143-148` daemon thread | interval loop; gets a remote-tick mode in Phase 2 (C17) |

State layout (everything per-project, which is why containerization is
clean): `.modastack/` holds config, sessions, KB sqlite DBs, event cursor
(`state/cursor.json`), monitor dedup (`state/monitor_state.json`),
`history.db`. Outside the project dir: `~/.claude/projects/` (Claude Code
transcripts — **required for session resume**, `session.py:184`), and the
fastembed model cache (embedding model). Both land on the volume by setting
`HOME` to a volume path; the C8 image instead pre-seeds the model cache at
build (see below).

Home-dir / CLI-path touchpoints — **resolved in C1 (#332):** `history.py` and
`browser.py` already follow `$HOME`; the only fix was `sdk.py`'s claude-CLI
fallback (now `_resolve_cli_path()`: PATH-first, Homebrew only on macOS).
Embedding-cache note (post-C4): fastembed honors `FASTEMBED_CACHE_PATH` but
**not** `HF_HOME`; the sidecar bridges this (`_resolve_cache_dir()`: prefers
`FASTEMBED_CACHE_PATH`, else `$HF_HOME/fastembed`), so C8 can pre-seed via
either env.

## 4. Architecture

```
                      ┌──────────────────────────────────┐
  GitHub / Slack /    │  Event server (Cloudflare Worker  │
  Linear webhooks ──▶ │  + Durable Objects)               │
                      │  per-deployment id + api_key      │
                      │  Phase 2: DO alarms, wake calls   │
                      └───────────────┬──────────────────┘
                                      │  outbound WSS only
                     ┌────────────────┼────────────────┐
                     ▼                ▼                ▼
               ┌───────────┐   ┌───────────┐   ┌───────────┐
               │ instance-a │   │ instance-b │   │ instance-c │   Fly Machines
               │ Fly machine│   │            │   │            │   (Firecracker),
               │ + volume   │   │ + volume   │   │ + volume   │   one app per
               └───────────┘   └───────────┘   └───────────┘   instance
```

Platform: **Fly Machines** (Firecracker microVMs). Chosen for: VM-grade
isolation (required — see §5), per-machine persistent volumes, stop/start
via REST API (Phase 2 wake), private networking, and a provisioner that's a
script rather than an operator. Fly is an independent provider (not AWS);
Firecracker is the AWS-originated open-source hypervisor it runs on. The
exit path if needed is Fargate/Cloud Run/K8s+gVisor — a provisioner rewrite,
not an architecture change, because of §2. (Why not AWS directly: Fargate
also runs on Firecracker, but AWS exposes only ephemeral tasks — no
resumable stateful microVM, which is exactly the primitive Phase 2 needs.
EC2 stop/start is 30–90 s + AMI management; the per-tenant cost floor of an
always-running instance defeats scale-to-zero economics. The current EC2
director *is* the AWS version of this design and is fine for one always-on
box; Fly is specifically the bet on Phase 2.)

Event server: the existing Worker. Local Node event server is not deployed
in instances.

Interaction (internal phase): Slack via the existing integration, plus
`fly ssh console -a <app> modastack ask|status|events`. The future dashboard
talks over the event bus (`user_message` events in, activity mirroring out)
— see §9; nothing is built for it now.

## 5. Isolation and security model

Sessions run with `bypassPermissions` and consume external, attacker-
influenceable content (webhook payloads, issue bodies). **Treat every
instance as hostile**: it is arbitrary code execution driven by an LLM.

In place for the internal phase:
- Firecracker microVM per instance (hardware-virtualized, no shared kernel).
- Dark instances: no inbound connectivity at all.
- Non-root container user. (Also load-bearing: Claude Code **refuses
  `bypassPermissions` as root** unless `IS_SANDBOX=1` — C8 acceptance
  criterion.)
- Per-instance, minimally-scoped tokens; per-instance Anthropic workspace
  keys with spend caps set in the console (cost-runaway control, C14).
- Resource limits (machine size) bound the blast radius of runaway
  processes.

**Accepted risk (internal phase only):** no egress filtering. A
prompt-injected agent can exfiltrate its own instance's tokens. Mitigants:
scoped tokens, internal-only tenants, dark instances. **Trigger to close the
gap:** before any non-employee tenant, deploy an egress proxy (Smokescreen
on the Fly private network, `HTTP(S)_PROXY` + per-instance allowlist:
api.anthropic.com, github.com, slack.com, linear.app, the Worker). Tracked
as deferred ticket D1.

## 6. Credentials (the separation principle)

The instance-side contract is **env vars only**, which already exists:
`.modastack/.env` / process env resolved through `${VAR}` refs
(`config.py:41-74`). This design adds **nothing** auth-related inside the
instance.

- Internal phase: a human sets `ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`,
  `LINEAR_API_KEY`, `GITHUB_TOKEN` (scoped PATs / bot tokens) via
  `fly secrets set`. Rotation = set + machine restart.
- SaaS phase (deferred, D2): a token broker (likely self-hosted Nango +
  GitHub App installation tokens) becomes the thing that populates/refreshes
  the same env vars, with refresh tokens held outside the VM. Short-lived
  tokens will need refresh-without-restart; the planned session-recycling
  (C21) doubles as the token-refresh point, and a `credential_helper`
  indirection in config is the fallback seam. None of this affects Phase
  0/1 code.

Event-server auth (deployment `api_key`, `config.py:178-199`) is unchanged
here and evolves separately under `docs/design/AUTH.md`.

### 6.1 Anthropic auth: API key vs subscription (decided 2026-06-12)

Two supported per-instance modes, selected at provision time
(`MODASTACK_AUTH=api_key|subscription`); the C8 image supports both.

**Fleet default: per-instance workspace API keys** with console spend caps
(C14). This is the only option with per-instance cost isolation.

**Subscription mode (internal dogfood instances only).** The proven
mechanism is what the EC2 director already does: one interactive
`claude /login`, OAuth credentials persisted to
`~/.claude/.credentials.json` on the volume (i.e. inside `$HOME`),
refresh-token rotation rewriting the file in place — verified live on prod
2026-06-12 (`subscriptionType: max`, file mtime same-day, no
`ANTHROPIC_API_KEY` anywhere). Rules:

- **One login per machine.** Each login gets its own refresh-token chain.
  Never copy `.credentials.json` between machines — shared chains
  invalidate each other on refresh.
- **`ANTHROPIC_API_KEY` must be entirely absent** in this mode: in the
  auth precedence order it silently beats `CLAUDE_CODE_OAUTH_TOKEN` and
  subscription credentials, and bills the API instead.
- `claude setup-token` (the nominally official headless path) was
  evaluated and **rejected** for now: 1-year stated lifetime, but repeated
  field reports of tokens dying in 8–12 h headless because refresh isn't
  handled (claude-code issues #12447, #31095). Revisit if fixed.
- **Economics, effective 2026-06-15:** Agent SDK usage bills a separate
  per-user monthly credit pool, not interactive plan limits — $200/mo on
  Max 20x, per account, non-shareable. All subscription instances on one
  account draw from that single pool with no per-instance attribution
  (only fleet-wide view: claude.ai/settings/usage for the logged-in
  account). Multiple always-on autonomous instances on one consumer
  account is also account-sharing gray zone under the usage policy. Both
  accepted for internal dogfooding only; D2 (token broker / API keys) is
  the durable answer.
- **Migration note:** the EC2 director rides a Max subscription today.
  Moving instances to API-key mode is the moment modastack token spend
  becomes a real line item — budget before migrating, not after. The
  June 15 metering also hits the existing EC2 box regardless.

First-login UX is C23 (Slack + event-bus bootstrap); `fly ssh console`
then `/login` is the manual fallback.

## 7. Scale-to-zero (Phase 2)

Internal phase runs **always-on** (a handful of machines; cost is trivial).
Scale-to-zero is the second dogfooding milestone because it's product-core
for SaaS economics. Design:

**Wake path.** A stopped machine has no WebSocket, so the Worker must wake
it: per-deployment DO stores a `machine_id` mapping (registered by the
provisioner) and holds a Fly API token; events arriving for a stopped
deployment trigger a machine-start call. The instance boots, reconnects with
its cursor, drains, acts. This is the only genuinely new control-plane logic
in the whole design.

**Monitors: move the clock out, keep execution in.**
- Monitor registry syncs *up* to the DO at provision/config-push; DO alarms
  fire `monitor_due` events (durable, free while idle).
- The scheduler gains a remote-tick mode: handle `monitor_due` instead of
  running an interval loop. Dedup state stays in `monitor_state.json` on the
  volume, so re-wakes never double-fire.
- **Native checks run hosted** (control-plane side, broker/scoped tokens):
  `pr_conflicts`, `stale_prs` (`monitors/checks.py`) are pure API polling —
  the common path ("nothing happened") must not boot a VM. Only findings
  publish events that wake the instance. Agent-based checks (no `check:`)
  still wake the VM; interval floors apply.
- Batch on wake: any boot runs everything due or nearly-due.

**Agent-written background processes.** Watch scripts / sleep-loops written
by sessions silently die at stop — worse than failing, the agent believes
it's watching. Three-part fix:
1. Ephemerality contract in `prompts/base.md`: machine stops when idle;
   never background-loop or sleep-to-wait; register a monitor or schedule
   instead.
2. New primitive `modastack schedule "<prompt>" --in <dur>` — one-shot
   wake-up (a `count=1` monitor backed by a DO alarm). Converts the whole
   watch-script class into durable state.
3. Manager quiescence protocol: idle ⇔ no active sessions, no running
   subagents, empty inboxes, no due monitors, **no live keepalive leases**.
   Leases (TTL'd, via manager endpoint or lease file) let legitimate
   long-running work pin the machine. At quiescence the manager kills orphan
   processes deliberately and exits 0; Fly `auto_stop` does the rest.

Fly machine *suspend* (memory snapshot) is explicitly not relied on —
resumed timers/sockets misbehave; at most a cold-start optimization later.

Escape valve: "always-on" stays available per-instance (`auto_stop: false`)
so no edge case ever forces contorting the platform.

## 8. Sizing

Memory is the cost driver and scales with concurrent `claude` processes
(observed 0.5 GB fresh → 1–2 GB on long transcripts, with documented leak
incidents beyond that; CPU is network-bound and nearly irrelevant).

> RAM ≈ 250 MB base + 1.5 GB × (concurrent sessions) + ~200 MB embedder
> (fastembed; ~1 GB if torch sidecar remains)

- Default machine: **4 GB / shared-2x** — persistent manager session + one
  subagent + embedder, with headroom.
- Heavy (3–4 concurrent agents): 8 GB. Floor: 2 GB single-session.

Bounded only if C3 (subagent concurrency semaphore) ships — today spawning
is uncapped. Leak containment rather than worst-case provisioning: cgroup
limit + C21 session recycling (restart `claude` on RSS threshold / N events;
resume-by-id makes this cheap and it doubles as the token-refresh point).

Disk: 10–20 GB volumes (repo clones, transcripts, `history.db`, KBs grow
unbounded); `doctor` disk check + retention policy in C13.

## 9. Deferred SaaS seams (record, don't build)

| Later capability | Seam that already exists after Phases 0–2 |
|---|---|
| Web dashboard chat | publish `user_message` events to the deployment topic; drain already delivers events to session inboxes |
| Dashboard read views / audit / billing | mirror `sessions/*/log.jsonl` + `state/decisions.jsonl` as events; control plane persists its own read model — never reach into VMs |
| Token broker | populates the same env vars; C21 recycling = refresh point |
| LLM gateway (per-tenant metering) | `ANTHROPIC_BASE_URL` env var |
| Egress filtering | D1, Smokescreen + proxy env vars |
| Provisioner service | replaces the C10 script + C22 Action behind the same contract |

### 9.1 Operator-agnostic — single-operator self-serve

The same swappability that defers SaaS also has to keep the *operator*
swappable: nothing built in Phases 0–1 may assume **moda-labs** is the one
running it. The internal fleet and a lone individual standing up one instance
on accounts they bought themselves are the **same code on different
accounts** — the solo case is strictly *simpler* than SaaS (no multi-tenancy,
no billing, no broker), so guarding it now costs near nothing and a hardcoded
assumption is the only thing that can foreclose it.

What the solo operator already gets for free:
- **§2 contract** — their own Fly volume, their own env-var credentials,
  outbound-only. Nothing reaches in.
- **Subscription auth (§6.1 / C23)** — the *ideal* solo path: `claude /login`
  with their own Claude Max sub, creds on their volume, no API billing to set
  up. The §6.1 shared-pool / account-sharing caveats are *multi-instance*
  concerns and don't apply to one person running one instance.

What must stay agnostic (acceptance criteria folded into C8/C10 below):
- **Two accounts, not one.** Besides Fly, an instance needs an event-server
  deployment. The solo operator brings **their own** by `wrangler deploy`-ing
  the Worker into their own Cloudflare account and pointing
  `event_server_url` at it. The shared moda-labs Worker is just *our*
  instance of that — never a hard dependency. C10 takes the Worker URL as a
  parameter; C8/C10 ship a "deploy your own event server" runbook.
- **No namespace squatting.** Fly app names are globally unique across all of
  Fly, so `modastack-<name>` must be operator-namespaced/configurable, not a
  fixed string that collides on a stranger's account.
- **The solo path is C10 standalone.** C22 (GitOps over *our* agent-teams
  repo) is a moda-labs convenience layered on top — never the only way in. A
  person runs C10 (or its `modastack`-command wrapper) against their own
  `flyctl` auth and is done.

Not building a polished self-serve UX now — just refusing to bake in
assumptions that would make one impossible later. No tracking issue; this is
a standing constraint on every Phase-1 ticket.

## 10. Work breakdown (ticket this, in order)

Ticket IDs below are local to this doc (C-numbers); file as GitHub issues
and back-link them here. Phases are strict dependency layers; tickets within
a phase are parallelizable unless noted.

**Filed 2026-06-18 — epic [#344](https://github.com/moda-labs/modastack/issues/344).**
MVP-cut issue mapping:

| C | Issue | Phase | Dispatch |
|---|---|---|---|
| C1 | [#332](https://github.com/moda-labs/modastack/issues/332) | 0 | ✅ merged (PR #345) |
| C2 | [#333](https://github.com/moda-labs/modastack/issues/333) | 0 | Modastack |
| C3 | [#334](https://github.com/moda-labs/modastack/issues/334) | 0 | Modastack |
| C4 | [#346](https://github.com/moda-labs/modastack/issues/346) | 0 | Modastack (promoted from deferred) |
| C5 | [#335](https://github.com/moda-labs/modastack/issues/335) | 0 | Modastack |
| C6 | [#336](https://github.com/moda-labs/modastack/issues/336) | 0 | Modastack |
| C7 | [#337](https://github.com/moda-labs/modastack/issues/337) | 0 | Modastack |
| C8 | [#338](https://github.com/moda-labs/modastack/issues/338) | 1 | human/infra |
| C9 | [#339](https://github.com/moda-labs/modastack/issues/339) | 1 | human/infra |
| C10 | [#340](https://github.com/moda-labs/modastack/issues/340) | 1 | human/infra |
| C12 | [#341](https://github.com/moda-labs/modastack/issues/341) | 1 | human/infra |
| C22 | [#342](https://github.com/moda-labs/modastack/issues/342) | 1 | human/infra |
| C23 | [#343](https://github.com/moda-labs/modastack/issues/343) | 1 | human/infra |

### MVP cut — EC2 → Fly migration (decided 2026-06-12)

Target state: the single EC2 director model replaced by ~3 internal
instances (one per agent team in the registry) on Fly, updatable for both
team-package and modastack-version changes. Always-on; no Phase 2.

**File these 12:** C1, C2, C3, C5, C6, C7, C8, C9, C10, C12, C22, C23.

- C3 is in despite being skippable-by-sizing: both 2026-06-11 EC2
  incidents (disk fill, RAM thrash) were uncapped concurrent sessions —
  don't migrate the failure mode.
- C7 is in because the MVP's point is rolling image updates against
  volumes that outlive them.
- C12 is in because three teams share one Slack workspace and GitHub org
  from day one. It is blocked on #177 — the #177 → C12 chain is the only
  schedule risk; everything else is parallel.

**Deferred from MVP** (fast-follows, not gates): C11, C13, C14 (as
runbook), all of Phase 2. (C4 was promoted into the active set 2026-06-18 —
it shrinks the C8 image by dropping torch — but is still not an MVP gate.)

### Phase 0 — framework prep (no infra dependency, all in modastack/)

**C1 — Make home-directory and CLI-path assumptions container-safe.**
Audit and fix: `history.py:13` (`~/.claude/projects/`), `browser.py:39-42`
(browse binary, playwright cache), `sdk.py:29` (claude CLI fallback path —
must resolve from `PATH` on Linux), HF cache (`HF_HOME`). Everything must
follow `$HOME` so a volume-mounted home works.
*Accept:* full unit suite passes with `HOME` pointed at a temp dir; no
absolute `/Users` or `/opt/homebrew` assumptions remain outside macOS
fallbacks.

**C2 — First-class foreground/PID-1 mode + manager health endpoint.**
`start --foreground` exists internally (`cli.py`); make it the documented
container entrypoint: skip PID-file daemonization checks in this mode, log
to stdout/stderr, handle SIGTERM with graceful session shutdown, and expose
`GET /health` on the manager (not just the event server) for machine checks.
*Accept:* runs as PID 1 under `tini` in a container; SIGTERM exits 0 within
grace period; `/health` returns manager + session status.

**C3 — Subagent concurrency semaphore.**
No cap exists (verified: no `max_concurrent`/semaphore in the package).
Add `max_concurrent_agents` to `agent.yaml` (default e.g. 2); launches
beyond the cap queue. Makes memory bounded: base + cap × 1.5 GB.
*Accept:* integration test launching cap+N agents shows queueing, not
parallel spawn; config knob respected.

**C4 — Replace torch embedding sidecar with fastembed/ONNX.**
Same model (`all-MiniLM-L6-v2`, 384-dim) via fastembed; ~200 MB RSS instead
of ~1 GB and drops the torch dependency from the image. Keep the sidecar
HTTP contract (`/health`, `/embed`) so `kb/embedder.py` is untouched, or
inline it — implementer's choice.
*Accept:* `kb` test suite passes; sidecar RSS < 300 MB after first embed;
torch absent from the deployment dependency set.

**C5 — Non-interactive install / first-boot provisioning path.**
`modastack install <team> --non-interactive`: no prompts, secrets assumed
present in env, suitable for an entrypoint to run when the volume is empty.
*Accept:* fresh empty dir + env vars → `install --non-interactive` →
`start --foreground` works end-to-end with no TTY.

**C6 — Never start the local event server when `event_server_url` is
remote.** Verify/gate `ensure_running()` (`events/server.py:82-134`) so a
remote URL means no Node requirement at runtime.
*Accept:* instance with remote URL runs in an image with no Node installed.

**C7 — State format version marker.**
Write `.modastack/state/format_version`; check on startup, refuse (with a
clear message) on unknown-newer, hook point for future migrations. Cheap
now, painful retrofit later (volumes outlive images).
*Accept:* version written on init; mismatch path covered by a unit test.

### Phase 1 — internal deployment, always-on (depends: Phase 0)

**C8 — Container image.**
Python 3.11+, `modastack` from source/wheel, **pinned** `claude` CLI version
on `PATH`, fastembed model pre-downloaded (set `FASTEMBED_CACHE_PATH`, or
`HF_HOME` which the sidecar bridges, to an image path seeded at build), **non-root user**, `tini` + `modastack start --foreground`
entrypoint, no Node. Verify headless SDK auth via `ANTHROPIC_API_KEY` and
that `bypassPermissions` works as the non-root user (root would require
`IS_SANDBOX=1` — do not run as root). Support both auth modes (§6.1) via
`MODASTACK_AUTH`; in subscription mode assert `ANTHROPIC_API_KEY` is unset
(precedence gotcha) and use volume credentials.
*Accept:* `docker run` with a mounted empty volume + env vars reaches a
healthy manager that completes one `modastack ask` round-trip against the
real API; subscription mode reaches the same state from a volume holding
valid `.credentials.json`.

**C9 — First-boot entrypoint logic.**
If the volume is empty: `install <team> --non-interactive` with team name
from `MODASTACK_TEAM` env, then start. Idempotent on restart.
*Accept:* same image boots both a fresh and an existing volume correctly.

**C10 — Provision script (`scripts/provision-instance.sh` or `make
new-instance`).** Fly API: create app (volume 10–20 GB), set secrets from a
local env file, register a deployment with an event-server Worker (capture
`deployment_id`/`api_key` into secrets), launch machine (4 GB / shared-2x,
`auto_stop: false` for now). Also `destroy-instance`.
**Amended 2026-06-18 (implementation):** the "register a deployment / capture
`deployment_id`/`api_key` into secrets" step is obsolete — it predates the #240
bubble model. Instances now **self-mint a bubble and self-register every session
at boot** (`subagent.py` → `ensure_bubble`/`register`), so the provisioner's only
event-server job is to pass the Worker URL (`MODASTACK_EVENT_SERVER`, an
`https://` value the client derives `wss://` from). No deployment IDs touch the
provisioner. The instance is **dark** (no `[http_service]`/inbound), which also
makes `auto_stop: false` the natural default until Phase 2. The volume's
`agent.yaml` is the config source of truth after seeding — the script never
re-writes it. Secrets set depend on auth mode (§6.1): skip the Anthropic key
entirely for subscription instances.
**Operator-agnostic (§9.1) — no moda-labs assumptions:** runs against
whatever account `flyctl` is authed to; the **app name is operator-namespaced
/ configurable** (Fly names are globally unique — never a fixed
`modastack-<name>` that squats on a stranger's account); the **event-server
URL is a parameter** pointing at the operator's own Worker, with the shared
moda-labs Worker just a default. Ship a short **"deploy your own event
server"** runbook (`wrangler deploy` the Worker into the operator's own
Cloudflare account) alongside the script.
*Accept:* one command → working instance reachable through Slack, run against
a **fresh personal Fly account** with no moda-labs-specific config; runbook
(including the bring-your-own-Worker steps) in the script header.

**C11 — Backup script.**
Nightly: `.modastack/` (minus `state/*.pid`, logs), `workspace/`,
`~/.claude/projects/` → object storage (S3 — deliberately outside Fly, so
tenant state is never hostage to the platform), per-instance prefix,
simple retention. Fly volume snapshots are single-host, daily, ~5-day
retention — not sufficient alone. Include a tested restore path.
*Accept:* restore drill: new volume from backup boots and resumes an
existing session.

**C12 — Verify Worker routing for shared-workspace dogfooding.**
Multiple instances, one Slack workspace/app and one GitHub org: confirm the
Worker + subscription keys (`events/subscriptions.py`) route Slack events
per-channel and GitHub events per-repo to the right deployment, rather than
broadcast-and-filter (tolerable internally, a cross-tenant leak for SaaS —
if broadcast is what exists, file the fix as a blocker on D-phase).
**Sequence after #177** (event contract v2). **Promoted into the MVP cut**
(2026-06-12): with three teams on one workspace/org from day one,
broadcast-and-filter means each instance receives — and burns tokens
triaging — every other team's events.
*Accept:* two live instances, disjoint channels/repos, no cross-delivery in
`events.jsonl`.

**C13 — Disk hygiene.** `doctor` check for volume usage; retention/rotation
for transcripts, `events.jsonl`, `history.db`.
*Accept:* doctor warns at threshold; rotation covered by a unit test.

**C14 — Cost-control runbook.** Per-instance Anthropic workspace keys with
console spend caps; document key issuance in the provisioning runbook (C10).
No gateway yet. Record the §6.1 decision: subscription auth was evaluated
as fleet default and rejected (no per-instance cost isolation, shared
per-account credit pool, account-sharing policy) — do not re-litigate.
*Accept:* documented; each running api_key-mode instance has a capped key.

**C22 — Deploy/update automation (GitOps).**
The agent-teams repo doubles as the registry (`registry.py` already
fetches remote teams). One GitHub Action on push to main, diffing team
dirs:
- **Added team** → run C10 `provision-instance.sh <team>` (secrets from
  per-team GitHub environment secrets, e.g. `TEAM_<NAME>_*`; an instance
  with unpopulated secrets provisions but sits unhealthy until filled —
  same seam D2 plugs into).
- **Changed team** → trigger `modastack agents update` on the matching
  instance. Never re-provision: volume `agent.yaml` is source of truth,
  reinstall must not clobber workspace edits.
- **modastack release** → scripted `fly deploy` loop over the fleet.
  Volumes and sessions survive; C9 idempotency makes restart safe; C7
  guards format-version skew.
- **Deleted team** → nothing automatic; `destroy-instance` stays human.

Keep it a script + ~50-line workflow — no deploy service, no deployment
DB; the Fly API is the state store, the Action log is the deploy log.
This implements the §9 provisioner seam; replace with a service only when
instance count makes the loop creak (§11.3).
*Accept:* push a new team dir → live instance with no manual step beyond
secrets; push a team edit → matching instance updated, workspace intact;
tag a modastack release → all instances on the new version with sessions
resumed.

**C23 — Subscription login bootstrap over Slack + event bus.**
For `MODASTACK_AUTH=subscription` first boot with no `.credentials.json`
on the volume: run `claude /login` under a pty and scrape the auth URL;
post it to a **private** Slack channel/DM via the `SLACK_BOT_TOKEN`
already in env; connect the event-server WebSocket and wait for the
human's reply (the pasted code) to arrive as a normal
Slack→Worker→deployment event; write the code to the pty; verify
`.credentials.json` appeared; exec `start --foreground`. Private channel
is a hard requirement — the code is short-lived/single-use but grants the
login to whoever pastes it first. Refresh rotation makes this a
once-per-machine ceremony (§6.1). Fallback: `fly ssh console` + `/login`.
Touches C8 (auth-mode switch) and C10 (skip Anthropic key secret).
*Accept:* fresh subscription-mode instance reaches healthy where the only
human actions are one browser login and one Slack reply.

### Phase 2 — scale-to-zero (depends: Phase 1 live; Worker work after #177)

**C15 — Wake-on-event in the Worker.** Deployment→`machine_id` mapping
(written by C10), Fly API token in Worker secrets, start-machine call on
event arrival for a stopped deployment. Failure mode: if wake fails, events
queue (cursor model already tolerates this) and an ops alert fires.
*Accept:* event posted to a stopped instance → machine running and event
drained < 10 s, demonstrated in a live test.

**C16 — Manager quiescence protocol + keepalive leases.** Idle definition
(§7), TTL'd leases, deliberate orphan-process cleanup, clean exit 0 →
`auto_stop` (flip C10 default to `auto_stop: true` when this lands).
*Accept:* integration test — instance with no work exits within idle
window; a held lease prevents exit; an expired lease doesn't.

**C17 — Monitor remote tick.** Registry sync-up to DO at provision/config
push; DO alarms publish `monitor_due`; scheduler handles it instead of the
interval loop when in remote mode; dedup via existing `monitor_state.json`;
batch-on-wake.
*Accept:* monitor fires on schedule with the machine stopped between ticks;
no double-fire across a wake.

**C18 — Hosted native checks.** Run `check:`-typed monitors
(`monitors/checks.py`) control-plane-side with scoped tokens; publish events
only on findings. The "nothing happened" path must not boot a VM.
*Accept:* stale-PR scenario wakes the instance; clean poll does not.

**C19 — `modastack schedule "<prompt>" --in <dur>`.** One-shot wake-up as a
`count=1` monitor backed by a DO alarm; delivers the prompt as an event.
*Accept:* schedule from inside a session, stop the machine, wake fires and
the prompt reaches the session.

**C20 — Ephemerality contract in `prompts/base.md`.** Machine-stops-when-
idle rules; point agents at monitors/`schedule`/leases instead of background
loops and sleep-waits. *Accept:* prompt updated; dogfood transcript review
shows agents using `schedule` rather than watch scripts.

**C21 — Session recycling.** Restart the persistent manager `claude`
process on RSS threshold or N events, resuming by session id
(`session.py:184`). Memory-leak containment now; token-refresh point later.
*Accept:* integration test — forced recycle preserves conversational
continuity; RSS drops after recycle.

### Deferred (file as placeholder issues; do not build)

- **D1 — Egress proxy** (Smokescreen + allowlists). Blocker for any
  non-employee tenant.
- **D2 — Token broker** (Nango/GitHub App; env-var contract unchanged).
- **D3 — Dashboard + gateway** (event-bus `user_message` + activity
  mirroring read model).
- **D4 — LLM gateway** (per-tenant virtual keys via `ANTHROPIC_BASE_URL`).
- **D5 — Worker multi-tenant hardening** (rate limits, retention, per-tenant
  webhook signature verification) — overlaps `docs/design/AUTH.md`; sequence
  with it.

## 11. Open questions

1. Wheel vs. source install in the image (C8) — affects release cadence for
   instance updates; default to wheel + pinned version.
2. Does `/browse` (Playwright Chromium, ~250 MB + system libs) ship in the
   base image or a variant? Default: leave it out; add a variant if dogfood
   demand appears.
3. ~~Fleet upgrade mechanics~~ — **resolved 2026-06-12 → C22** (scripted
   `fly deploy` loop + GitHub Action); revisit only when instance count
   makes the loop creak.
4. C12 outcome decides whether subscription routing needs Worker changes
   before SaaS (potential new ticket under the event contract v2 umbrella).
5. Subscription credit monitoring: all subscription-mode instances on one
   account share a single monthly Agent SDK pool (§6.1) with no
   per-instance attribution — how do we notice one team starving the
   others before work silently stalls? Only fleet-wide view today is
   claude.ai/settings/usage.
