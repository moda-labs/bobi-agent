# Fine-Grained Metrics Operations

Metrics records provider usage and JEV decisions without blocking agent work.
Collection and routing are separate: `BOBI_METRICS_MODE=enabled` enables
collection; experiment configuration enables routing. Both are disabled by
default when unconfigured.

## Configuration

- Set `BOBI_METRICS_MODE=enabled` in the deployment environment.
- Configure `BOBI_METRICS_EXPERIMENT_JSON` and `TYPESAFE_API_KEY` for JEV.
  Keep provider and TypeSafe credentials in private environment files, never
  in profiles or source.
- The config declares control/candidate models, routing instructions,
  eligible entrypoints/roles, privacy scope, and deadlines. See
  [the routing contract](JEV_ROUTER_DATA_FLOW.md).
- JEV `shadow` records a recommendation but executes the control model;
  `enforce` executes an eligible policy recommendation. Explicit overrides,
  sticky session/workflow decisions, and safe fallbacks still apply.
- Candidate models are deployment inputs, not framework defaults. They must be
  models the agent's own brain runs: Claude models for a claude brain, OpenAI
  models for a codex brain.
- Environment changes require container recreation. Preserve the existing
  named volume; never use `docker compose down -v`.

### Concise JEV configuration

`BOBI_METRICS_EXPERIMENT_JSON` takes this flat schema. Only these three JSON
fields are required:

```json
{
  "control_model": "haiku",
  "candidate_models": ["haiku", "sonnet"],
  "instructions": "Route coding & complex logic to sonnet; trivial queries to haiku."
}
```

Use a single-line JSON value in the two-line environment configuration
(`BOBI_METRICS_EXPERIMENT_JSON` and `TYPESAFE_API_KEY`), with private values
supplied by your deployment. Every eligible session calls the pinned
`jev-1.13.0` TypeSafe policy, and each decision is recorded with variant
`treatment_jev`. `experiment_id` defaults to the constant `jev-routing-v1`; set
an explicit ID to label separate rollouts. `mode` defaults to `enforce`, and
`roles` to `director`/`engineer`. Internal headers default to `jev-router`,
`v1`, and `jev-features-v1`. The former long form (`variants` and a nested
`policy` object) is rejected as `long-form experiment config is no longer
supported; save from the editor to rewrite it`.
Optional flat fields include `brain` (`auto` by default: route whichever brain
the agent runs; `claude` or `codex` pins one), `entry_points`,
`criteria` (a description per candidate), `min_confidence` (0.85),
`deadline_ms` (3000), `endpoint`, `policy_version`, `prompt_egress` (`none`),
and `max_prompt_bytes` (8192). Default entry points are all four that a caller
produces: `subagent_phase`, `subagent_persistent`, `subagent_supervised`, and
`workflow_start`; default criteria name each candidate and defer to the routing
instructions. `control_model` must be a candidate, candidates must be unique,
and instructions must be non-empty. Explicit null/empty defaults are rejected.
To classify actual task text in production,
explicitly set `prompt_egress` to `redacted`; otherwise only structural context
is sent. The web editor offers this as **Prompt sent to JEV**, defaulting a new
policy to metadata only (`none`), the same as a hand-written config.

Saving checks that the policy can run on the agent. A pinned `brain` must match
the agent's brain (`agent.yaml` `brain.kind`, else `BOBI_BRAIN` in the agent's
`run/.env`, the order the agent starts with; the web app's own environment
never counts). Candidates
must be models that brain runs (`bobi.costs.native_models`), with or without a
gateway; the save rejects another vendor's model (`opus` on codex, `gpt-5.4` on
claude) with the fix in the message, for example `gpt-5.4 is not an Anthropic
model this agent's claude brain can run; pick one from the model list`.
`GET .../jev-config` returns `agent_brain`, `prompt_egress` and
`config_warnings` for a saved policy that cannot route the agent.

In the web editor (Metrics → JEV button → **Configure**), candidates are picked
by filtering and clicking a list of the models the agent's own brain runs
(`bobi.costs.native_models`: Claude Code aliases and Anthropic ids for claude,
OpenAI ids for codex); there is no free-text model input. The editor saves straight to `run/.env` (**Save and Apply**); there is no clipboard
export. Every save writes the short form: `mode`, `control_model`,
`candidate_models`, `prompt_egress` and `instructions`, plus `criteria` and any
setting or label that differs from its default. A rejected long-form line
shows as the config error; saving from the editor replaces it with the short
form. The editor
does not show scoped roles or the minimum confidence; both keep their saved
values (defaults `director`/`engineer` and 0.85), and Overview shows the roles
read-only. Change them in `run/.env` or through the API. The control / fallback model is chosen from the candidate list, so it
cannot drift off it. Saving rejects two names for one model (`opus` and
`claude-opus-5-5`), because the policy would split its probability between
them. Save errors name the field (`invalid JEV configuration: min_confidence
must be between 0 and 1`). Setting the mode to **Disabled** keeps the policy in
`BOBI_METRICS_EXPERIMENT_DISABLED_JSON` for re-enabling; **Reset / Delete**
removes it. In shadow mode
the policy is called and recorded but turns run the control model.

