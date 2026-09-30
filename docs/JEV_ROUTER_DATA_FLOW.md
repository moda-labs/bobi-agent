# JEV Model Router: Architecture and Data Flow

Status: specification, not yet implemented. Branch `feat/fine-grained-metrics`.
Last revised 2026-10-01.

This document specifies how Bobi selects a model with a semantic routing
policy (TypeSafe AI's System One model, "JEV") and how that choice is
measured. It builds on the experiment machinery described in
`docs/FINE_GRAINED_METRICS.md` ("Enable an experiment") and replaces the
earlier per-turn draft of this file.

## 1. Decisions

| ID | Decision |
|---|---|
| D1 | The model is chosen **once per fresh brain session**, before the session connects. There is no per-turn or mid-session model switching. Rotation, reconnect, and recovery reuse the session's model. |
| D2 | Routing runs at exactly three entry points: session initiation (`bobi/session.py`), subagent launch (`bobi/subagent.py`), and workflow run start (`bobi/workflow/orchestrator.py`). A fourth point, a workflow step that starts a fresh session because its agent changes, is Phase 3. |
| D3 | Randomization and policy are separate levels. The existing HMAC assignment (`hmac-sha256-u64-v1`) assigns an **arm** (`control` or `treatment_jev`). Only inside the treatment arm does the policy pick a model. `variant_id` is always the arm. |
| D4 | Eligibility is decided **before** assignment. Sessions with an explicit model, no routing input, an out-of-scope role or entry point, a non-matching brain, or a pre-existing transcript are not enrolled and produce no router decision. |
| D5 | `router_score` keeps its meaning (the HMAC bucket). Everything the policy returns lives under `metadata_json.policy`. The schema needs no new columns. |
| D6 | Policies plug in through a generic `ModelPolicy` protocol and a name registry. The TypeSafe adapter is an optional extra. `bobi/` stays vendor-neutral. |
| D7 | A process-wide circuit breaker (closed / open / half-open) protects every policy call. Any failure falls back to `control_model` and is recorded. The session never fails because of routing. |
| D8 | Sending prompt text to the policy is opt-in, redacted, and size-capped. The control arm never sends anything. The policy's free-text reason is not stored by default. |
| D9 | Decisions are sticky. A resumed session or suspended workflow reuses its recorded decision instead of routing again. |
| D10 | Rollout starts in `shadow` mode: the policy is called and recorded, but the control model executes. `enforce` is a later config change, not a code change. |

### Non-goals

- Per-turn routing, or a routing call inside `_process_message`.
- Routing sessions that start without a prompt (inbox-only sessions). Holding
  back `connect()` until the first inbox message would break `Session.start()`'s
  readiness contract (#1016).
- Routing monitor `script_cache` runs, which have no brain session.
- Replacing the HMAC assignment or the intent-to-treat analysis contract.

## 2. Architecture

```
 entry point (E1 session / E2 subagent / E3 workflow)
        │  builds RoutingContext
        ▼
 ┌─────────────────────── resolve_route() ─ bobi/metrics/routing.py ──────────┐
 │ 1. eligibility gate ──── ineligible ─────────────► configured model,       │
 │        │                                            no decision row        │
 │ 2. sticky lookup ─────── match ──────────────────► recorded model          │
 │        │                                                                    │
 │ 3. HMAC arm assignment (existing route())                                  │
 │        ├── control ───────────────────────────────► control_model          │
 │        └── treatment_jev                                                   │
 │               4. circuit breaker ── open ────────► control_model (fallback)│
 │               5. feature build + redaction                                 │
 │               6. ModelPolicy.decide() under deadline                       │
 │               7. policy guard (candidates, confidence, version, mode)      │
 │ 8. admission: session.recorded via producer put_nowait()                   │
 │        └── rejected ────────────────────────────────► control_model,      │
 │                                                        not enrolled         │
 │ 9. persist decision (route.json / workflow _runtime.route)                 │
 └────────────────────────────────────────────────────────────────────────────┘
        │  RouteOutcome(model, decision | None)
        ▼
 load_resumable_session_id(name, model) → make_session(options.model) → connect
        │
        ▼
 turns: TurnObservation materializes the admitted decision per turn (no routing)
        │
        ▼
 outcomes + usage → projection → `bobi admin metrics_experiment`
```

All three entry points call the same async function, `resolve_route()`. Like
`continuation_token`, it is the only place that decides a route. Entry points
only build the context and apply the returned model.

## 3. Entry points

### 3.1 Shared input: `RoutingContext`

```python
@dataclass(frozen=True)
class RoutingContext:
    entry_point: str          # "session_start" | "subagent_phase" |
                              # "subagent_persistent" | "subagent_supervised" |
                              # "workflow_start" | "workflow_agent_change"
    session_name: str         # stable Bobi session name
    configured_model: str     # role > team default, "" = provider default
    explicit_model: bool      # launch flag / step.model / explicit arg
    prompt: str               # first prompt for this session, may be ""
    role: str
    fresh: bool               # caller asked for a new transcript
    run_key: str = ""
    experiment_subject: str = ""
    phase: str = ""
    workflow_name: str = ""
    step_name: str = ""
```

`RouteOutcome` carries the model to run (`""` means provider default) and the
admitted `RouterDecision`, or `None` when the session is not enrolled.

### 3.2 E1: session initiation (`bobi/session.py`)

- Remove the routing block in `Session.__init__` (currently lines 356-368).
  At construction time no prompt exists, and `__init__` runs on the caller's
  thread.
- `Session` gains an optional `routing: RoutingContext | None`. `None` means
  "do not route", which is the default for callers that pick the model
  themselves.
- In `Session._run`, **before** `load_resumable_session_id(...)`:
  1. `outcome = await resolve_route(ctx with prompt=startup_prompt, fresh=self._fresh)`.
  2. When `outcome.model` differs from the configured model, set
     `self._extra_options["model"] = outcome.model`.
  3. Continue with the existing resume lookup, `_make_brain_session`, and
     `connect()`.
- The route resolves on the session thread's own event loop, inside the
  `start()` timeout, so the caller's thread is not blocked.
- `_make_brain_session` for rotation, reconnect, and recovery reads the
  same `_extra_options` and never routes again.

### 3.3 E2: subagent launch (`bobi/subagent.py`)

`_resolve_launch_model` stays a pure, synchronous resolver from config. It
has no prompt and is also used as the reference for effort resolution.
Routing sits directly after it, in each launch path:

| Path | Current routing call | New behavior |
|---|---|---|
| `run_phase_blocking` | none (Session-internal) | Pass `RoutingContext(entry_point="subagent_phase", phase=phase, run_key=run_key, explicit_model=False)`; E1 routes with the phase prompt. |
| `run_persistent_agent` | `resolve_experiment_model` at line 889 | Remove the call. Pass `RoutingContext(entry_point="subagent_persistent", explicit_model=bool(model))`; E1 routes with `task`. |
| `_run_agent_supervised` | `resolve_experiment_model` at line 409 | Replace with `await resolve_route(RoutingContext(entry_point="subagent_supervised", prompt=prompt, ...))` before `load_resumable_session_id` (line 413). This path uses a raw brain session, not `Session`. |

All subagents in one workflow run share `run_key`, so they share an arm.
The policy can still pick a different model per subagent, because each
subagent is its own fresh session.

### 3.4 E3: workflow run start (`bobi/workflow/orchestrator.py`)

- `_effective_step_model` returns only the configured model (launch flag >
  step override > role > team default). It no longer calls
  `resolve_experiment_model`.
- At run start, after `first_prompt_model` is computed and before
  `current_model` is chosen:
  - If `_runtime.route` exists in the restored scopes, reuse its model (D9).
  - Otherwise, call `resolve_route` with `entry_point="workflow_start"`, the
    rendered prompt of `first_prompt_step`, and
    `explicit_model = bool(launch_model or first_prompt_step.model)`.
- `_checkpoint` writes the admitted decision into `_runtime.route` next to
  `model`, so a suspend/resume or retry does not route again.
- Same-agent steps keep today's behavior: the step's configured model is
  compared against `current_model`, and the routed model does not trigger a
  switch. The comparison uses the routed model as the session's model for
  steps that do not set their own.

**Phase 3, E3b (agent-change boundary).** When `next_agent != current_agent`,
the switch branch already starts a fresh session with no continuation token.
At that point Bobi may route again with `entry_point="workflow_agent_change"`
and the step's rendered prompt, keeping the run's arm. This needs a per-segment
decision: a new raw event, `router_segment.recorded`, keyed by
`(session_id, segment_ordinal)`. Projection attributes each turn to the latest
segment that started at or before it. It is deferred because it changes the
projection rule in `bobi/metrics/projection.py`.

## 4. Eligibility gate (D4)

Checked in order. The first match returns the configured model and records
nothing. These are covariates known before assignment, so excluding them does
not bias the arm comparison.

| Order | Condition | Why |
|---|---|---|
| 1 | Metrics disabled, no experiment loaded, or producer not started | Existing fail-open contract |
| 2 | `ctx.explicit_model` | An operator's explicit choice always wins |
| 3 | Active brain `!= policy.brain` | Candidate models are brain-specific |
| 4 | `ctx.entry_point` or `ctx.role` not in `policy.scope` | Enrollment is an allowlist |
| 5 | `ctx.prompt` is empty after stripping | No routing input |
| 6 | Not `fresh`, a saved session id exists, and no matching sticky record | A pre-existing transcript; routing it could force a fresh start |

This fixes a current bug: `MetricsRuntime.resolve_model` calls
`route(requested_model="")`, so an explicit `--model` is silently replaced by
the treatment model today. After this change, explicit models are never
enrolled.

Ineligible sessions are recorded in `MetricsRuntime` so
`TurnObservation.start` does not route them. `TurnObservation.start` no longer
calls `route()` at all; it only materializes an admitted decision.

## 5. Arm assignment and policy decision (D3)

### 5.1 Arm

This is the unchanged `route()` in `bobi/metrics/router.py`. The key order is
explicit `experiment_subject`, then `run_key`, then session name. The control
arm executes `control_model` and never calls the policy. The treatment arm
runs steps 4-7 below.

### 5.2 `ModelPolicy` protocol (D6)

```python
@dataclass(frozen=True)
class PolicyRequest:
    feature_schema_version: str
    features: Mapping[str, object]      # see 5.3
    candidate_models: tuple[str, ...]
    control_model: str
    pinned_version: str

@dataclass(frozen=True)
class PolicyResult:
    model: str
    confidence: float | None            # [0, 1]
    tier: str | None                    # bounded enum-like code
    model_version: str                  # version that actually answered
    reason_text: str | None             # dropped unless store_reason_text
    cost_usd: float | None              # policy call cost, when reported

class ModelPolicy(Protocol):
    name: str
    secret_env_names: tuple[str, ...]   # scrubbed from provider subprocesses
    async def decide(self, request: PolicyRequest, *, timeout_s: float) -> PolicyResult: ...
```

- The registry lives in `bobi/metrics/policy.py`. It maps `policy.name` to a
  factory through the `bobi.model_policies` entry-point group, plus a built-in
  `static` policy used by tests and smokes.
- The TypeSafe adapter is `typesafe-jev`, installed with `pip install
  "bobi[jev]"` and imported lazily. The vendor wire format belongs only to the
  adapter.
- If an adapter is missing or fails to import, routing is disabled with a
  warning, the same way a bad experiment config is handled today.
- Each `Session` runs its own event loop on its own thread. Adapters must
  therefore create their async HTTP client per call or per loop, and never
  share one across loops.

### 5.3 Features (`feature_schema_version = "jev-features-v1"`)

| Feature | Source | Notes |
|---|---|---|
| `task` | `ctx.prompt` after redaction (section 8) | Omitted when `egress.prompt = "none"` |
| `prompt_bytes` | original prompt length | Always sent |
| `entry_point`, `role`, `phase` | `RoutingContext` | |
| `workflow_name`, `step_name` | `RoutingContext` | Empty outside workflows |
| `brain` | active brain kind | |

Not sent: transcripts, system prompts, long-term memory, file contents,
absolute paths, environment variables, the tool list. Features are built in
memory with no disk or network I/O. A change to this table requires a new
`feature_schema_version`, and therefore a new `experiment_id`.

Example adapter request and response, illustrative only:

```json
{"task": "Fix lint errors in bobi/metrics/router.py", "context":
 {"role": "engineer", "entry_point": "subagent_phase", "prompt_bytes": 412},
 "candidate_models": ["haiku", "sonnet"], "version": "jev-2026-q3"}
```

```json
{"recommended_model": "haiku", "confidence": 0.94,
 "complexity_tier": "simple_code_modification", "version": "jev-2026-q3"}
```

### 5.4 Policy guard

Checks run in this order after `decide()` returns. Any failure executes
`control_model` and sets `fallback_reason`.

1. `result.model` is in `policy.candidate_models`, else
   `policy_invalid_response`.
2. `result.model_version == policy.version`, else `policy_version_drift`. A
   server-side model change mid-experiment is a confound, not a detail.
3. `confidence >= policy.min_confidence` (default 0.85), else
   `policy_low_confidence`.
4. `policy.mode == "shadow"`: execute `control_model`, record the
   recommendation, and leave `fallback_reason` NULL. The mode is recorded in
   metadata, so shadow does not inflate the fallback rate.

## 6. Circuit breaker (D7)

There is one breaker per `(policy name, endpoint)`, shared by all threads in
the process through a `threading.Lock`, not an asyncio lock. Its state is not
persisted, so a restart begins closed.

| State | Behavior | Transition |
|---|---|---|
| closed | Calls go through | To open after 5 consecutive failures, or a failure rate of at least 50% over the last 20 calls (minimum 10) |
| open | No call; fallback `policy_circuit_open`, near-zero latency | To half-open after the cooldown: 30 s, doubling on each failed probe, capped at 10 min |
| half-open | Exactly one probe call; concurrent requests fall back with `policy_circuit_open` | Probe success goes to closed and resets counters; probe failure goes back to open |

- Counted as failures: timeout, transport error, HTTP 5xx, HTTP 429, and
  invalid response.
- HTTP 401/403 open the breaker at the maximum cooldown and log once. That is
  a configuration error, and retrying will not fix it.
- `policy_low_confidence` and `policy_version_drift` are valid answers and do
  not count as failures.
- Deadline: `policy.deadline_ms`, default 1000, allowed range 50-3000. Routing
  happens once per session, next to a connect that takes seconds, so the
  deterministic router's p95 50 ms / p99 100 ms benchmark gate
  (`FINE_GRAINED_METRICS.md`) still applies to the HMAC path only.
- Concurrency: a process-wide semaphore (`policy.max_in_flight`, default 8)
  bounds calls during a fleet start. Waiting counts against the deadline.
- `MetricsRuntime.health()` gains `router_policy: {state, consecutive_failures,
  opened_at_us, last_failure_kind}`.

## 7. Persistence and resume (D9)

| Scope | Store | Written | Reused when |
|---|---|---|---|
| Session (E1, E2) | `<sessions>/<name>.route.json` via `fsutil.atomic_write_json` | After admission, before connect | Not `fresh`, the saved session id exists, and `experiment_id` + `config_fingerprint` match the current config |
| Workflow run (E3) | `_runtime.route` in the run's variable scopes | By `_checkpoint` | The run is resumed or retried |

- The record holds `experiment_id`, `config_fingerprint`, `variant_id`,
  `model_selected`, policy status and mode, and `decided_at_us`. It never holds
  the prompt or the raw assignment key.
- `save_session_id(name, "")` deletes `route.json` together with `.model` and
  `.brain`, so a fresh session never inherits a stale route.
- On reuse, the admission `session.recorded` event is emitted again with the
  recorded decision and `metadata_json.policy.status = "reused"`. No policy
  call is made.
- A fingerprint mismatch means the experiment changed. The session is then
  treated as pre-existing (section 4, row 6) and is not enrolled.

## 8. Privacy and egress (D8)

- **Opt-in.** `policy.egress.prompt` is required and has no default:
  - `"none"`: send features only.
  - `"redacted"`: also send the redacted task text.
  - A config without the field is invalid.
- **Redaction.** Move `redact_secrets` from `bobi/setup/actions.py` to a shared
  `bobi/redact.py`. The task text is prepared in this order:
  1. Strip any `## Long-Term Memory` section. The entry-point bootstrap
     injects memory into the task text.
  2. Apply `redact_secrets`.
  3. Replace absolute paths with repo-relative ones.
  4. Truncate to `egress.max_prompt_bytes` (default 8192) as head + tail.
- **Control arm.** Never calls the policy, so nothing leaves the host.
- **Stored output.** `tier` is stored only if it matches `^[a-z0-9_]{1,64}$`.
  The free-text reason may echo prompt content, so it is dropped unless
  `store_reason_text: true`.
- **Logs.** Never log task text or policy responses. Debug logs carry reason
  codes only.
- **Credentials.** The API key comes from `.env` or the environment, under the
  name in `policy.credential_env`. Config fields named like secrets
  (`api_key`, `token`) are rejected, because the config fingerprint is public.
  `provider_subprocess_env` scrubs `ROUTING_ENV_NAMES` plus every loaded
  policy's `secret_env_names`, so Claude and Codex children never see the key.
- **Security docs.** `docs/SECURITY.md` gains an entry listing the policy
  endpoint as an egress destination. It is covered by the egress-proxy work
  (epic #395).

## 9. Configuration

`BOBI_METRICS_EXPERIMENT_JSON` gains an optional `policy` object. A variant
carries either `model` (fixed arm) or `policy` (policy arm). Existing
fixed-model experiments stay valid.

```json
{
  "experiment_id": "jev-router-2026-10",
  "router_name": "bobi-arm",
  "router_version": "1",
  "policy_version": "jev-policy-v1",
  "feature_schema_version": "jev-features-v1",
  "control_model": "sonnet",
  "cohort": "production-opt-in",
  "variants": [
    {"variant_id": "control", "weight": 0.5, "model": "sonnet"},
    {"variant_id": "treatment_jev", "weight": 0.5, "policy": "typesafe-jev"}
  ],
  "policy": {
    "name": "typesafe-jev",
    "version": "jev-2026-q3",
    "brain": "claude",
    "mode": "shadow",
    "candidate_models": ["haiku", "sonnet", "opus"],
    "min_confidence": 0.85,
    "deadline_ms": 1000,
    "max_in_flight": 8,
    "scope": {
      "entry_points": ["session_start", "subagent_phase", "subagent_persistent", "workflow_start"],
      "roles": ["engineer", "reviewer"]
    },
    "egress": {"prompt": "redacted", "max_prompt_bytes": 8192},
    "store_reason_text": false,
    "credential_env": "TYPESAFE_API_KEY",
    "options": {"endpoint": "https://api.typesafe.example/v1/route"}
  }
}
```

Validation rules, extending `ExperimentConfig.from_mapping`:

- `policy` is required if and only if some variant names a policy, and the
  names must match.
- Exactly one variant has `model == control_model`.
- `control_model` must be in `candidate_models`.
- `mode` is `shadow` or `enforce`.
- `scope.entry_points` and `scope.roles` are non-empty allowlists.
- `options` is adapter-validated and may not contain secret-like keys.
- The public fingerprint (`public_config`) includes the whole `policy` object
  except `options.endpoint`.

Changing any of these, including `shadow` to `enforce`, requires a new
`experiment_id`. The existing immutability rule quarantines conflicting events.

## 10. Telemetry mapping (D5)

The existing `router_decisions` table needs no DDL change. One row per turn is
materialized from the session's admission, as today.

| Column | Value |
|---|---|
| `experiment_id`, `cohort` | From config |
| `variant_id` | Arm: `control` or `treatment_jev` |
| `assignment_*` | Unchanged HMAC fields |
| `router_name`, `router_version` | Experiment-level (`bobi-arm`, `1`) |
| `policy_version`, `feature_schema_version` | From config |
| `candidate_models_json` | `policy.candidate_models` for the policy arm; variant models otherwise |
| `model_selected` | Model actually executed |
| `control_model` | From config |
| `router_score` | HMAC bucket, unchanged |
| `router_reason` | `control_arm`, `policy_selected`, `policy_shadow`, or `policy_fallback` |
| `router_latency_ms` | Total resolve time, including the policy call |
| `fallback_reason` | Section 11, NULL when the policy result executed or in shadow |
| `metadata_json` | Existing fields plus `entry_point` and `policy` (below) |

```json
{
  "config_fingerprint": "…",
  "expected_weight": 0.5,
  "expected_weights": {"control": 0.5, "treatment_jev": 0.5},
  "entry_point": "subagent_phase",
  "policy": {
    "name": "typesafe-jev",
    "version": "jev-2026-q3",
    "mode": "shadow",
    "status": "decided",
    "recommended_model": "haiku",
    "confidence": 0.94,
    "tier": "simple_code_modification",
    "latency_ms": 212.4,
    "breaker_state": "closed",
    "egress": "redacted",
    "redactions": 0,
    "cost_usd": null
  }
}
```

`policy.status` is one of `decided`, `reused`, `skipped` (circuit open),
`failed`, or `not_called` (control arm).

## 11. Fallback reasons

All of these execute `control_model` after assignment. The session keeps its
arm under intent-to-treat.

| `fallback_reason` | Cause |
|---|---|
| `policy_timeout` | Deadline exceeded, including semaphore wait |
| `policy_unavailable` | Transport error, HTTP 5xx/429, auth failure |
| `policy_circuit_open` | Breaker open, or half-open with a probe already in flight |
| `policy_invalid_response` | Malformed response, or model not in candidates |
| `policy_version_drift` | Answered by a model version other than the pinned one |
| `policy_low_confidence` | Confidence below `min_confidence` |
| `admission_rejected` | Producer queue refused the admission. The session is **not enrolled**, which is the existing rule. |
| `preconfigured_client_model` | Legacy value from the per-turn path. Kept for reading old data; no longer written. |

## 12. Analysis

- **Unit of analysis.** The assignment unit (run or session), not the turn.
  Routing happens per session, so turns within a session are clustered.
- **Primary comparison, causal.** Arm against arm, intent-to-treat. Measure
  completion and error rate, actual cost per unit from `best_usage`, turns and
  retries per unit, latency, and evaluator or human quality outcomes from
  `experiment_outcomes`.
- **SRM.** Run on arms only, using `expected_weights`. It is unaffected by
  what the policy chooses.
- **Secondary comparison, descriptive only.** Inside the treatment arm, break
  down by `recommended_model`, confidence bucket, and tier, to calibrate
  `min_confidence`. These slices are selected by the policy and must not be
  reported as causal savings.
- **Precision check.** Treatment decisions with `confidence >= 0.9` whose
  unit ended with runtime errors or a failing evaluator outcome.
- **Cost.** Actual cost only. Never price treatment tokens at control-model
  rates (`FINE_GRAINED_METRICS.md`, cost rules). The treatment arm's cost
  includes `policy.cost_usd` when the adapter reports it.
- **Quality.** Quality outcomes come from an external evaluator or a human.
  The policy never grades itself.

`bobi/metrics/query.py`'s experiment summary gains an arm-level
intent-to-treat block and a treatment-only policy breakdown.

## 13. Code changes

| File | Change |
|---|---|
| `bobi/metrics/router.py` | `ExperimentConfig` gains `policy`, and variants accept `policy`. `provider_subprocess_env` also scrubs policy secret env names. `route()` is unchanged. |
| `bobi/metrics/policy.py` (new) | `ModelPolicy`, `PolicyRequest`, `PolicyResult`, registry, `CircuitBreaker`, guard, `static` policy |
| `bobi/metrics/routing.py` (new) | `RoutingContext`, `RouteOutcome`, and the single `async resolve_route()` |
| `bobi/metrics/policies/typesafe.py` (new, extra `jev`) | TypeSafe adapter |
| `bobi/metrics/runtime.py` | `resolve_model` becomes an async admission used by `resolve_route`. Remove the `route()` call in `TurnObservation.start`. Add ineligible-session tracking and breaker health. |
| `bobi/session.py` | Remove routing from `__init__`. Add the `routing` parameter. Route in `_run` before the resume lookup. |
| `bobi/subagent.py` | Remove the `resolve_experiment_model` calls. Pass `RoutingContext` from all three launch paths. |
| `bobi/workflow/orchestrator.py` | `_effective_step_model` returns the configured model only. Route at run start. Persist `_runtime.route`. |
| `bobi/sdk.py` | Save, load, and clear `<name>.route.json` alongside the session id |
| `bobi/redact.py` (new) | `redact_secrets`, moved from `bobi/setup/actions.py` |
| `bobi/metrics/query.py` | Arm-level and policy-breakdown experiment summary |
| `docs/FINE_GRAINED_METRICS.md`, `docs/SECURITY.md` | Routing contract (explicit models, eligibility, policy arm), egress entry |

## 14. Verification

- **Unit tests.**
  - Config validation matrix.
  - Eligibility order.
  - Guard order.
  - Breaker state machine on a fake clock (thresholds, cooldown doubling,
    single half-open probe, 401 handling).
  - Redaction, including memory-section stripping.
  - Env scrubbing.
  - Sticky reuse and clearing.
  - `TurnObservation` never routing.
- **Stub e2e.** One test per entry point, with the `static` policy and a
  fault-injecting fake policy server (timeout, 5xx, invalid model, version
  drift). Each asserts:
  - the executed model reaches `make_session(options.model)`;
  - the decision row and `metadata_json.policy` are correct;
  - a resumed session or workflow does not call the policy.
- **Claude leg.** One `[claude]` parametrization proving the routed model is
  the model the real CLI session runs under. Routing is otherwise
  brain-agnostic, so this is the only place the real brain carries risk.
- **Live smoke (manual).** Shadow mode against the real TypeSafe endpoint in
  an isolated `BOBI_HOME`. Check the recorded latency, breaker health, and
  that no prompt text appears in the spool, logs, or database.

## 15. Rollout

| Phase | Scope | Exit criteria |
|---|---|---|
| P0 | Move routing to the entry points. Remove per-turn routing. Add eligibility and explicit-model precedence. HMAC-only experiments. | Existing experiment tests pass. Explicit `--model` is never overridden. |
| P1 | Policy protocol, breaker, redaction, TypeSafe adapter, `mode: shadow` | Two weeks of shadow data. Breaker never stuck open. p95 policy latency within the deadline. |
| P2 | New `experiment_id` with `mode: enforce` | SRM passes. The arm comparison shows no quality regression at the planned sample size. |
| P3 | E3b workflow agent-change boundary with `router_segment.recorded` | Projection tests for multi-segment runs |

## 16. Open questions

- **Q1.** TypeSafe's API contract, data-processing and retention terms, and
  whether it supports pinning a model version (section 5.4 depends on it).
- **Q2.** Whether TypeSafe reports a per-call cost for `policy.cost_usd`, or
  whether we price it from a contract rate.
- **Q3.** Whether the manager role should ever be in scope. Its first prompt
  is a bootstrap brief, not user intent, so the default allowlist excludes it.
