# JEV Model Router: Architecture and Data Flow

Status: core routing and synthetic live acceptance verified; operational shadow gates pending. Branch `feat/fine-grained-metrics`.
Last revised 2026-10-01.

### Cleanup amendment (2026-10-03)

The approved production cleanup removes aggregate experiment analytics, runtime outcome
emission, external outcome helpers, and research harnesses. References to those surfaces
below describe the original design, not shipped interfaces. Historical outcome schemas
and projection remain readable; routing, sticky lifecycle, privacy, and fallback
contracts are unchanged. Use per-turn decisions and exact usage for operational audits;
no statistical effectiveness claim is made.

### Implementation clarifications after legacy cleanup

- The legacy `route()`, `resolve_model`, and admission cache were removed at
  `461556e`. References below to retaining those functions describe the original
  baseline, not code to restore. New routing uses `choose_assignment_key` and
  `assign_variant` for arm assignment only.
- In shadow mode, `model_selected` is the executed control model. The policy
  recommendation belongs only in `metadata_json.policy.recommended_model`.
- Inspect the raw saved session ID and brain/endpoint provenance before the
  model-aware resume guard. Matching sticky decisions preserve the session model;
  they do not make incompatible transcripts eligible. Explicit overrides win.
  The recorded transcript model must match the sticky selected model; a later
  explicit launch must not leave a stale route eligible for reenrollment.
- Capacity limits must be thread-safe across independent event loops; an
  `asyncio.Semaphore` shared across session threads is not sufficient. Capacity
  waiting and HTTP execution share one monotonic deadline. Cancellation releases
  both capacity and half-open probe ownership.
- Queue acceptance is not a durability guarantee. Persistence failures must not
  launch an untracked treatment session. Workflow decisions must be checkpointed
  before first connect, and same-agent config comparisons must not undo routing.
- Workflow checkpoints are the only sticky store for workflow routes. They do
  not write or reuse the session route file, preventing a failed checkpoint
  from leaving a conflicting treatment record in another store.
- Internal rotation and stale-token recovery preserve the session route while
  clearing the provider ID. Explicit session clearing still removes the route.
- A policy call ID is created only when the adapter is invoked. Capacity-wait
  timeouts and open-breaker skips retain their fallback reason but have no call
  ID or unknown vendor cost. The built-in static policy reports zero call cost.
- Count policy-call cost once per call, not once per projected turn or sticky
  reuse. Unknown cost remains unknown. Unit-level causal summaries remain distinct
  from descriptive recommendation slices.
- The real TypeSafe API is `POST https://api.typesafe.ai/v1/systemone` with Bearer
  authentication, `state`, `model`, and `questions`. A Choice answer contains
  `choice`, `confidence`, and `probabilities`; response `model` is the answering
  version. Pin a versioned model, not `jev-latest`. Illustrations below are not
  the vendor wire contract. Account-specific retention approval is required
  before live prompt egress; public ZDR availability is enterprise-specific.
- Endpoint changes require a new experiment ID even though the public fingerprint
  omits the endpoint. Candidate criteria/instructions remain fingerprinted.
- Outside-repository paths are redacted, not fabricated as relative paths.
  Memory removal covers nested headings; truncation preserves UTF-8 within the
  byte cap. Missing/nonfinite confidence is invalid, never an accepted decision.

Policy configuration, registry, breaker, privacy features, admission, sticky
storage/reuse, and session/subagent/workflow-start wiring are implemented.
Unit-level arm cost, external quality, missingness, and descriptive policy
analysis and explicit evaluator-failure precision are implemented. Scripted
turn execution covers all entrypoint contexts and loopback vendor faults; it
does not prove a real provider or vendor request. An opt-in gateway test drives
real Codex and Claude CLIs, checks the selected model, persists reported tokens,
and verifies sticky reuse. A separate authenticated TypeSafe-to-Codex shadow
test verifies metadata-only policy selection, fresh-turn tokens, runtime
credential handoff, sticky reuse and absence of a prompt canary or credential
in metrics artifacts. Neither synthetic test proves operational treatment
sampling or the observation, SRM and quality gates. Precision covers observed unit turns
and externally supplied, versioned failure flags rather than inferred scores.

