# JEV Model Router: Architecture and Data Flow

How Bobi picks a model for a fresh brain session with TypeSafe AI's System One
policy ("JEV"), and how each decision is recorded. Metrics storage, queries
and the dashboard are described in `docs/FINE_GRAINED_METRICS.md`.

## 1. Rules

| ID | Rule |
|---|---|
| D1 | The model is chosen **once per fresh brain session**, before it connects. Rotation, reconnect and recovery keep the session's model; there is no per-turn switching. |
| D2 | Routing runs only at the session, subagent and workflow-start entry points listed in section 3. |
| D3 | Every eligible session calls the policy. In `shadow` mode the control model runs and the recommendation is recorded; in `enforce` mode the recommendation runs when it passes the guards. |
| D4 | Eligibility is checked before the policy is called. Ineligible sessions run their configured model and produce no decision. |
| D5 | Any policy failure runs `control_model` and records why. Routing never fails a session. |
| D6 | Prompt egress is opt-in, redacted and size-capped. |
| D7 | Decisions are sticky: a resumed session or workflow reuses its saved decision without calling the policy. |

Non-goals: per-turn routing; sessions that start without a prompt (inbox-only
sessions keep their configured model); monitor `script_cache` runs, which have
no brain session.

## 2. Configuration

The policy lives in one environment variable, `BOBI_METRICS_EXPERIMENT_JSON`,
normally written to the installed runtime's `run/.env` by the agent page's JEV
editor. Metrics must be enabled (`BOBI_METRICS_MODE=enabled`) and the TypeSafe
key supplied as `TYPESAFE_API_KEY` in the same `.env` or the environment.

```json
{
  "mode": "shadow",
  "control_model": "sonnet",
  "candidate_models": ["haiku", "sonnet", "opus"],
  "prompt_egress": "none",
  "instructions": "Choose the least costly candidate that can reliably complete the task. When task information is insufficient, choose sonnet.",
  "criteria": {
    "haiku": "Small, well-defined changes with low reasoning complexity.",
    "sonnet": "General engineering work or insufficient task information.",
    "opus": "Complex reasoning or cross-system architectural work."
  },
  "roles": ["engineer", "reviewer"]
}
```

| Field | Default | Rule |
|---|---|---|
| `mode` | `enforce` | `shadow` or `enforce` |
| `control_model` | required | Must be one of `candidate_models`; the fallback model |
| `candidate_models` | required | Non-empty, unique; the models the agent's own brain runs |
| `instructions` | required | Non-empty routing instructions sent to the policy |
| `criteria` | one generic line per candidate | Must cover exactly the candidates |
| `prompt_egress` | `none` | `none` (features only) or `redacted` (also the redacted task text) |
| `roles` | `director`, `engineer` | Allowlist of roles that are routed |
| `entry_points` | all four | Subset of `subagent_phase`, `subagent_persistent`, `subagent_supervised`, `workflow_start` |
| `brain` | `auto` | `auto` follows the agent's brain; `claude` or `codex` pins it |
| `min_confidence` | `0.85` | `[0, 1]` |
| `deadline_ms` | `3000` | `50`-`3000`, covers capacity wait and the HTTP call |
| `max_prompt_bytes` | `8192` | `1`-`65536`, cap on the redacted task text |
| `policy_version` | `jev-1.13.0` | A pinned TypeSafe model; `jev-latest` and `jev-preview` are rejected |
| `endpoint` | `https://api.typesafe.ai/v1/systemone` | HTTPS, no credentials or query |
| `experiment_id`, `cohort`, `router_name`, `router_version`, `feature_schema_version` | `jev-routing-v1`, none, `jev-router`, `v1`, `jev-features-v1` | Labels copied onto each decision row |

`ExperimentConfig.from_mapping` (`bobi/metrics/router.py`) validates the config
and builds the policy settings; constructing `TypeSafePolicy` checks the
instructions, criteria, endpoint and version pin. Unknown fields are rejected.
The former long form (`variants` and a nested `policy` object) is rejected
with `long-form experiment config is no longer supported; save from the editor
to rewrite it`. The editor shows that message as the config error, and saving
from the editor writes the short form over the old line.

The editor always saves the minimal form (`ExperimentConfig.to_mapping`): the
key fields plus any setting or label that differs from its default. Setting the
mode to `off` empties `BOBI_METRICS_EXPERIMENT_JSON` and keeps the config in
`BOBI_METRICS_EXPERIMENT_DISABLED_JSON` (a real key, so any `.env` rewrite such
as a setup credential save keeps it) for re-enabling; routing never reads that
key. Deleting removes both. A saved config applies when
the agent restarts.

