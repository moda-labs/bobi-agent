# Manage agent routines and workflows from chat

> **Status:** Engineering plan in review; product scope and technical approach approved by Luke (2026-11-04)
> **Tracking issue:** [MOD-446](https://linear.app/moda-labs/issue/MOD-446/enable-bobi-agent-updates-from-chat) · **Created:** 2026-11-03 · **Last amended:** 2026-11-04
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked

## Purpose

Let anyone chatting with a Bobi agent create, inspect, update, disable, or delete that agent's routines and workflows.
When a request implies a configuration change, the agent gathers the relevant details through conversation, confirms the design, updates the source configuration, submits a PR, and handles its redeployment automatically after merge.
Reading or explaining an existing routine or workflow does not require a PR.
The user should not need a separate coding-agent session or a manual deployment.
This is a technical product contract; proposed integration details below are not claims of existing functionality.

## Problem

This PRD scopes MOD-446 to routine and workflow management through chat, using `agent-design` and a PR to make configuration changes official.
General behavioral and policy changes are deferred.
The missing product flow is deciding whether a chat request should be handled as ordinary work, retained as memory, or turned into a reviewed change to the agent's setup.
Today, Bobi has chat, memory, team configuration, and deployment mechanisms, but those building blocks do not by themselves provide this end-to-end experience.
The user should describe the outcome they want; the agent should identify the appropriate path and explain what will change.

Evidence baseline: local `bobi-agent` commit `744ffa626cb5cc20d8d33bf5dbcf4b6b0216df31`, plus inspected sibling checkouts of `moda-skills` and `moda-agents`.
The decision-log section of `docs/BUILDING_AGENT_TEAMS.md` describes legacy memory behavior; current memory code and prompts govern this PRD.

## Solution

### Product scope

- Support create, read, update, and delete (CRUD) operations on the addressed agent's routines and workflows, including disabling/re-enabling routines, schedules, triggers, steps, inputs, outputs, and workflow-specific approval gates.
- Read/list/explain requests return the effective installed configuration and distinguish pending PR changes without editing files or opening a PR.
- Source edits to role/context instructions are allowed only where required to implement the requested routine or workflow; standalone personality, general behavior, and agent-wide policy changes are out of scope.
- Treat one-off requests as ordinary work and stable facts/preferences as memory candidates; ask when the intended persistence is ambiguous.
- Let every person who can chat with the agent propose and confirm a design; introduce no agent-owner list or additional requester role.
- Require the repository's existing review and merge policy before activation; a Slack confirmation authorizes preparing the PR, not merging it.
- Automatically activate merged changes only on the addressed deployment and report the observed result in the original conversation.
- Launch with Moda-hosted activation plus a deployment-specific integration for Overmatch, the self-hosted target customer. General-purpose local/self-hosted support is deferred.
- Use a safe restart for activation: wait for work to reach a safe boundary, preserve resumable state, and defer replacement if safety cannot be established.
- Exclude framework upgrades, shared-base edits, secret provisioning through chat, fleet-wide changes, and new web UI from this feature.
- Surface missing services or credentials as prerequisites; never ask for secrets in a thread or claim an unconfigured routine is usable.

### User requests and supported outcomes

These are the user-facing scenarios this feature must distinguish.
Memory and one-off work already have execution paths; this feature must route to them appropriately while adding the reviewed agent-change path.
The routing rule is: facts and preferences use the existing memory path; inspecting routines/workflows is read-only; creating, updating, or deleting them requires a config PR and automatic redeployment after merge.
General behavior changes outside a specific routine or workflow are deferred, not redirected into memory as a workaround.
A word such as “remember” or “always” does not determine the route by itself.

| What the user asks | What the agent does and what changes for the user | Change to the agent's setup | PR required? |
|---|---|---|---|
| “Summarize these issues now.” | Produces a summary in the conversation. It does not schedule future summaries. | None; ordinary task execution and transcript only. | No. |
| “Remember that I prefer concise summaries.” | Acknowledges the preference and uses it in context. The existing sleep cycle can retain it for future sessions; the agent does not claim it has already been durably saved. | Runtime memory, not source configuration. No schedule, workflow, or deployment changes. | No. |
| “Remember that Acme is our largest customer.” | Retains a useful fact with its context for later work. | Runtime memory, not an instruction to launch new work. | No. |
| “Every Monday at 9, summarize open issues here.” | Clarifies timezone and issue scope, proposes the routine, and returns a PR after design confirmation. After merge and verified activation, summaries arrive automatically. | A schedule/trigger plus instructions for creating and delivering the summary in this agent's source overlay. | Yes. |
| “Move the Monday digest to Tuesday at 10.” | Identifies the existing routine, confirms the timezone and change, and proposes updating it rather than adding another digest. The old schedule remains active until the update deploys. | Change the existing monitor's schedule; retain its procedure and destination unless requested otherwise. | Yes. |
| “Stop sending the weekly digest.” | Proposes disabling or removing that routine. Explains that it remains active until the PR merges and the change deploys. | Disable/remove the routine's trigger and adjust any now-unused routing or procedure references. | Yes. |
| “When a new support issue arrives, triage it and notify this channel.” | Clarifies which issues qualify and what triage may do, then proposes an event-driven routine. | Event routing and a procedure/workflow, using an already configured service connection. | Yes. |
| “For the weekly digest, include blockers and group issues by owner.” | Proposes a change to the defined digest output, tests sample results, and provides a PR. Future digests use the new format after activation. | The routine's context/skill instructions or workflow prompt; its schedule need not change. | Yes. |
| “Add a draft-approval step before sending in the outreach workflow.” | Identifies that workflow, clarifies who approves and what happens on rejection/no response, then proposes and tests the changed steps. | That workflow's approval gate and associated procedure instructions. | Yes. |
| “What routines do you run, and how does the outreach workflow work?” | Lists effective installed routines and explains the selected workflow's triggers, steps, and outputs; identifies pending changes separately. | None; read existing configuration. | No. |
| “Delete the outreach workflow.” | Identifies the exact workflow, checks dependent triggers and active/suspended runs, and confirms removal and its consequences before preparing the PR. | Remove the workflow and its exclusive triggers/routing; preserve shared dependencies. | Yes. |
| “Change your personality” or “Always ask permission before doing anything.” | Explains that this feature handles routines and workflows; asks whether the user means a specific one when that is unclear. | No general behavioral or agent-wide policy change through this feature. | Out of scope. |
| “Keep me updated on these issues.” | Asks whether the user wants a summary now, context remembered, or ongoing notifications; it does not silently pick a persistent change. | None until intent is clear; then use the corresponding path above. | Depends on the clarified request. |
| “Watch Salesforce for new leads,” when Salesforce is not connected. | Explains the missing connection and operator setup needed. It does not ask for credentials in chat or claim the routine is active. | Connection/credential provisioning is a prerequisite outside this feature; the routine can then follow the PR path. | A routine PR cannot substitute for provisioning access. |

The distinction is about the requested outcome, not whether a change can technically be expressed as prose.
“Prefer concise answers” is a general preference; “the weekly digest must have these sections” changes a defined routine; “add approval to the outreach workflow” changes that workflow.
A broad request such as “get approval before sending anything” must be clarified if it might refer to a specific workflow; it does not authorize an agent-wide policy change in this release.
If a remembered preference conflicts with an explicit configured procedure, the agent must surface the conflict and offer the PR path for that routine/workflow rather than silently override it through memory.
If the conflict requires a general behavioral change, explain that it is outside this feature.

### What changes in the Bobi agent setup

An **overlay** is the addressed deployment's own source directory, composed with a shared base team at installation.
All source changes below belong to that overlay so sibling deployments keep their existing behavior.
These are candidate edit locations, not a requirement to touch every file for every request; `agent-design` inspects the composed agent and chooses the smallest complete change.

| Kind of change | Where it belongs | When it takes effect |
|---|---|---|
| Fact or general preference | The conversation transcript feeds the sleep cycle, which maintains `<run>/state/long_term_memory.md`; the chatting agent does not edit that file directly. | Available in the current conversation; durable use in future sessions follows memory processing and prompt loading. No PR or restart. |
| Scheduled routine or schedule change | Source `agent.yaml` monitor configuration or `monitors/defaults.yaml`, following the team's existing convention; procedure/routing files as needed. | After PR merge, automatic deployment, and verification of the effective loaded schedule. |
| Event-driven routine | Existing event subscription/routing configuration, role instructions, and `workflows/*.yaml` where orchestration is needed. | After the same reviewed deployment flow, with required service access already configured. |
| Routine/workflow procedure or output format | Source `context/` skill/reference files, `roles/<role>/ROLE.md`, or workflow prompts, depending on where the current behavior is defined. | After the reviewed source is installed and loaded by the agent. |
| Approval step within a named workflow | Source workflow definition and its associated role/procedure instructions. | After deployment and verification that the approval step governs the intended action. |
| Pending change request | Durable workflow/request state: proposal, confirmation, source revision, PR, and activation status. | Tracks progress immediately; it does not change the live routine. |

For PR-backed changes, source is edited in a separate worktree and composed into a candidate package for validation.
Automatic deployment installs the approved result into the addressed agent's `run/package/` and verifies it is loaded.
The feature must not use `bobi ... monitors add` on the live agent as a shortcut: that command directly changes runtime monitor configuration before review.
Existing runtime overrides must be reconciled or activation blocked if they would prevent the reviewed source from taking effect.

### The user's workflow on each path

**One-off work:** ask in chat → receive the result → no persistent setup change.

**Memory:** state a fact or preference → receive an acknowledgment → the existing sleep cycle retains appropriate information for future sessions.
The user does not review a PR or deploy anything.

**Read a routine/workflow:** ask what exists or how it works → receive an explanation of installed configuration and any pending changes → no PR or redeployment.

**Create, update, or delete a routine/workflow:** describe the requested change → answer any necessary questions → confirm the proposed design → receive and review the PR → merge under the repository's existing policy → automatic activation → receive a verified result in the same thread.
Confirming the design authorizes preparation of the PR; it does not change the running agent.
The user needs no separate coding-agent session and runs no deployment command.
If activation fails, the agent reports that the PR merged but the new behavior is not active, along with recovery status.

For example, “Send me an issue summary every Monday” creates a schedule and summary procedure in the addressed agent's overlay.
“Make those summaries shorter” must be clarified if it is unclear whether the user means a general style preference or an explicit change to that routine's defined output.
Neither a pending proposal nor its design confirmation should become an active standing instruction through memory.
The sleep cycle may retain the proposal's reference and status; cancellation, rejection, and failed activation must retain their non-active meaning across session rotation.

### Supporting implementation capabilities

The following are implementation dependencies for the user journeys above, not additional things users need to ask the agent to do.

| Building block | Why the user journey needs it | Verified code and remaining integration |
|---|---|---|
| Chat delivery | Questions, PR links, and activation results stay in the original thread. | `bobi/events/client.py:format_event_for_manager` and `bobi/events/gateway.py`; persist the conversation address with the request. |
| Memory processing | Facts/preferences survive beyond the conversation without a PR. | `bobi/memory.py`, `bobi/prompts/base.md`, `bobi/prompts/sleep_cycle.md`, and `bobi/prompts/resolver.py`; distinguish active knowledge from pending changes. |
| Agent design | A natural-language request becomes a coherent change whose intended behavior is exercised. | Generalize the existing Moda `agent-design` skill and ship it with Bobi; use the existing `bobi skill` distribution path and adapt its interactive stages for Slack. |
| Team composition and provenance | Only the addressed agent changes, and the system can verify what version is running. | `bobi/compose.py` and `bobi/install.py`; add the repository/revision binding beyond existing source paths and file hashes. |
| Durable request state | The user can answer later without losing the proposal after a restart or creating duplicate PRs. | `bobi/workflow/state.py` and `bobi/workflow/orchestrator.py`; add correlated chat resume in a dedicated per-run process. This is the feature's own progress state, distinct from a user-authored business workflow. |
| Merge-triggered deployment | Merging the PR makes the change live without a manual deployment step. | `moda-agents/.github/workflows/deploy-agent-teams.yml` currently uses tags/manual dispatch; add merge triggering and exact target/revision selection. |
| Deployment and health checks | “Your change is active” means the intended revision is installed and healthy. | `moda-agents/bobi-deploy/bobi_deploy/src/bobi_deploy/deploy.py:deploy`; extend the existing engine with revision verification, result delivery, and recovery. |

### Generalize and ship agent-design with Bobi

Routine/workflow mutations must use our `agent-design` skill to make the resulting changes effective, not merely produce syntactically valid configuration.
Start from `moda-skills/plugins/moda/skills/agent-design/SKILL.md`, iterate on it for Bobi, and ship the generalized skill with `bobi-agent`.
Read-only inspection does not require the authoring process.
Generalizing the skill does not expand this feature beyond routine and workflow CRUD.

The recommended distribution is a canonical `skills/agent-design.md`, available through `bobi skill agent-design` in both source checkouts and installed packages.
This follows the existing `bobi/cli.py:skill` lookup and `pyproject.toml` wheel inclusion mechanism; the file and command output are proposed additions, not available today.
Prefer this existing distribution path over requiring every agent team to install the Moda skill pack.

Adapt the skill rather than copying it unchanged:

- Preserve expected-outcome framing, whole-agent inspection, minimal design, independent review, user confirmation, isolated edits, behavioral evaluation, and PR evidence.
- Replace hard dependencies on sibling Moda `build`, `investigate`, and `complexity-review` files with self-contained requirements or explicit repository-provided hooks. A stock Bobi installation must not contain dead references or require access to a private skill repository.
- Make interaction work through asynchronous chat: questions and approvals can pause and resume without a live user in the worker's terminal.
- Use Bobi's composed agent, installed revision, routines/workflows, source overlay, and candidate runtime as the objects of inspection and validation.
- Keep repository-specific review/merge policy in repository instructions and deployment mechanics in the existing activation adapter. The skill prepares the PR; verified merge triggers automatic redeployment through the feature's controller.
- Record the shipped skill revision with authoring evidence so the behavior of the design process can be reproduced.

Evaluate the generalized skill on representative create, update, disable/re-enable, and delete requests, including ambiguous intent, conflicting inherited instructions, and a change that must not affect another routine.
For each request, define observable success and regression scenarios before editing; compare the existing and candidate agent where a baseline exists, inspect transcripts, and repeat borderline model-dependent cases.
A valid YAML file or successful deployment is not proof that the routine performs the requested work.
Use failed scenarios to revise the skill and rerun the evaluation before accepting the generalized version.
The PR for each generated routine/workflow change must report scenarios exercised, actual outcomes, untested cases, and any remaining limitations; a failed or unrun behavioral check must not be represented as verified effectiveness.
Resolve ownership when introducing the public skill so later fixes have one canonical home; any retained Moda wrapper should reference the generalized skill and add only Moda-specific policy rather than maintain an independent copy.

### Conversation and authoring contract

When the agent infers a routine/workflow management request, first establish the operation and target from the conversation and installed configuration.
Answer read-only questions directly; apply the steps below to creates, updates, disables, re-enables, and deletions.
Gather all details needed for that operation, asking only for information not already established:

- Purpose and desired result; for an existing item, its exact identity and what should stay unchanged.
- Trigger: manual invocation, event/condition, or schedule including timezone and relevant timing rules.
- Inputs and connected sources, steps and conditions, expected output, and delivery destination.
- Any workflow-specific human approval, failure/retry behavior, and duplicate/overlap handling relevant to the requested work.
- For removal or disabling: dependent triggers, shared components to preserve, and the effect on already-running or suspended work.

Summarize the resulting configuration change and resolve ambiguity before seeking design confirmation.
Do not require irrelevant details, such as a schedule for a manually invoked workflow, or re-ask details the existing configuration already establishes.

1. Identify the addressed deployment and its configured source binding before offering to change a routine or workflow.
2. Load relevant existing routines, workflows, and composed instructions; reuse or modify the identified item rather than creating a duplicate.
3. Follow Bobi's shipped, generalized `agent-design` skill: establish expected behavior, unchanged behavior, and two to five positive/negative/regression scenarios; confirm the frame with the user.
4. Inspect the whole composed agent, including inherited instructions, deployed revision, context, tool access, workflows, monitors, and relevant memory.
5. Present the proposed design and review findings; obtain explicit confirmation tied to that proposal revision before editing source.
6. Prepare the change in an isolated worktree rooted at the configured source repository's fresh base; edit only the addressed deployment's allowed overlay.
7. Validate composition and configuration, run the skill's behavioral dry runs with external side effects disabled, and apply its review requirements.
8. Open one PR and reply with its URL, changed behavior, validation evidence, affected deployment, and “activates automatically after merge.”
9. Observe merge and deployment independently of the chat brain; report activation success or failure back to the original conversation.

Add chat correlation to the existing detached resume implementation in `bobi/webapp/run_actions.py:resume_run`, extending gate-revision validation and crash recovery.
Do not wire the unused threaded `try_resume_for_event`; each resumed run stays in a dedicated process so cancellation cannot terminate the manager.

Keep the conversation asynchronous: a pending clarification must not hold a model process open or prevent other conversations from using the agent.
A materially revised design requires renewed confirmation; an unrelated “yes,” quoted approval, bot message, or reply in another thread must not authorize it.
Anyone chatting may propose a separate change; confirmations should remain correlated to the initiating conversation and requester, without introducing an owner role.
A change of requester within a proposal requires an explicit conversational handoff rather than silently interpreting another person's reply as the pending answer.

### Source binding and instance isolation

Introduce an operator-provisioned binding from deployment identity to repository, base branch, writable overlay path, deployment configuration, and activation adapter.
Treat the binding as trusted configuration, not a repository URL or filesystem path supplied in message text.
Capture the installed revision separately from the authoring base revision so drift is visible during design and review.

A deployment using a shared team needs its own source overlay before this feature is enabled.
Reuse `from:` composition for the shared base and apply the change in that deployment's leaf overlay.
Verify the composed result because prose concatenation and keyed config merges do not guarantee that an appended instruction overrides the original.
Validate semantic changes as well as paths: an overlay can change `build.run_root`, `requires`, service access, and executable monitor commands.
Dependency installation, privilege expansion, and new service credentials are operator prerequisites outside the routine-change path; changed procedure/monitor code must be visible in review and evaluated only in an isolated candidate runtime.
The PR must not change shared base files, other deployment bindings, CI definitions, credentials, or the feature's own approval restrictions.
Requests that require those changes explain the boundary and leave the agent unchanged.
Run authoring and candidate evaluation without production runtime mounts, production service/deployment credentials, or merge authority.
Use disposable state and restricted test tools/egress; prompt instructions and a local worktree alone do not disable side effects because Bobi brains normally bypass interactive permission checks.
Constrain source publication through a repository/path-scoped publisher; activation remains a separate privileged operation.
This isolates new authoring work without claiming to sandbox all existing chat-agent capabilities.
Provisioning that dedicated overlay is an enablement prerequisite, not permission to fork or redirect an arbitrary repository from chat.

### Durable request and PR identity

Use a stable change-request ID scoped to deployment and originating conversation, retained through session rotation and worker retries.
Persist the requester, source event identity, design revision and confirmation, source/base commit, branch, PR URL, merged commit, target deployment, activation attempt, outcome, and notification status.
Use existing workflow state/handoff storage where sufficient; add only metadata that the existing workflow record cannot express.
Do not use the long-term memory file as the transaction database.

Expose user-facing states equivalent to `clarifying`, `awaiting confirmation`, `preparing PR`, `awaiting merge`, `activating`, `active`, `failed`, and `cancelled`.
Keep clarification loops and revised designs within one request; each unrelated request has a distinct ID.
Duplicate events and recovery after a crash must reconcile the existing branch/PR before creating anything new.
Serialize source publication and deployment per addressed agent; revalidate against newer merged changes before publishing a stale proposal.

### Automatic activation after merge

The activation controller must run outside the agent being replaced so an agent restart cannot lose the operation or its reply.
Verify merge from the configured repository and base branch using provider state; never trust a chat claim that a PR merged.
Bind deployment to the merged source commit and resolved package/dependency identity, not a floating checkout of `main`.
The existing Moda deploy workflow rewrites skill pins to current `moda-skills/main`; this activation path must instead retain reviewed dependency identities or revalidate and obtain a new reviewed change.
Limit the change to the configured deployment and fail visibly if credentials, artifacts, or the deployment adapter are unavailable.
For Moda-hosted agents, extend the existing `bobi deploy` path and CI orchestration in `moda-agents`.
Overmatch is explicitly in initial delivery scope despite being self-hosted; implement its deployment-specific adapter against the same merge, revision, safe-restart, health, and result-reporting contract.
The engineering design below verifies its source mapping and GCP IAP transport; live runtime/IAM readiness remains an enablement check.
Its partial deployment automation must be completed at the existing apply seam; do not assume Fly or a finished deploy workflow.
Do not expand this work into support for arbitrary self-hosting environments.
Public Bobi supplies generic change/provenance contracts and never imports private deployment code.

Before replacing the live package, validate the candidate and preserve the previous known-good artifact and configuration identity.
Reconcile conflicting runtime monitor records before activation: `MonitorRegistry._load` also reads root `run/package/monitors.yaml`, which source installation does not generally remove.
Block activation or explicitly migrate the conflicting target routine while preserving unrelated runtime overrides; prove the effective loaded configuration matches the review.
Use the deployment engine's lifecycle controls to quiesce/start the manager and preserve runtime state, memory, credentials, and resumable work.
Use a safe restart: stop admitting affected work as needed, allow running work to reach a safe boundary, preserve resumable state, and then replace/restart the agent.
If a safe boundary cannot be established within the adapter's bounded wait, keep activation pending, report the delay, and retry without forcing termination.
Specify and test the wait/retry values for each supported adapter before implementation readiness; do not silently abandon in-flight work.
Suspended workflows retain a numeric step index and resume against the installed definition today; preserve their original definition or defer incompatible changes until those runs complete.
An activation succeeds only when the intended package identity is installed, the manager passes its health probe, and the requested routine/workflow configuration is loaded, including verified absence or disabling for deletions and disables.
A process restart or a successful CI exit alone is insufficient proof.
Publish the activation result with request ID, deployment, merged commit, installed artifact identity, and health outcome.
Retry delivery of the final chat notification independently of deployment; a failed Slack send must not redeploy the agent.

For consecutive merges, serialize activations and prevent an older attempt or retry from overwriting a newer active revision.
If a newer merged revision supersedes a queued one, report the revision actually activated and which requested change it contains.
Automatic rollback is new capability: the inspected deploy failure path pauses or contains the runtime rather than restoring the previous package.
On activation failure, report “merged, activation failed”; restore the previous known-good package where replacement occurred and verify its health.
Report rollback failure explicitly and stop further automatic replacements until reconciled.
A rollback restores code/configuration, not external side effects already performed by a routine.

### Engineering design (2026-11-04)

This section makes the approved product contract concrete.
New module names and command shapes below are proposed interfaces, not existing APIs.

#### Verified implementation baseline

- Public Bobi: refreshed `origin/main` at `0650990b`; the earlier PRD baseline remains historical evidence only.
- Overmatch: refreshed `Velocity-Defense/bobi` `main` at `8727b32`; the local checkout directory and old remote alias are named `bobi-context`.
- Overmatch's `teams.conf` maps source `agents/alfred` to installed slot `commitment-keeper`, with `restart` set to `-`; another row owns engineering's `eng-team` slot.
- Overmatch currently pins Bobi `0.62.0` in `.github/bobi-version`; do not assume current public-main APIs exist on its VM.
- Overmatch main contains `scripts/compose.py`, `scripts/check.py`, `scripts/deploy.sh`, `scripts/deploylib.py`, CI, and `vm-facts.yml`; it does not contain the planned `deploy.yml`, `release.yml`, `vm-apply.sh`, or `vm_ops.py`.
- `scripts/deploy.sh` supplies GCP SSH through IAP; `scripts/compose.py` creates per-slot tarballs and a manifest containing source SHA and tree hashes.
- The existing Alfred installer installs and starts in separate operations, preserves runtime monitor overrides, and must not be used as proof of safe draining or transactional rollback.
- Overmatch's approved MOD-426 plan selects release-tag deployment, a VM-wide deployment lock, and a ten-minute idle wait; these are design commitments, not evidence that its unshipped apply machinery exists.
- Latest Bobi has `bobi/webapp/run_actions.py:resume_run`, which spawns the CLI resume command; the child owns the claim. Reuse that path rather than the unused threaded `try_resume_for_event`.
- `WorkflowRun` now includes checkpoints and run-key lookup, but `claim()` still documents a crash window leaving intermediate claim files; this feature's restart/replay guarantee requires recovery of that window.
- Ordinary `bobi agent ... restart` is stop/start, not an admission-and-drain protocol. `Session._inbox_loop` and monitor execution are additional work-start seams beyond subagent launch.

No VM was contacted during this planning pass.
Source inspection establishes the intended Overmatch target and transport, not its live version, IAM readiness, or production health.

#### Components and ownership

| Component | Location | Responsibility |
|---|---|---|
| Generalized design skill | New public `skills/agent-design.md` | One reusable design/evaluation method, distributed through the existing CLI and wheel. |
| Routine-change integration | New public `bobi/changes/` package | Typed binding/request/result schemas, chat correlation, validation, and publication/activation adapter contracts. No provider-specific deploy code. |
| Durable orchestration | Existing `bobi/workflow/` | Run lifecycle, gate persistence, dedicated resume processes, cancellation, and crash recovery. Change-request metadata lives in its persisted scope rather than a competing workflow engine. |
| Isolated authoring runner | Provider-controlled disposable runner/container | Run the skill against a source snapshot and isolated Bobi runtime; return questions, design, candidate patch, and evaluation evidence. |
| Narrow publisher | Trusted controller outside the authoring sandbox | Validate and publish the confirmed candidate to the configured repository; reconcile one branch/PR per request. |
| Safe activation guard | New public `bobi/maintenance.py` with existing runtime callers | Slot-scoped admission barrier, drain evidence, maintenance ownership, and crash-safe recovery state. |
| Moda adapter | Existing private deploy engine and CI in `moda-agents` | Merge-driven single-deployment activation, using shared public contracts. |
| Overmatch adapter | Existing transport and planned apply seam in `Velocity-Defense/bobi` | Single-slot activation for Alfred over IAP; share release-path locks and records. Do not deploy all `teams.conf` rows for a routine change. |

The controller invokes a trusted configured runner adapter; message text cannot select an executable, repository, image, or deployment target.
Use a disposable CI/container worker for authoring, with an explicit environment allowlist, model authentication, source snapshot, test fixtures, and isolated `BOBI_HOME`.
Do not inherit the live manager environment, production MCP connections, runtime mounts, GitHub publication credentials, or deployment credentials.
Questions and results return as structured artifacts; the controller sends chat replies through the existing gateway.
Do not give the authoring sandbox event-bus administrative credentials merely to send its own replies.
Run user-procedure evaluations with fixture/sandbox tools and explicit model-network access; ordinary brain permission bypass must not provide access to production resources.
Use GitHub Actions as the initial authoring runner for both Moda and Overmatch, with trusted workflow code on the source repository's protected base branch.
The controller dispatches a request ID and phase, then polls the resulting job/artifact identity; request content is bounded private input, never executable workflow code.
Runner execution uses the existing public Bobi reference image at a pinned released digest, with the generalized skill, in a disposable container launched by a shared public runner helper.
Mount a read-only source snapshot and confirmed request input, plus an empty writable output directory; create disposable runtime state inside the container.
Do not mount the Docker socket, host home, host cloud metadata access, Git credential helpers, or GitHub Actions token; run unprivileged with dropped capabilities and no privilege escalation.
The runner helper enforces network isolation: deny production/private networks and permit only the configured model endpoint through an allowlisted proxy; fixture tools serve routine evaluation locally.
Supply dedicated evaluation model credentials through an allowlisted secret channel, not the deployed agent's credential environment. Overmatch's production Vertex/model identity must not be inherited implicitly.
Each phase returns only a schema-validated handoff, candidate patch, and redacted scenario report. Do not upload credential stores or raw runtime homes as artifacts.
Reconstruct the next phase from the durable handoff/source revision instead of relying on a runner VM remaining alive between user replies.
The publisher runs outside this container, with its own restricted credential and no execution of candidate-provided scripts.
Add provider dispatch/poll wiring in each private source/deployment repository; keep the shared runner/validation implementation in public Bobi rather than duplicating scripts.
Prove this design with hostile-candidate tests at the implementation gate; failed isolation blocks enablement, not a fallback to an ordinary production subagent.

#### Configuration and request contract

Keep operator bindings outside the writable team overlay and outside model-authored patches.
Each binding contains a stable deployment ID, repository identity, base branch, source directory, runtime slot, allowed source surfaces, runner/publisher/activation adapter IDs, and installed provenance.
For Overmatch, the verified source directory is `agents/alfred` and slot is `commitment-keeper`; repository access and live runtime identity still require enablement checks.
Allow role/context edits only when linked to the selected routine/workflow; forbid changes to shared bases, deployment scripts, credentials, build/dependency declarations, source bindings, and approval restrictions.
A path allowlist alone is insufficient: inspect `agent.yaml` changes by field and reject symlinks or paths escaping the overlay.

The persisted change scope carries:

```json
{
  "schema_version": 1,
  "request_id": "opaque-generated-id",
  "deployment_id": "configured-instance",
  "operation": "create",
  "target_id": "weekly-digest",
  "conversation": "provider-conversation-reference",
  "requester_id": "provider-user-id",
  "source_event_id": "provider-event-id",
  "design_revision": 1,
  "gate_id": "opaque-per-suspension-id",
  "confirmed_revision": null,
  "base_sha": null,
  "candidate_sha": null,
  "pr_url": null,
  "merged_sha": null,
  "artifact_sha256": null,
  "activation_status": "not_requested",
  "notification_status": "pending"
}
```

These are example values, not observed wire output.
Operations are `create`, `update`, `disable`, `enable`, and `delete`; reads use the existing installed-package readers without creating a mutation run.
Null denotes not-yet-known identity, not an empty success result.
Keep transcripts and secrets out of this metadata; store bounded design/scenario artifacts under the request's private working state.
Capture original event/conversation/requester values from normalized ingress metadata; do not accept model-written identity fields as proof of who confirmed a design.
Revisions increment when the proposed configuration meaning changes; confirmation applies only to that revision.
Generate and persist a fresh opaque `gate_id` for every suspension before notifying the user, including repeated visits to the same step.
Atomically validate `request_id`, `gate_id`, and `design_revision` in the winning claim: a frame approval must not confirm a later design gate even when its revision number is unchanged.
Use a generated request ID and source-event dedup key, then one deterministic branch such as `agent-change/<request-id>` and one PR.

#### Chat, confirmation, and publication

1. The entry agent classifies intent and identifies an existing routine/workflow where relevant. A small shipped guide invokes the generalized skill rather than copying it into each role.
2. Read requests describe effective installed configuration and separately label pending proposals. Mutations start a workflow with the trusted binding and source event context.
3. Each runner turn returns a question, proposed design, or evaluated candidate. Persist the turn outcome before replying; do not leave a model process waiting for a human.
4. Associate replies with the exact outstanding request and gate revision. If the thread has more than one pending request, ask which one; quoted or bot-authored text is not confirmation.
5. Extend the existing detached resume path to pass the expected request/gate/revision token; validate it atomically in the claiming child before advancing. Preserve rejection routing and rework loops.
6. Recover interrupted `.claiming`/`.resuming` transitions using owner/process identity and durable state; never steal a live claim. A crash after accepted confirmation must yield either one continued run or a recoverable waiting state, not a permanently stranded request.
7. The isolated worker edits the fresh source snapshot, composes the full candidate, and performs the skill's behavioral evaluation. Failed scenarios return to revision; material design changes return to confirmation.
8. The publisher takes `request_id`, `confirmed_design_revision`, `base_sha`, candidate diff, and evidence. It derives the target from the trusted binding, checks field/path restrictions, and requires evaluation against the exact candidate being published.
9. If the repository base advanced, rebase and reevaluate; renew confirmation when the design changes. Before retrying an ambiguous push/PR result, look up the deterministic branch and existing PR.
10. Post the PR link and activation expectation. Neither authoring nor publication merges the PR; normal repository review controls apply.

Reuse `bobi/webapp/run_actions.py:resume_run` as the existing subprocess seam; if layering requires moving it, retain one implementation and compatibility imports for current callers.
The chat bridge must not invoke the unused threaded resume helper.
Workflow definitions used by pending authoring runs are versioned with the feature and protected from routine-edit patches.
Expose a thin `bobi agent <slot> changes start/answer/status` CLI over this integration; reuse existing launch `--id`, structured inputs, and requested-by metadata rather than inventing another launcher.
Prepare the worktree from the binding's freshly fetched base; existing workflow worktree creation from local HEAD is not sufficient. Extend its source/base option or prepare one bound worktree and disable duplicate workflow worktree creation.
Use canonical conversation addressing through the existing gateway for notifications; adapt the current Slack notifier, which uses channel/thread fields, rather than reconstructing addresses in model text.
Record SHA-256 of loaded skill bytes and installed Bobi version in the evidence.

#### Safe restart protocol

Add one maintenance implementation used by both adapters; do not approximate it with a CLI status-text poll.
Proposed external CLI operations are `bobi agent <slot> maintenance begin/status/finish/recover --json`; return typed status, operation ID, target revision, and drain evidence.
Only the trusted deployment controller calls these operations; ordinary chat never supplies a maintenance command.

- Take the adapter's deployment lock, then create a durable maintenance operation for that slot. A second operation cannot supersede it.
- Close admission at actual work-start boundaries: new manager turns, independent subagent launches, workflow resumes, and monitor dispatch including command/native/out-of-band checks. Use the same slot-scoped barrier for the final check and admission so there is no check/start race.
- Let already-admitted work finish, including child work needed for an active turn to complete. Track that lineage as existing work; blocking all child launches would deadlock the drain.
- Queue inbound events without acknowledging unprocessed work; preserve the existing ACK-after-processing/replay semantics. Pause scheduling without advancing monitor cursors for work that did not run; keep existing missed-slot policy.
- Require manager turn completion, no active work leases, and reconciled monitor callbacks before reporting drained. Unknown or stale liveness data blocks activation rather than implying idle.
- Compare complete affected workflow definitions and supporting routine files against waiting runs. Defer if a waiting run depends on changed/deleted material; do not rely on a step-name fingerprint to prove semantic compatibility. In this release choose deferral, not a new workflow-version execution engine.
- Proposed default: wait at most 600 seconds per drain attempt, matching Overmatch's approved ten-minute boundary. If busy, release the pre-mutation barrier, leave the update pending, notify once, and retry after 300 seconds; coalesce repeated status notices. These settings are operator-configurable and never authorize force termination.
- After drain, hold admission closed through stop, package replacement, and startup. New manager startup and supervisor auto-restart must respect an active maintenance operation, so a watchdog cannot launch the old package during replacement.
- The controller issues an operation-bound startup permit for one expected package digest and manager generation. It permits initialization, subscription registration, and health/config verification while model turns and monitor execution remain blocked. The rollback path obtains a new permit for the previous verified digest; a generic start or watchdog respawn without a matching permit is refused. Revoke the permit when the operation ends.
- Mark `mutation_started` durably before replacing files. Before that marker, a dead controller can safely release the barrier after ownership checks; afterward, recovery must reconcile installed identity and restore/start a verified package before reopening admission. Never expire a mutation-phase guard on a timer alone.
- Preserve the slot's state, receipts, memory, and credentials; do not use `--fresh` or restart a shared event server for a routine-only update.
- Verify the new manager and effective configuration, then release maintenance. On failure restore the previous package, verify it, and report failure; failed rollback leaves admission closed with explicit operator recovery status.

Apply strict tests at `Session._inbox_loop`, `launch_agent`, CLI workflow resume, monitor dispatch, service start/restart, and supervisor respawn; these are load-bearing runtime seams.

#### Activation adapters and merge reconciliation

The activation contract takes a verified request ID, merged source SHA, trusted deployment binding, and expected artifact digest.
Results distinguish `pending_safe_boundary`, `active`, `failed`, and `rollback_failed`; include observed installed revision/digest, health result, and a retriable flag.
Persist results outside the replaced agent, keyed by deployment and request/revision, so retries and chat delivery can reconcile them.

Verify merge and approved repository/base with GitHub provider state, not PR body text, labels, or chat assertions.
Check the entire final merged diff and the resulting package against the request's allowed surfaces; human amendments outside routine scope route to the ordinary release path rather than this automatic adapter.
Compose the exact merged revision with pinned bases/dependencies and validate it again before activation; unrelated changes on current `main` must not ride along.
Compare the complete candidate package against the recorded installed package, not only the PR diff: earlier merged but undeployed changes could otherwise be activated accidentally.
If the candidate includes changes outside the confirmed request, leave activation pending and route those changes through ordinary release/review; never manufacture a synthetic partial deployment silently.
Retain the last known-good package and provenance for rollback without restoring old mutable receipts/state.
Serialize against ordinary releases as well as other chat changes; an older retry cannot replace a newer deployment record.
Do not claim a newer revision includes the requested behavior unless its composed configuration/evidence proves it; superseded or removed requests receive an explicit status.

For Moda, wrap the existing deploy engine with this preflight/maintenance/result contract and preserve reviewed skill pins instead of resolving `moda-skills/main` at deployment time.
For Overmatch, extend MOD-426's apply seam and existing IAP transport rather than cloning it into public Bobi or adding another all-team deploy script.
The minimal chat activation must compose and apply only the configured slot, leave its env/OAuth cache unchanged, and never update Bobi itself or restart engineering/shared ingress.
A separately approved runtime upgrade to a released Bobi containing the required APIs is an enablement dependency; this feature's routine PRs never bump the runtime pin.
The release-tag path remains authoritative for framework, secrets, shared dependencies, and other broad changes.
A narrow merge-trigger exception for verified routine/workflow PRs needs a dated amendment to Overmatch's approved release plan and a CI identity permitted by its WIF policy; never run privileged apply from a PR branch.
The exact missing apply scripts and shared lock/ledger must be implemented or adopted from completed MOD-426 work, not assumed shipped.
Live GCP access, source publishing credentials, protected-branch permissions, and the current VM version must be verified during enablement without printing secrets.

#### Implementation checkpoints and release ordering

1. Ship and evaluate the generalized skill; define bindings and runner/publisher contract tests.
2. Add durable chat orchestration and exact gate-revision claims using existing workflow primitives; prove crash recovery and isolated evaluation.
3. Add safe maintenance/drain with both supervised and unsupervised runtime tests; this is required before either adapter can promise safe redeployment.
4. Integrate Moda and Overmatch adapters, sharing their ordinary release locks and trusted records. Land compatible consumers before enabling any new contract they require.
5. Release public Bobi through its release runbook as a separate release task, update consumer pins through their normal reviewed upgrade process, then enable this feature on configured deployments.
6. Prove an end-to-end create/update/delete sequence and a failed activation on both target environments; keep the feature disabled where prerequisites or acceptance checks are incomplete.

Do not treat release authority or production deployment authorization as implied by approving this plan.

### Acceptance criteria

| ID | Observable outcome |
|---|---|
| AC1 | One-off, preference, and routine/workflow read requests create no config PR or schedule; an ambiguous request receives a clarification. |
| AC2 | A new weekly routine includes an explicit timezone, trigger, procedure, destination, and testable output before design confirmation. |
| AC3 | A person with chat access can complete the proposal flow without owner registration; repository review policy still gates merge. |
| AC4 | No source edit occurs before design confirmation; no live routine change occurs before merge and successful activation. |
| AC5 | The PR changes only the addressed deployment's overlay and includes composed-agent review and behavioral evidence. |
| AC6 | A merged PR automatically activates the intended revision on that deployment and posts verified success to its originating conversation. |
| AC7 | A second deployment sharing the base retains its prior installed identity and behavior. |
| AC8 | Replayed messages, approval events, merge events, and worker restarts produce one logical PR and one effective activation for a revision. |
| AC9 | Rejected/closed PRs, cancelled proposals, and failed activation do not become active instructions through memory or runtime monitor commands. |
| AC10 | Failed replacement restores a verified prior version or reports rollback failure; concurrent merges never allow stale activation. |
| AC11 | Missing source binding, skill, credentials, or activation support yields a precise prerequisite error without claiming completion. |
| AC12 | Existing runtime memory and resumable work survive activation under the documented lifecycle policy. |
| AC13 | Create, inspect, update, disable/re-enable, and delete scenarios work for routines and workflows; mutations gather relevant details and follow the config PR and automatic redeploy path. |
| AC14 | A deletion removes its exclusive triggers/references without breaking shared dependencies; the deployed item no longer starts new runs. Existing runs follow the documented lifecycle policy. |
| AC15 | Requests for standalone behavioral or agent-wide policy changes are identified as outside scope; they do not create config PRs or become active through memory as a substitute. |
| AC16 | An installed Bobi authoring worker loads and uses the generalized `agent-design` skill without requiring the Moda skill pack; its revision is recorded in the evidence. |
| AC17 | Generated changes include scenario-based evidence of intended behavior and regression checks; failed evaluations lead to revision, not a claim of effectiveness based only on valid config. |
| AC18 | Both Moda-hosted and Overmatch-specific activation complete the merge-to-live flow; generic self-hosted support is not required. |
| AC19 | An update merged during running work waits for a safe restart; unsafe activation remains pending and reports its status without abandoning work. |

### Alternatives

- Direct runtime edits are quicker but bypass review, provenance, and reproducible deployment.
- Memory-only instructions cannot reliably represent schedules and can activate unreviewed behavior.
- A new scheduler, generic approval service, or Fly deploy engine duplicates existing seams.
- Owner allowlists were rejected by the user as unnecessary complexity; existing repository merge controls remain authoritative.

## Relevant files

### Existing (verified 2026-11-03)

- `bobi/prompts/base.md`, `bobi/prompts/sleep_cycle.md`, `bobi/prompts/resolver.py`, `bobi/memory.py`: routing guidance and memory boundary.
- `bobi/events/client.py`, `bobi/events/gateway.py`: chat event context and replies.
- `bobi/workflow/schema.py`, `bobi/workflow/state.py`, `bobi/workflow/orchestrator.py`: durable multi-turn work and resume claims.
- `bobi/compose.py`, `bobi/install.py`, `bobi/runtime_guard.py`, `bobi/cli.py:monitor_add`: composition, installed provenance, and runtime mutation seams.
- `bobi/supervisor/probe.py`, `bobi/supervisor/snapshot.py`: health and deployment observations.
- `docs/BUILDING_AGENT_TEAMS.md`, `docs/WORKFLOW_ENGINE.md`, `docs/SECURITY.md`: affected authoring and operational contracts.
- Sibling `moda-skills/plugins/moda/skills/agent-design/SKILL.md`: starting point to generalize, including dependencies that must be adapted.
- `bobi/cli.py:skill` and `pyproject.toml`: existing skill lookup and packaged distribution.
- Sibling `moda-agents/.github/workflows/deploy-agent-teams.yml`: existing tag/manual deploy orchestration.
- Sibling `moda-agents/bobi-deploy/bobi_deploy/src/bobi_deploy/deploy.py`: existing update and health path.

### New

- Proposed `skills/agent-design.md`: canonical generalized authoring skill shipped with Bobi, adapted from the existing Moda skill.
- A chat-update workflow and context guide: invokes the shipped skill and carries its questions, confirmations, and evidence across turns without duplicating its methodology.
- A small generic request/provenance component only where existing workflow state cannot support revision checks, reconciliation, and activation results.
- Instance overlay and deployment binding for each enabled agent; exact paths belong to its source repository.
- Focused chat-update and activation integration suites named below.

## Questionables

- **Q: How should Overmatch's release-only policy coexist with this feature?** Recommendation: a narrow automatic-deploy exception for verified routine/workflow PRs affecting Alfred only, sharing MOD-426 apply/lock/ledger machinery; broader changes retain release tags. Awaiting confirmation.
- **Engineering defaults for approval:** GitHub Actions with an isolated container and dedicated model credentials for authoring; 600-second drain / 300-second retry defaults. These are proposed implementation decisions, not claims of existing infrastructure.
- **Dependency:** reconcile missing MOD-426 apply work at its existing seam; land the required single-slot apply path before enabling Overmatch activation.
- **Q: Which design methodology should this feature use?** Options: a new authoring flow or the existing `agent-design` skill adapted for Bobi.
  **Decision (2026-11-04, Luke):** use our `agent-design` skill to establish effective changes; bring it into Bobi and iterate/generalize it during integration.
- **Q: Does this include general behavioral changes?** Options: routines/workflows only or general agent behavior as well.
  **Decision (2026-11-04, Luke):** routines and workflows only; gather relevant details conversationally, update agent config, submit a PR, and handle redeployment after merge. General behavioral changes are deferred.
- **Q: Who may propose changes?** Options: agent owners or anyone with chat access.
  **Decision (2026-11-03, Luke):** anyone who chats with the agent; avoid an owner authorization system.
- **Q: Does this end at a PR?** Options: PR only or automatic activation after merge.
  **Decision (2026-11-03, Luke):** include automatic activation after merge.
- **Q: Which deployments change?** Options: addressed agent only or all users of its team package.
  **Decision (2026-11-03, Luke):** only the addressed agent.
- **Q: Which activation environments ship first?** Options: Moda-hosted deployments first, or hosted and local/self-hosted together.
  **Decision (2026-11-04, Luke):** Moda-hosted first, with Overmatch's self-hosted deployment explicitly included because this feature is being built for Overmatch. Broader self-hosted support remains deferred; Overmatch needs a verified deployment-specific integration.
- **Q: What happens to running work during activation?** Options: drain with a bounded timeout and defer if busy, or interrupt and resume supported jobs.
  **Decision (2026-11-04, Luke):** use a safe restart when an update merges while work is running. Wait for a safe boundary and defer if safety cannot be established; adapter-specific wait/retry details must be specified and tested.

## Phases

### Phase 1 - Bind a chat request to an editable agent

- [ ] Inspect Overmatch's deployment and document its concrete adapter, source binding, safe-restart controls, health evidence, and recovery procedure alongside the Moda-hosted path.
- [ ] Specify bounded safe-restart wait/retry behavior for both adapters and close the remaining technical details in readiness review.
- [ ] Generalize and ship `agent-design` through Bobi's existing skill distribution; remove required dependencies on unshipped Moda skills and define canonical ownership.
- [ ] Evaluate and iterate on the generalized skill using representative routine/workflow scenarios, retaining transcripts and actual outcomes.
- [ ] Provision the addressed agent's overlay and trusted source binding; ensure the authoring worker can load the shipped skill and record its revision.
- [ ] Implement the shared isolated-container runner and private GitHub Actions dispatch/poll adapters, with dedicated evaluation credentials and constrained input/output artifacts.
- [ ] Specify persisted request fields and confirmation/revision checks using workflow state where possible.

**Validation gate**

- [ ] Prove `bobi skill agent-design` loads from a built, installed wheel outside the checkout, with every required reference resolvable and no Moda pack installed.
- [ ] Record real-brain skill evaluation for create, update, disable/re-enable, and delete; iterate until intended outcomes and unchanged-behavior scenarios pass, identifying any skipped checks.
- [ ] Hostile candidate code cannot read host/production credentials, reach private/metadata endpoints, escape through symlinked output artifacts, execute code through publisher inputs, publish a PR, or deploy; remove each isolation restriction in turn and observe the corresponding test fail.
- [ ] New `tests/test_chat_agent_updates.py` verifies binding validation, path confinement, revision correlation, and duplicate request reconciliation.
- [ ] Two deployments inheriting one base compose independently; changing the target overlay leaves the sibling unchanged.

### Phase 2 - Carry agent-design from Slack to PR

- [ ] Implement routine/workflow CRUD intent detection, read-only inspection, operation-specific clarification, frame/design confirmations, isolated config editing, scenario evaluation, and PR delivery.
- [ ] Keep general behavioral changes outside scope; distinguish them from routine/workflow-specific instructions.
- [ ] Keep proposal status separate from active instructions through the sleep-cycle path.
- [ ] Handle cancellation, revision, interrupted authoring, existing-PR recovery, and unavailable dependencies.
- [ ] Add dedicated-process chat resume dispatch and isolated authoring/evaluation with constrained source publication.

**Validation gate**

- [ ] New `tests/integration/test_chat_agent_updates.py` exercises routine and workflow CRUD, disable/re-enable, general-behavior exclusions, restart between confirmation turns, and a real sandbox PR.
- [ ] Verify deletion handles dependent triggers and shared components, and read-only inspection distinguishes installed configuration from pending changes without writing source.
- [ ] Extend `tests/integration/test_gate_verdict_resume.py`, `tests/test_webapp_resume.py`, and `tests/test_workflow_state.py` to prove exact per-suspension gate tokens, delayed approval at unchanged revision, and crash recovery at each claim transition.
- [ ] Prove source worktree base freshness, canonical same-thread replies, and one existing PR after a publish timeout.
- [ ] Parametrize the orchestration flow with stub and real Claude; require a recorded real-brain run for intent judgment, skill use, and approval behavior.
- [ ] Run a sleep cycle over proposed, confirmed-but-unmerged, cancelled, and activated changes; inspect resulting memory and next-session behavior.

### Phase 3 - Activate merged changes and report truthfully

- [ ] Extend the Moda-hosted deploy controller and implement the Overmatch-specific adapter with merge triggering, target/revision verification, serialized reconciliation, and durable result delivery.
- [ ] Preserve prior artifacts and verify candidate activation and rollback.
- [ ] Wire end-to-end notification without depending on the replaced manager's session.

**Validation gate**

- [ ] New private `bobi-deploy/tests/test_chat_update_activation.py` covers wrong repo/branch/target, duplicate merge, missing credentials, superseded revisions, health failure, rollback failure, and an unrelated earlier undeployed change in the candidate package.
- [ ] New public `tests/test_maintenance.py` and `tests/integration/test_maintenance_flow.py` cover admission races, active turns/monitor commands, nested work needed to drain, queued-event replay, controller crashes before/after mutation, permitted candidate startup, refused stale respawn, and rollback startup.
- [ ] Exercise both Moda-hosted and Overmatch's verified deployment path in isolated environments: merge a real routine PR, observe automatic install and healthy startup, and demonstrate the changed routine without manual deploy commands.
- [ ] Merge while work is running; prove safe restart preserves work and that an unsafe/busy target remains pending with a clear chat status rather than being forcibly replaced.
- [ ] Verify the sibling deployment, persisted memory, and an in-flight/resumable job against the selected lifecycle policy.
- [ ] Seed a conflicting runtime monitor override and prove activation blocks or reconciles only that routine.
- [ ] Advance the remote skill branch after review and prove activation retains the reviewed dependency.
- [ ] Change a suspended workflow definition and prove safe deferral or original-definition resume; cancel a resumed authoring run and prove the manager remains alive.

## Proof of work

Engineering review (2026-11-04): security required real isolation and mechanical publication; implementer required exact gate occurrence identity, fresh-base worktrees, and canonical replies; integration required installed-to-candidate comparison and operation-bound startup permits.
All findings were incorporated; no review finding was silently rejected.
The review did not prove runner isolation, live GCP access, or runtime deployment behavior.

This is new behavior: assert observable outcomes rather than requiring tests to fail on nonexistent APIs.
Use mutation checks for negative guarantees: remove the confirmation revision check; permit an outside-overlay write; route a proposal to `monitor_add`; let the sleep cycle promote a pending routine; remove merge verification; remove the stale-revision check; report active before the health/revision check.
Each corresponding test must fail with its named mutant applied.
Record before/after composed prompts and behavioral transcripts; use sandbox service credentials and disabled outward effects for routine evaluation.
Run the real Claude path because classification and skill-driven interaction are the risk; deterministic tests alone cannot establish that behavior.
A skipped real-brain or real deployment test is missing acceptance evidence, not success.

Regression commands for the public changes:

```sh
pytest tests/test_compose.py tests/test_install.py tests/test_runtime_guard.py tests/test_long_term_memory.py tests/test_sleep_cycle.py tests/test_workflow_state.py tests/test_monitors.py -q
pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q
pytest tests/integration/test_chat_agent_updates.py -q
```

The new integration suite and private activation suite are deliverables, not tests that exist today.
The implementation PR must include a real request/result payload, PR URL, merged revision, installed identity, health evidence, same-thread notification, and failure/rollback demonstration.
No frontend changes are required; framework versions and changelogs remain untouched by feature PRs.

## Lane map (only if fanned out)

Not assigned; this PRD defines checkpoints, not separate builder assignments.

## Amendments

- **2026-11-04** (engineering planning): refreshed Bobi/Overmatch main, verified Overmatch slot and partial GCP deployment implementation, specified component/data/claim/maintenance/adapter contracts, and identified the narrow release-policy exception and remaining runner provisioning work.
- **2026-11-04** (Luke technical approval): approved the technical approach, selected safe restart, and selected Moda-hosted initial support with Overmatch's self-hosted deployment explicitly included; general self-hosted support is deferred. Overmatch adapter details and bounded wait/retry behavior remain technical readiness work.
- **2026-11-04** (Luke approval): approved the PRD product scope; activation-environment coverage and running-work policy remain explicitly unresolved before implementation readiness.
- **2026-11-04** (Luke design-skill direction): made `agent-design` mandatory for mutations and added generalization, packaged distribution, iterative behavioral evaluation, and canonical ownership requirements.
- **2026-11-04** (Luke scope decision): narrowed to routine/workflow CRUD and disable/re-enable, with conversational requirements gathering, config PRs, and automatic redeployment after merge; deferred standalone behavioral changes and added read-only inspection and deletion acceptance criteria.
- **2026-11-04** (Luke requested clarification): organized the feature around user requests, no-PR memory versus PR-backed setup changes, affected configuration files, and the user workflow; moved infrastructure into a separate supporting-capabilities table.
- **2026-11-03** (Luke requested clarification): replaced “Verified primitive” with “Existing capability,” connected each capability to a concrete use case, and added the end-to-end Monday-summary example.
- **2026-11-03** (planning session): created from MOD-446 and inspected architecture; incorporated Luke's decisions on chat access, automatic activation, and addressed-agent scope.

## Notes

Keep `MOD-446` distinct from historical GitHub issue `#446` referenced in `compose.py`; they are unrelated identifiers.
Changes span public Bobi, the agent source/deployment repository, and potentially the shared skill package.
The product decisions and technical approach are approved.
Engineering review completed across security, integration, and implementer lenses; all concrete findings were incorporated.
Readiness remains conditional on approval of the engineering defaults, resolving the Overmatch release-policy exception, and sequencing the missing MOD-426 apply dependency.
Runner isolation and live activation are implementation acceptance gates; neither has been executed during planning.
The proposed safe-restart defaults are 600 seconds to drain and 300 seconds between attempts; final engineering approval must include these values.
Live deployment enablement and the required public-runtime release remain explicit prerequisites, not proof already obtained.
