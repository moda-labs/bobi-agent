# Fine-Grained Metrics Operations

Metrics records provider usage and JEV decisions without blocking agent work.
Collection and routing are separate: `BOBI_METRICS_MODE=enabled` enables
collection; experiment configuration enables routing. Both are disabled by
default when unconfigured.

## Configuration

- Set `BOBI_METRICS_MODE=enabled` in the deployment environment.
- Configure `BOBI_METRICS_EXPERIMENT_JSON` and a private
  `BOBI_METRICS_ASSIGNMENT_SECRET` for JEV. Keep provider and TypeSafe
  credentials in private environment files, never in profiles or source.
- The experiment declares control/candidate models, variant weights, policy,
  eligible entrypoints/roles, privacy scope, and deadlines. See
  [the routing contract](JEV_ROUTER_DATA_FLOW.md).
- JEV `shadow` records a recommendation but executes the control model;
  `enforce` executes an eligible policy recommendation. Explicit overrides,
  sticky session/workflow decisions, and safe fallbacks still apply.
- Candidate models and gateway URLs are deployment inputs, not framework
  defaults. For the Claude brain, the Anthropic-compatible base URL must match
  the provider's Messages endpoint contract.
- Environment changes require container recreation. Preserve the existing
  named volume; never use `docker compose down -v`.

### Concise JEV configuration

`BOBI_METRICS_EXPERIMENT_JSON` accepts this minimal schema as well as the legacy
variant/policy schema. Only these three JSON fields are required:

```json
{
  "control_model": "ds/deepseek-flash",
  "candidate_models": ["ds/deepseek-flash", "ds/deepseek-v4-pro"],
  "instructions": "Route coding & complex logic to Pro; trivial queries to Flash."
}
```

Use a single-line JSON value in the three-line environment configuration
(`BOBI_METRICS_EXPERIMENT_JSON`, `BOBI_METRICS_ASSIGNMENT_SECRET`, and
`TYPESAFE_API_KEY`), with private values supplied by your deployment.
The loader compiles a zero-weight `control` variant and a full-weight
`treatment_jev` variant with a pinned `jev-1.13.0` TypeSafe policy.
`experiment_id` defaults to the constant `jev-routing-v1`, so repeated loads
keep the same identity and assignments. Set an explicit ID for separate
experiments. `mode` defaults to `enforce`, and `roles` to `director`/`engineer`.
Internal headers default to `jev-router`, `v1`, and `jev-features-v1`.
Explicit IDs, headers, legacy variants, and policy options remain unchanged;
mixing flat candidate fields with legacy `variants`/`policy` is rejected.
Optional flat fields include `brain` (`auto` by default: route whichever brain
the agent runs; `claude` or `codex` pins one), `entry_points`,
`criteria` (a description per candidate), `min_confidence` (0.85),
`deadline_ms` (3000), `endpoint`, `policy_version`, `prompt_egress` (`none`),
and `max_prompt_bytes` (8192). Default entry points are `session_start`,
`subagent_phase`, `subagent_persistent`, `subagent_supervised`, and
`workflow_start`; default criteria name each candidate and defer to the routing
instructions. `control_model` must be a candidate, candidates must be unique,
and instructions must be non-empty. Explicit null/empty defaults are rejected.
To classify actual task text in production,
explicitly set `prompt_egress` to `redacted`; otherwise only structural context
is sent. The web editor offers this as **Prompt sent to JEV**, defaulting a new
policy to redacted. Production routing still requires `BOBI_METRICS_ASSIGNMENT_SECRET`.

Saving checks that the policy can run on the agent. A pinned `brain` must match
the agent's brain (`BOBI_BRAIN`, else `agent.yaml` `brain.kind`). When the agent
dials a gateway (`BOBI_GATEWAY_BASE_URL`, `brain.base_url`, or a gateway brain
kind), any provider's model may be a candidate. A native brain calls one vendor,
so the save rejects a routing-prefixed id (`cx/`, `ds/`) or another provider's
model (`opus` on codex, `gpt-5.4` on claude) with the fix in the message.
`GET .../jev-config` returns `agent_brain`, `gateway`, `prompt_egress` and
`config_warnings` for a saved policy that cannot route the agent.