### Interactive sandbox

Open Metrics → JEV button → **Test Routing** and click **Run test**. One
prompt is followed through four numbered steps, the way production handles it:

1. **Your prompt**, with what JEV receives: redacted task text when the
   policy's `prompt_egress` is `redacted`, metadata only when it is `none`.
2. **JEV suggests**: the pick, its confidence, latency and per-model
   probabilities. A failed call (bad key, timeout) is shown here.
3. **Bobi chooses**: the model the turn runs on and why (confidence clears the
   minimum, a fallback reason, shadow mode, or no suggestion → control model).
4. **Output from <model>**: the chosen model answers the prompt, with latency,
   tokens and a list-price estimate.

Role and entry point offer only the policy's scope (entry point defaults to
the policy's first one). The test always uses the policy's saved key
(`TYPESAFE_API_KEY` in `run/.env`); the tab says so, or disables **Run test**
when the key is not set (`credential_configured` in `GET .../jev-config`).

Step 4 calls `POST /api/agents/{name}/metrics/test-run` with `prompt` and
`model` (a policy candidate). It runs one turn through the agent's own Claude
brain and login (`BOBI_AUTH`, `run/.env`) with nothing allowed: no built-in
tools (`tools=[]`), no MCP server (strict, none passed), no user or project
settings and no permission bypass, plus a fixed sandbox system prompt and an
empty temporary working directory, so no project files or instructions reach
the model. It opens no session and writes no
metrics. Runs are capped at 180 s, two at a time, and 64 KiB of output.
Agents on a codex brain or a gateway get `model_run_unsupported`; a model
error returns 502 `model_run_failed`, a timeout 504 `model_timeout`. On a
subscription the answer counts against the account's usage limits.

### Fallback log and failed candidates

Routing incidents are written to the agent log and, one JSON object per line
with a `logged_at` timestamp, to `run/state/jev_routing_fallback.log`.
Fallbacks the policy step decided (`policy_*`, `route_persistence_failed`) are
not written there; they live on the decision row. Two events appear in the
file:

- `jev_routing_fallback`: routing made no decision (`fallback_reason` is one of
  the session-level reasons, or `admission_rejected`).
- `jev_candidate_failed`: the policy chose a model other than the control model
  and that model failed to connect or failed its first turn. The record names
  `attempted_model`, `brain`, `provider`, the redacted `error` and
  `fallback_model`. The session does not fail: it starts a fresh brain session
  on the control model and reruns the task once. This covers every routed
  entry point: `Session`-backed sessions (`subagent_phase`,
  `subagent_persistent`), supervised subagents, and workflow runs
  (`workflow_start`), where the first prompt step reruns and later same-agent
  steps stay on the control model. A failed turn reruns only when nothing would
  repeat (`may_rerun_on_control`): no tool ran in any attempt of it (a
  rerun would repeat a push or a post; a turn that ends without a terminal
  result counts as having run tools, since nothing records which did), the
  error is not a turn-cap, authentication or credit failure (the control
  model would fail the same way), and the decision was
  made fresh, not reused from a saved session route. Otherwise the failure
  stands. A failed connect always falls back, since nothing ran. The router
  decision keeps the policy's pick, so the dashboard still attributes the turn
  to that model.

`POST /api/agents/{name}/metrics/test-route` uses the existing web UI token
and Host guard. Its JSON body accepts `prompt`, optional `features`
(`role`, `entry_point`, `brain`), `api_key`, and `endpoint`. It calls the
saved TypeSafe policy directly with bounded, redacted test text, not production
transcripts. The policy may be disabled. It never starts a session, opens SQLite, or emits metrics. Requests
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
events are idempotent. Invalid events are quarantined. A frame that reuses an
imported event's id with different content (a rerun reconcile after its
session row changed) is set aside: the first event stands, the frame is logged
and counted in collector health (`conflicting_events` and `last_conflict_at_us`,
kept across restarts), and import continues past it. Queue acceptance is not a durability guarantee;
failed routing admission falls back safely.

