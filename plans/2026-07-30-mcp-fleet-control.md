# Agentic fleet control: an MCP server on the event-server Worker

> **Status:** Building
> **Tracking issue:** none by decision (2026-07-30) · **Created:** 2026-07-30 · **Last amended:** 2026-08-03
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

**Scope of v1: full control, no new authorization.** The agent driving this is a
human-operated interactive session, and it gets every command the console gets. The
credential model stays exactly as it is (Q2, Q3, Q6). What that buys and what it costs
is stated plainly under "The security posture" below, because it is the one thing about
this plan a future reader must not have to reconstruct.

## Problem

**There is no agent-shaped surface on the control plane.** `GET /fleet/status`,
`POST /fleet/instances/:f/:i/commands`, and the poll-by-id route are a fine HTTP API
and a poor tool surface: an agent has to be told the URL shape, the 202-then-poll
contract, the per-command argument shapes (documented only in a comment at
`event-server/worker/src/fleet.ts`, above `ADMIN_COMMANDS`), and the fact that
`chat` never returns a reply.
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

## Solution

### The Worker serves MCP

Add a `/mcp` route to `event-server/worker/src/index.ts`, positioned like the
`/fleet/*` block beside it: bearer auth first, then dispatch. It is served by
**`createMcpHandler`** from `agents/mcp/server` (Q1) — a stateless handler that builds
one `McpServer` per request over a web-standards transport, with no Durable Object and
no session state. Cloudflare's own guidance for it is to keep application state in KV
rather than behind a session id, which is already how the fleet read model works.

The tools call the same functions the HTTP routes call — `buildFleetStatus`,
`buildInstanceDetail`, `buildCommandView`, and the publish-to-admin-topic path — as
in-process calls against `createFleetKVStorage(env.EVENTS)`. No second implementation,
no HTTP hop, one place where the command vocabulary lives.

This resolves the language/repo problem outright: the MCP server is TypeScript in
`event-server/`, which reorg Movement 1 publishes. Nothing about the reorg changes
except its non-goal line, corrected on that plan's own PR.

**Bobi teams can consume it with no framework change.** `mcp_servers` already accepts
`type: http` with `url` and `headers` (`bobi/validate.py:493`,
`bobi/mcp_handshake.py:94`), and the config preflight already runs an `initialize`
handshake against declared servers. An ops team declares the endpoint in `agent.yaml`
and `bobi agent <name> doctor` proves the connection.

### The tool surface is task-shaped, not a command dump

Nine near-identical tools is a worse surface than six well-named ones. The MCP layer
is where the vocabulary gets shaped for a reader who has never seen the fleet:

| Tool | Backed by | Notes |
|---|---|---|
| `bobi_fleet_status` | `buildFleetStatus` | The orienting read. Every instance, reachability, manager state, versions. One call, no arguments. |
| `bobi_instance_detail` | `buildInstanceDetail` | Full heartbeat + lifecycle trail for one instance. |
| `bobi_read_transcript` | `transcript` command | Output is third-party content, framed as data (see the security posture). Session defaults to the manager. |
| `bobi_send_message` | `chat` command | Async by nature; returns a `command_id`. Description states plainly that no reply is returned and the reply is read back via `bobi_read_transcript` (Q5). |
| `bobi_lifecycle` | `restart`/`stop`/`start` | One tool, an `action` enum, and a **required `reason`** recorded on the command. The reason is an audit control, not an authorization one. |
| `bobi_command_result` | `buildCommandView` | Poll a `command_id`. The escape hatch for anything the bounded wait didn't resolve. |

`roster`, `spend`, and `session_log` fold into `bobi_instance_detail` or stay separate
— a build-time call once the response sizes are measured against a real fleet.

**Bounded server-side wait.** `tools/call` for a command waits up to **5s** (env-tunable,
Q4) for the fold before returning, so the fast cases — `transcript`, `roster`, `spend`
all return sub-second in production — resolve in a single tool call instead of three.
Slow cases return `{status: "pending", command_id}` and the agent polls
`bobi_command_result`. The console already polls this exact read path at 0.5s intervals
(`webapp/runtime.py:COMMAND_POLL_INTERVAL`), so the KV read-after-write behavior is
proven in practice; the Worker-internal loop uses the same read path, and Phase 2's
proof verifies it rather than assuming it.