In the web editor (Metrics → JEV button → **Configure**), candidates are picked
by filtering and clicking a list of the models the agent's own brain runs
natively, unprefixed (`bobi.costs.native_models`: Claude Code aliases and
Anthropic ids for claude, OpenAI ids for codex). **+ Add custom gateway model**
takes a typed routing id (`cx/gpt-6.1-sol`, `ds/deepseek-flash`); without a
gateway the save rejects it. A dashed tag marks a name with no list price. The
editor saves straight to `run/.env` (**Save and Apply**); there is no clipboard
export, and the TypeSafe endpoint and experiment id stay as saved (defaults
`https://api.typesafe.ai/v1/systemone`, `jev-routing-v1`). The control / fallback model is chosen from the candidate list, so it
cannot drift off it. Saving rejects two names for one model (`opus` and
`claude-opus-5-5`), because the policy would split its probability between
them. Save errors name the field (`invalid JEV configuration: min_confidence
must be between 0 and 1`). Setting the mode to **Disabled** keeps the policy as
a commented line for re-enabling; **Reset / Delete** removes it. In shadow mode
the policy is called and recorded but turns run the control model.

### Interactive sandbox

Open Metrics → JEV button → **Test Routing**. Enter a
test prompt, role, and entry point (default `session_start`). An optional masked API key is used only for
that request; an empty field uses the agent's configured credential. The
simulation sends what production would: redacted task text when the policy's
`prompt_egress` is `redacted`, metadata only when it is `none`. The web UI
calls the saved endpoint; the API still accepts an `endpoint` override on the
saved TypeSafe hostname and port. Keys are cleared on close and are never saved
by the sandbox.

### Fallback log and failed candidates

Every routing fallback is written to the agent log and, one JSON object per
line, to `run/state/jev_routing_fallback.log`. Two events appear there:

- `jev_routing_fallback`: routing made no decision (`fallback_reason` is one of
  the session-level reasons, or `admission_rejected`).
- `jev_candidate_failed`: the policy chose a model other than the control model
  and that model failed to connect or failed its first turn. The record names
  `attempted_model`, `brain`, `provider`, the redacted `error` and
  `fallback_model`. The session does not fail: it starts a fresh brain session
  on the control model and reruns the task once. This covers every routed
  entry point: `Session`-backed sessions (`session_start`, `subagent_phase`,
  `subagent_persistent`), supervised subagents, and workflow runs
  (`workflow_start`), where the first prompt step reruns and later same-agent
  steps stay on the control model. Turn-cap, authentication and credit failures do not
  fall back, because the control model would fail the same way. The router
  decision keeps the policy's pick, so the dashboard still attributes the turn
  to that model.

`POST /api/agents/{name}/metrics/test-route` uses the existing web UI token
and Host guard. Its JSON body accepts `prompt`, optional `features`
(`role`, `entry_point`, `brain`), `api_key`, and `endpoint`. It calls the
saved TypeSafe policy directly with bounded, redacted test text, not production
transcripts. The policy may be disabled/commented out and no assignment secret
is needed. It never starts a session, opens SQLite, or emits metrics. Requests
are real TypeSafe API calls and may incur vendor charges.

The response includes `selected_model`, `confidence`, `probabilities`,
`latency_ms`, `features`, `outgoing_payload`, and `raw_response`. Wire inspection
redacts credentials. `effective_model`, `mode`, and `fallback_reason` distinguish
the classifier recommendation from the shadow/low-confidence control fallback.
The sandbox tests the classifier, not production scope eligibility or sticky
session reuse. Errors are redacted and every response is uncached.
The HTTP body is capped at 128 KiB with a three-second read deadline, prompt
input at 64 KiB, policy requests at 128 KiB, vendor responses at 64 KiB, and
returned JSON at 512 KiB. The configured policy deadline is 50–3000 ms and
each local runtime admits at most four concurrent simulations. Redirects are
disabled; endpoint overrides require HTTPS and the configured host/port.

## Runtime and storage

The hot path enqueues bounded events. A background producer writes framed
append-only spools; one elected collector stages immutable `raw_events` and
projects normalized rows. Out-of-order parents defer children; duplicate
events are idempotent. Invalid events are quarantined. Queue acceptance is not
a durability guarantee; failed routing admission falls back safely.

SQLite connections enable WAL, foreign keys, a busy timeout, and automatic
checkpointing. Readers are read-only and query-only. Metrics failures must not
fail an agent turn; inspect collector health rather than assuming missing
telemetry means zero usage.