The TypeSafe adapter uses the existing `httpx` dependency with a per-call client,
no automatic retries or redirects, and bounded response parsing. `bobi[jev]` is
a compatibility installation extra without additional dependencies; the adapter
is imported only when selected. Its options require `instructions` and a
`criteria` mapping covering every candidate model. It does not infer model
capabilities from model aliases. Per-call cost remains unknown because the public
API reports usage, not invoiced cost.

This document specifies how Bobi selects a model with a semantic routing
policy (TypeSafe AI's System One model, "JEV") and how that choice is
measured. It builds on the experiment machinery described in
`docs/FINE_GRAINED_METRICS.md` ("Enable an experiment") and replaces the
earlier per-turn draft of this file.

## 1. Decisions

| ID | Decision |
|---|---|
| D1 | The model is chosen **once per fresh brain session**, before the session connects. There is no per-turn or mid-session model switching. Rotation, reconnect, and recovery reuse the session's model. |
| D2 | Routing runs at exactly three entry points: session initiation (`bobi/session.py`), subagent launch (`bobi/subagent.py`), and workflow run start (`bobi/workflow/orchestrator.py`). Workflow agent-change routing is a separately gated extension. |
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
 │ 3. HMAC arm assignment (assign_variant)                                    │
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

**Deferred E3b (agent-change boundary).** When `next_agent != current_agent`,
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

The removed implementation called `route(requested_model="")` from
`MetricsRuntime.resolve_model`, replacing explicit launch models. Explicit
models now bypass assignment and policy invocation.

Ineligible sessions are recorded in `MetricsRuntime` so
`TurnObservation.start` does not route them. `TurnObservation.start` no longer
calls `route()` at all; it only materializes an admitted decision.

## 5. Arm assignment and policy decision (D3)

### 5.1 Arm

This uses `choose_assignment_key` and `assign_variant` in
`bobi/metrics/router.py`. The key order is
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
The result schema (including finite confidence and nonnegative finite cost)
is validated first, before the semantic checks below. Malformed answers never
contribute recommendation or cost metadata, including on version drift.

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
  `.brain`. Internal rotation and stale-token recovery pass
  `preserve_route=True`; they retain the chosen model without routing again.
  A fresh launch ignores prior decisions. After recovery, a new provider ID
  must be saved before session-file sticky reuse is eligible on process restart.
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
  `provider_subprocess_env` scrubs `ROUTING_ENV_NAMES`, `TYPESAFE_API_KEY`,
  the configured `credential_env` even before policy initialization, and every
  loaded policy's `secret_env_names`. Out-of-scope and explicit-model Claude
  and Codex children therefore never inherit the policy key.
  Claude launch options explicitly blank filtered inherited variables because
  the SDK merges them over the parent environment; omission alone is insufficient.
  A non-secret internal marker identifies sanitizer-created routing blanks.
  Root-bound Bobi CLI and child launches consume it before loading the installed
  runtime's `.env`, restoring routing configuration and policy credentials only
  inside Bobi. Unmarked empty overrides and non-empty overrides remain unchanged;
  subsequent provider launches scrub the restored values again.
  Control and policy arms record the same experiment-wide candidate model list,
  so mixed-arm decisions satisfy immutable experiment projection checks.
- **Security docs.** `docs/SECURITY.md` gains an entry listing the policy
  endpoint as an egress destination. It is covered by the egress-proxy work
  (epic #395).

## 9. Configuration

`BOBI_METRICS_EXPERIMENT_JSON` gains an optional `policy` object. A variant
carries either `model` (fixed arm) or `policy` (policy arm). Existing
fixed-model experiments stay valid.

Variant weights may be zero and must sum to one. For a full policy rollout,
retain the control variant at weight `0` and set the policy variant to weight
`1`, using a new experiment ID. Assignment never selects a zero-weight arm;
the control model remains the safety fallback. A single active arm is not an
A/B comparison, and existing scope, explicit-model and sticky-session rules apply.

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
    "version": "jev-1.13.0",
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
    "egress": {"prompt": "none", "max_prompt_bytes": 8192},
    "store_reason_text": false,
    "credential_env": "TYPESAFE_API_KEY",
    "options": {
      "endpoint": "https://api.typesafe.ai/v1/systemone",
      "instructions": "Choose the least costly candidate that can reliably complete the task. When task information is insufficient, choose sonnet.",
      "criteria": {
        "haiku": "Small, well-defined changes with low reasoning complexity.",
        "sonnet": "General engineering work or insufficient task information.",
        "opus": "Complex reasoning or cross-system architectural work."
      }
    }
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
  `completion_definition=all_observed_turns_completed` and
  `error_definition=any_observed_turn_error` describe observed telemetry only.
  A completed turn does not prove a workflow/task succeeded or closed. Use
  externally evaluated, versioned outcomes for that claim, and report missing
  outcomes instead of inferring success from cost or router confidence.
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
  The query exposes `reported_total_cost_usd_per_unit` only when every
  observed turn has reported execution cost and every scanned policy call
  has reported cost. Calls are deduplicated by `call_id` within each arm.
  Missing cost or a truncated policy scan makes the total null; the known
  policy subtotal and unknown-call count remain available separately.
  Capacity-wait timeouts and circuit-open skips are not vendor calls. The
  built-in static policy's per-call cost is zero; TypeSafe cost remains unknown.
- **Quality.** Quality outcomes come from an external evaluator or a human.
  The policy never grades itself.
  Evaluators may supply a boolean `is_failure` to `record_quality_outcome`,
  interpreted under their recorded rubric/version. Precision counts explicit,
  non-estimated failure flags per assignment unit; arbitrary numeric scores
  and outcomes without a failure flag are not inferred to be failures.

`bobi/metrics/query.py`'s experiment summary gains an arm-level
intent-to-treat block and a treatment-only policy breakdown.

## 13. Code changes

| File | Change |
|---|---|
| `bobi/metrics/router.py` | Strict policy/model variants, HMAC arm helpers, public fingerprints, and policy-secret subprocess scrubbing. Legacy `route()` is removed. |
| `bobi/metrics/policy.py` (new) | `ModelPolicy`, `PolicyRequest`, `PolicyResult`, registry, `CircuitBreaker`, guard, `static` policy |
| `bobi/metrics/routing.py` (new) | `RoutingContext`, `RouteOutcome`, and the single `async resolve_route()` |
| `bobi/metrics/policies/typesafe.py` (new, extra `jev`) | TypeSafe adapter |
| `bobi/metrics/runtime.py` | Synchronous `admit_route` accepts a resolved decision. Turn observation only materializes admitted decisions; health includes the policy breaker. Legacy `resolve_model` is removed. |
| `bobi/session.py` | Remove routing from `__init__`. Add the `routing` parameter. Route in `_run` before the resume lookup. |
| `bobi/subagent.py` | Remove the `resolve_experiment_model` calls. Pass `RoutingContext` from all three launch paths. |
| `bobi/workflow/orchestrator.py` | `_effective_step_model` returns the configured model only. Route at run start. Persist `_runtime.route`. |
| `bobi/sdk.py` | Save, load, and clear `<name>.route.json` alongside the session id |
| `bobi/redact.py` (new) | `redact_secrets`, moved from `bobi/setup/actions.py` |
| `bobi/metrics/query.py` | Arm-level and policy-breakdown experiment summary |
| `docs/FINE_GRAINED_METRICS.md`, `docs/SECURITY.md` | Routing contract (explicit models, eligibility, policy arm), egress entry |

## 14. Verification

### Build the repository engineering image

Use the development Python environment and Node.js 20 from
`docs/REFERENCE_IMAGE.md`. Build a wheel from the intended checkout and render
the team's declared dependencies. Stage only build inputs, never `.env`,
credentials, runtime state or private acceptance artifacts:

```bash
BUILD_CONTEXT=$(mktemp -d)
mkdir -p "$BUILD_CONTEXT/dist/team-deps"
cp Dockerfile pyproject.toml "$BUILD_CONTEXT/"
cp -R docker "$BUILD_CONTEXT/"
python -m build --wheel --outdir "$BUILD_CONTEXT/dist"
python -m bobi.build_render agents/eng-team \
  --out "$BUILD_CONTEXT/dist/team-deps/eng-team.sh"
docker build --build-arg BOBI_BUILD=wheel \
  --build-arg TEAM_DEPS=dist/team-deps/eng-team.sh \
  -t bobi-framework:metrics "$BUILD_CONTEXT"
```

A deployment repository must use this image as its base and stage the
repository team with its chosen brain overlay, rather than silently fetching
a registry team. Rebuild and reinstall after package changes; a container
restart alone does not update the installed package. Keep metrics and routing
values in the ignored deployment `.env` and installed runtime `.env`; the
acceptance procedure below defines the credential handoff. An in-memory local
event-server restart can lose registrations even while container health passes.
Do not reset registration state or remove the durable volume without approval.

### Operator acceptance procedure

1. Use an isolated `BOBI_HOME` and installation root. Do not reuse a live Slack
   bot, production state directory, or subscription volume for fault tests.
2. Set `BOBI_METRICS_MODE=enabled`, a private assignment secret, and the section 9
   JSON in `BOBI_METRICS_EXPERIMENT_JSON`. Supply `TYPESAFE_API_KEY` privately;
   never include its value in a committed config, command transcript, or report.
   For provider tools that launch `bobi` CLI children, also configure these values
   in the installed runtime's `.env` at mode 0600. Provider subprocesses deliberately
   scrub routing configuration and credentials; root-bound Bobi child launches
   rehydrate them from that file. Container environment alone does not cover this
   launch path. Preserve existing runtime credentials when updating the file.
3. Validate the config offline before enabling vendor calls. Start with shadow
   mode and `egress.prompt=none`. Features are still external egress; obtain
   account-specific retention approval before the live smoke.
4. Launch an eligible engineer session without an explicit model override.
   Verify the executed model stays on control in shadow mode and inspect the
   experiment query through the existing admin/MCP `metrics_experiment` surface.
   The physical database is `<runtime-root>/state/metrics/metrics.db`.
5. Confirm arm, executed model, policy recommendation, fallback reason, latency,
   call ID and breaker health. Resume the same session/run and confirm `reused`
   metadata without another vendor call. Unknown vendor cost must remain null.
6. Search isolated logs, spool and database for a unique task canary; no prompt
   text may be retained by routing telemetry. Repeat with explicit override and
   verify no experiment enrollment. Never use real secrets as canaries.
7. The opt-in Claude proof is
   `BOBI_JEV_LIVE_CLAUDE=1 .venv/bin/pytest tests/integration/test_jev_claude_routing.py -q`.
   It makes a real provider request and requires authorized Claude authentication.
   A skipped test is not acceptance evidence.
   For an authorized gateway, provide `BOBI_GATEWAY_BASE_URL`,
   `BOBI_GATEWAY_API_KEY` and `BOBI_BRAIN_MODEL` privately, then run
   `BOBI_JEV_LIVE_GATEWAY=1 .venv/bin/pytest tests/integration/test_jev_gateway_routing.py -q -s`.
   This runs the real Codex and Claude CLI paths using a static routing policy,
   isolated provider homes, and the gateway's Responses and Messages APIs.
   It checks persisted token totals and sticky reuse without TypeSafe egress.
   The first turn of a newly constructed resumed adapter may have no usage:
   without a trusted cumulative baseline it stays unknown, never zero. The
   test uses reported invocation usage when the turn aggregate is absent and
   reports measured and missing turn counts; passing it does not prove
   complete resumed-token coverage or authenticated TypeSafe routing.
   For approved TypeSafe plus Codex gateway acceptance, additionally supply
   `TYPESAFE_API_KEY` and a supported alternative in `BOBI_JEV_CANDIDATE_MODEL`,
   then run:

   ```bash
   BOBI_JEV_LIVE_TYPESAFE=1 .venv/bin/pytest \
     tests/integration/test_jev_gateway_routing.py::test_typesafe_shadow_gateway_tokens_and_runtime_handoff -q -s
   ```

   This uses a separate synthetic cohort, verifies root-bound CLI credential
   handoff, makes one authenticated policy call, executes the control model in
   shadow mode, and checks token persistence and sticky reuse. It is not a
   production treatment sample or evidence for SRM, quality, or elapsed observation.
8. Do not enable enforcement before the shadow observation and quality/SRM gates
   below pass. Changing endpoint, model version, mode or policy criteria requires
   a new experiment ID; do not blend those cohorts as one causal comparison.

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
  `tests/metrics/test_routing_acceptance.py` runs scripted provider turns for
  all five entrypoint contexts with static success and loopback TypeSafe
  timeout, 5xx, invalid-model and version-drift faults. Session-backed paths
  also exercise sticky resume, stale-token recovery, and rotation. Phase and
  persistent launcher-context wiring and workflow retry/checkpoint reuse have
  separate owning tests. Event-bus transport and real provider execution are
  not simulated-provider evidence.
- **Claude leg.** One `[claude]` parametrization proving the routed model is
  the model the real CLI session runs under. Routing is otherwise
  brain-agnostic, so this is the only place the real brain carries risk.
- **Live smoke (manual).** Shadow mode against the real TypeSafe endpoint in
  an isolated `BOBI_HOME`. Check the recorded latency, breaker health, and
  that no prompt text appears in the spool, logs, or database.

### Routing regression coverage

Use `tests/metrics/test_routing.py`, `test_routing_acceptance.py`, `test_reconcile.py`, and the provider-contract tests to verify policy evaluation, explicit fallbacks, sticky model selection, and exact token accounting.
The research measurement matrix and scenario datasets have been removed from the production PR.
Live acceptance remains a fresh routine and demanding Slack request followed by the deployment wrapper's read-only `scripts/metrics_summary.py` inspection.

### Grouped acceptance coverage

| Category | Executable evidence | Remaining acceptance |
|---|---|---|
| Routing / lifecycle | Scripted turn matrix, owning session/subagent/workflow tests, sticky resume, recovery, rotation and checkpoint failures | Event-bus transport is not covered by the scripted matrix; E3b segments remain separately gated |
| Analysis | Arm-unit cost, quality, missingness, precision and admin-query parity tests | Operational sample size, SRM and quality gates require real experiment data |
| Privacy / fault handling | Config/guard/breaker tests, loopback vendor faults, redaction and log/spool/database canaries; opt-in authenticated TypeSafe check | Account-specific vendor retention approval is an operator prerequisite, not a unit-test result |
| Provider execution | Opt-in real Claude/Codex gateway model test; authenticated TypeSafe-to-Codex shadow, runtime handoff, persisted tokens and sticky reuse | Unknown first-resume usage is reported explicitly; native-provider, synthetic gateway and operational cohort acceptance are distinct |

## 15. Rollout

| Gate | Scope | Exit criteria |
|---|---|---|
| Entry-point routing | Remove per-turn routing; add eligibility, explicit-model precedence and HMAC assignment | Explicit launch models remain unchanged; deterministic entrypoint tests pass |
| Shadow observation | Policy protocol, breaker, redaction, TypeSafe adapter, `mode: shadow` | Two weeks of shadow data; breaker recovery; p95 policy latency within deadline |
| Enforcement | New `experiment_id` with `mode: enforce` | SRM passes; no quality regression at the planned sample size |
| Workflow segments | E3b agent-change boundary with `router_segment.recorded` | Projection tests for multi-segment runs; separately gated, not implemented |

## 16. Open questions

- **Q1.** Account-specific TypeSafe data-processing and retention approval is
  still required. The implemented API contract supports a pinned model version.
- **Q2.** The public API reports tokens, not invoiced per-call cost. Keep
  `policy.cost_usd` null until a verified billing contract supplies actual cost.
- **Q3.** Whether the manager role should ever be in scope. Its first prompt
  is a bootstrap brief, not user intent, so the default allowlist excludes it.