Each collector cycle opens one connection, migrates once, and reads only the
segments with bytes past their cursor. A segment that fails for any reason is
skipped for that cycle without stopping the others; health reports
`status=degraded` with the error type in `last_error`, and the log warns once
per segment and error type. A fully imported segment that has been quiet for
five minutes is deleted, with its directory once empty, when its writer is
done: a reconciler segment always, a producer segment once that process has
exited. The retired producer's drop and writer-error counts stay in collector
health. An exited producer's incomplete last frame (a short write before it
died) is dropped with the segment and counted in `torn_tail_bytes`, so it never
holds `import_lag_ms` up.

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

The database holds a copy of every spool payload, so it is created at mode
0600 like the spool segments (its `-wal` and `-shm` follow); one created looser
by an earlier version is tightened on the next open.


## Exact accounting

- Use `best_usage`, not all `usage_measurements`: the latter retains
  superseded estimates and duplicate scopes. One model under several spellings
  (`claude-opus-5-5[1m]` in Claude Code's terminal usage, `claude-opus-5-5`
  in its transcript, a dated id, a Claude Code alias, a route prefix) is one
  measurement. `llm_invocations`, `usage_measurements` and `cost_measurements`
  carry a `model_key` column for this (`bobi.costs.model_identity`, written at
  projection and backfilled on migrate); join and compare on it, and keep
  `model` for display. At turn scope, Claude's exact terminal total outranks
  the transcript even when the transcript carries more detail or
  reconciliation marked the total superseded, because it includes background
  calls the transcript never logs; per call, the transcript stays preferred
  because streamed output counts are partial. The view is rebuilt on collector
  start when its definition changes (`best_usage_version` in `schema_meta`).
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
- A turn that ran several models (a subagent on another model) gets one cost
  row per model: Claude's per-model `costUSD` splits the reported total when
  every model has one and they add up, otherwise one row with no model. Each
  model's estimate is its own row. `llm_invocations.model_requested` is set
  only on invocations of the requested model in such a turn, so a filter on
  the requested model does not count the other model's calls.
- Claude's `total_cost_usd` / `modelUsage` and Codex's turn usage count the
  whole provider session, including turns from before a resume in a new
  process. Each turn reports the difference from the previous turn's totals,
  which are saved per session under `state/usage_baselines/`. A fresh session
  saves zero totals as soon as its id is known, so a first turn that fails
  before any usage still leaves a baseline. A file is pruned only after 30 idle
  days and when no saved session id refers to it; a failed write removes the
  older file. A resumed session with no saved totals (started before they were
  kept, or whose write failed) reports no usage for its first turn rather than
  the whole session again.
- A multi-model Claude turn is split by the per-model `costUSD` values only
  when they add up to `total_cost_usd`. Otherwise the turn keeps one cost row
  with no model, so a model-filtered summary does not count it: the
  unexplained part of the bill is never assigned to a model by guesswork.
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
  The spend tile and turn rows add provider-reported and estimated dollars and
  mark the estimate with `~`, for example `$1.5000 + ~$0.2500`.
  Missing dimensions read **not recorded**; estimate flags remain in telemetry without an `est` suffix, and
  invocation coverage separates exact, estimated, and unknown measurements.
- Routing shows the recommendation, policy confidence, selected and
  recorded provider models, latency, and fallbacks. Distinct call IDs count policy calls;
  subsequent turns can reuse a session route without another policy request.
- Reported and estimated costs remain separate. When a turn has neither, the
  view shows a `~`-prefixed list-price figure (`list_price_usd`) computed from
  `bobi/costs.py` `PRICE_TABLE`; it is never added to recorded spend.
- List prices come from `bobi/model_prices.json`, generated from the official
  pricing pages (platform.claude.com and developers.openai.com, both served as
  Markdown) by `scripts/update_prices.py`. The **Update model prices** workflow
  runs it weekly and opens one pull request when a price changes; the script
  refuses to write when a page stops parsing, and a model that drops off a
  page keeps its last price. The pull request is opened with `GITHUB_TOKEN`,
  which does not trigger CI, so a maintainer re-runs the checks (or closes and
  reopens the PR) before merging. Models the pages never listed (some retired
  or codex-only ids, Google) keep hand-kept fallback prices in
  `bobi/costs.py`. A model with no exact entry (after alias, date and `[1m]`
  normalisation) has no list price and renders as token volume; no family
  name is guessed. Estimates bill cache reads at the cached rate and cache
  writes at their own rate: 5-minute writes at 1.25x input and 1-hour writes at
  2x on Claude (the official 5m/1h columns), the published cache-write rate or
  plain input on OpenAI. Checked against 25 Claude turns of a live agent, these
  estimates equal Claude Code's own reported cost on 24; the remaining turn
  differs because Claude Code prices Sonnet 5.5 cache reads at $0.20 where the
  official page lists $0.10.