The database lives at:

```bash
export BOBI_HOME="${BOBI_HOME:-$HOME/.bobi}"
export BOBI_AGENT_NAME="my-agent"
export METRICS_DB="$BOBI_HOME/agents/$BOBI_AGENT_NAME/run/state/metrics/metrics.db"
sqlite3 -readonly "$METRICS_DB" 'PRAGMA integrity_check; PRAGMA foreign_key_check;'
```

Schema and historical event projection are unchanged. No migration or data
rewrite is required for this cleanup. Historical `experiment_outcomes` remains
readable, but new runtime outcome events and external evaluator helpers are
not produced.

## Exact accounting

- Use `best_usage`, not all `usage_measurements`: the latter retains
  superseded estimates and duplicate scopes.
- `measurement_source=provider_stream` and `is_estimated=0` identify provider
  facts. Transcript reconciliation can also recover exact facts. Unknown
  dimensions stay `NULL`, never zero.
- Do not add turn totals to invocation totals. Exact terminal turn aggregates
  are canonical per model. Missing terminal dimensions are filled only from
  complete exact invocation facts; sole-model provider aliases are treated as
  one logical model. Genuinely additional model partitions remain additive.
- Cache reads/writes and reasoning dimensions retain provider semantics.
  Tool byte sizes and attributed tokens are not provider billing totals.
- Provider-reported and estimated costs are separate, never added together.
- Exact usage can coexist with uncovered turns elsewhere. Report both local
  turn evidence and global collector/reconciliation debt.

## Inspection

### Local web view

Start `bobi app start`, open the installed agent, and select **metrics & routing**
(`#/agents/<name>/metrics`). The existing run panel also has **transcript**,
**usage**, and **routing** sections; these do not change chat or resume behavior.

- The recent-session picker shows each lifecycle's name, short telemetry ID, and start time.
  Selecting a lifecycle filters by exact `session_id`; older name-based run links remain explicitly labeled as spanning all matching lifecycles.
  Model filtering matches case-insensitive substrings of requested or selected invocation models and updates 300 ms after typing,
  including canonical terminal usage whose model uses a provider alias.
- Tiles and UTC buckets use canonical usage, not the sum of provenance rows.
  The summary has **Total Input Tokens**, **Total Output Tokens** and **Total Spend (USD)** tiles; cache-read tokens and their share of input appear in the input tile, for example `8,885,624 (87.3% cache read)`.
  Missing dimensions read **not recorded**; estimate flags remain in telemetry without UI suffixes, and
  invocation coverage separates exact, estimated, and unknown measurements.
- Routing shows assignment, recommendation, policy confidence, selected and
  recorded provider models, latency, and fallbacks. Confidence is the policy
  value, not the assignment bucket. Distinct call IDs count policy calls;
  subsequent turns can reuse a session route without another policy request.
- Reported and estimated costs remain separate. When a turn has neither, the
  view shows a `~`-prefixed list-price figure (`list_price_usd`) computed from
  `bobi/costs.py` `PRICE_TABLE`; it is never added to recorded spend.
  Collector health is global, not limited by view filters.