**Self-targeting is allowed** (Q6). An agent may restart the instance it is running on;
the tool response says plainly that the caller is killing itself and will not receive
a result. This keeps self-restart available as a self-heal primitive.

### The security posture

v1 authenticates with the existing `FLEET_OPERATOR_TOKEN` (`fleet.ts`'s
`requireOperator`) and adds no scopes, no approval gates, and no per-principal attribution (Q2, Q3). Holding the token
means full control of every instance in the fleet. This is a deliberate call, and the
reasoning it rests on should be checked whenever the deployment posture changes:

**What makes it acceptable now.** The intended holder is a human-operated interactive
session — an operator's Claude Code, or a bobi team a person is talking to. A human is
reading the tool calls as they happen, which is the same control that governs the
hosted console today. The token is not new: it already grants exactly this, and the
console already holds it. The MCP route widens the *interface*, not the authority.

**What it costs, stated honestly.** An MCP client agent reads transcripts —
`bobi_read_transcript` and `session_log` are a concentrated feed of the
attacker-controllable Slack/GitHub/email text that `docs/SECURITY.md:64` warns about —
while holding lifecycle and chat tools over other agents. `bobi_send_message` is the
sharp end: it injects operator-attributed text into another agent's turn, reaching that
agent's reasoning rather than just its process. Elsewhere in bobi a prompt-injected
agent can do what its own scoped tokens allow; here the token is the fleet. The
mitigations that remain are real but partial: transcript output is structurally framed
as untrusted third-party content rather than instructions, `reason` is required on
lifecycle so the KV command trail is readable after the fact, and a human is watching.

**The trigger for revisiting.** The moment an *unattended* agent holds this credential —
a monitor, a scheduled job, an autonomous remediation loop, anything where no human
reads the tool calls in real time — the posture above no longer holds, and scoped
credentials (Q2) become a prerequisite rather than a deferral. That is a specific,
checkable condition, not a vague someday.

### What this does not add to the admin vocabulary

The nine commands stay nine. Extending them (event injection, monitor list/trigger,
workflow status, config read, log tail) is a coordinated sidecar-and-Worker change
with a bad failure mode: a supervisor predating the command drops it with no reply, so
the caller burns the full timeout — the console already carries dedicated short
timeouts for exactly this skew (`DEFAULT_SESSION_LOG_COMMAND_TIMEOUT`, with the
mid-rollout comment). v1 proves the surface on the vocabulary that already ships
everywhere. New commands come after, each with a version gate.

## Non-goals

- **No scoped credentials, approval gates, or actor attribution** (Q2, Q3). Deferred
  with a named trigger, above — not forgotten.
- **No OAuth and no claude.ai connector support** (Q7). Bearer covers both intended
  consumers.
- **No local MCP server.** A single dev box running against a local event server gets
  nothing here. A Python stdio `bobi mcp` over `LocalRuntime` is a different product
  for a different user; if it's wanted, it's a separate plan.
- **No new admin commands**, per above.
- **No change to the bus, bubbles, or the sidecar.** The sidecar is untouched by this
  plan; every command already exists on it.
- **No public Python operator client.** The reason this design was chosen.

## Relationship to the repo reorg

This plan targets `bobi-agent/event-server/` **after** reorg Movement 1 Lane A moves
the Worker there. It is not strictly blocked: built before the move, it lands in
`bobi-deploy/event-server/` and rides along with everything else. What it must not do
is land in a *third* place.

Two couplings, both already recorded on that plan's PR (#870):

- **Its non-goal was corrected in place** — it read "external consumers build their own
  (MCP client, dashboards)" and now records that the MCP server is a route on the
  Worker it publishes. Edited, not amended: that plan is still Draft with no build
  started.