- A **routed turn** is one whose latest decision is an enforced policy pick
  with no fallback. Shadow turns and fallback turns (including the turns after
  a picked model failed and the session reran on the control model) ran the
  control model, so they are not routed and carry no saving. `routing` counts
  them as `fallback_turns` and `shadow_turns`; with no routed turn the card
  names both counts instead of the savings figures.
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
  Savings**; the flagship's own row reads *Baseline*. Models are shown by
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
  picks, per-pick `routed_cost_usd` / `ceiling_cost_usd`, and the set's own saving; a pick
  outside the candidate list is appended with `off_list: true`. Claude Code aliases (`opus`, `sonnet`, `haiku`) and a
  context suffix (`claude-opus-5-5[1m]`) price as the current model.
- The header chip reads the summary's collector health: `collector: live` only
  while the collector reports `running` with a heartbeat under 30 seconds old
  (`collector.live`); otherwise it names the state (`stale`, `degraded`,
  `stopped`, `not running`), or `unknown` when the summary could not be read.
- The web view shows no telemetry health strip; collector state, coverage,
  import lag and reconciliation errors stay in the `summary` payload and the
  CLI.
- Recent turns default to one card per session (thread). The header holds the
  first user message (or the lifecycle label), a **user prompt** / **system
  lifecycle** tag, agent and role, the Slack origin, and the session ID; turn
  rows repeat none of these and show the step instead (initial request,
  follow-up, assistant response, or the lifecycle step such as **Inbox Event
  Processing**). Tool use is the step's detail line (`Bash +1 more · 10.8s`,
  or `2 tool calls` when the tool names were not captured), never its name. **Flat** lists turns
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
  A turn's transcript window is read from the newest end of the file and stops at the first record more than 15 minutes older than the window (records are appended, not time-ordered: hook attachments carry earlier timestamps), or after 16 MiB or 250 ms per file, keeping the newest part it read; there is a one-second overall enrichment budget. A long transcript therefore still shows its latest turns, and oversized or unavailable transcripts do not block the manager or alter telemetry.
  The local authenticated turns endpoint adds 60-character prompt and response previews, tool counts, canonical reported/estimated cost totals, and recorded Slack conversation references when available; unknown origins fall back to the trigger kind.
  Conversation content is not added to SQLite, Admin RPC, or supervisor MCP responses.

Authenticated, read-only endpoints under `/api/agents/{name}/metrics`:

| Endpoint | Parameters | Response |
|---|---|---|
| `summary` | Required ISO `from`, `to`; optional `model`, `session_name` | Canonical totals, coverage, UTC buckets, routing counts/models, `routing.savings`, collector health with `live` |
| `turns` | Same range/filters; optional `session_id`, `limit`, `cursor` | Newest-first turns, usage, invocations, allowlisted policy fields, `costs` (`reported_cost_usd`, `estimated_cost_usd`, `list_price_usd`), local conversation previews, `next_cursor` |
| `sessions` | Required `from`, `to`; optional `limit`, `cursor` | Distinct recent lifecycles (`session_id`, name, start/end timestamps, status), `next_cursor` |
| `sessions/{id}` | Optional `limit`, `cursor` | Existing session detail and bounded turn page |
| `turns/{id}` | None | Turn detail, session identity, canonical `best_usage`/`invocation_usage` (each row with `list_price_usd`)/`usage_totals`, allowlisted policy fields, local-only `conversation` (`input`, `response`, scoped `entries`, `origin`, `status`, `truncated`) |
| `jev-config` | None | Current routing configuration; an installed agent without an active experiment returns HTTP 200 with `enabled: false`, `mode: off` and no invented models. Also `native_models` (the picker list for `agent_brain`), `config_warnings`, `credential_configured`, and `config_error` naming why a saved policy is not active |

`routing.savings` shape (USD, list prices; totals are null when nothing could
be priced; `route_sets` has one entry per candidate set, candidates shortened
here):

