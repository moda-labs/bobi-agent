# Agentic fleet control: an MCP server on the event-server Worker

> **Status:** Draft
> **Tracking issue:** TBD (label `plan`) · **Created:** 2026-07-30 · **Last amended:** —
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

Give an agent — a human's Claude, or one of bobi's own teams — first-class control of a
bobi fleet, by serving the Model Context Protocol from the Cloudflare event-server
Worker over the admin channel that the sidecar already answers.

The control plane exists and is proven: the sidecar's `AdminListener` executes nine
commands against a live deployment, the Worker publishes them into the target's
bubble-namespaced admin topic and folds the reply, and the hosted console has driven
that path in production since 2026-07-10. What is missing is a surface an agent can
bind to. Today the only client is a private Python web console; the reorg plan
(`plans/2026-07-29-repo-reorg.md`) publishes the *protocol* and tells external
consumers to build their own client.

This plan says the Worker should be that client's server. An MCP route on the Worker
is a schema wrapper over functions it already calls in-process — and it lands in
`event-server/`, which the reorg publishes anyway.

## Problem

**There is no agent-shaped surface on the control plane.** `GET /fleet/status`,
`POST /fleet/instances/:f/:i/commands`, and the poll-by-id route are a fine HTTP API
and a poor tool surface: an agent has to be told the URL shape, the 202-then-poll
contract, the per-command argument shapes (documented only in a comment at
`event-server/src/fleet.ts:330-343`), and the fact that `chat` never returns a reply.
Every consumer re-derives all of it.

**The obvious implementation is in the wrong language and the wrong repo.** A Python
MCP server would reuse `TeamRuntime` (`bobi/webapp/runtime.py:67`) — a public ABC whose
method list (`dashboard`, `team_status`, `start/stop/restart_team`, `subagents`,
`messages`, `chat_submit`, `chat_job`, `spend_summary`, `session_log`) is already
almost exactly the right tool surface, including the submit-then-poll split that MCP's
request/response model forces anyway. But its fleet backend, `EventBusRuntime`, is
private and the reorg moves it *deeper* into private (Movement 3, into `moda-agents`).
A public Python MCP server would have to drag an operator client out into the wheel —
new public API, new version surface, cutting against the reorg mid-flight.

**The credential model was sized for one trusted client.** `FLEET_OPERATOR_TOKEN`
(`fleet.ts:429`) is one bearer for the whole control plane: every fleet, every
instance, every command, and no actor recorded on the command record. That was
proportionate when the only holder was moda's own console behind moda's own auth. It
is not proportionate to a credential held by an agent whose context contains
attacker-controllable text.

**The injection surface is qualitatively worse here than anywhere else in bobi.**
`docs/SECURITY.md:64` establishes that inbound event content is untrusted and layers
defenses — scoped tokens, `await` gates, deterministic workflows, observability. An
MCP client agent breaks the assumption those defenses rest on. It *reads transcripts*
(`transcript`, `session_log`), which are a concentrated feed of exactly the
attacker-controllable Slack/GitHub/email text the security model warns about, and it
*holds lifecycle and chat tools* over other agents. `chat` is the sharp end: it is not
a read or a restart, it injects operator-attributed text into another agent's turn.
A single injected string in a transcript that reads as an instruction is a
confused-deputy path from one team's inbox into another team's control plane.

## Solution

### The Worker serves MCP

Add a `/mcp` route to `event-server/src/index.ts`, structurally identical to the
`/fleet/*` block at `index.ts:414`: bearer auth first, then dispatch. It speaks MCP's
Streamable HTTP transport in **stateless** mode — each `POST /mcp` carries one
self-contained JSON-RPC message and returns `application/json`. No session, no SSE, no
Durable Object. A tools-only server needs four methods (`initialize`,
`notifications/initialized`, `tools/list`, `tools/call`) plus `ping`; `GET /mcp` returns
405, which the spec permits for a server that never initiates a stream.

The tools call the same functions the HTTP routes call — `buildFleetStatus`,
`buildInstanceDetail`, `buildCommandView`, and the publish-to-admin-topic path — as
in-process calls against `createFleetKVStorage(env.EVENTS)`. No second implementation,
no HTTP hop, one place where the command vocabulary lives.

This resolves the language/repo problem outright: the MCP server is TypeScript in
`event-server/`, which reorg Movement 1 publishes. Nothing about the reorg changes
except its non-goal line ("external consumers build their own MCP client"), which
becomes "the Worker is the MCP server."