- **Its Q3** (how strong is the admin protocol's compatibility promise) now notes that
  published tool schemas bind harder than a document, since a consumer's agent re-reads
  them at every `tools/list`.

## Relevant files

### Existing (verified 2026-07-30)

Paths below were re-verified against the post-reorg layout on 2026-08-03 (see
Amendments). The Worker now lives at `event-server/worker/` in this repo.

**Worker (`bobi-agent/event-server/worker/`):** `src/index.ts` (the `/fleet/*`
route block — the positional model for `/mcp`); `src/fleet.ts` (`ADMIN_COMMANDS`,
`isAdminCommand`, `buildCommandView`, `buildInstanceDetail`, `buildFleetStatus`,
`requireOperator` — reused unchanged, `FleetStorage` + `createFleetKVStorage`,
`FleetCommandRecord`); `test/fleet.spec.ts`, `test/index.spec.ts` (the miniflare
pool the MCP suite joins); `package.json` (gains `agents`,
`@modelcontextprotocol/server`, `zod`); `wrangler.jsonc`.

**Sidecar (`bobi/supervisor/`):** `admin.py` — the topic/vocabulary contract,
dispatch, and the async `chat` path (why `bobi_send_message` cannot return a
reply). Untouched by this plan.

**Public `bobi-agent`:** `bobi/webapp/runtime.py:67` (`TeamRuntime` — the tool surface
this mirrors); `bobi/validate.py:483-524` and `bobi/mcp_handshake.py:89-95` (the
`type: http` + `headers` consumer path that needs no change); `docs/SECURITY.md:64`
(the prompt-injection section this extends); `docs/ADMIN_PROTOCOL.md` § Commands
(the published admin-protocol contract).

**Private console:** `moda-labs/moda-agents`, under `bobi-deploy/` —
`_issue_command`/`_await_command`/`_command`, the reference implementation of the
202-then-poll contract and the source of the timeout constants the bounded wait
should match.

### New

- `event-server/worker/src/mcp.ts` — `createMcpHandler` wiring and the tool registry.
- `event-server/worker/test/mcp.spec.ts` — miniflare suite.
- `event-server/worker/test/fixtures-claude-code-mcp.json` — captured real-client traffic.
- `docs/SECURITY.md` § agentic control surface.
- `docs/ADMIN_PROTOCOL.md` § MCP control surface.

## Questionables

All resolved 2026-07-30. Kept with their reasoning: the deferrals below are the ones a
later reader will most want the "why" for.

### Q1 — Hand-rolled JSON-RPC, the MCP SDK, or Cloudflare's `agents`? `[resolved]`

**`createMcpHandler` from `agents/mcp/server`.** The original recommendation here was
hand-rolled, on the premise that `agents` means DO-backed sessions and SSE we do not
need. That premise was wrong: `createMcpHandler` is explicitly stateless — one
`McpServer` per request, no Durable Object, web-standards transport — and its
`authContext` parameter is a ready-made seam for the bearer check. Spec conformance
(protocol version negotiation, error mapping, future revisions) is maintained upstream
instead of by us, which is the failure mode hand-rolling was worst at: a client that
mostly works.

Accepted cost: three npm dependencies (`agents`, `@modelcontextprotocol/server@2.0.0`,
`zod`) in a Worker that has one today, and a release cadence coupled to theirs. Worth
it against owning conformance forever.

### Q2 — Scoped operator credentials? `[resolved: deferred]`

**Not in v1.** The single `FLEET_OPERATOR_TOKEN` stays and `requireOperator` is reused
unchanged. The argument for scoping — that "read-only fleet access" cannot exist under
one god-token — is real and unrefuted; it is outweighed for now by the fact that the
intended holder is a human-operated interactive session with the same authority the
console already has.

The trigger that flips this: an unattended agent holding the credential. See "The
security posture". At that point the work is a KV token record, a principal return
type from `requireOperator`, a scope assert per route, and an `actor` field on
`FleetCommandRecord`.

### Q3 — What gates `chat` and lifecycle? `[resolved]`

**Nothing.** An agent gets full control, because the agents in scope are interactive
and human-driven. The alternatives considered and rejected: a Worker-side approval
queue (parked commands, out-of-band approve, expiry) is the only gate that actually
binds — the existing `await` gate lives agent-side in the workflow engine, so an
injected agent simply skips it — but it is a feature in its own right and it buys
nothing while a human is reading every tool call. Gating lifecycle but not `chat` was
also rejected: `chat` is arguably the more dangerous of the two, since it reaches
another agent's reasoning rather than just its process, so a split that protects
processes and not reasoning protects the wrong thing.

### Q4 — How long is the bounded wait? `[resolved]`

**5s, env-tunable, validated by measurement in Phase 2.** Chosen to sit under the
console's deliberate fail-fast constants (`DEFAULT_FLEET_SPEND_COMMAND_TIMEOUT` and
`DEFAULT_SESSION_LOG_COMMAND_TIMEOUT`, both 8.0 against a 30s default, so a
wedged-but-addressable box drops fast rather than holding a worker). Too short and
every read costs two round trips; too long and one wedged instance holds an agent's
tool call while its client counts down. Phase 2 measures real fold latency and moves
the default if the data disagrees.