- The routing card prices each routed turn's canonical tokens at list price
  three ways: on the model that ran, on the cheapest candidate in that
  decision's `candidate_models` (**always cheapest**), and on the most expensive
  one (**always strongest**). The card opens with four figures: net saved
  against always strongest, routing rate, safety fallbacks and router
  overhead. **Model Distribution** is one bar segment per model the router
  picked, sized by its share of priced routed turns. **Model Breakdown & Cost
  Savings** is one flat table with a row per picked model (merged across route
  sets, busiest first): share / turns, **Actual Spend**, **Baseline Cost
  (Flagship)** (the same turns on the most expensive candidate) and **Net
  Savings**; the flagship's own row reads *Baseline*. A `Fallback ×N` badge on
  the model names the recorded fallback reasons on hover. Models are shown by
  their real ids; there are no capability tiers. The always-cheapest cost and `captured_pct` (the share of
  the largest possible saving the router kept) stay in the payload. This card
  and the spend tiles use Title Case and green savings by operator decision,
  departing from the design system's lowercase chrome and no-green palette.
  The recorded
  `control_model` is not the baseline: routers record their fallback there,
  which is usually the cheapest candidate. Calls inside a routed turn on a
  model other than the selected one (a harness's background haiku call) count
  at their executed price on every side. Turns whose executed model or any
  candidate has no list price are left out and named in `unpriced_models`.
  Each candidate set is one entry in `route_sets` with its candidates' rates,
  picks, per-pick `routed_cost_usd` / `ceiling_cost_usd` / `fallback_turns` /
  `fallback_reasons` (reason to turn count), and the set's own saving; a pick
  outside the candidate list is appended with `off_list: true`. Claude Code aliases (`opus`, `sonnet`, `haiku`) and a
  context suffix (`claude-opus-5-5[1m]`) price as the current model.
- Telemetry health is one strip under the spend tiles: collector state,
  invocation coverage, usage granularity, import lag and reconciliation
  errors. Only a problem (unknown usage, lag over a minute, reconciliation
  errors) takes colour.
- Recent turns default to one card per session (thread). The header holds the
  first user message (or the lifecycle label), a **user prompt** / **system
  lifecycle** tag, agent and role, the Slack origin, and the session ID; turn
  rows repeat none of these and show the step instead (initial request,
  follow-up, tool call, assistant response). **Flat** lists turns
  chronologically and shows `turn #n` only for sessions with more than one
  loaded turn. The turn drawer's earlier/later buttons stay within the session.
- Run drilldowns match the session **name** across telemetry lifecycles in the
  last 31 days. Names are not unique telemetry IDs; each turn shows its actual
  session and turn IDs. Provenance and superseded rows are explicitly nonadditive.
- Turn detail has one **Model Invocations & Token Breakdown** table, with requested/resolved models, latency in milliseconds, input/output/reasoning tokens, cache reads/writes, provenance, and separately labeled costs.
  Raw records stay in a collapsed **Raw debug / JSON** disclosure.
- Input and response come from the recorded provider session's transcript, matched to the turn's timestamps, never the current session-name resume file.
  The two drawer tabs are **Overview & Execution** (KPIs, turn input/response, nonempty tool cards, routing reasons) and **Technical Telemetry** (canonical token breakdown, separately labeled costs, recorded policy internals, collapsed raw JSON with copy).
  Unavailable input and response produce one compact callout, never a substitute from another turn or two empty message boxes. Inspection remains read-only; workflow approval gates retain their verdict controls.
  Assistant response is the recorded model text, not a delivery receipt for tool-mediated Slack replies.
  Message previews are bounded to 32 KiB each and explicitly marked when truncated.
  Long prompts collapse to 180px with a full-prompt toggle. Maintenance prompts have a system label; actual user messages retain their user label.
  Tool-result previews allow 4,000 characters, preserve line or word boundaries, and retain original byte counts and truncation flags. Long results expand from ten lines or 220px into a scrollable view; floating copy controls copy all available preview text. Earlier discarded preview content cannot be reconstructed without its original transcript.
  Slack thread links have a distinct clickable affordance. Turns without a valid Slack thread link show no origin badge.
  Transcript scans stop after 16 MiB or 250 ms per file, with a one-second overall enrichment budget; oversized or unavailable transcripts do not block the manager or alter telemetry.
  The local authenticated turns endpoint adds 60-character prompt and response previews, tool counts, canonical reported/estimated cost totals, and recorded Slack conversation references when available; unknown origins fall back to the trigger kind.
  Conversation content is not added to SQLite, Admin RPC, or supervisor MCP responses.

Authenticated, read-only endpoints under `/api/agents/{name}/metrics`:

| Endpoint | Parameters | Response |
|---|---|---|
| `summary` | Required ISO `from`, `to`; optional `model`, `session_name` | Canonical totals, coverage, UTC buckets, routing counts/models, `routing.savings`, collector health |
| `turns` | Same range/filters; optional `session_id`, `limit`, `cursor` | Newest-first turns, usage, invocations, allowlisted policy fields, `costs` (`reported_cost_usd`, `estimated_cost_usd`, `list_price_usd`), local conversation previews, `next_cursor` |
| `sessions` | Required `from`, `to`; optional `limit`, `cursor` | Distinct recent lifecycles (`session_id`, name, start/end timestamps, status), `next_cursor` |
| `sessions/{id}` | Optional `limit`, `cursor` | Existing session detail and bounded turn page |
| `turns/{id}` | None | Turn detail, session identity, canonical `best_usage`/`invocation_usage` (each row with `list_price_usd`)/`usage_totals`, allowlisted policy fields, local-only `conversation` (`input`, `response`, scoped `entries`, `origin`, `status`, `truncated`) |
| `jev-config` | None | Current routing configuration; an installed agent without an active experiment returns HTTP 200 with `enabled: false`, `mode: off` and no invented models. Also `native_models` (the picker list for `agent_brain`), `priced_models` (every priced id, gateway forms included), `unpriced_models` among the saved candidates, and `config_error` naming why a saved policy is not active |

`routing.savings` shape (USD, list prices; totals are null when nothing could
be priced; `route_sets` has one entry per candidate set, candidates shortened
here):

```json
{"routed_cost_usd": 0.000213, "ceiling_cost_usd": 0.00023, "floor_cost_usd": 5.9e-05,
 "saved_usd": 1.7e-05, "saved_pct": 7.4, "premium_pct": 259.6, "captured_pct": 10.0,
 "ceiling_model": "ds/deepseek-v4-pro", "floor_model": "ds/deepseek-flash",
 "priced_turns": 3, "unpriced_turns": 0, "escalated_turns": 2, "unpriced_models": [],
 "route_sets": [{"candidates": [{"model": "ds/deepseek-flash", 
   "prices": {"input_per_mtok": 0.05, "cached_input_per_mtok": 0.001, "output_per_mtok": 0.2},
   "turns": 1, "off_list": false, "routed_cost_usd": 5e-06, "ceiling_cost_usd": 2.2e-05,
   "fallback_turns": 0, "fallback_reasons": {}}],
  "control_model": null, "floor_model": "ds/deepseek-flash", "ceiling_model": "ds/deepseek-v4-pro",
  "turns": 3, "routed_cost_usd": 0.000213, "ceiling_cost_usd": 0.00023,
  "floor_cost_usd": 5.9e-05, "saved_pct": 7.4}]}
```

These use the app's existing token and loopback Host guard, `Cache-Control:
no-store`, and filter-bound keyset cursors. Reads use short SQLite read-only WAL
snapshots, a one-second query deadline, 200-row/512-KiB bounds, and at most four
concurrent local readers. Invalid queries return 400/422; unknown records 404;
busy/unavailable storage 503 with `Retry-After`. The view refreshes every ten
seconds on the latest page, pauses in hidden tabs, and cancels reads on navigation. One toolbar owns the time range, lifecycle picker/chip, provider filter, loaded-page search, refresh, and auto-refresh toggle. **All (up to 31d)** respects the existing query bound; it is not an all-history aggregation. Dashboard, agent, and metrics surfaces share a 1360px container with 32px desktop and 18px mobile gutters.
It does not run migrations, checkpoints, models, or routing policies.
The JEV header falls back to **JEV: Disabled** when its configuration cannot be read within 2.5 seconds; the editor reports the unavailable configuration instead of treating a failed read as a successful load.

Local and Docker-hosted `LocalRuntime` are supported. Hosted `EventBusRuntime`
returns an explicit `metrics_unsupported` response; it does not show false zeros
or fetch a host's unrelated local database. No new dependencies or frontend build
step are required.

```bash
bobi agent "$BOBI_AGENT_NAME" metrics status --json
bobi agent "$BOBI_AGENT_NAME" metrics reconcile --turn-id "<turn-id>" --wait --json
bobi agent "$BOBI_AGENT_NAME" metrics calibrate --json
```

Calibration is optional and only qualifies model-specific estimators against
exact samples. It does not turn estimates into provider facts.

Admin aliases and corresponding read-only MCP tools:

| CLI alias | Wire command | MCP tool |
|---|---|---|
| `metrics_summary` | `usage` | `bobi_usage_summary` |
| `metrics_session` | `usage_session` | `bobi_usage_session` |
| `metrics_turn` | `usage_turn` | `bobi_usage_turn` |

Session/turn aliases require supervisor 0.4.0 or newer; summary retains legacy
compatibility. Queries have bounded range (31 days), rows (200), response
size (512 KiB), and execution time. Saturation returns `metrics_busy`;
missing storage returns `metrics_not_ready`. Session pagination uses
filter-bound cursors. Protect the fleet operator token.

```bash
bobi admin metrics_turn --url "$BOBI_ADMIN_URL" --fleet "$BOBI_FLEET"   --instance "$BOBI_INSTANCE" --args '{"turn_id":"<turn-id>"}' --wait --json
```

The deployment wrapper retains `scripts/metrics_summary.py` for a human-readable
comparison of recent engineer turns. Run from that wrapper repository:

```bash
docker compose exec -T agent python - < scripts/metrics_summary.py
```

For direct per-turn inspection (one row per model partition):

```sql
SELECT t.turn_id, t.status, d.variant_id,
       json_extract(d.metadata_json, '$.policy.status') AS policy_status,
       json_extract(d.metadata_json, '$.policy.recommended_model') AS recommendation,
       json_extract(d.metadata_json, '$.policy.confidence') AS confidence,
       d.fallback_reason, d.model_selected,
       u.provider, u.model AS measured_model, u.input_tokens, u.output_tokens,
       u.measurement_source, u.is_estimated
FROM turns t
LEFT JOIN router_decisions d USING (turn_id)
LEFT JOIN best_usage u ON u.turn_id=t.turn_id AND u.scope='turn'
ORDER BY t.started_at_us DESC LIMIT 20;

SELECT turn_id, router_decision_id, provider, model_requested, model_selected, status
FROM llm_invocations ORDER BY started_at_us DESC LIMIT 20;
```

Inspect transcripts to prove token parity; a database-only check proves recorded
values, not agreement with the original provider receipt.

## Recovery and standard SQLite maintenance

Gateway sessions retain `provider=gateway`. Reconciliation chooses the parser
from the stored Claude/Codex brain. A historical unspecified brain requires one
unambiguous transcript format; it never guesses between both formats.

Background reconciliation makes at most three failed attempts per turn, persists
counts in `collector.state.json`, and reports retry exhaustion. Restore missing
transcripts before manually reconciling; manual recovery is not retry-limited.
Keep unrecoverable turns visible rather than inventing measurements.

Before maintenance, take a consistent SQLite backup, including committed WAL
data. Run in an environment that can access the database:

```bash
export METRICS_BACKUP="/secure/backups/metrics-backup.db"
python - <<'PYTHON'
import os
import sqlite3
from pathlib import Path

source = sqlite3.connect(Path(os.environ["METRICS_DB"]).resolve().as_uri() + "?mode=ro", uri=True)
with source, sqlite3.connect(os.environ["METRICS_BACKUP"]) as target:
    source.backup(target)
    assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
source.close()
PYTHON
```

Use a new private backup path for each backup. Normal operation relies on
SQLite automatic WAL checkpointing. If a manual checkpoint is needed:

```bash
sqlite3 "$METRICS_DB" 'PRAGMA wal_checkpoint(PASSIVE);'
```

Never delete live WAL files, manually prune telemetry/cursors, or replace an
open database. For corruption, stop the writer, preserve the damaged files,
and restore a verified backup under operator supervision. There is no
automatic retention deletion; monitor volume usage.

## Routing diagnostics and privacy

Invalid experiment JSON/schema, missing assignment secrets, unavailable
policies, and configured routing with unavailable telemetry emit structured
`jev_routing_fallback` warnings and explicit failure codes. With telemetry
available, pre-assignment failures are recorded in
`sessions.metadata_json.router_fallback`; invalid configurations do not
fabricate assignments or decisions. Policy failures after assignment are
recorded on `router_decisions.fallback_reason`.

Warnings omit raw configuration, credentials, and exception messages.
Metrics/Admin/MCP exclude prompt/response bodies, tool payloads, secret values,
full paths, and private assignment hashes. TypeSafe input follows the configured
privacy scope; do not enable task-text sharing without approval.

## Removed production surfaces

The approved 2026-10-03 cleanup removes benchmark/live-matrix/soak scripts,
SRM and causal/intent-to-treat aggregates, hotspot/experiment Admin and MCP
queries, external outcome helpers, and bespoke rebuild/prune/retention queues.
Historical plan sections describing those surfaces are superseded. Operational
summary/session/turn queries, exact accounting, reconciliation, and JEV policy
evaluation remain supported.

Run offline regression checks:

```bash
.venv/bin/python -m pytest tests/metrics/ -q --timeout=30 --basetemp=/tmp/bobi-metrics
```

Real Slack/provider tests are separate, opt-in checks and may consume quota.