**Bobi teams can consume it with no framework change.** `mcp_servers` already accepts
`type: http` with `url` and `headers` (`bobi/validate.py:493`,
`bobi/mcp_handshake.py:94`), and `bobi validate` already runs an `initialize`
handshake against declared servers. An ops team declares the endpoint in `agent.yaml`
and the existing preflight proves the connection.

### The tool surface is task-shaped, not a command dump

Nine near-identical tools is a worse surface than six well-named ones. The MCP layer
is where the vocabulary gets shaped for a reader who has never seen the fleet:

| Tool | Backed by | Notes |
|---|---|---|
| `bobi_fleet_status` | `buildFleetStatus` | The orienting read. Every instance, reachability, manager state, versions. One call, no arguments. |
| `bobi_instance_detail` | `buildInstanceDetail` | Full heartbeat + lifecycle trail for one instance. |
| `bobi_read_transcript` | `transcript` command | **Output is untrusted data** (Q3). Session defaults to the manager. |
| `bobi_send_message` | `chat` command | Async by nature; returns a `command_id`. Description states plainly that no reply is returned and the reply is read back via `bobi_read_transcript`. |
| `bobi_lifecycle` | `restart`/`stop`/`start` | One tool, an `action` enum, and a **required `reason`** string recorded on the command. Write scope. |
| `bobi_command_result` | `buildCommandView` | Poll a `command_id`. The escape hatch for anything the bounded wait didn't resolve. |

`roster`, `spend`, and `session_log` fold into `bobi_instance_detail` or stay separate
— a build-time call once the response sizes are measured against a real fleet.

**Bounded server-side wait.** `tools/call` for a command waits up to ~5s for the fold
before returning, so the fast cases — `transcript`, `roster`, `spend` all return
sub-second in production — resolve in a single tool call instead of three. Slow cases
return `{status: "pending", command_id}` and the agent polls `bobi_command_result`.
The console already polls this exact read path at 0.5s intervals
(`webapp/runtime.py:COMMAND_POLL_INTERVAL`), so the KV read-after-write behavior is
proven in practice; the Worker-internal loop uses the same read path, and Phase 3's
proof verifies it rather than assuming it.

### Scoped operator credentials

Serving MCP changes *who holds the credential*, so the credential model has to change
with it (Q2). Replace the single-token check with a principal lookup:

- A token record in KV: `{label, scopes: ["fleet:read" | "fleet:write" | "fleet:chat"], fleets: [...] | null}`.
- `requireOperator` returns a **principal** rather than `null`-or-`Response`; every
  route asserts a scope. The existing `FLEET_OPERATOR_TOKEN` env secret stays valid
  and resolves to an all-scopes principal, so the console and every existing caller
  keep working unchanged.
- `FleetCommandRecord` gains an `actor` field (the principal's label). A `restart` in
  the KV trail becomes attributable, which it is not today.

This is the difference between a guard and a boundary. A client-side allowlist in the
MCP layer is bypassable by anyone holding the token with `curl`; a scope checked at
the Worker is not. It is also the precondition for Q3: "this agent can read the fleet
but cannot restart it" has to be enforceable, not documented.

### The injection stance

Three controls, in decreasing order of how much they actually do:

1. **Scopes are the structural control.** An agent whose job is monitoring gets
   `fleet:read` and *cannot* restart anything, whatever its context says. `fleet:chat`
   is separate from `fleet:write` because injecting text into another agent's turn is
   a different risk from bouncing a process.
2. **Transcript output is framed as data.** `bobi_read_transcript` returns messages in
   a structure that names them as untrusted third-party content, and the tool
   description says so. This is a mitigation, not a control — it raises the bar on a
   naive injection and stops nothing determined.
3. **`reason` is required on lifecycle.** Not a security control; an audit control.
   It makes the KV trail readable after the fact and gives a human reviewing the trail
   something to disbelieve.

`docs/SECURITY.md` gains a section for this surface. The honest framing: bobi's
existing stance assumes the agent reading untrusted text has narrowly scoped tokens.
The MCP client agent is the first one where the *tokens themselves* are the fleet, so
scoping them is the whole defense.

### What this does not add to the admin vocabulary

The nine commands stay nine. Extending them (event injection, monitor list/trigger,
workflow status, config read, log tail) is a coordinated sidecar-and-Worker change
with a bad failure mode: a supervisor predating the command drops it with no reply, so
the caller burns the full timeout — the console already carries dedicated short
timeouts for exactly this skew (`DEFAULT_SESSION_LOG_COMMAND_TIMEOUT`, with the
mid-rollout comment). v1 proves the surface on the vocabulary that already ships
everywhere. New commands come after, each with a version gate.

## Non-goals

- **No local MCP server.** A single dev box running against a local event server gets
  nothing here. A Python stdio `bobi mcp` over `LocalRuntime` is a different product
  for a different user; if it's wanted, it's a separate plan.
- **No OAuth in v1** (Q7). Bearer covers Claude Code and bobi agents, which is the
  whole intended audience. claude.ai custom connectors are a later phase.
- **No new admin commands**, per above.
- **No change to the bus, bubbles, or the sidecar.** The sidecar is untouched by this
  plan; every command already exists on it.
- **No public Python operator client.** The reason this design was chosen.

## Relationship to the repo reorg

This plan targets `bobi-agent/event-server/` **after** reorg Movement 1 Lane A moves
the Worker there. It is not strictly blocked: built before the move, it lands in
`bobi-deploy/event-server/` and rides along with everything else. What it must not do
is land in a *third* place.

Two couplings to name:

- **It strengthens the reorg's Q3** (how strong is the admin protocol's compatibility
  promise). MCP tool schemas are a second binding to that contract, and a published
  tool surface is harder to change than a published document. Q3 should be decided
  knowing this exists.