### Q5 — Does `bobi_send_message` wait for the reply? `[resolved]`

**No, and the tool description says so loudly.** `chat` runs on a detached thread in
the sidecar and resolves its command as soon as `service.ask` returns
(`admin.py:304-326`); the *reply* lands in the transcript minutes later. Any tool that
appears to return a reply will be believed. A `bobi_send_message_and_wait` that polls
the transcript is a v2 convenience needing a reply-detection heuristic we do not have.

### Q6 — Self-targeting: what happens when an agent restarts its own instance? `[resolved]`

**Allowed, with the response saying plainly what is about to happen.** Refusing it
would foreclose self-restart as a self-heal primitive — directly relevant to the
dead-transport work, where a wedged box cannot fix itself from inside. Without
per-principal identity (Q2) the Worker cannot reliably detect self-targeting anyway,
so "allow and annotate" is also the only option that is honest about what v1 knows.
The command trail is the evidence it happened.

### Q7 — OAuth for claude.ai connectors? `[resolved: dropped]`

**Bearer only; no claude.ai support, and the OAuth phase is deleted.** Verified rather
than assumed: Claude Code accepts `--header "Authorization: Bearer ${VAR}"` with env
interpolation, and bobi teams pass `headers` through `mcp_servers` — both intended
consumers work today. claude.ai custom connectors are OAuth-only in the standard UI;
a `static_headers` beta exists but shares one org-wide credential, which would collapse
every browser user into a single principal. Controlling the fleet from a browser is not
a goal, so neither path is worth carrying.