```json
{"routed_cost_usd": 0.0075, "ceiling_cost_usd": 0.009, "floor_cost_usd": 0.0045,
 "saved_usd": 0.0015, "saved_pct": 16.7, "premium_pct": 66.7, "captured_pct": 33.3,
 "ceiling_model": "sonnet", "floor_model": "haiku",
 "priced_turns": 3, "unpriced_turns": 0, "escalated_turns": 2, "unpriced_models": [],
 "route_sets": [{"candidates": [{"model": "haiku",
   "prices": {"input_per_mtok": 1.0, "cached_input_per_mtok": 0.1, "output_per_mtok": 5.0},
   "turns": 1, "off_list": false, "routed_cost_usd": 0.0015, "ceiling_cost_usd": 0.003}],
  "control_model": "haiku", "floor_model": "haiku", "ceiling_model": "sonnet",
  "turns": 3, "routed_cost_usd": 0.0075, "ceiling_cost_usd": 0.009,
  "floor_cost_usd": 0.0045, "saved_pct": 16.7}]}
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
```

Admin aliases and corresponding read-only MCP tools:

| CLI alias | Wire command | MCP tool |
|---|---|---|
| `metrics_summary` | `usage` | `bobi_usage_summary` |
| `metrics_session` | `usage_session` | `bobi_usage_session` |
| `metrics_turn` | `usage_turn` | `bobi_usage_turn` |

Session/turn aliases require supervisor 0.4.0 or newer; summary retains legacy
compatibility: when the fine-grained block is refused (`metrics_not_ready`,
`metrics_busy`, `query_too_slow`, or a range over 31 days) it answers with the
legacy fold alone, and malformed arguments still fail.
Queries have bounded range (31 days), rows (200), response
size (512 KiB), and execution time. Saturation returns `metrics_busy` (retry);
a query that misses its one-second deadline returns `query_too_slow` (do not
retry; narrow the range or filters); missing storage returns `metrics_not_ready`.
A query's cost follows the turns in its range, not the size of the history:
`best_usage` picks each turn's best rows through the turn index. The
dashboard summary over the widest preset is the heaviest query: on a laptop it
takes about 0.7 s for 11,000 routed turns in the range (four calls each), and
its cost grows with the turns in range. An agent running more than about
15,000 turns a month should expect `query_too_slow` on "All (up to 31d)" and
use the 7-day preset instead. Session pagination uses
filter-bound cursors. Protect the fleet operator token: `bobi admin` refuses an
`--url` that is neither `https` nor a loopback address, since every request
carries it.

```bash
bobi admin metrics_turn --url "$BOBI_ADMIN_URL" --fleet "$BOBI_FLEET"   --instance "$BOBI_INSTANCE" --args '{"turn_id":"<turn-id>"}' --wait --json
```

`bobi admin` prints the result envelope and exits `0` on success (or once the
command is submitted, without `--wait`), `1` when the command finished with
`status: "error"` or the request was rejected, and `2` when `--wait` reached
`--timeout` before the command resolved.

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
unambiguous transcript format; it never guesses between both formats. A Codex
rollout is read by its shape, not its `cli_version`: per-turn
`token_usage_record` rows, or cumulative `token_count` snapshots turned into
per-turn deltas (the shape of the 0.144.5 CLI the image pins, and of 0.159).
A rollout mixing both keeps every turn: a record wins for its turn, and a
finished turn without one takes its snapshot delta. A rollout with a completed
turn but neither shape is a contract error, retried like any other; a turn
still running is not.

Background reconciliation makes at most three failed attempts per turn, keeps
the counts in the `reconcile_attempts` table, and reports only the number of
exhausted turns in collector health. A pass stops after five seconds and the
next pass continues; each transcript is parsed once per pass. A turn still
running is refused, because its transcript is still growing. Rerunning a
recovery after the transcript grew adds the new calls: every run writes as its
own producer, and the turn row's event is named by its content. Restore missing
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

Invalid experiment JSON/schema, unavailable policies, and configured routing
with unavailable telemetry emit structured `jev_routing_fallback` warnings and
explicit failure codes. With telemetry available, failures before a decision
are recorded in `sessions.metadata_json.router_fallback`; invalid
configurations do not fabricate decisions. Policy failures after the decision
are recorded on `router_decisions.fallback_reason`.

Warnings omit raw configuration, credentials, and exception messages.
Metrics/Admin/MCP exclude prompt/response bodies, tool payloads, secret values,
and full paths. TypeSafe input follows the configured
privacy scope; do not enable task-text sharing without approval.

## Out of scope

There are no benchmark, live-matrix or soak scripts, no SRM or
causal/intent-to-treat aggregates, no hotspot or experiment Admin/MCP queries,
no external outcome helpers, and no rebuild/prune/retention queues. The
supported surface is the operational summary/session/turn queries, exact
accounting, reconciliation, and JEV policy evaluation.

Run offline regression checks:

```bash
.venv/bin/python -m pytest tests/metrics/ -q --timeout=30 --basetemp=/tmp/bobi-metrics
```

Real Slack/provider tests are separate, opt-in checks and may consume quota.