- **It corrected the reorg's non-goal.** That plan read "external consumers build
  their own (MCP client, dashboards)"; it now records that the MCP server is a route
  on the Worker it publishes. Edited in place, not amended — the reorg plan is still
  Draft with no build started (PR #870, 2026-07-30).

## Relevant files

### Existing (verified 2026-07-30)

**Worker (`bobi-deploy/event-server/`, moving public):** `src/index.ts:414-534` (the
`/fleet/*` route block — the structural model for `/mcp`); `src/fleet.ts:344-359`
(`ADMIN_COMMANDS`, `isAdminCommand`), `:381-419` (`buildCommandView`,
`buildInstanceDetail`), `:313-328` (`buildFleetStatus`), `:429-441`
(`requireOperator` — the scope refactor's target), `:92-208` (`FleetStorage` +
`createFleetKVStorage` — where the token records go), `:72-90` (`FleetCommandRecord` —
gains `actor`); `test/fleet.spec.ts`, `test/index.spec.ts` (the miniflare pool the MCP
suite joins); `package.json` (one runtime dependency today — see Q1);
`wrangler.jsonc`.

**Sidecar (`bobi-deploy/.../supervisor/`, moving to `bobi/supervisor/`):**
`admin.py:52-63` (the topic/vocabulary contract), `:175-222` (dispatch),
`:304-326` (the async `chat` path — why `bobi_send_message` cannot return a reply).

**Public `bobi-agent`:** `bobi/webapp/runtime.py:67` (`TeamRuntime` — the tool surface
this mirrors); `bobi/validate.py:483-524` and `bobi/mcp_handshake.py:89-95` (the
`type: http` + `headers` consumer path that needs no change); `docs/SECURITY.md:64`
(the prompt-injection section this extends).

**Private console (moving to `moda-agents`):**
`bobi_deploy/webapp/runtime.py:154-206` (`_issue_command`/`_await_command`/`_command`
— the reference implementation of the 202-then-poll contract, and the source of the
timeout constants the bounded wait should match).

### New

- `event-server/src/mcp.ts` — transport (JSON-RPC framing, `initialize`, `tools/list`,
  `tools/call`, error mapping) and the tool registry.
- `event-server/src/operator-auth.ts` — principal resolution and scope assertion,
  extracted from `requireOperator`.
- `event-server/test/mcp.spec.ts` — miniflare suite.
- `docs/SECURITY.md` § agentic control surface.
- MCP endpoint section in the admin protocol spec (reorg Lane C).

## Questionables

### Q1 — Hand-rolled JSON-RPC, the MCP SDK, or Cloudflare's `agents`?

**Recommendation: hand-rolled, stateless.** A tools-only stateless server is four
methods and a small error map. The Worker has exactly **one** runtime dependency today
(`@moda-labs/bobi-events-core`) and the reorg is in the business of deleting npm
bridges, not adding them. `@modelcontextprotocol/sdk`'s HTTP server transport is built
for Node's `http` req/res, not a Workers `fetch` handler; Cloudflare's `agents`
package (`McpAgent`) solves session state and SSE with a Durable Object, which a
stateless tools server does not need. Revisit if v2 wants progress notifications,
sampling, or resumable streams — those are the things the SDK earns its weight on.

**Risk to accept:** hand-rolled means we own spec conformance, including protocol
version negotiation in `initialize`. Mitigation: the miniflare suite asserts against
recorded real-client traffic (Phase 1 captures a Claude Code session), not against our
own reading of the spec.

### Q2 — Scopes with the endpoint, or single token plus a blocker?

**Recommendation: scopes land with the endpoint (Phase 2, before any write tool).**
The alternative — ship on the single token, file scoping as a blocker on agent-held
deployments — is tempting and gets a human-operated server sooner. It fails on the
first thing anyone will actually want to do with this, which is give a bobi ops team
read access to the fleet. Under one god-token there is no such thing as read access.

The cheap version is genuinely cheap: a KV token record, a principal return type, a
scope assert per route, an `actor` field. It is smaller than the MCP transport it
guards.

### Q3 — What gates `chat` and lifecycle beyond a tool description?

**Recommendation: scope separation (`fleet:read` / `fleet:write` / `fleet:chat`) is
the control; everything else is mitigation.** Open sub-question worth deciding before
Phase 3: does a lifecycle action from an agent-held principal require a human
approval, and if so through what? bobi has an `await`-gate primitive
(`docs/SECURITY.md:73`) but it lives in the workflow engine, on the *agent* side, not
in the Worker — so using it would mean the controlling agent gates itself, which an
injected agent will happily skip. A Worker-side approval queue is a real feature, not
a checkbox. **Proposal: v1 ships no approval gate and no agent holds `fleet:write`;
the human-operated principal does.** Autonomous lifecycle control gets its own plan.

### Q4 — How long is the bounded wait?

**Recommendation: ~5s, configurable via env, matching the console's fail-fast
constants** (`DEFAULT_FLEET_SPEND_COMMAND_TIMEOUT = 8.0`,
`DEFAULT_SESSION_LOG_COMMAND_TIMEOUT = 8.0` — both deliberately shorter than the 30s
default, so a wedged-but-addressable box drops fast). Too short and every read costs
two round trips; too long and one wedged instance holds an agent's tool call while its
client counts down. Verify against measured fold latency in Phase 3, not by taste.

### Q5 — Does `bobi_send_message` wait for the reply?

**Recommendation: no, and say so loudly in the description.** `chat` runs on a
detached thread in the sidecar and resolves its command as soon as `service.ask`
returns (`admin.py:304-326`); the *reply* lands in the transcript minutes later. Any
tool that appears to return a reply will be believed. The description states the
two-step contract explicitly. The alternative — a `bobi_send_message_and_wait` that
polls the transcript — is a v2 convenience that needs a reply-detection heuristic we
do not have.

### Q6 — Self-targeting: what happens when an agent restarts its own instance?

Unresolved. An ops team running *on* the fleet can restart the box it is running on:
the manager dies mid-turn, the tool call never returns, and the command result is
never read. With Q2's token records the Worker could know the caller's own instance
(the principal's label) and refuse or loudly flag a self-targeted lifecycle action.
**Proposal: Phase 2 records enough to detect it; Phase 4 decides refuse-vs-warn**,
informed by whether anyone actually wants self-restart as a feature (it is a
plausible self-heal primitive).

### Q7 — OAuth for claude.ai connectors?

**Recommendation: not in v1.** Claude Code accepts a remote HTTP MCP server with a
custom `Authorization` header, and bobi teams pass `headers` through `mcp_servers` —
that covers both intended consumers. claude.ai custom connectors expect OAuth 2.1 with
discovery metadata; Cloudflare publishes `workers-oauth-provider` for exactly this
shape. **Verify current client behavior before committing either way** — this is the
claim in the plan most likely to have drifted. If claude.ai access is wanted, it is a
self-contained later phase, not a redesign.

### Q8 — Does this plan's approval change the reorg? `[resolved 2026-07-30]`

Resolved by editing the reorg plan in place on its own open PR (#870), since it is
still Draft with no build started: the non-goal now says the MCP server is a route on
the Worker, and Q3 records that tool schemas bind the admin protocol harder than a
document does. Nothing here gates a reorg lane, and nothing there gates a phase below
— the two plans review independently.

## Phases

### Phase 1 — MCP transport + read-only tools (Lane A) `[ ]`

`src/mcp.ts`: stateless Streamable HTTP, `initialize` / `tools/list` / `tools/call` /
`ping`, JSON-RPC error mapping, `GET /mcp` → 405. Tools: `bobi_fleet_status`,
`bobi_instance_detail`, `bobi_command_result`. Bearer auth via the existing
`requireOperator`, unchanged. Miniflare suite in the existing vitest pool. Capture a
real Claude Code session's traffic as the conformance fixture.

### Phase 2 — Scoped operator credentials (Lane B) `[ ]`

Parallel with Phase 1; **required before Phase 3.** KV token records, `requireOperator`
returns a principal, per-route scope asserts, `actor` on `FleetCommandRecord`, the
env-secret compatibility path (existing token → all scopes). Record enough principal
identity to detect self-targeting (Q6). Console and existing callers unchanged, proven
by their suites.

### Phase 3 — Write tools (Lane C) `[ ]`

Depends on A + B. `bobi_read_transcript`, `bobi_send_message`, `bobi_lifecycle`
(action enum + required `reason`), each behind its scope. The bounded wait, with the
window set from measured fold latency (Q4). Untrusted-data framing on transcript
output.

### Phase 4 — The agentic consumer (Lane D) `[ ]`

Depends on C. An ops team's `agent.yaml` declares the endpoint with a read-scoped
token and `bobi validate` proves the handshake. `docs/SECURITY.md` § agentic control
surface. Self-targeting decision (Q6). The admin protocol spec gains its MCP section.

### Phase 5 — OAuth for claude.ai connectors (Lane E) `[ ]` — deferred

Only if wanted (Q7). `workers-oauth-provider`, discovery metadata, dynamic client
registration. Self-contained; no earlier phase changes.

## Proof of work

- **Protocol conformance:** a real Claude Code client connects to the deployed Worker,
  completes `initialize`, lists the tools, and calls each one. The miniflare suite
  replays that captured traffic, so conformance is asserted against a real client
  rather than our reading of the spec.
- **End to end against a live agent:** with a sidecar-supervised deployment running,
  an agent drives the full loop — read fleet status, read the manager's transcript,
  send a message, observe the reply appear on a follow-up transcript read, restart the
  instance, observe `restart_count` increment on the next heartbeat. This is the
  acceptance test.
- **Scopes are a boundary, not a guard:** a `fleet:read` token is rejected on
  `bobi_lifecycle` **at the Worker**, and the equivalent raw `POST .../commands` with
  the same token is rejected identically. Proven by a failing-first test on the
  Worker, not by the tool being absent from `tools/list`.
- **Attribution:** a restart issued through MCP appears in the KV command record with
  its principal label and its `reason`.
- **Bounded wait:** measured fold latency for `transcript`/`roster`/`spend` against a
  real instance justifies the chosen window; a deliberately wedged instance returns
  `pending` within it rather than hanging the tool call.
- **Bobi-team consumption:** a team declaring the endpoint in `mcp_servers` passes
  `bobi validate`'s existing `initialize` probe with no framework change.
- **No regression:** the console's suites and the existing `/fleet/*` route tests stay
  green through the `requireOperator` refactor.

## Lane map

| Lane | Scope | Depends on | Parallel with |
|---|---|---|---|
| A | MCP transport + read-only tools | — | B |
| B | Scoped credentials + actor attribution | — | A |
| C | Write tools + bounded wait | A, B | — |
| D | Agentic consumer + security doc + Q6 | C | — |
| E | OAuth (deferred, optional) | A | — |

## Amendments

_None yet._

## Notes

### Why the Worker and not a Python server

Three independent reasons converge. The Worker is the only component that sees the
whole fleet, so any client talks to it regardless — co-locating removes a hop and a
versioned client. The tool implementations are functions it already calls in-process.
And it sidesteps a repo problem the reorg is actively solving: a Python server would
need `EventBusRuntime` published into the wheel, exactly opposite the direction
Movement 3 is pushing it. The Python `TeamRuntime` ABC remains the *shape* worth
copying — it solved the sync/async split correctly — without being the implementation.

### Why the injection risk is different here

Everywhere else in bobi, a prompt-injected agent can do what its own scoped tokens
allow, against its own resources. An MCP client agent's tokens *are* the fleet, and
its reading material is the concentrated output of every untrusted channel every team
subscribes to. The plan's answer is not a better prompt: it is that the credential
holding the power and the credential reading the untrusted text should be able to
differ, which is what Q2 buys and why it is not deferrable.
