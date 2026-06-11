# Containerized Instances — Technical Design

Status: **design complete, ready to ticket (2026-06-11).** This doc is a
handoff: an agent should be able to file the §10 tickets in order without
re-deriving the analysis. Research session covered runtime inventory,
isolation options, sizing, auth handling, scale-to-zero, and an internal-first
scoping pass.

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
   instance.
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
| Per-session inbox HTTP servers | `inbox.py:128-144` | `127.0.0.1:0` random ports, negligible footprint |
| Embedding sidecar | `kb/embedder.py:70-101`, `kb/sidecar.py` | lazy-loads sentence-transformers/torch (~0.6–1 GB RSS); replaced by fastembed in C4 |
| Local Node event server | `events/server.py:82-134`, port 8080 | **not run in deployed instances** — they point at the Worker via `event_server_url` (C6) |
| Monitor scheduler | `monitors/scheduler.py:143-148` daemon thread | interval loop; gets a remote-tick mode in Phase 2 (C17) |

State layout (everything per-project, which is why containerization is
clean): `.modastack/` holds config, sessions, KB sqlite DBs, event cursor
(`state/cursor.json`), monitor dedup (`state/monitor_state.json`),
`history.db`. Outside the project dir: `~/.claude/projects/` (Claude Code
transcripts — **required for session resume**, `session.py:184`),
`~/.cache/huggingface/` (embedding model). Both land on the volume by
setting `HOME` to a volume path.

Known home-dir touchpoints to audit in C1: `history.py:13`,
`browser.py:39-42`, `sdk.py:29` (claude CLI path fallback).

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
not an architecture change, because of §2.

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
| Provisioner service | replaces the C10 script behind the same contract |

## 10. Work breakdown (ticket this, in order)

Ticket IDs below are local to this doc (C-numbers); file as GitHub issues
and back-link them here. Phases are strict dependency layers; tickets within
a phase are parallelizable unless noted.

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
on `PATH`, fastembed model pre-downloaded (set `HF_HOME` to an image path
seeded at build), **non-root user**, `tini` + `modastack start --foreground`
entrypoint, no Node. Verify headless SDK auth via `ANTHROPIC_API_KEY` and
that `bypassPermissions` works as the non-root user (root would require
`IS_SANDBOX=1` — do not run as root).
*Accept:* `docker run` with a mounted empty volume + env vars reaches a
healthy manager that completes one `modastack ask` round-trip against the
real API.

**C9 — First-boot entrypoint logic.**
If the volume is empty: `install <team> --non-interactive` with team name
from `MODASTACK_TEAM` env, then start. Idempotent on restart.
*Accept:* same image boots both a fresh and an existing volume correctly.

**C10 — Provision script (`scripts/provision-instance.sh` or `make
new-instance`).** Fly API: create app `modastack-<name>`, volume (10–20 GB),
set secrets from a local env file, register a deployment with the Worker
(capture `deployment_id`/`api_key` into secrets), launch machine
(4 GB / shared-2x, `auto_stop: false` for now). Also `destroy-instance`.
The volume's `agent.yaml` is the config source of truth after seeding — the
script never re-writes it.
*Accept:* one command → working instance reachable through Slack; runbook
in the script header.

**C11 — Backup script.**
Nightly: `.modastack/` (minus `state/*.pid`, logs), `workspace/`,
`~/.claude/projects/` → object storage (Tigris/S3), per-instance prefix,
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
**Sequence after #177** (event contract v2).
*Accept:* two live instances, disjoint channels/repos, no cross-delivery in
`events.jsonl`.

**C13 — Disk hygiene.** `doctor` check for volume usage; retention/rotation
for transcripts, `events.jsonl`, `history.db`.
*Accept:* doctor warns at threshold; rotation covered by a unit test.

**C14 — Cost-control runbook.** Per-instance Anthropic workspace keys with
console spend caps; document key issuance in the provisioning runbook (C10).
No gateway yet. *Accept:* documented; each running instance has a capped
key.

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
3. Fleet upgrade mechanics — `fly deploy` per app, scripted loop is fine
   internally; revisit when instance count grows.
4. C12 outcome decides whether subscription routing needs Worker changes
   before SaaS (potential new ticket under the event contract v2 umbrella).
