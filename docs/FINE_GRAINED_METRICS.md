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
  Model filtering selects turns with a matching invocation,
  including canonical terminal usage whose model uses a provider alias.
- Tiles and UTC buckets use canonical usage, not the sum of provenance rows.
  The summary has input tokens, output tokens, and spend tiles; cache-read tokens and their percentage of total input appear together in the input tile.
  Missing dimensions read **not recorded**; estimated usage is labeled and
  invocation coverage separates exact, estimated, and unknown measurements.
- Routing shows assignment, recommendation, policy confidence, selected and
  recorded provider models, latency, and fallbacks. Confidence is the policy
  value, not the assignment bucket. Distinct call IDs count policy calls;
  subsequent turns can reuse a session route without another policy request.
- Reported and estimated costs remain separate. No savings claim or pricing
  inference is made. Collector health is global, not limited by view filters.
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
  Transcript scans stop after 16 MiB or 250 ms per file, with a one-second overall enrichment budget; oversized or unavailable transcripts do not block the manager or alter telemetry.
  The local authenticated turns endpoint adds 60-character prompt and response previews, tool counts, canonical reported/estimated cost totals, and recorded Slack conversation references when available; unknown origins fall back to the trigger kind.
  Conversation content is not added to SQLite, Admin RPC, or supervisor MCP responses.

Authenticated, read-only endpoints under `/api/agents/{name}/metrics`:

| Endpoint | Parameters | Response |
|---|---|---|
| `summary` | Required ISO `from`, `to`; optional `model`, `session_name` | Canonical totals, coverage, UTC buckets, routing counts/models, collector health |
| `turns` | Same range/filters; optional `session_id`, `limit`, `cursor` | Newest-first turns, usage, invocations, allowlisted policy fields, local conversation previews, `next_cursor` |
| `sessions` | Required `from`, `to`; optional `limit`, `cursor` | Distinct recent lifecycles (`session_id`, name, start/end timestamps, status), `next_cursor` |
| `sessions/{id}` | Optional `limit`, `cursor` | Existing session detail and bounded turn page |
| `turns/{id}` | None | Turn detail, session identity, canonical `best_usage`/`invocation_usage`/`usage_totals`, allowlisted policy fields, local-only `conversation` (`input`, `response`, scoped `entries`, `origin`, `status`, `truncated`) |

These use the app's existing token and loopback Host guard, `Cache-Control:
no-store`, and filter-bound keyset cursors. Reads use short SQLite read-only WAL
snapshots, a one-second query deadline, 200-row/512-KiB bounds, and at most four
concurrent local readers. Invalid queries return 400/422; unknown records 404;
busy/unavailable storage 503 with `Retry-After`. The view refreshes every ten
seconds on the latest page, pauses in hidden tabs, and cancels reads on navigation. One toolbar owns the time range, lifecycle picker/chip, provider filter, loaded-page search, refresh, and auto-refresh toggle. **All (up to 31d)** respects the existing query bound; it is not an all-history aggregation. Dashboard, agent, and metrics surfaces share a 1360px container with 32px desktop and 18px mobile gutters.
It does not run migrations, checkpoints, models, or routing policies.

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
