# Repository engineering team metrics and model routing

Status: core implementation and live smokes verified; operational shadow gates pending.
Created: 2026-10-01.
Architecture contract: `docs/JEV_ROUTER_DATA_FLOW.md`.

## Objective

Use the repository's `agents/eng-team` as the single engineering team under test.
Build and run its source-bound container image, prove provider token tracking, remove obsolete routing behavior, and implement the model-router architecture contract.
Keep credentials, machine-specific paths, runtime state, and operator overrides out of commits.

## Initial baseline before cleanup

- The branch is `feat/fine-grained-metrics`, with architecture documentation added at `6e6f3bd`.
- `agents/eng-team/agent.yaml` declares Slack and GitHub as required services and includes a declarative build hook for `jq` and the GitHub CLI.
- The framework Dockerfile supports source builds and the `TEAM_DEPS` hook; a plain framework build does not prove that the team's required tools are installed.
- Fine-grained collection is disabled by default in `bobi/metrics/runtime.py`; `BOBI_METRICS_MODE=enabled` enables collection independently of routing configuration.
- The inspected Docker runtime uses an external team package, a different image, and subscription authentication with metrics in `shadow` mode.
- That runtime is not evidence that the repository engineering team runs or records tokens.
- The old router assigned weighted variants directly to models, without separating arm assignment from semantic selection.
- The old runtime routed during model resolution and turn observation; session construction also applied experiment routing.
- The architecture document at `6e6f3bd` described a specification, not an implemented TypeSafe integration.

## Current implementation evidence

The coordinator now owns eligibility, HMAC arm assignment, policy invocation,
guards, admission and sticky reuse. Session, subagent and workflow-start paths
apply its resolved model before connection. Workflow routes use only the durable
run checkpoint; internal session recovery and rotation retain the chosen route.
Policy-call accounting excludes capacity-wait timeouts and open-breaker skips.

Deterministic tests execute scripted provider turns for all five entrypoint
contexts, including loopback TypeSafe timeout, 5xx, invalid-model and version-drift
faults, session resume/recovery/rotation and workflow checkpoint/retry tests.
The separately gated gateway test verifies real Claude/Codex CLI execution,
reported-token persistence and sticky reuse with an authorized execution backend.
First-resume usage without a trusted cumulative baseline remains unknown.
The separately gated TypeSafe-to-Codex shadow check proves authenticated policy
selection, real control-model execution, persisted fresh-turn tokens, root-bound
CLI credential handoff and sticky reuse in an isolated synthetic cohort.
Operator-confirmed retention approval precedes this check. Synthetic acceptance
does not prove production treatment sampling or the operational rollout gates.

### 2026-10-01 acceptance amendment

- The operator-approved gateway replaces the initial subscription-only bootstrap.
  Credentials and deployment overrides remain outside commits.
- The canonical engineering bot's real Slack request/reply and provider-reported
  token persistence were verified. Replies to a mention inside an existing thread
  belong to the parent `thread_ts`, not the mention message's own timestamp.
- The deployed shadow cohort recorded a real control-arm workflow decision.
  Control correctly invokes no policy; its request does not certify treatment.
- The authenticated TypeSafe-to-Codex synthetic check made one policy call,
  executed the control model, persisted fresh-turn usage and reused the decision.
  Missing first-resume usage remains unknown; it is not inferred to be zero.
- Provider-launched Bobi CLI children require routing values in the installed
  runtime `.env`, not only container env. The root-bound handoff is verified.
- Two weeks of observation, treatment sampling, latency/SRM and quality gates
  remain required before enforcement. Workflow agent-change segments remain deferred.

### 2026-10-01 verification amendment

- The final TypeSafe-to-Codex shadow test passed with a routing privacy canary:
  one policy call, sticky reuse, private runtime credential handoff and no
  canary or credential in metrics artifacts. First-resume usage remains null.
- The three stored turns from the second Slack request match their retained
  raw provider usage across input, output, cache and reasoning dimensions.
  Missing cache-write dimensions remain null; subfields are not added to totals.
- Source-image build guidance now uses a minimal, credential-free Docker context
  and the repository engineering team's rendered dependency hook.
- Python Admin-query parity is verified; live hosted Admin/MCP instance parity
  remains unproven. Restarting the local Docker daemon lost event-server
  registrations; restoring those registrations requires separate approval and
  is not covered by container health or historical Slack replies.

## Delivery order

### Establish the engineering team runtime and token evidence