The **config fingerprint** is a SHA-256 of the minimal form without
`endpoint`. Any other change (mode, candidates, instructions, criteria, roles,
thresholds, labels) changes it and makes saved sticky routes stale.

## 3. Entry points

All entry points build a `RoutingContext` and call the single async
`resolve_route()` in `bobi/metrics/routing.py`, then apply the returned model.
`Session._run` (`bobi/session.py`) decides before its resume lookup, under the
entry point its caller named; a session started any other way is not routed.

| Entry point | Caller | Prompt used |
|---|---|---|
| `subagent_phase` | `run_phase_blocking` (`bobi/subagent.py`) through `Session` | Phase prompt |
| `subagent_persistent` | `run_persistent_agent` through `Session` | Task |
| `subagent_supervised` | `_run_agent_supervised`, before its raw brain session | Phase prompt |
| `workflow_start` | Workflow run start (`bobi/workflow/orchestrator.py`) | The run's background block (launch input and scopes) plus the rendered prompt of the first prompt step |

`RouteOutcome` carries the model to run and the admitted `RouterDecision`, or no
decision with an optional `reason` when routing did not decide.

## 4. Flow

```
 RoutingContext
   1. eligibility ───────────── ineligible ───────► configured model, no decision
   2. load + validate config ── invalid ──────────► configured model, routing_config_invalid
   3. sticky reuse (not fresh) ─ match ───────────► saved model, status "reused"
                               └ no match ────────► saved session's model (else configured), routing_sticky_stale
   4. features + redaction
   5. call_policy(): breaker, capacity, deadline, TypeSafePolicy.decide()
   6. guard_result(): candidates, schema, version, confidence
   7. admission (session.recorded) ── rejected ───► control model, admission_rejected
   8. persist route (session route file; workflows keep it in their checkpoint)
```

### 4.1 Eligibility

Checked in order; the first match runs the configured model.

| Order | Condition | Recorded |
|---|---|---|
| 1 | `explicit_model` (launch flag, step model, explicit argument) | Nothing; an operator's choice always wins |
| 2 | Metrics disabled while a config is set | `routing_metrics_unavailable` |
| 3 | No config | Nothing |
| 4 | Config invalid | `routing_config_invalid` |
| 5 | `brain` pinned to another brain | `routing_brain_mismatch` |
| 6 | Entry point or role outside the allowlists | Nothing |
| 7 | Not fresh and a saved session or checkpoint record exists | Sticky reuse (4.2) |
| 8 | Empty prompt, or a resumed session with an existing transcript | Nothing |

### 4.2 Sticky reuse

| Scope | Store | Written |
|---|---|---|
| Session and subagents | `<sessions>/<name>.route.json` (`bobi/sdk.py`, atomic write) | After admission, before connect |
| Workflow run | `_runtime.route` in the run's checkpointed scopes | By the workflow checkpoint |

A saved route is reused only when all of these hold:

- it was saved under the same brain label, and a saved provider session also
  ran under that brain;
- its `config_fingerprint` equals the current config's;
- its `model_selected` is still in `candidate_models`;
- when a provider session is saved, that session ran `model_selected` (an
  operator who later relaunched with another model keeps that model).

Otherwise it records `routing_sticky_stale`; an error while reading the record
records `routing_sticky_invalid`. Either way a resumed session keeps the model
its saved provider session ran (a config edit never switches a session
mid-transcript); with no resumable session under this brain it runs its
configured model. Reuse re-admits the saved decision with
`policy.status = "reused"` and makes no policy call. A workflow reads only its
own checkpoint record, never the session route file.

`save_session_id(name, "")` deletes the route with the session id. Internal
rotation and stale-token recovery pass `preserve_route=True` and keep it.

### 4.3 Features

`build_features` (`bobi/metrics/features.py`) builds the policy state in memory:

| Feature | Notes |
|---|---|
| `prompt_bytes` | Always sent |
| `entry_point`, `role`, `brain`, `phase`, `workflow_name`, `step_name` | Must match `[a-zA-Z0-9_.-]{0,128}`; empty outside workflows |
| `task` | Only with `prompt_egress: redacted` |

The task text is prepared by stripping any `## Long-Term Memory` section,
applying `redact_secrets` (`bobi/redact.py`), replacing paths inside the repo
with relative ones and paths outside it with `[path]`, and truncating to
`max_prompt_bytes` as head + tail on a UTF-8 boundary. Transcripts, system
prompts, file contents and environment variables are never sent.

### 4.4 Policy call

