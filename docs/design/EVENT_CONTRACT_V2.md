# Event Contract v2 — Design Handoff

Status: **design reviewed and tickets filed (2026-06-10).** Envelope shape
approved; migration revised to hard cutover at the user's direction. The
§7 tickets are filed as #177 (A), #178 (B), #179 (C), #180 (D), #181 (E);
#164/#165 are closed as superseded. The brief sections below are preserved
as-is; the design follows under "Draft design."
Owner: design pass for GitHub issues **#164** (service genericization) and
**#165** (engineer hardcoding), to be done together as one envelope revision.

This document is a context handoff so the design work can resume in a fresh
session. It captures the problem, the deferred findings, the open decisions,
and the constraints — not the solution.

---

## Why this is one design, not two issues

#164 and #165 look independent but both change **what an event envelope
carries and how it routes**, and both are **breaking for installed teams'
subscriptions and workflow YAML**. If they ship separately, every agent team
migrates its config twice. Treat them as a single "event contract v2"
revision: design once, migrate once.

Two items were also folded in from #167 (see the #164 comment thread) because
they're really envelope-shape decisions:
- the `events/drain.py` Slack special-case
- the `format_event_for_manager` field allowlist

So the real scope of this design is **four findings unified under one
envelope/routing contract.**

---

## The core problem

The framework's stated principle (`feedback_no_framework_opinions` /
CLAUDE.md): *"Domain behavior comes from agent teams — the framework has no
topology opinions."* The event layer violates this in four places by
hardcoding the three launch services (github/slack/linear) and one agent team's
role vocabulary (engineer) into framework code.

### Finding 1 — Service config is hardcoded (#164)
- `modastack/config.py`: `Config` has first-class fields `slack_bot_token`,
  `linear_api_key`, `venn_api_key`; `native_services` is the literal list
  `["github", "slack", "linear"]`.
- `modastack/validate.py`: per-service if/elif credential checks.
- `modastack/events/subscriptions.py:_resolve_source`: if/elif over the same
  three names for auto-detection.
- `event-server/src/core.ts`: `NormalizedEvent` carries one optional field per
  service (`repo`, `team_key`, `workspace`, `channel`, `installation_id`);
  `subscriptionKeysForEvent` builds routing keys from those fields with a
  per-service if-chain.

**Cost:** adding a 4th event source (Discord, email, custom) requires
coordinated edits to the Config dataclass, its parser, `native_services`,
validate.py, subscriptions.py, the TS envelope, and the key builder. Agent
teams cannot declare a new service's credentials — the framework owns the list.

### Finding 2 — The "engineer" role is hardcoded in the executor (#165)
- `subagent.py`: session names prefixed `eng-`; lifecycle events emitted on
  `engineer/session.started|completed|failed` **regardless of the running
  role**; the orchestrator emits `engineer/workflow.*` / `engineer/step.*`
  the same way.
- `monitors/scheduler.py:_default_spawn_check`: spawns checks with hardcoded
  `--role engineer`.
- Run identity is **regex-scraped from freeform task text**
  (`subagent._parse_issue_number` matching "issue #5" / bare "#42") and
  threaded into session names, dedup keys, and workflow-resume matching
  (`WorkflowRun.find_waiting(event_type, issue_id)`). `issue_id` — a
  tracker-domain concept — is cemented into `AgentResult`, `SessionEntry`,
  and workflow state.

**Cost:** a non-engineering team (e.g. the dogfood content-review team) gets
events on `engineer/*` topics that subscriptions can't distinguish by actual
role, and any task mentioning two issue numbers — or a Slack message quoting
"#42" — produces wrong/colliding session names and resume matching.

### Finding 3 — Drain loop special-cases Slack (from #167)
- `events/drain.py`: the only delivery loop partitions every batch into Slack
  vs non-Slack via a hardcoded `source == "slack"` check and delivers Slack
  last. Whatever ordering property this buys is unavailable to any other chat
  source, and the intent isn't recorded in the code.

### Finding 4 — Manager event formatting is an allowlist (from #167)
- `events/client.py:format_event_for_manager`: renders only a hardcoded set of
  github/linear/slack/lifecycle field names. Events from any other source
  render as a bare `Event: source/type` line — **the payload is silently
  dropped before the agent sees it.**