- [x] Inventory bot identity and event subscriptions without logging tokens or changing live registration.
- [x] Confirm which Slack app and channels belong to the canonical engineering team before stopping or replacing any bot.
- [x] Add portable, documented source-image build and runtime configuration using the repository team and its rendered build hook.
- [x] Keep metrics collection enabled explicitly and experiment routing unset for this baseline.
- [x] Verify the image contains the branch framework, repository team package, `gh`, and `jq`.
- [x] Verify startup, service configuration, manager readiness, and a real Slack request through the canonical bot.
- [x] Verify collector health and persisted session, turn, invocation, and tool records from that request.
- [x] Compare provider usage with stored usage, including input, output, cache, reasoning, provenance, and unknown-value semantics.
- [ ] Verify Admin and MCP usage queries refer to the same instance and durable metrics store.
- [x] Stop and remove the superseded Docker bot at the operator's explicit request; preserve its durable volume for recovery.

Acceptance requires a successfully built image and a real provider-backed request, not only container health or synthetic telemetry.
Subscription credentials and Slack identity are operator inputs, never committed fixtures.

### Remove obsolete model routing

- [x] Inventory routing callers, persisted decisions, tests, smoke vectors, and public experiment contracts before deleting code.
- [x] Remove per-turn assignment and direct variant-to-model routing, including obsolete model mutation and tests that enforce the old behavior.
- [x] Preserve token collection, provider normalization, collector lifecycle, usage queries, outcomes, and backward-readable historical records.
- [x] Preserve reusable HMAC arm assignment and intent-to-treat analysis rather than deleting the metrics experiment infrastructure indiscriminately.
- [x] Make retired experiment configuration fail clearly or follow an explicitly documented compatibility path; never silently apply an obsolete routing contract.
- [x] Prove configured models remain unchanged across session start, turns, reconnect, recovery, and workflow resume.
- [x] Repeat token-tracking acceptance against the canonical engineering team.

### Implement the architecture contract

- [x] Implement generic routing context, eligibility, policy protocol, registry, and static policy.
- [x] Separate HMAC arm assignment from treatment-only policy model selection.
- [x] Route only at fresh session initiation, subagent launch, and workflow run start.
- [x] Preserve explicit model precedence and sticky decisions across resume, reconnect, and recovery.
- [x] Implement policy deadlines, circuit breaking, bounded redaction, environment scrubbing, and control-model fallback.
- [x] Implement shadow execution with decision metadata and arm-level analysis before enabling enforcement.
- [x] Add the optional TypeSafe adapter only against a verified vendor API contract.
- [x] Cover entrypoint contexts with scripted turn execution and loopback policy faults.
- [x] Prove selected-model execution with real Claude and Codex CLIs against an authorized isolated gateway.
- [x] Prove authenticated TypeSafe shadow smoke and account-specific retention approval.
- [x] Verify no routing occurs inside turn observation and no prompt text reaches default routing telemetry artifacts.
- [x] Treat workflow agent-change routing as a separately gated extension of the architecture contract.

## Initial runtime bootstrap decisions

- The canonical Slack app is ByteDev Eng Agent, serving only the operator-selected engineering channel.
- The replacement uses native Codex subscription authentication, with provider API keys and gateway settings absent.
- Fine-grained token collection is enabled with `BOBI_METRICS_MODE=enabled`; experiment routing remains unset.
- The branch wheel and team-dependency image were built successfully; the operational image adds Node.js 20 for the embedded event server.
- Manager readiness and a native Codex request returning `METRICS_READY` were verified; provider usage measurements were persisted.
- Full Slack request/reply acceptance remains pending; channel subscription registration alone does not prove delivery.
- The superseded Bobi Dev Agent container was stopped and removed before replacement acceptance at the operator's explicit request.
- Its durable volume was preserved; the unrelated stopped event-server container was not changed.
- Slack channel IDs and credentials remain operator runtime inputs rather than committed deployment configuration.

## Decisions required before policy integration

- The API contract and version-pinning behavior are validated offline and through an authenticated synthetic smoke. Every deployment still requires operator approval of its TypeSafe credentials, egress scope and account-specific data-retention terms.

## Commit and verification rules

Use grouped imperative commit subjects consistent with neighboring implementation commits, prefixed with `[MOD-404]` for this implementation.
Do not include delivery-stage labels, machine-specific artifacts, credentials, release changes, or agent co-authors.
Record build results, targeted tests, and redacted live acceptance evidence separately for each changeset.
Do not claim live acceptance when a provider test is skipped or bot identity remains unresolved.