`TypeSafePolicy` (`bobi/metrics/policies/typesafe.py`) sends one
`POST {endpoint}` with `Authorization: Bearer $TYPESAFE_API_KEY` and a body of
`model` (the pinned version), `state` (the features) and one choice question
carrying the instructions and criteria. It uses a per-call `httpx` client with
no retries or redirects, caps the request at 128 KiB and the response at 64 KiB,
and requires a choice among the candidates with finite probabilities summing to
one, a finite confidence and integer usage. The vendor reports tokens, not cost,
so `cost_usd` stays null.

`call_policy` (`bobi/metrics/policy.py`) wraps the call:

- **Circuit breaker**, one per endpoint and shared across threads:

  | State | Behavior | Transition |
  |---|---|---|
  | closed | Calls go through | Opens after 5 consecutive failures, or a 50% failure rate over the last 20 calls (at least 10) |
  | open | No call; `policy_circuit_open` | Half-open after the cooldown: 30 s, doubling per failed probe, capped at 10 min |
  | half-open | One probe; concurrent calls fall back | Probe success closes and resets; failure reopens |

  A missing key or HTTP 401/403 opens the breaker for 600 s and logs once.
  Low-confidence and version-drift answers are valid and do not count as
  failures. Breaker health is reported under `router_policy` in metrics health.
- **Capacity**: at most 8 calls in flight per process; waiting counts against
  the deadline, and cancellation releases both the slot and probe ownership.
- **Call identity**: a `call_id` is created only when the adapter is invoked, so
  capacity timeouts and open-breaker skips have none.

### 4.5 Guards

`guard_result` validates the answer schema first (finite confidence,
non-negative cost, string fields), then in order:

1. `model` is a candidate, else `policy_invalid_response`;
2. `model_version` is on the pinned release line, else `policy_version_drift`.
   A patch bump or build suffix (`jev-1.13.2`, `jev-1.13.0+b7` against
   `jev-1.13.0`) matches; a minor or major change does not;
3. `confidence >= min_confidence`, else `policy_low_confidence`.

Low-confidence and drift answers still record the recommendation and reported
cost; malformed answers record neither.

## 5. Decision record

One `router_decisions` row is projected per turn of a routed session from the
session's admission metadata (`session.recorded.metadata_json.router_assignment`).
The schema is unchanged; assignment columns from older builds stay readable.

| Column | Value |
|---|---|
| `experiment_id`, `cohort`, `router_name`, `router_version`, `policy_version`, `feature_schema_version` | From config |
| `variant_id` | Always `treatment_jev` |
| `assignment_status` | Always `assigned` |
| `assignment_unit`, `assignment_key_hash`, `assignment_algorithm`, `router_score` | Not written (filled only by older builds) |
| `candidate_models_json`, `control_model` | From config |
| `model_selected` | The model actually executed (control in shadow mode) |
| `router_reason` | `policy_selected`, `policy_shadow` or `policy_fallback` |
| `router_latency_ms` | Total resolve time, including the policy call |
| `fallback_reason` | Section 6; null when the recommendation ran or in shadow mode |
| `metadata_json` | `config_fingerprint`, `entry_point`, `policy` |

```json
{
  "config_fingerprint": "…",
  "entry_point": "subagent_phase",
  "policy": {
    "name": "typesafe-jev", "version": "jev-1.13.0", "mode": "shadow",
    "egress": "none", "status": "decided", "call_id": "…",
    "recommended_model": "haiku", "confidence": 0.94, "cost_usd": null,
    "latency_ms": 212.4, "breaker_state": "closed", "redactions": 0
  }
}
```

`policy.status` is `decided`, `failed`, `skipped` (no call made) or `reused`.

## 6. Fallbacks

After a decision, these run `control_model` and are stored as the row's
`fallback_reason`:

| `fallback_reason` | Cause |
|---|---|
| `policy_timeout` | Deadline exceeded, including capacity wait |
| `policy_unavailable` | Transport error, HTTP 5xx or 429 |
| `policy_unauthenticated` | Key missing, or HTTP 401/403 |
| `policy_circuit_open` | Breaker open, or a half-open probe already in flight |
| `policy_invalid_response` | Malformed answer or a model outside the candidates |
| `policy_version_drift` | Answered by another release line |
| `policy_low_confidence` | Confidence below `min_confidence` |
| `route_persistence_failed` | The route file could not be written; the decision is re-admitted on control |
| `candidate_failed` | The policy's model failed to start or failed its first turn and the session reran on control (6.1). Saved on the route, so it appears on the rows of later resumes; the failing session's own row keeps the policy's model |