*Known client risk:* two open Claude Code issues report configured headers not being
attached in some versions/platforms (#50464, #29562). Phase 1's conformance capture
against a real client is where that surfaces if it affects us.

### Q8 — Does this plan's approval change the reorg? `[resolved]`

Resolved by editing the reorg plan in place on its own open PR (#870), since it is
still Draft with no build started. Nothing here gates a reorg lane, and nothing there
gates a phase below — the two plans review independently.

## Phases

### Phase 1 — MCP handler + read tools (Lane A) `[x]`

`src/mcp.ts`: `createMcpHandler` wired into the Worker's fetch dispatch behind
`requireOperator`; tools `bobi_fleet_status`, `bobi_instance_detail`,
`bobi_command_result` registered with zod schemas. `package.json` gains the three
dependencies; bundle size checked against the Workers limit. Miniflare suite in the
existing vitest pool. Capture a real Claude Code session's traffic as the conformance
fixture — including whether the configured bearer header actually arrives (Q7).

Delivered 2026-08-03. Bundle 34 KiB → 209 KiB gzipped, ~7% of the 3 MiB
compressed ceiling. Q7's client risk did not materialize (see Amendments);
`nodejs_compat` did (see Amendments).

### Phase 2 — Write tools + bounded wait (Lane B) `[x]`

Depends on A. `bobi_read_transcript`, `bobi_send_message`, `bobi_lifecycle` (action
enum + required `reason`). The bounded wait, with the 5s default validated against
measured fold latency and the KV read-after-write behavior verified inside a single
Worker invocation. Self-target annotation on lifecycle responses. Untrusted-data
framing on transcript output.

### Phase 3 — The agentic consumer + security stance (Lane C) `[ ]`

Depends on B. An ops team's `agent.yaml` declares the endpoint and `bobi agent <name> doctor`
proves the handshake. `docs/SECURITY.md` gains the agentic control surface section,
carrying the posture and its trigger condition verbatim — this is the artifact that
has to survive after the plan is archived. The admin protocol spec gains its MCP
section.

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
- **Bounded wait:** measured fold latency for `transcript`/`roster`/`spend` against a
  real instance justifies the 5s default; a deliberately wedged instance returns
  `pending` within the window rather than hanging the tool call.
- **Self-restart:** an agent restarts its own instance, the tool call does not return,
  and the command trail plus the next heartbeat show what happened.
- **Auth is closed:** an unauthenticated and a wrong-token request to `/mcp` are both
  rejected before any tool runs — proven by a failing-first test, since `/mcp` is a new
  public route on a Worker that also serves the bus.
- **Bobi-team consumption:** a team declaring the endpoint in `mcp_servers` passes
  the existing `initialize` preflight (`bobi agent <name> doctor`) with no
  framework change.
- **No regression:** the console's suites and the existing `/fleet/*` route tests stay
  green.

## Lane map

| Lane | Scope | Depends on | Parallel with | Status |
|---|---|---|---|---|
| A | MCP handler + read-only tools | — | — | `[x]` |
| B | Write tools + bounded wait | A | — | `[ ]` |
| C | Agentic consumer + security doc | B | — | `[ ]` |

Marker mode: `solo` — same repo, one lane at a time, markers flip in the code PR.

Sequential by construction: the tool registry built in A is what B extends, and C
documents what B establishes. Parallelism would cost more in conflicts than it saves.

## Amendments

### 2026-08-03 — Lane B built; the bounded wait does not apply to `chat`

Recorded on Lane B's PR.

**Bookkeeping note: the Lane-map row for B still reads `[ ]`, and that is a
tooling limitation rather than a status.** `check-plan-artifact.sh`
normalizes markers in list items and in phase headings, not in table cells,
so once a plan leaves Draft its Lane-map markers are frozen as review-surface
prose and flipping one fails the check. Lane A's row was flipped while the
plan was still Draft, when the freeze did not apply. **Phase 2's heading
marker and this amendment are authoritative for Lane B's state.**

**`bobi_send_message` does not get a bounded wait, and Q4's reasoning does
not reach it.** Q5 established that `chat` cannot return a reply, citing
that it "resolves its command as soon as `service.ask` returns". That is
literally true and reads as *fast*, which is the wrong inference:
`service.ask` runs the entire turn, and `admin.py`'s own module docstring
says the detached thread publishes `done` only "once the turn's reply lands
in the transcript" — minutes. A 5s wait on `chat` would therefore expire by
construction, spending the budget to learn nothing. `bobi_send_message`
returns its `command_id` immediately; the other write tools wait. Q5's
conclusion is unchanged, only the reason it matters.

**The command-issue path had to be extracted before it could be shared.**
The plan's principle — "a schema wrapper, not a second implementation" —
held for Lane A because the read builders were already functions in
`fleet.ts`. The issue path was not: it lived inline in `index.ts`'s POST
route. It is now `issueAdminCommand` in `fleet.ts`, taking an injected
publisher so the fleet module stays independent of the bus storage adapter,
and both the REST route and the MCP tools call it. Without this the two
surfaces would have had separate copies of the address-check → publish →
record sequence, including its deliver-before-record ordering.

**KV listings are eventually consistent; keyed reads are not.** The bounded
wait polls `buildCommandView` (two exact-key `get`s) and never a prefix
listing. This was found by test rather than reasoned: a helper that asserted
on `list()` raced the Worker's own write and reported a command that
demonstrably existed as absent. A wait built on a listing would report
`pending` for a command that had already resolved. The plan asked for
read-after-write to be "verified rather than assumed" — it now is, by
folding a supervisor result from a separate request while a tool call is in
flight.

**`MCP_SERVER_VERSION` moves to `1.1.0`** (the plan left this open). It
versions the tool surface, not the Worker release; Lane B adds three tools
and changes none of Lane A's three, so a client holding cached `1.0.0`
schemas stays correct.

**Also carried forward: `roster`, `spend`, and `session_log` are still not
exposed as tools.** The Solution section left this as "a build-time call once
the response sizes are measured against a real fleet" — and the measurement
that call depends on is the same one Q4 needs, so the call was not made rather
than made blind. `bobi_instance_detail` returns whatever the heartbeat carries,
and all three commands stay reachable over the REST route the console uses. The
tool surface is six, not nine, deliberately.

**Carried forward, NOT met: Q4's 5s default is still unvalidated against
measured fold latency.** The plan requires Phase 2 to measure real folds and
move the default if the data disagrees. Lane A shipped in `v0.53.0` and
`/mcp` is live, so the measurement is now possible — but the operator
credential was not reachable from the session that built this, so it was not
taken. The default ships at 5s, env-tunable via `MCP_COMMAND_WAIT_MS`, and
this proof-of-work item remains open for Lane C rather than being counted as
done.


### 2026-08-03 — Lane A built; three things the plan had wrong

Recorded on Lane A's PR.

**Relevant-files paths were pre-reorg.** The plan was written against
`bobi-deploy/event-server/`; Movement 1 landed and the Worker is now
`bobi-agent/event-server/worker/`. The code did not change, only its address.
The section above is corrected, and the "MCP endpoint section in the admin
protocol spec (reorg Lane C)" line now names the file that actually exists,
`docs/ADMIN_PROTOCOL.md`.

**`nodejs_compat` IS required — the plan assumed it was not.** The reasoning
was that `createMcpHandler` is web-standards, so no Node compatibility should
be needed. It is: the handler carries its per-request server context in an
`AsyncLocalStorage`, so the bundle imports `node:async_hooks`, and the
`wrangler deploy --dry-run` bundle warns accordingly. The flag is now set in
`wrangler.jsonc`. This matters beyond us — it is Worker-wide, and the failure
is total rather than confined to `/mcp`: workerd refuses the script outright
with `No such module "node:async_hooks"`, so the bus goes down with it.

That last sentence is a correction. This amendment first recorded the failure
as quiet — "every other route serves normally and only MCP tool calls throw" —
and that was wrong, propagated into both docs and several test comments before
anyone checked it. What established the truth was the CI coverage added at the
`wrangler dev` rung: removing the flag does not fail a tool call, it fails
Worker startup. Corrected everywhere on the same PR.

**Q7's known client risk did not materialize.** The two open Claude Code issues
about configured headers not being attached (#50464, #29562) do not affect
`claude-code/2.1.220`: a real session was driven through a logging proxy at a
live `wrangler dev`, and `Authorization: Bearer …` was present on every
request, including the SSE `GET`. That capture is now
`event-server/worker/test/fixtures-claude-code-mcp.json`, and the miniflare
suite replays it, so the transport choice rests on observed client behavior
rather than on the spec. Q7 stays resolved-as-dropped; no reopen.

Two smaller build-time calls, recorded so Lane B does not re-litigate them:

- **Tool results are JSON text, not `structuredContent`.** The spec pairs
  structured output with a declared `outputSchema`, and pinning one here would
  freeze the read model's shape into the tool contract. The read model is owned
  by `fleet.ts` and the supervisor's heartbeat and grows additively; text keeps
  the tool contract to the argument shape.
- **CORS is off on `/mcp`.** Browser-based fleet control is already a non-goal
  (Q7), so the route advertises no cross-origin access at all rather than
  carrying the handler's permissive default.

And one stale claim corrected: the plan said `bobi validate` runs the
`initialize` preflight. There is no `bobi validate` command — `validate_config`
is a module reached through `bobi agent doctor` (`bobi/doctor.py`). Caught by
`tests/test_tool_guides.py`, which checks documented `bobi` invocations against
the real CLI. Lane C's acceptance depends on this, so it is corrected above
rather than left to be discovered there.

## Notes

### Why the Worker and not a Python server

Three independent reasons converge. The Worker is the only component that sees the
whole fleet, so any client talks to it regardless — co-locating removes a hop and a
versioned client. The tool implementations are functions it already calls in-process.
And it sidesteps a repo problem the reorg is actively solving: a Python server would
need `EventBusRuntime` published into the wheel, exactly opposite the direction
Movement 3 is pushing it. The Python `TeamRuntime` ABC remains the *shape* worth
copying — it solved the sync/async split correctly — without being the implementation.

### What the descope actually removed

An earlier draft carried five phases, the extra two being scoped operator credentials
and an OAuth server. Both were cut deliberately (Q2, Q7), not for schedule. The scoping
work was cut because its beneficiary — an unattended agent — does not exist yet, and
building an authorization model for a principal that has not been designed is how
authorization models go wrong. The OAuth work was cut because browser-based fleet
control is not a goal. Neither cut is load-bearing on anything below: v1's three phases
are the same three phases they would have been.