**This one has live operational consequences.** It is the likely root cause of
the director missing `issues.assigned` triggers twice during this session: the
assignee field isn't in the allowlist, so the director literally couldn't see
who an issue was assigned to without shelling out to `gh`. We patched it with a
prompt nudge; the durable fix is here.

---

## The directional fix (from the issues — NOT yet designed)

These are the directions recorded in #164/#165, to be pressure-tested, not
accepted as-is:

- **Service descriptors in agent.yaml:**
  `services: [{name, events, credentials: {KEY: ${VAR}}}]` with a
  `cfg.credential(service, key)` accessor. validate.py iterates declared
  credentials generically. "Native" stops being a hardcoded list and becomes
  "a registered ingestion adapter exists."
- **Normalizer-computed routing:** each normalizer (the code that knows a
  payload's shape) computes `topics: string[]` on the envelope.
  `subscriptionKeysForEvent` collapses to `event.topics ?? [event.type]`. On
  the Python side, `_resolve_source` becomes a detector registry keyed by
  service name so teams ship detectors with their tool guides.
- **Role-parameterized lifecycle events** (see OPEN DECISION below).
- **Explicit run identity:** a `--id` / `correlation_id` on `agents launch`,
  populated from the trigger event in workflows (`${{input.issue_id}}`-style),
  falling back to a generated id. Rename the tracker-specific `issue_id` to a
  domain-neutral `run_key`; teams map their tracker IDs onto it via config.
- **Delivery class on the envelope:** replace drain.py's `source == "slack"`
  with a `delivery: chat | bulk` (or similar) field set at normalization time.
  Policy becomes data.
- **Generic manager formatting:** normalizers set a `summary` / `text` field on
  the envelope at ingestion, OR the formatter renders all top-level scalar
  payload fields with a size cap. Service-specific pretty-printing moves to
  agent-team tool guides.

---

## DECIDED (2026-06-10, with the user): role in payload (Option B)

**Role-parameterized event types: one stable topic set
(`agent/session.*`, `agent/workflow.*`, `agent/step.*`) with `role` as a
payload field.** Consumers correlate on `run_key`, not role.

Why B won (and why the option-A note above about "simple prefix matches"
was wrong):

- Subscription matching is an **exact-key Map lookup** in both runtimes
  (`event-server/src/local.ts` `subscriptionIndex`) — there is no prefix or
  wildcard matching. Topic-per-role therefore means enumerating every
  role × event-type pair, and an "all sessions" subscriber (dashboard,
  director, monitor) cannot exist without a registry of every role name
  every team invents. A would have forced wildcard matching into both
  runtimes just to be livable.
- Role names in topics become routing infrastructure: renaming a role
  breaks subscriptions *silently* (exact-key pub/sub fails by delivering
  nothing). Same failure class as the #163/#166 drift bugs.
- No consumer filters by role today. The orchestrator correlates by
  `issue_id` (→ `run_key`); the director correlates by "the session I
  spawned." Role-as-routing-key solved a need that doesn't exist.
- B's known cost — no server-side role filtering, so lifecycle events fan
  out to all subscribers and consumers filter in payload — is cheap at
  current fleet sizes, and the `topics[]` mechanism leaves an additive
  escape hatch: the emitter can later publish a role-qualified topic
  alongside `agent/session.*` without breaking anyone.

Design consequence: the v2 contract must pair this with `run_key`
correlation (already in scope) so await steps and spawners filter on a
reliable exact ID rather than asking LLM consumers to filter on role.

Everything else in the directional fix the assistant was comfortable drafting
on its own judgment with rationale for veto.

---

## Hard constraints the design must respect

1. **No framework opinions.** The whole point. If the design names github/
   slack/linear/engineer anywhere in framework code, it has failed. Service and
   role specifics live in agent-team config, prompts, tool guides, and
   normalizers/detectors — never in `modastack/` or the event-server routing.
2. **Two runtimes stay in sync.** The event server is now unified behind
   `core.ts` handlers over a `StorageAdapter` (shipped in #169). The envelope
   and routing-key logic live in `core.ts`; the Python client mirrors envelope
   reading. Any contract change touches `core.ts` (`NormalizedEvent`,
   normalizers, `subscriptionKeysForEvent`) AND the Python side
   (`events/client.py`, `events/subscriptions.py`). Don't let them drift — that
   was the #163/#166 lesson.
3. **Migration is breaking — plan it explicitly.** Installed teams have
   `.modastack/agent.yaml` (services, credentials, subscribe lists) and
   workflow YAML that reference the current contract. The reference team
   (`agents/eng-team` or `software_team`) and the dogfood team
   (`content-review`) must both migrate as part of the change, and the dogfood
   battery must pass after. Decide: versioned envelope? back-compat shim for a
   release? hard cutover with a migration note?
4. **Verification bar.** 620 unit + 86 integration + 37 event-server tests must
   stay green, and a full dogfood run (manager lifecycle, ask round-trip,
   webhook→manager pipeline) must pass. The webhook→manager pipeline is the
   integration that exercises the whole contract end to end.

---

## What's already shipped that this builds on

Landed this session (all on `main`, **not yet deployed** — pending a
`WEBHOOK_SECRET` ops step + a v0.13.1 tag):
- **#168** — worker GitHub signature verification + local Slack self-loop
  filtering (closed the security drift).
- **#169** — unified the worker and local event servers behind `core.ts`
  handlers over a `StorageAdapter`. **This is the foundation for v2:** the
  contract now has one home. Read `event-server/src/core.ts` first.
- **#170** — replaced the inbox 0.5s state-poll with an `asyncio.Event`
  (`session.py`); added the slack-send 502 catch in `core.ts`.

Also note v0.13.0 (the full-repo simplify) already removed a lot of the dead
weight around this code and extracted shared helpers, so the surface is
cleaner than the issues were written against.

---

## Files to read first (in order)

1. `event-server/src/core.ts` — `NormalizedEvent`, the normalizers,
   `subscriptionKeysForEvent`, and the unified handlers. The envelope contract
   lives here.
2. `modastack/events/subscriptions.py` — `discover_subscriptions` +
   `_resolve_source`; the Python mirror of routing-key construction.
3. `modastack/events/client.py` — `format_event_for_manager` (Finding 4) and
   envelope reading.
4. `modastack/config.py` — `Config`, `native_services`, service parsing
   (Finding 1).
5. `modastack/subagent.py` — `_session_name`, `_parse_issue_number`,
   `_emit_session_*`, `PHASE_TIMEOUT`/role strings (Finding 2).
6. `modastack/events/drain.py` — the Slack special case (Finding 3).
7. A current installed `agent.yaml` (e.g. dogfood `content-review`) — to see
   what teams actually declare today, which constrains the migration.

Related memory: `project_simplify_deferred` indexes #163-#167; `agent.yaml`,
`no-framework-opinions`, `separate-concerns` are relevant prior decisions.

---

## Suggested first moves for the design session

1. ~~Settle the OPEN DECISION~~ — done, see DECIDED above (role in payload).
2. Draft the v2 `NormalizedEvent` shape: what's generic (`topics[]`,
   `delivery`, `summary`/`text`, `run_key`, `role`) vs what's payload.
3. Draft the agent.yaml `services:` descriptor schema + `cfg.credential()`.
4. Define the normalizer/detector registry contract (TS normalizers compute
   `topics`; Python detectors auto-discover sources).
5. Write the migration plan for eng-team + dogfood, and decide back-compat.
6. Only then: break into dispatchable implementation tickets (the impl may be
   agent-dispatchable in pieces once the contract is written and reviewed).

Do NOT hand this to an agent as a single dispatch — it's a design-first task.
The pattern this session: human-reviewed design doc → small implementation
tickets, not a 1,500-line unreviewed diff.

---
---

# Draft design (2026-06-10)

Everything below is the draft to review. Judgment calls the user may want to
veto are marked **[CALL]**.

## 0. Scope restatement

One contract revision unifying four findings: generic service config (#164),
role/identity genericization (#165), the drain Slack special-case, and the
manager-formatting allowlist (both from #167). Framework core (config,
routing, drain, executor, validate) becomes service- and role-name-free;
service specifics live in **adapters** (see §4) and agent-team config.

**[CALL] Reading of constraint 1.** "No github/slack/linear in framework
code" is read as: no service names in framework *core*. The framework still
ships ingestion adapters for github/slack/linear (normalizers + detectors +
webhook endpoints) — that code must live somewhere, and the event server
already has per-service webhook routes with signature verification. The
contract is that adapters are the *only* place service names appear, behind
a uniform interface, so adding service #4 means adding one adapter module
and zero core edits. The purist alternative (adapters as separately
installed packages) is deferred — it adds packaging machinery with no
current consumer.

## 1. The v2 envelope (`NormalizedEvent` in `core.ts`)

```ts
interface NormalizedEvent {
  v: 2;                       // contract version
  id: string;                 // delivery id / uuid
  source: string;             // adapter name: "github" | "slack" | ... | "agent" | "monitor" | "custom"
  type: string;               // dot-namespaced event type, e.g. "github.issues.assigned"
  topics: string[];           // routing keys, computed by the normalizer
  timestamp: string;          // ISO 8601
  delivery: "chat" | "bulk";  // drain ordering class (replaces source=="slack")
  text: string;               // one-line human summary, REQUIRED, set at normalization
  fields?: Record<string, string | number | boolean>;
                              // flat scalar highlights for agent display (optional)
  run_key?: string;           // correlation id (lifecycle + workflow events)
  payload: Record<string, unknown>;  // raw-ish payload, adapter-shaped
}
```

Removed from the envelope: `repo`, `team_key`, `workspace`, `channel`,
`installation_id`. Those are adapter vocabulary; they move into `payload`
(github keeps `repo`/`installation_id` in payload, slack keeps
`channel`/`workspace`, etc.). During migration they are also still set
top-level for one release (§6).

Routing collapses to:

```ts
export function subscriptionKeysForEvent(event: NormalizedEvent): string[] {
  return event.topics?.length ? event.topics : [event.type];
}
```

Topic values are exactly today's key shapes — **no subscription key
migration**:
- github adapter: `["github:org/repo"]`
- linear adapter: `["linear:TEAM"]`
- slack adapter: `["slack:WORKSPACE_ID"]`
- lifecycle/monitor/custom topic events: `[type]` (e.g.
  `["agent/session.completed"]`)

**[CALL] Topics are minimal, not additive.** We do NOT also add the bare
`type` to webhook events' topics (which would let a subscriber take
`github.issues.assigned` across all repos). It changes fanout semantics and
risks double-delivery for overlapping subscriptions; the `topics[]`
mechanism makes it a one-line additive change later if a real consumer
appears.

`role` is **not** an envelope field — per the DECIDED section it rides in
the payload of lifecycle events only.

## 2. Lifecycle events (Finding 2)

### Topics (framework-owned, finite)

| v1 (emitted today)              | v2                          |
|---------------------------------|-----------------------------|
| `engineer/session.started`      | `agent/session.started`     |
| `engineer/session.completed`    | `agent/session.completed`   |
| `engineer/session.failed`       | `agent/session.failed`      |
| `engineer/workflow.started`     | `agent/workflow.started`    |
| `engineer/workflow.completed`   | `agent/workflow.completed`  |
| `engineer/workflow.failed`      | `agent/workflow.failed`     |
| `engineer/workflow.resumed`     | `agent/workflow.resumed`    |
| `engineer/step.*` (if emitted)  | `agent/step.*`              |

### Payload (lifecycle)

`{role, run_key, session_id, project, phase, task|summary|error, duration,
requested_by}` — `role` is whatever role string the executor was launched
with; the framework never interprets it.

### Run identity: `run_key` replaces `issue_id`

- `agents launch` gains `--id <run_key>`. Workflow-triggered runs populate
  it from the trigger event via the variable context
  (`run_key: ${{ event.payload.issue.number }}`-style, declared in workflow
  YAML). Fallback: generated `adhoc-<hash8>`.
- **Regex scraping (`_parse_issue_number`) is deleted**, not demoted to a
  fallback. Tracker IDs arrive explicitly: webhook payloads carry them
  structurally, workflows map them via variables, humans pass `--id`. A
  generated id is a better fallback than a regex guess that collides on
  "fix #42 and #43" or a quoted Slack message.
- Renames: `AgentResult.issue_id` → `run_key`; `SessionEntry.issue_id` →
  `run_key`; `WorkflowRun.issue_id` → `run_key`;
  `WorkflowRun.find_waiting(event_type, run_key)`. State readers accept the
  old key when loading existing JSON (one-line shim, cheap to keep).
- Session names: `_session_name` → `{role}-{run_key}[-{phase}]` (was
  `eng-…`); `make_session_name` unchanged in shape but takes run_key.

### Role plumbing

- `subagent.spawn_adhoc` already takes `role=` — the emitters thread it into
  lifecycle payloads instead of hardcoding "engineer" in topic + text.
- `monitors/scheduler._default_spawn_check`: monitor records gain an
  optional `role:` field; the scheduler passes it through. Default when
  absent: the team's `defaults.role` from agent.yaml, else the framework
  spawns with no role prompt (base prompt only). **[CALL]** — alternative
  was a framework default role name ("worker"), rejected as a framework
  opinion.
- Event `text` becomes role-aware: `"{role} started working on {run_key}"`.

## 3. Service descriptors in agent.yaml (Finding 1)

```yaml
services:
  - name: github
    events: true
  - name: slack
    events: true
    credentials:
      bot_token: ${SLACK_BOT_TOKEN}
  - name: linear
    events: true
    credentials:
      api_key: ${LINEAR_API_KEY}
  - name: discord          # hypothetical 4th service — zero framework edits
    events: true
    credentials:
      bot_token: ${DISCORD_BOT_TOKEN}
```

- `ServiceConfig` gains `credentials: dict[str, str]` (interpolated from
  `${VAR}` like everything else).
- New accessor: `cfg.credential(service, key) -> str` (empty string if
  absent). All call sites that read `cfg.slack_bot_token` /
  `cfg.linear_api_key` move to it; those Config fields are **deleted**.
  Loader keeps reading the legacy top-level `slack:`/`linear:` blocks into
  the equivalent service credentials for one release, with a deprecation
  warning (§6).
- `native_services` property is deleted. "Native" becomes *"an ingestion
  adapter is registered for this name"* — `adapters.is_registered(name)`.
  `venn_services` becomes `[s for s in services if not adapters.is_registered(s.name)]`.
- `validate.py:_check_native_credentials` → generic: for every declared
  service, every declared credential key must interpolate non-empty; adapter
  registration determines the "native"/"venn" label in output. The
  per-service if/elif is deleted. (github declaring no credentials stays
  valid — auth rides on `gh`.)
- `venn_api_key` stays as-is for now — Venn is a gateway, not an event
  source; folding it into `services:` is out of scope (tracked in
  `project_mcp_venn_remaining`).

## 4. The adapter registry (normalizers + detectors)

One concept, two runtimes:

**TS side (`event-server/src/adapters/`):** each adapter exports
`{name, normalize(...): NormalizedEvent | skip}`. The normalizer is the only
code that knows the payload shape; it computes `type`, `topics`, `delivery`,
`text`, `fields`, `payload`. `core.ts` keeps the generic envelope type,
`subscriptionKeysForEvent`, signature helpers, and an adapter registry;
github/slack/linear normalizers move out of `core.ts` into
`adapters/{github,slack,linear}.ts`. Webhook routes resolve their adapter by
name.

**Python side (`modastack/events/adapters.py`):** a registry keyed by
service name; each entry provides
`detect(project_path, cfg) -> list[str]` (subscription keys). The current
`_detect_github/_detect_slack/_detect_linear` bodies move here unchanged;
`_resolve_source`'s if/elif becomes `adapters.get(name).detect(...)`, falling
back to `[name]` for unregistered services (today's behavior).
Credential access inside detectors goes through `cfg.credential(...)`.

**[CALL] Teams do not ship executable detectors.** The brief floated "teams
ship detectors with their tool guides" — rejected for now. That means
loading team-supplied Python/TS into the framework process: a plugin/security
surface with no current consumer. Teams with non-native event sources
declare explicit `subscribe:` keys in agent.yaml (already supported, already
first in resolution order) and post events to `/events/{topic}`. Adapter
authorship stays a framework contribution until a real third-party need
shows up.

## 5. Delivery + formatting (Findings 3 & 4)

### drain.py

The Slack partition is replaced by the `delivery` class:

```python
chat  = [e for e in batch if e.get("delivery") == "chat"]
bulk  = [e for e in batch if e.get("delivery") != "chat"]
for group in (bulk, chat): ...
```

with a comment recording the intent the v1 code never wrote down: chat
events are delivered last so the most recent thing injected into the
session is the human's message, keeping the agent's next turn pointed at
the conversation. The slack adapter sets `delivery: "chat"`; everything
else defaults to `"bulk"`. Any future chat source gets the ordering by
setting one field at normalization.

### format_event_for_manager

The allowlist dies. v2 rendering, in order:

1. `Event: {source}/{type}` header (+ `run_key` if present).
2. `text` — the adapter-written summary line.
3. `fields` — every entry, verbatim (adapters keep it small).
4. Fallback when `fields` is absent (custom/monitor events): all top-level
   *scalar* payload entries, each value truncated (~200 chars), capped at
   ~20 entries. Nested objects/arrays are skipped, not dumped.
5. `requested_by` keeps its pretty-printer (it's framework routing
   metadata, not service vocabulary).

Nothing is silently dropped anymore: every event at minimum renders
header + text, and adapters own making `text`/`fields` useful. The github
adapter's `fields` must include `action`, `assignee`/`assignees`,
`sender`, `issue`/`pr` number+title, `state`, `url` — the
`issues.assigned` incident (Finding 4) becomes structurally impossible
rather than prompt-patched.

**[CALL]** `fields` is still an extraction choice — but it moved from a
cross-service allowlist in the formatter (wrong layer, silently lossy) to
the adapter that owns the payload shape, with a scalar-fallback so unknown
sources degrade to "verbose" instead of "blank". Agents needing more depth
shell out (`gh`, tool guides) as designed.

## 6. Migration plan

**Strategy: hard cutover. No shims.** (Decided with the user 2026-06-10 —
the soft-cutover draft was defending constituencies that don't exist. There
are no external installs; every consumer of the contract is a deployment we
control: the prod EC2 director, modastack-dogfood, and local dev. Each
proposed shim only protected a deploy-ordering window we can close by
runbook, so the complexity bought nothing.)

What ships, with no compatibility layer:
- v2 envelope only. Legacy top-level `repo`/`team_key`/`workspace`/
  `channel`/`installation_id` are gone in the same release. The `v: 2`
  field stays — it's one line, and it makes the *next* contract change
  debuggable.
- Loader reads only the `services:` + `credentials:` format. Legacy
  `slack:`/`linear:` blocks are simply unknown keys; `modastack install`
  regenerates agent.yaml.
- State readers read only `run_key`. In-flight workflow runs and session
  state from v1 are discarded by the `--fresh` re-install (accepted: at
  current scale nothing long-lived is worth preserving across this).
- Lifecycle topics `engineer/*` → `agent/*` and session-name shape
  (`eng-…` → `{role}-…`) change outright. Grep shows no agent-team YAML
  references `engineer/*` topics today — consumers are framework-internal —
  so the practical blast radius is near zero.

Reference migrations (in the same change):
1. `agents/eng-team` (this repo) — agent.yaml to service-descriptor
   credentials; workflows gain explicit `run_key:` mapping from trigger
   payloads.
2. `agents/market-research` (this repo, about to merge) — same treatment;
   coordinate with that branch so it lands already on v2 or migrates
   immediately after.
3. `content-review` — **decided 2026-06-10: the modastack-dogfood repo is
   retired.** Isolated per-project installs make a standing dogfood repo
   unnecessary: the pack moves into `agents/content-review/` in this repo
   and the battery installs it into throwaway temp projects instead
   (ticket D in §7, which also carries the v2 migration + email-service
   routing verification). Full dogfood battery (manager lifecycle, ask
   round-trip, webhook→manager pipeline) must pass.

Cutover runbook (per deployment, prod EC2 + dogfood):
1. `modastack stop`
2. Deploy the v2 event server (worker / local)
3. Upgrade the CLI (`uv tool upgrade modastack` / pull + reinstall)
4. `modastack install <team>` (regenerates `.modastack/` config) +
   `modastack start --fresh`

The only degradation window is between steps 2 and 4 *if a deployment is
left running against a v2 server*: old clients still receive and route
events (keys are unchanged, `type`/`source`/`payload` survive) but render
them thinner. The runbook closes the window by stopping first; if a
deployment is missed, it degrades, it doesn't break.

Verification bar: 620 unit + 86 integration + 37 event-server tests green;
dogfood run green; one new integration test asserting an unknown-source
event posted to `/events/{topic}` renders with text + scalar fallback in
the manager inbox (the Finding-4 regression test).

## 7. Implementation tickets (agent-dispatched)

Cutting principle (agreed with the user): an agent does all the coding, so
a ticket earns its existence only if the repo is **green and independently
verifiable after it lands alone**. By that test the contract change across
two runtimes is one atomic ticket (splitting TS from Python would leave the
repo in cross-runtime drift between them — the #163/#166 failure mode), and
the cutover is a checklist, not a dispatch. Each coding ticket links this
doc as the plan; the ticket body carries the file-set and the gate.

The three coding tickets touch **disjoint file-sets**:

- **A (#177). v2 envelope, both runtimes** — `core.ts` → `adapters/*.ts`
  extraction; add `v/topics/delivery/text/fields`, drop legacy top-level
  fields, collapse `subscriptionKeysForEvent`; `client.py` v2 rendering;
  `drain.py` delivery grouping.
  *Gate:* event-server tests + unit + the new Finding-4 integration test.
- **B (#178). service descriptors + detector registry** — `config.py` credentials
  + `cfg.credential()`, generic `validate.py`, `subscriptions.py` detector
  registry, delete `native_services`; migrate eng-team `agent.yaml` in the
  same diff so tests exercise the new format.
  *Gate:* unit tests.
- **C (#179). run_key + role-parameterized lifecycle** — renames (`issue_id` →
  `run_key`), `agents launch --id`, regex deletion, `agent/*` topics,
  monitor `role:`, session naming, in `subagent.py` / `orchestrator.py` /
  `state.py` / `monitors/` / `cli.py`.
  *Gate:* unit + integration.
- **D (#180). chore: absorb content-review + retire modastack-dogfood** —
  (decided 2026-06-10: isolated per-project installs make a standing
  dogfood repo unnecessary). Move `agents/content-review/` from
  modastack-dogfood into this repo's `agents/` (+ `registry.yaml`);
  migrate it to v2 in the same diff; re-point its `context.content_dirs`
  (guides/runbooks/research) at in-repo fixture content; rewrite the
  `/dogfood` command to install the pack from the local path into a
  throwaway temp project instead of cloning the dogfood repo; archive
  moda-labs/modastack-dogfood.
  Scoping notes (2026-06-10): the pack is nearly v2-shaped already (no
  legacy `slack:`/`linear:` credential blocks; zero
  `issue_id`/`engineer/*`/`await` references), so the migration itself is
  small — **plus one real verification**: the pack declares `email` with
  `events: true` (a service with no adapter — detector falls back to
  subscription key `email`) while its monitor posts `email/received`
  topic events. Those are different exact-match keys; confirm how email
  events actually reach the manager (server routing vs direct monitor
  injection) and fix the declaration or the topic if they don't line up.
  content-review is the only live exercise of the "4th service, zero
  framework edits" promise — this ticket is where that promise gets
  proven.
  **Open question (settle at filing):** the webhook→manager pipeline test
  needs a real GitHub repo with webhooks pointed at the event server —
  the role modastack-dogfood's remote plays today. Either point webhooks
  at the modastack repo itself (labeled test-noise issues land on the
  framework repo) or keep one minimal repo alive purely as a webhook
  sink.
  *Gate:* dogfood battery green against a v2 framework build, running
  from the in-repo pack.
- **E (#181). cutover (ops checklist, not a dispatch)** — market-research branch
  coordination, prod runbook per §6, archive modastack-dogfood once D's
  battery is green.
  *Gate:* dogfood battery green on the upgraded prod install.

A is fully independent; B before C is mildly cleaner (C's monitor role
default reads team config). All three can otherwise proceed in parallel
worktrees without merge conflicts. D needs a v2 framework build to verify
against (after A–C); E follows everything.
