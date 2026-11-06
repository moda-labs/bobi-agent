# Manage agent routines and workflows from chat

> **Status:** PRD approved by Luke (2026-11-04); engineering planning delegated to the lead engineer
> **Tracking issue:** [MOD-446](https://linear.app/moda-labs/issue/MOD-446/enable-bobi-agent-updates-from-chat) · **Created:** 2026-11-03 · **Last amended:** 2026-11-04

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

Make the generalized skill available to installed Bobi agents without requiring each team to install the Moda skill pack.
Bobi already has a CLI and wheel distribution path for skills; the lead engineer will select the integration.

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

Keep the conversation asynchronous: a pending clarification must not hold a model process open or prevent other conversations from using the agent.
A materially revised design requires renewed confirmation; an unrelated “yes,” quoted approval, bot message, or reply in another thread must not authorize it.
Anyone chatting may propose a separate change; confirmations should remain correlated to the initiating conversation and requester, without introducing an owner role.
A change of requester within a proposal requires an explicit conversational handoff rather than silently interpreting another person's reply as the pending answer.

### Source and addressed-agent isolation

The agent must know which source configuration belongs to its deployment before offering to change it.
Chat text cannot redirect the change to an arbitrary repository or deployment.
Changes must affect only the addressed agent, including when multiple deployments share a base team.
The lead engineer will choose the source-binding and overlay implementation.

Limit edits to the requested routine/workflow and the instructions necessary for it to work.
Dependency installation, new service credentials, shared-base changes, deployment scripts, and approval restrictions are outside the routine-change flow.
If a request requires one of these, explain the prerequisite rather than silently expanding the change.

Candidate evaluation must not execute unreviewed routines against production resources or modify the live configuration.
The agent must not bypass the PR and activation flow through direct runtime edits.

### Progress and continuity

The agent must retain each change request's conversation, confirmed design, PR, and activation outcome across session rotation and restarts.
Clarifications and revised designs remain part of the same request; unrelated changes remain separate.
Duplicate messages and retries must not create duplicate PRs or repeat activation.
The user can distinguish clarification, awaiting confirmation, preparing a PR, awaiting merge, awaiting safe restart, activating, active, failed, and cancelled states.
A materially changed design needs renewed confirmation; an unrelated or stale reply cannot approve it.
Long-term memory is not the authoritative record of whether a configuration change has been approved or deployed.

### Automatic activation after merge

A merged routine/workflow PR automatically activates on the addressed agent without a manual deployment command.
Moda-hosted agents and Overmatch's specific self-hosted deployment are in initial scope; general self-hosted support is deferred.
The lead engineer will determine how each deployment integrates with its existing release controls.

Verify that the PR actually merged and deploy the intended reviewed configuration.
Do not activate unrelated changes or silently affect another agent sharing the same base team.
Existing runtime overrides must not prevent the approved configuration from taking effect without an explicit error.

When work is running, wait for a safe restart and report that activation is pending.
Preserve runtime memory, credentials, business state, and resumable work; do not force an unsafe restart to meet a timeout.
A workflow change or deletion must not cause an existing run to continue against incompatible steps.

Report success only after the intended configuration is installed, loaded, and the agent is healthy.
For removal or disabling, verify that the routine/workflow no longer starts new work.
A successful CI job or process restart alone is insufficient evidence of activation.

Keep activation and its final notification recoverable even when the agent itself restarts.
Repeated or out-of-order events must not redeploy an obsolete revision over a newer one.
A failed Slack notification must not cause deployment to run again.

On failure, report “merged, activation failed” and restore the previous working configuration where replacement occurred.
Verify recovery and explicitly report rollback failure when it cannot be completed.
Do not restore older business state over completed external operations or claim that rollback reverses those operations.

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

## Approved product decisions

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
  **Decision (2026-11-04, Luke):** use a safe restart when an update merges while work is running. Wait for a safe boundary and defer if safety cannot be established; timing and retry mechanics belong to the engineering plan.

## Engineering handoff

The lead engineer owns the implementation plan, architecture choices, component/API design, deployment integration, and detailed test strategy.
This PR approves the product requirements and acceptance criteria; it does not select an authoring runner, prescribe maintenance APIs, or set drain/retry timing.

The engineering plan must account for the following product requirements:

- Use and generalize `agent-design` in Bobi, evaluating whether generated changes produce the requested outcomes.
- Preserve the addressed-agent boundary and existing memory behavior; keep pending changes inactive until approved and deployed.
- Support Moda-hosted deployment and Overmatch's specific self-hosted deployment, including the source-to-runtime mapping and existing release controls.
- Handle safe restart, running/suspended work, duplicate requests, failed activation, and recovery without losing user state or silently affecting another agent.
- Return progress and verified activation results to the originating conversation.

## Amendments

- **2026-11-04** (Luke direction): removed the engineering design, implementation phases, proposed infrastructure defaults, and detailed test plan from this PR; retained the approved PRD and acceptance criteria for engineering planning by the lead engineer.
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
This is a PRD-only change; implementation planning and build readiness are owned by the lead engineer.