Without a decision, the session runs its configured model and the reason is
stored in the session's `router_fallback` metadata: `routing_brain_mismatch`,
`routing_sticky_stale`, `routing_sticky_invalid`, `routing_config_invalid`,
`routing_policy_unavailable`, `routing_metrics_unavailable`,
`routing_unavailable`. `admission_rejected` (the producer refused the session
record, so nothing is enrolled) can only be logged.

The no-decision reasons and `admission_rejected` are also written as JSON
lines to `state/jev_routing_fallback.log` and the agent log; policy fallbacks
live on the decision row.

### 6.1 Failed routed model

When the policy moved a session off its control model and that model then
fails:

- **Cannot start** (connect error): `candidate_failed` logs a
  `jev_candidate_failed` incident (attempted model, brain, provider, redacted
  error, fallback model) and the session reconnects on `control_model`.
- **First turn fails**: `may_rerun_on_control` allows one rerun on
  `control_model` only when no tool ran in any attempt of the turn (a rerun
  would repeat side effects; a turn lost without a terminal result counts as
  having run tools, and Codex error results carry the tools they ran), the
  error is not one the control model shares (`NO_MODEL_FALLBACK_KINDS`:
  authentication, credits exhausted, turn cap), and the decision was made fresh
  rather than reused from a saved session.

After either rerun the saved route (the session route file, or a workflow's
checkpoint) is rewritten to `control_model` with `fallback_reason:
candidate_failed`, so the next resume reuses the control model instead of
finding a stale route.

## 7. Privacy and credentials

- Prompt text leaves the host only with `prompt_egress: redacted`, after the
  redaction in 4.3. Features are still external egress; obtain the vendor's
  retention approval before enabling routing.
- Task text, policy responses and free-text reasons are never stored or logged;
  `tier` is stored only if it matches `^[a-z0-9_]{1,64}$`.
- `provider_subprocess_env` removes `BOBI_METRICS_EXPERIMENT_JSON`,
  `BOBI_METRICS_EXPERIMENT_DISABLED_JSON` and `TYPESAFE_API_KEY` from Claude and Codex subprocesses. Claude launch options
  blank them explicitly because the SDK merges options over the parent
  environment. A non-secret marker names the blanked variables so root-bound
  Bobi CLI children restore them from the runtime `.env`; provider launches
  scrub them again.
- The JEV config and sandbox endpoints return whether the key is set, never the
  key itself.

## 8. Operator surfaces

| Surface | Purpose |
|---|---|
| `GET/POST/DELETE /api/agents/{name}/metrics/jev-config` | Read, save (short form, `run/.env` at mode 0600) or remove the config. Returns `config_error` for an unreadable or rejected config and `config_warnings` for candidates the agent's brain cannot run. A config supplied by the process environment rather than `.env` refuses edits with `config_environment_override`. |
| `POST /api/agents/{name}/metrics/test-route` | Sandbox: one TypeSafe call with the saved config, showing features, outgoing payload, raw answer and the guarded effective model. Writes nothing. |
| `POST /api/agents/{name}/metrics/test-run` | Runs one prompt on a candidate model through the agent's native Claude brain with no tools, no MCP and no settings. Not recorded in metrics. |
| `state/jev_routing_fallback.log` | One JSON line per no-decision fallback, rejected admission or failed routed model |

## 9. Verification

- Unit: `tests/metrics/test_routing.py` covers config validation, the long-form
  rejection, fingerprints, guards, breaker, redaction, environment scrubbing,
  sticky reuse, fallbacks and the rerun rule. Its scripted-turn matrix runs all
  four entry points against a loopback TypeSafe server, including timeout, 5xx,
  401/403, 429, malformed, invalid-model, low-confidence and version-drift
  faults, and checks the projected decision rows.
- Webapp: `tests/test_webapp_server.py` covers the editor and sandbox contracts.
- Real Claude (opt-in, makes a provider request):
  `BOBI_JEV_LIVE_CLAUDE=1 .venv/bin/pytest tests/integration/test_jev_claude_routing.py -q`.
  A skipped run is not evidence.
- Live shadow check: in an isolated `BOBI_HOME`, enable shadow mode with
  `prompt_egress: none`, launch an eligible session, confirm the control model
  ran and the decision row carries the recommendation, resume it and confirm
  `reused` with no second vendor call, and search the spool, logs and database
  for a unique task canary.

## 10. Open questions

- **Q1.** Account-specific TypeSafe data-processing and retention approval is
  still required before live egress.
- **Q2.** The API reports tokens, not invoiced cost; `policy.cost_usd` stays
  null until a billing contract supplies it.
- **Q3.** Whether the manager role should ever be in scope. Its first prompt is
  a bootstrap brief, so the default allowlist excludes it.
