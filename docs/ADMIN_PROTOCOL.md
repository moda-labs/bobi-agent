# Admin protocol

The wire contract between a bobi deployment's **supervisor sidecar** and any
operator surface that monitors or controls it — moda's hosted console, the
Worker's MCP fleet-control route, or a dashboard you write yourself.

This document is the contract. The code on both sides implements it:
`bobi/supervisor/` (Python, the deployment half) and
`event-server/worker/src/fleet.ts` (TypeScript, the server-side read model and
query surface). Neither is private, because a contract with a secret half is
not a contract.

A working **client** of that surface ships here too: `EventBusRuntime`
(`bobi/webapp/event_bus.py`) issues commands and polls their results over the
operator-authed `/fleet` routes, and is what a hosted console runs. Read it as
the reference implementation if you are writing your own operator surface —
including the parts a first attempt gets wrong, like which poll failures are
transient and which are not.

- **Version:** `SUPERVISOR_VERSION = "0.2.0"` (`bobi/supervisor/snapshot.py`),
  reported on every heartbeat at `supervisor.version`.
- **Transport:** the bobi event bus. See `docs/EVENT_SERVER.md` for the server,
  `docs/SELF_HOSTED_EVENT_SERVER.md` for running your own.

## Compatibility promise

Pre-1.0, this protocol is **versioned and additive-only**:

- **Additive changes are free.** New fields may appear on any payload at any
  time. Consumers MUST ignore unknown fields rather than reject the message.
- **Breaking changes bump `SUPERVISOR_VERSION`** and ship with a migration note
  in this document. A breaking change is: removing or renaming a field,
  changing a field's type, changing a topic string, or changing what a command
  does.
- **No formal deprecation window** while direct consumers number two. That is
  an honest statement of maturity, not an invitation to churn — the supervisor
  is still actively developed.

One pressure worth naming: the MCP fleet-control route publishes **tool
schemas** derived from this contract, and a tool schema binds harder than a
document. A consumer's agent re-reads it at every `tools/list`, so a shape
change is felt immediately rather than at the next read of a doc. That is
precisely why breaking changes get the full version-bump-plus-migration-note
discipline instead of casual drift.

## Identity

Every payload carries the deployment's identity, resolved once at supervisor
start (`bobi/supervisor/identity.py`):

```json
{
  "fleet": "default",
  "instance": "eng-team",
  "platform": "fly" | "k8s" | "docker",
  "machine": "148ed...",
  "region": "iad",
  "node": "node-3"
}
```

`fleet` and `instance` are the addressing pair — together they name exactly one
deployment. `platform` is a best-effort label with per-platform enrichment
(Fly machine/region, the Kubernetes downward API's `POD_NAME`/`NODE_NAME`);
`machine`, `region`, and `node` may be `null` on an unknown runtime.

## Security model

Security of the write path rests on **bubble namespacing**, not on obscurity of
the topic strings — which are published here precisely because they are not a
secret.

The admin topic is non-global, so the bus keys it as
`<bubble>:fleet/admin/<fleet>/<instance>`. The server publishes a command INTO
the target deployment's bubble, and only a subscriber in that same bubble, on
that exact topic string, can receive it. A deployment cannot receive another
deployment's commands even by guessing the topic, because it cannot join the
other's bubble. This is fail-closed by construction and introduces no new
server-side trust primitive.

Two consequences worth stating plainly:

- **Bubble membership is the authorization boundary.** Anything that can
  publish into a deployment's bubble can restart it. Protect bubble credentials
  accordingly.
- **The read path is separately gated.** Fleet queries on the server side are
  operator-token-gated (`FLEET_OPERATOR_TOKEN`); heartbeats themselves are
  bubble-signed on publish.

## Topics

| Topic | Direction | Carries |
|---|---|---|
| `fleet/heartbeat` | supervisor → server | tier-1 snapshot, latest-value fold |
| `fleet/lifecycle` | supervisor → server | tier-2 lifecycle edges, bounded 48h trail |
| `fleet/admin/<fleet>/<instance>` | server → supervisor | one operator command |
| `fleet/command_result` | supervisor → server | one command's reply |

**All four are bubble-scoped.** None is a global topic: `isGlobalTopic`
(`event-server/core/src/core.ts`) treats only the inbound webhook prefixes
(`github:`, `linear:`, `slack:`, `whatsapp:`, `discord:`) as global, and
`fleet/*` matches none of them. So every topic here is keyed
`<bubble>:<topic>`, and a deployment's heartbeats, lifecycle edges, and command
replies are readable only within its own bubble — they do not fan out across
tenants.

The admin topic string is built identically on both sides — `adminTopic(fleet,
instance)` in `fleet.ts` and `admin_topic(fleet, instance)` in `admin.py`. They
must match byte-for-byte; the topic string *is* the producer/consumer contract.

## Commands

A command is published to the deployment's admin topic:

```json
{
  "command": "restart",
  "command_id": "01J8...",
  "args": {}
}
```

`command_id` is caller-supplied and opaque to the supervisor; it is echoed on
the result so a caller can correlate. A command whose `command` is not one of
the fifteen below, or which carries no `command_id`, is **dropped without a
reply** (the supervisor logs it locally). An unrecognized command has no
command to acknowledge, so a consumer must not wait on a result for one — it
never executes and never answers.

**That silence is what makes `supervisor.version` load-bearing.** A fleet is
not uniform mid-roll, and an instance running an older sidecar drops a newer
verb exactly as it drops a misspelled one — so a consumer's only symptom is a
timeout, indistinguishable at that moment from a wedged dispatch worker.
Before blaming the network, read `supervisor.version` off the heartbeat
(`GET /fleet/instances/:fleet/:instance`) and compare it against the version
that introduced the command. `EventBusRuntime._view_command` does exactly
this, and reports a too-old instance **by name** rather than as a timeout.
Bound your polls accordingly: a page polling a command an old box will never
answer should use a short timeout, not the default.

`args` is coerced to an object when absent or malformed, so a command sent with
`args` as a string, an array, or `null` still dispatches on its verb rather
than failing in a way that would leave it pending forever.

Every reply is published to `fleet/command_result`:

```json
{
  "deployment": { "fleet": "...", "instance": "...", "...": "..." },
  "command_id": "01J8...",
  "status": "done" | "error",
  "result": { },
  "error": "..."
}
```

`result` is omitted when there is none; `error` is omitted unless `status` is
`"error"`. The server folds the pending command record and its result into one
operator-facing view: `pending` until the supervisor replies, then
`done`/`error`.

**A refusal carries a machine-readable `code` on `result`, beside the prose
`error`.** When the supervisor rejects a command because of what the *caller*
asked for — a run id that no longer exists, a run that is no longer at a gate
— `error` stays human prose and `result` carries the reason as a code, so a
consumer never has to string-match a sentence to tell "gone" from "not right
now":

```json
{ "status": "error",
  "error": "run 01J8... is 'completed', not 'waiting'",
  "result": { "code": "not_waiting", "run_id": "01J8...", "status": "completed" } }
```

| `code` | Means |
|---|---|
| `unknown_run` | No run with that id under this deployment. Carries `run_id`. |
| `not_waiting` | The run exists but is not at a gate. Carries `run_id` and its actual `status`. |
| `action_failed` | The precondition held; the action itself could not complete (e.g. the workflow is no longer installed). Carries `run_id`. |
| `bad_request` | A required arg was missing or empty. |

An unexpected failure carries no `code` — absence means "this was not your
fault", which is itself the signal to retry or escalate rather than correct
the request.

### Lifecycle commands

`restart`, `stop`, and `start` are queued onto the supervisor's main loop —
process management stays single-threaded — and acknowledged **immediately**:

```json
{ "accepted": true, "action": "restart" }
```

The acknowledgement means *accepted*, not *complete*. The lifecycle **effect**
(restart count, manager status, stopped/started) surfaces on the tier-1
heartbeat, which is where an operator confirms it landed. A consumer that
treats `{"accepted": true}` as "the manager has restarted" will be wrong for as
long as the restart takes.

`stop` is an operator-intent stop: the manager is then reported as `stopped`
rather than as a probe failure, so an intentionally-down manager never reads as
an incident.

### Read commands

| Command | `args` | `result` |
|---|---|---|
| `status` | — | the latest heartbeat snapshot, or `{"status": "starting"}` before the first |
| `transcript` | `{"session": "<name>", "detail": bool}` (both optional) | `{"messages": [...]}`, plus `{"session", "entries": [...], "usage": {...}}` when `detail` is set |
| `roster` | — | `{"subagents": [...]}` |
| `spend` | — | `{"spend": {...}}` |
| `session_log` | — | `{"sessions": [...], "counts": {...}, "truncated": bool}` |
| `runs` | `{"status", "query", "offset", "limit"}` (all optional) | `{"runs": [...], "counts": {...}, "total", "offset", "limit", "query", "truncated"}` |
| `overview` | — | `{"overview": {...}}` |
| `run_details` | `{"run_id"}` **required** | `{"details": {...}}` |

These are answered inline from the deployment's local `$BOBI_HOME` through the
same public `bobi` surfaces the local web UI uses. The sidecar shares the
manager's host, state directory, and brain config, so they are plain filesystem
reads — they keep working when the manager is wedged, which is the point.

That sharing is not incidental, it is the design: `runs`, `overview` and
`run_details` delegate to `bobi.webapp.runs.build_runs`,
`bobi.webapp.overview.build_overview` and `bobi.webapp.details.build_details`
— the *same* functions `LocalRuntime` calls. Neither surface re-implements a
read, so the local page and the hosted page cannot drift apart; there is only
one implementation to drift from. `tests/test_hosted_single_agent_view.py`
asserts the payloads are identical for one root.

`session_log` caps its row list (newest first) so the reply fits one bus
message; `truncated` says whether the cap bit, and `counts` is computed over
the full set, not the capped rows.

`runs` coerces its pagination args the way the local runtime does: a
non-positive or unparseable `limit` falls back to the builder default rather
than failing the command.

**`transcript` and its `detail` arg.** Without `detail`, the reply is
unchanged: `messages` is the **chat** view — two roles, prose only, tool calls
discarded. With `detail: true` the reply gains the **debugging** view of the
same transcript: `entries` (timestamped lines *including* tool calls and their
results) and `usage` (the slab header's duration/tokens/cost). This is a
widening rather than a new command because it reads the same file for the same
session — but it has to be a widening rather than something a consumer derives,
because nothing downstream can recover a tool call from `messages`. A consumer
that receives no `entries` is talking to a supervisor older than `0.2.0` and
should say so, not render the chat view as a debugging transcript with every
tool call silently missing.

### Run commands

`resume_run`, `remind_run`, and `close_run` act on one **suspended workflow
run** — the protocol's first run-scoped writes. Each takes a required
`{"run_id"}`, and each is refused with a `code` (see above) when the run is
gone or no longer at a gate.

| Command | `result` |
|---|---|
| `resume_run` | `{"ok": true, "accepted": true, "run_id", "workflow", "await_event"}` |
| `remind_run` | `{"ok": true, "delivered": true, "run_id", "workflow", "await_event"}` |
| `close_run` | `{"ok": true, "closed": true, "run_id", "workflow"}` |

They delegate to `bobi.webapp.run_actions`, the same module `LocalRuntime`
delegates to, for the same anti-drift reason as the reads.

`accepted` is the honest word, and carries the same discipline `chat` does: a
workflow run takes as long as it takes, so `resume_run` returns once the resume
is **under way** and never holds a request open for the workflow. The operator
watches the `runs` table for the status to move. Note also what resume does
*not* do — it spawns the CLI resume as a separate process rather than running
it inline, because resuming re-stamps the run's session registry entry with the
running process's pid, and a supervisor that stamped its own would later be
signalled by a reconciler timeout meant for the run.

Two concurrent resumes are safe: neither this command nor the local runtime
holds the claim (a claim held by a caller that then fails to spawn would strand
the run), so both spawn and `WorkflowRun.claim` arbitrates — exactly one
proceeds.

`remind_run` re-sends the gate's notification and does **not** advance the
workflow. `close_run` abandons the run and marks its session cancelled.

### `chat`

`chat` is the only **asynchronous** command. A real turn can take minutes, and
the dispatch worker is single-threaded and strictly ordered — a `restart` must
stay responsive — so `chat` runs on a detached thread that owns its own single
`command_result` publish: `done` once the turn's reply lands in the transcript,
or `error`.

The reply itself does **not** come back on the command result. It reaches the
operator through a follow-up `transcript` read, mirroring the local UI's
submit-then-poll shape.

## Heartbeat

Published to `fleet/heartbeat` on a fixed interval and folded latest-value
(KV last-write-wins, no replay mirror). This is the read model a dashboard
renders.

```json
{
  "deployment":  { "fleet": "...", "instance": "...", "platform": "..." },
  "supervisor":  { "pid": 1, "uptime_s": 1043.2, "version": "0.1.0" },
  "manager": {
    "status": "running" | "idle" | "starting" | "wedged" | "down" | "stopped",
    "pid": 42,
    "healthy": true,
    "idle_seconds": 12,
    "restart_count": 0,
    "last_restart_reason": null,
    "last_restart_at": null,
    "expected_busy": null | {
      "active": true,
      "lease_count": 1,
      "expires_at": 1786500000.0
    }
  },
  "sessions":     [ { "name": "...", "role": "...", "status": "..." } ],
  "resources":    { "disk_free_mb": 8120, "mem_free_mb": 512, "mem_pct": 68.0 },
  "versions":     { "image": "...", "team_package": "...", "bobi": "0.50.0" },
  "expectations": { "subscriptions": [...], "monitors": [ { "name": "...", "schedule": "..." } ] },
  "event_client": null,
  "generated_at": "2026-07-30T17:04:11Z"
}
```

`manager.status` is a **derived** verdict, not a raw probe reading — it folds
the health file, process liveness, and status-file age
(`bobi/supervisor/probe.py::derive_manager_status`). Treat it as the single
authoritative answer to "is this deployment working"; the raw inputs beside it
are for diagnosis, not for re-deriving your own verdict.

| Value | Means |
|---|---|
| `running` | alive and working a turn |
| `idle` | alive, no work in flight |
| `starting` | booting, or never yet addressable — an unreachable probe here is not a wedge |
| `wedged` | a process is alive but not making progress (stalled turn, stale status file, or healthy-then-silent) |
| `down` | nothing alive: the supervised child exited AND the manager pid is gone |
| `stopped` | operator-stopped, so intentionally down — never a probe verdict, and it does not open a failure episode |

Note `status` is the verdict and `healthy` is a separate raw boolean; they are
not the same field. There is no `"healthy"` or `"dead"` status value — switch on
`running`/`idle` and on `down`.

`manager.expected_busy` is additive supervisor context for sanctioned heavy
commands. A worker obtains it with `bobi agent <name> busy -- <command>`; the
wrapper writes a crash-safe multi-holder lease, renews it while the command is
alive, clamps every renewal to 30-3600 seconds, and releases it on normal exit.
If the wrapper or machine dies, the TTL expires without cleanup. While at least
one lease is active, ambiguous liveness signals (probe misses, stale active-turn
progress, and `status=error` under host starvation) do not restart or charge the
manager budget and do not open a probe-failing episode. A real supervised-child
exit still follows the normal crash budget. `status` keeps its existing
vocabulary; consumers that want to explain why a long-running manager remains
healthy can inspect `expected_busy`.

`expectations` is what the deployment *should* be running (its configured
subscriptions and monitors), which is what makes a "configured but not running"
gap visible to a consumer that never reads the team's config.

## Lifecycle events

Published to `fleet/lifecycle` on edges only, and appended to a bounded 48-hour
trail. Each carries the deployment identity plus event-specific fields.

| Event | Fired when |
|---|---|
| `manager_started` | the manager was spawned (`reason: "operator"` when commanded) |
| `manager_stopped` | the manager exited or was stopped (`reason: "operator"` when commanded) |
| `manager_restarted` | the supervisor restarted a failing manager, with `reason` |
| `probe_failing` | the derived status crossed into a failing verdict |
| `probe_recovered` | it crossed back out |
| `budget_exhausted` | the restart budget ran out; the supervisor exits `70` |

`budget_exhausted` is terminal and deliberately loud: it means the supervisor
gave up restarting a manager that kept failing, and the orchestrator's own
restart policy (Fly machine restart, Kubernetes `CrashLoopBackOff`, `docker
--restart`) takes over from the non-zero exit.

## Failure behavior

The supervisor is **fail-open** on every admin path: a broker outage, a
malformed command, or a failed publish must never take the supervisor down.
Concretely — the admin listener runs on its own bus connection inside the same
bubble as the telemetry layer, so it keeps working when the manager is wedged;
if the listener cannot start at all, the supervisor still supervises.

For a consumer this means **absence of a reply is not a failure signal you can
act on**. A command whose result never arrives may have been delivered and
executed. Confirm lifecycle effects on the heartbeat, and make retries
idempotent — `restart` is safe to repeat, and a duplicate `command_id` simply
produces a second result record.

## MCP control surface

Everything above is the wire contract; this is the surface an **agent** binds
to instead of implementing it. The Worker serves the Model Context Protocol at
**`POST /mcp`** — the same fleet read model and the same command path, as
named tools with declared argument schemas.

It is a schema wrapper, not a second implementation: each tool calls in-process
the same builder the corresponding `/fleet/*` route calls. There is no second
place where the vocabulary lives, and nothing new to keep in sync.

**Authentication is the existing operator credential.** `/mcp` is gated by
`FLEET_OPERATOR_TOKEN` exactly as `/fleet/*` is, checked before any tool body
runs. Holding that token means full control of every instance in the fleet;
the MCP route widens the interface, not the authority. There are no scopes and
no per-principal attribution.

That is a deliberate call, and it rests on the holder being a **human-operated
interactive session** — an operator's Claude Code, or a bobi team someone is
talking to — where a person reads the tool calls as they happen. The moment an
*unattended* agent holds this credential (a monitor, a scheduled job, an
autonomous remediation loop), that reasoning no longer holds and scoped
credentials become a prerequisite. The full posture, including what it costs,
is in `plans/2026-07-30-mcp-fleet-control.md` § "The security posture", and
moves into `docs/SECURITY.md`.

**Transport is streamable HTTP with a bearer header.** No OAuth, no session
id: the server is stateless, one MCP server instance per request, with fleet
state read from KV. Browser clients are not supported and CORS is off.

### Tools

| Tool | Arguments | Returns |
|---|---|---|
| `bobi_fleet_status` | none | Every instance: reachability, manager state, sessions, versions |
| `bobi_instance_detail` | `fleet`, `instance` | One instance's full heartbeat plus its lifecycle trail |
| `bobi_command_result` | `fleet`, `instance`, `command_id` | One command's folded view (`pending` / `done` / `error`) |
| `bobi_read_transcript` | `fleet`, `instance`, `session?` | One session's recent messages, framed as untrusted content |
| `bobi_send_message` | `fleet`, `instance`, `message`, `session?` | A `command_id`. **Never the reply** |
| `bobi_lifecycle` | `fleet`, `instance`, `action`, `reason` | The `restart`/`stop`/`start` command's view |

Six tools over fifteen admin commands, because the vocabulary is deliberately
**not** one tool per command: `bobi_lifecycle` folds `restart`/`stop`/`start`
behind an `action` enum, and `status` is already covered by the heartbeat that
`bobi_instance_detail` returns.

Nine commands are **not** exposed as tools in v1 — `roster`, `spend`,
`session_log`, and the six that back the single-agent view (`runs`,
`overview`, `run_details`, `resume_run`, `remind_run`, `close_run`). The three
writes are the notable omission and a deliberate one: acting on a suspended
workflow run is an operator decision made while looking at the runs table, and
v1 of the MCP surface does not put that table in front of an agent. Whatever the heartbeat happens to carry (sessions, and spend
when the supervisor reports it) is readable through `bobi_instance_detail`;
the commands themselves are not callable from MCP. They remain available over
`POST /fleet/instances/:fleet/:instance/commands`, which the hosted console
uses. Exposing them is a sizing question — whether their responses belong
inline in `bobi_instance_detail` or as separate tools — and that needs
response sizes measured against a real fleet.

An unknown instance or command id comes back as a **tool error with a
recovery** ("check the names with `bobi_fleet_status`"), not a protocol error,
so an agent can correct itself and call again on the same connection.

### The bounded wait

A command is asynchronous — the Worker publishes it and the supervisor replies
later — but a tool call that always returned `pending` would cost three round
trips for an operation that usually completes in well under a second. So the
write tools **wait server-side for up to 5s** (`MCP_COMMAND_WAIT_MS`, in
milliseconds; `0` disables waiting) and return the resolved command view if the
reply lands. If it does not, they return `status: "pending"` with the
`command_id`, and `bobi_command_result` reads it back later. **`pending` means
not-yet-answered, never failed.**

The wait polls by command id, never a KV prefix listing: keyed reads are
strongly consistent and listings are not, so a listing-based wait would report
`pending` for a command that had already resolved.

`bobi_send_message` is the exception and **never waits**. The supervisor runs
`chat` on a detached thread that publishes the command result only once the
whole turn finishes — minutes — so any bounded wait would expire by
construction.

### What the write tools guarantee, and what they do not

- **`bobi_send_message` does not return the agent's reply**, and does not wait
  for one. The reply lands in the target's transcript; read it back with
  `bobi_read_transcript`. A tool that appeared to return a reply would be
  believed, so its description says this outright.
- **`bobi_read_transcript` output is untrusted third-party content.** It is a
  concentrated feed of the attacker-controllable Slack/GitHub/email text
  `docs/SECURITY.md` warns about, returned to a client that also holds
  lifecycle and messaging tools. It arrives as a **separate content block
  behind an explicit warning**, so injected text reads as quoted data rather
  than as instructions.
- **`bobi_lifecycle` requires a `reason`**, recorded on the command so the KV
  trail is readable after the fact. It is an **audit** control, not an
  authorization one: nothing checks or rejects the reason.
- **Self-targeting is allowed.** An agent may restart the instance it is
  running on — refusing would foreclose self-restart as a self-heal primitive,
  and without per-principal identity the Worker cannot reliably detect it
  anyway. A `restart`/`stop` that stays `pending` is expected rather than
  failed: the supervisor can go down with the process before replying. Confirm
  with `bobi_instance_detail`, where a restart shows an incremented
  `restart_count` on the next heartbeat.
- **A command to an instance whose supervisor holds no admin subscription is
  rejected and nothing is recorded.** There is no `command_id` to poll: a
  recorded-but-undelivered command is a pending row that can never resolve.

### Connecting

An operator's Claude Code:

```bash
claude mcp add --transport http bobi-fleet https://events.example.com/mcp \
  --header "Authorization: Bearer ${FLEET_OPERATOR_TOKEN}"
```

A bobi team, in its `agent.yaml` — this needs no framework change, and
`bobi agent <name> doctor` proves the connection with the existing `initialize`
preflight it already runs against declared MCP servers:

```yaml
mcp_servers:
  bobi-fleet:
    type: http
    url: https://events.example.com/mcp
    headers:
      Authorization: "Bearer ${FLEET_OPERATOR_TOKEN}"
```

### Deployment note

The route requires the **`nodejs_compat`** compatibility flag, already set in
`event-server/worker/wrangler.jsonc`. The MCP handler carries its per-request
context in an `AsyncLocalStorage`, so the bundle imports `node:async_hooks`.
Without the flag the whole Worker fails to boot, not just `/mcp`: workerd
refuses to start with `No such module "node:async_hooks"`, taking the bus down
with it. So if you maintain your own wrangler config, carry the flag across —
this is not a degraded-MCP failure, it is a dead Worker.
