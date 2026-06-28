# Two-Layer Engineering Org Migration

## Context

Moda wants the eng-team/bobi-agent architecture to move from a three-layer
steady state:

```text
director -> persistent project lead -> engineer
```

to a two-layer steady state:

```text
director -> repo worker
```

U0952RZTHBR agreed to this direction on 2026-06-28 after discussing the
operating model. The goal is to remove persistent project leads as the normal
middle layer while keeping the engineering workflow quality gates intact.

This spec stops before broad implementation. The change touches runtime routing,
agent package configuration, monitor responsibilities, Slack attribution, and
backward compatibility for installed teams.

## Current Model

The current eng-team package treats the director as an org control plane and
launches one persistent `project_lead` per managed repo. Those leads subscribe
to repo and tracker events, decide which workflow applies, launch engineer
workflows, post project status, and answer routine project questions.

Important current surfaces:

- `agents/eng-team/roles/director/ROLE.md`
  - derives managed repos from GitHub subscriptions
  - relaunches persistent project leads during startup reconciliation
  - routes human work to project leads
  - aggregates status by asking each lead
- `agents/eng-team/roles/project_lead/ROLE.md`
  - owns repo event handling
  - dispatches engineers
  - applies issue/PR/comment policy
  - handles per-repo status reporting
- `agents/eng-team/agent.yaml`
  - defines deterministic `auto_dispatch` rules for issue lifecycle, PR
    feedback, and PR close workflows
- `agents/eng-team/monitors/defaults.yaml`
  - emits repo-wide monitor findings, including status roundup
- `bobi/events/reactor.py`
  - auto-dispatches matching events into workflows before the LLM sees them
- `bobi/subagent.py` and `bobi/workflow/orchestrator.py`
  - already carry `requested_by`, `run_key`, workflow input fields, and
    lifecycle events for launched workers

## Target Model

The director becomes the only persistent human-facing orchestration agent.
Repo-specific work happens in short-lived or workflow-scoped repo workers.
Deterministic routing moves into an explicit eng-team router rather than being
left to prompt reasoning. The director invokes or benefits from that router; it
does not hand-build shell commands from untrusted event text.

```text
director
  - subscribes to Slack, GitHub, tracker, monitor, and lifecycle topics
  - owns repo routing and requester attribution
  - dispatches workflows directly with --role engineer
  - aggregates status from registry, GitHub, tracker, and worker lifecycle
  - escalates blocked work to humans

repo worker
  - runs one workflow or bounded investigation
  - receives explicit repo, tracker, requester, and source event context
  - writes handoff/status through the existing workflow contract
  - terminates when complete unless explicitly launched as a temporary monitor
```

Persistent `project_lead` remains available as a compatibility role during the
migration, but it is no longer launched during normal startup once direct mode
is enabled.

## Repo Config

Add a first-class managed repo declaration to agent configuration rather than
using persistent lead subscriptions as the repo routing table.

Proposed `agent.yaml` shape:

```yaml
engineering_org:
  dispatch_mode: direct
  managed_repos:
    - repo: moda-labs/bobi-agent
      path: ~/dev/bobi-agent
      tracker:
        kind: github_issues
    - repo: moda-labs/jobtack
      path: ~/dev/jobtack
      tracker:
        kind: linear
        team: JOB
```

Rules:

- `dispatch_mode: direct` means the director does not launch project leads.
- `dispatch_mode: leads` keeps the current behavior for compatibility.
- Missing `engineering_org.dispatch_mode` defaults to `leads` for existing
  installs.
- `managed_repos[].repo` is the canonical GitHub `owner/name`.
- `managed_repos[].path` is the local checkout path used as the workflow cwd.
- A production-ready entry should also support:
  - `host`, defaulting to `github.com`, for GitHub Enterprise compatibility
  - stable remote URL validation, so a renamed or transferred repo is detected
  - optional `id` when the source service exposes a durable repository ID
  - path expansion and validation before launch
  - duplicate checkout handling by making `repo + host` unique unless an
    explicit `checkout_name` is provided
- `managed_repos[].tracker` binds tracker events to a repo:
  - GitHub issues need no external tracker subscription.
  - Linear uses explicit repo hints first, then a team mapping only when the
    team maps to exactly one repo.
- Derived overlays, such as Moda's `moda-eng-team`, can override or append this
  list with compose rules. Compose behavior must define merge keys, duplicate
  handling, removal semantics, and validation before direct mode ships.

This replaces the lead subscription as durable config. Startup no longer has to
infer managed repos from live subagent subscriptions.

Direct mode should refuse to start when any configured repo has a missing path,
non-git path, remote mismatch, duplicate tracker mapping, or ambiguous Linear
team mapping.

## Monitor Responsibilities

Monitors should emit actionable conditions with repo identity and enough source
context for direct dispatch.

Current repo monitors already include `repo`, `pr_number`, `title`, and `url`
in condition data. In direct mode, monitor handling should become:

- `monitor/pr.conflict_detected`
  - director dispatches one `merge-conflict` or `adhoc` engineer workflow for
    the affected repo and PR
  - no project lead is required to interpret the event
- `monitor/pr.stale`
  - monitor emits only a condition
  - director decides whether to post status, dispatch an investigation, or
    suppress it based on config
- `monitor/status.roundup_due`
  - director aggregates from source data directly instead of asking leads
- `system/policy.updated`
  - unchanged
- `monitor/system.disk_low`
  - unchanged, director escalates or dispatches cleanup if policy allows

Monitors should not become hidden project leads. Their job is detection and
event emission only. The director owns routing and notification. Workflow
workers own remediation.

## Direct Router

Direct mode requires a small deterministic eng-team router before prompt
changes become active. The router is package-specific unless another package
needs the same abstraction later.

Router responsibilities:

- Load and validate `engineering_org.managed_repos`.
- Resolve source events to a single repo or a typed ambiguity error.
- Select the workflow and run key from the source event.
- Generate an idempotency key before launching:
  - GitHub issue event: `github:<repo>:issue:<number>:<action>`
  - GitHub PR comment/review: source delivery ID when available, otherwise
    `github:<repo>:pr:<number>:comment:<comment_id>:<workflow>`
  - GitHub PR close: `github:<repo>:pr:<number>:closed`
  - Linear event: `linear:<issue_id>:<updated_at-or-delivery-id>:<workflow>`
  - Monitor condition: `monitor:<monitor_name>:<condition_key>:<workflow>`
  - Slack request: `slack:<workspace>:<channel>:<thread_ts>:<message_ts>`
- Check idempotency state before launch so replays, restarts, and duplicate
  webhooks do not double-dispatch.
- Validate cwd and remote before launch.
- Pass a bounded, redacted event context into workflow input fields.
- Launch the workflow with structured arguments, avoiding shell interpolation.

Prompt guidance may handle ambiguous Slack requests, but deterministic webhook
and monitor routing should be code-backed.

## Direct Worker Dispatch

The director uses the direct router for source events. For manual Slack
requests, the prompt can collect clarification and then launch through the same
wrapper.

1. Load `engineering_org.managed_repos`.
2. Resolve incoming events to exactly one repo:
   - GitHub events: use the event topic or `repository.full_name`.
   - Linear events: prefer explicit repo hints in issue fields, PR links, or
     branch links; use `team.key` only when it maps to exactly one repo.
   - Slack requests: match explicit repo name/path, then ask if ambiguous.
   - Monitor events: use `fields.repo`.
3. Pick the workflow:
   - assigned issue or agent-labeled issue -> `issue-lifecycle`
   - PR feedback -> `pr-feedback`
   - PR close -> `pr-closed`
   - CI failure -> `build-failure`
   - merge conflict -> conflict-resolution workflow or bounded `adhoc`
   - one-off investigation -> `adhoc`
4. Launch directly through the wrapper:

```bash
cd <repo-path> &&
bobi agent <agent> subagents launch \
  -w <workflow> \
  --role engineer \
  --task "<source reference>" \
  --requested-by '<requester-json>'
```

5. Record status through existing registry and lifecycle events.

The source reference rule stays strict. For issue lifecycle work, the task must
be the original ticket reference, not a paraphrase.

If routing is ambiguous for a webhook or monitor event, the router should not
guess. It should emit a blocked routing event or director-visible status that
names the missing mapping.

## Requester Attribution

Requester attribution becomes director-owned for every launch.

For Slack-originated work, the director passes:

```json
{
  "from": "<slack-user-id>",
  "workspace": "<workspace-id>",
  "channel": "<channel-id>",
  "thread_ts": "<thread timestamp>"
}
```

For webhook-originated work, the director should pass source attribution instead
of an empty requester when the source exposes it:

```json
{
  "source": "github",
  "actor": "<login>",
  "repo": "<owner/name>",
  "url": "<event or artifact url>"
}
```

Linear-originated work should include the Linear actor, issue identifier, and
issue URL when available.

Required behavior:

- `requested_by` stays on the session registry entry.
- `requested_by` is included in `agent/session.started`,
  `agent/session.completed`, `agent/session.failed`,
  `agent/workflow.completed`, and `agent/workflow.failed` events.
- Slack replies use the original request thread when available.
- Directly auto-dispatched work should not lose the triggering event source;
  workflow input fields should carry bounded event metadata.

Minimum event context fields:

- GitHub issue: `event_type`, `repo`, `issue_number`, `action`, `url`, `actor`.
- GitHub PR feedback: `event_type`, `repo`, `pr_number`, `comment_id` or
  `review_id`, `url`, `actor`, `body_excerpt`.
- GitHub PR close: `event_type`, `repo`, `pr_number`, `action`, `merged`, `url`.
- Linear: `event_type`, `issue_id`, `identifier`, `team_key`, `url`, `actor`,
  `repo_hint`.
- Monitor: `event_type`, `monitor_name`, `condition_key`, `repo`, `url`, and
  monitor-specific fields.
- Slack: `workspace`, `channel`, `thread_ts`, `message_ts`, `from`, `permalink`,
  and `text_excerpt`.

Bodies and titles should be size-limited and redacted for secrets where the
source adapter can do so.

Existing `bobi subagents launch --requested-by` and workflow context support are
sufficient. The migration should add tests that direct-mode routing actually
passes the requester through.

## Status Aggregation

Without leads, the director aggregates status from durable and live sources:

- `bobi agent <agent> status` / session registry
  - active workflows
  - role, run key, phase, cwd, requester, and last activity
- GitHub CLI
  - open PRs, review state, check state, merge conflicts
- tracker API
  - in-progress tickets, blocked tickets, assigned issues
- monitor findings
  - stale PRs, conflicts, system health
- worker handoffs
  - latest resolution summaries, blockers, PR URLs, QA status

The status roundup monitor should trigger the director to build one report by
repo. It should not ping persistent leads. The report should include:

- human-attention items first
- active workers and current phase
- open PRs with review/CI state
- blocked tickets
- quiet repos summarized briefly

If a repo cannot be queried because credentials or the checkout path are
missing, the report should say that explicitly.

Status aggregation needs source precedence and freshness rules:

- Active registry entries beat stale tracker labels.
- Recent handoffs explain completed work, but GitHub/Linear remain the source of
  truth for current PR and ticket state.
- Failed workers without handoffs appear as human-attention items.
- Sources should include query timestamps so stale partial data is visible.
- Slack thread attribution comes from `requested_by`, not conversational memory.

If this proves too broad for prompt space, status aggregation should become a
first-class command before production cutover.

## Migration Plan

### Phase 1: Compatibility Config

- Add `engineering_org` parsing to runtime config.
- Default `dispatch_mode` to `leads`.
- Add tests for parsing direct-mode repo declarations.
- Add validation for paths, remotes, duplicate repos, duplicate tracker
  mappings, and unsupported tracker kinds.
- Do not change prompt behavior yet.

### Phase 2: Direct Router

- Implement the explicit eng-team router:
  - repo resolver
  - source event normalizer
  - workflow selector
  - idempotency key generator
  - launch wrapper
  - typed ambiguity and validation errors
- Keep it inactive unless `dispatch_mode: direct`.
- Add tests for GitHub, Linear, monitor, and Slack-shaped routing inputs.

### Phase 3: Director Direct Mode

- Update the director role prompt with a direct-mode branch:
  - in `direct`, do not launch project leads during startup
  - use the direct router for GitHub, Linear, and monitor events
  - route clarified Slack requests through the same launch wrapper
  - aggregate status from registry/GitHub/tracker instead of leads, or invoke a
    status aggregation command if one exists
  - keep the existing lead path for `leads`
- Add tests for direct event-to-workflow routing where the logic is in code.

### Phase 4: Auto-Dispatch Ownership

- Move deterministic direct-mode auto-dispatch ownership to the director router.
- In `leads` mode, leave existing session-local lead auto-dispatch unchanged.
- Include repo cwd and tracker mapping in the workflow launch.
- Add regression tests for:
  - GitHub PR feedback dispatch
  - GitHub assigned issue dispatch
  - Linear team to repo dispatch
  - cooldown/dedup behavior
  - replayed event idempotency

### Phase 5: Monitor Cutover

- Update `team-status-roundup` instructions to direct aggregation.
- Confirm monitor condition payloads include repo identity for every direct
  remediation workflow.
- Add any missing workflow for merge conflict remediation if `adhoc` is too
  loose.

### Phase 6: Moda Deployment Cutover

- Add `engineering_org.dispatch_mode: direct` and `managed_repos` to the Moda
  eng-team overlay.
- Suppress persistent lead startup reconciliation in Moda's direct-mode
  steady-state prompt.
- On startup, warn about active project leads for managed repos in direct mode
  and cancel only after confirming they are not running active work.
- Keep `project_lead` role installed for rollback.
- Run both modes in staging:
  - direct mode in a test agent
  - existing lead mode in production
- Cut production after direct mode handles:
  - Slack-requested issue work
  - GitHub assigned issue
  - PR feedback
  - PR close cleanup
  - status roundup
  - merge conflict monitor

### Phase 7: Deprecate Lead Mode

- Mark `project_lead` as legacy in docs.
- Keep `dispatch_mode: leads` supported for at least one minor release.
- After adoption, remove lead-specific startup reconciliation from the reusable
  eng-team default or leave it as an opt-in compatibility mode.

## Backward Compatibility

Existing installs must continue to behave as they do today unless they opt into
direct mode.

Compatibility requirements:

- No `engineering_org` block -> current lead-based behavior.
- `dispatch_mode: leads` -> current lead-based behavior.
- `project_lead` role remains valid.
- Existing `auto_dispatch` rules continue to work in lead sessions.
- Existing workflows and handoff schema stay unchanged.
- Existing Slack placeholder/edit behavior is unchanged.
- Existing tracker bindings remain valid.

Rollback requirements:

- Preserve the legacy lead startup prompt and role.
- Preserve enough repo/tracker mapping to relaunch equivalent project leads.
- Test `direct -> leads` downgrade before production cutover.
- On rollback, stop director-level direct routing before lead subscriptions are
  restored to avoid duplicate dispatch.
- Existing active engineer workflows continue to completion; only new event
  routing changes.

## Risks

- **Duplicate dispatch:** During migration, both director and project lead could
  react to the same event, and event sources can replay. Mitigation: direct mode
  must not launch leads, lead mode must not run director-level direct dispatch,
  and router idempotency keys must guard every deterministic launch.
- **Lost repo routing:** Subscriptions currently encode some routing. Mitigation:
  require validated `managed_repos` before enabling direct mode.
- **Status quality regression:** Leads currently hold conversational context.
  Mitigation: aggregate from registry, GitHub, tracker, and handoffs with source
  precedence and freshness rules instead of memory.
- **Linear mapping ambiguity:** Linear team keys may not be one-to-one with repos.
  Mitigation: require explicit repo hints or one-to-one mappings for automatic
  dispatch; otherwise emit a blocked routing event.
- **Prompt-only routing drift:** If direct dispatch is only prompt guidance, it
  can be missed. Mitigation: implement deterministic event routing in code, using
  prompts only for ambiguous Slack requests.
- **Security regression:** More untrusted external text reaches the director.
  Mitigation: validate repo paths/remotes, avoid shell interpolation, bound event
  bodies, and pass structured args to launch code.
- **Split brain:** A stale or manually launched project lead may remain active in
  direct mode. Mitigation: startup detects active leads for managed repos and
  surfaces them before direct routing starts.

## Verification Plan

Automated:

- Config parsing tests for `engineering_org`.
- Compose merge tests for `managed_repos`.
- Event routing tests for GitHub, Linear, monitor, and Slack-shaped inputs.
- Regression tests that `requested_by` survives direct workflow launches.
- Auto-dispatch tests proving direct mode does not double-dispatch.
- Idempotency tests for replayed GitHub webhook, replayed monitor condition,
  director restart mid-dispatch, and duplicate PR comment delivery.
- Negative routing tests for unknown repo, missing repo path, remote mismatch,
  duplicate Linear team mapping, multiple Slack matches, repo rename/transfer,
  and missing repo hints.
- Security tests for path traversal, shell-sensitive requester/task content,
  malicious issue title/comment content, and oversized event bodies.
- Upgrade and rollback tests:
  - no `engineering_org` behaves as current lead mode
  - direct startup launches no project leads
  - direct to leads restores lead startup and routing
- Status tests with partial outages:
  - missing GitHub auth
  - missing tracker auth
  - missing checkout
  - stale registry entry
  - failed worker with no handoff
- Existing workflow, reactor, and subagent tests.

Manual staging:

- Start a direct-mode test agent with one GitHub-issues repo and one Linear repo.
- Confirm startup detects and reports stale active project leads.
- Ask Slack to start an issue task and verify the reply routes back to the
  originating thread.
- Trigger a GitHub assigned issue and verify an engineer launches directly.
- Trigger PR feedback and verify exactly one feedback workflow launches.
- Trigger status roundup and verify it reports without project leads.
- Trigger merge conflict monitor and verify the correct repo worker launches.
- Switch back to `dispatch_mode: leads` and verify legacy lead startup still
  works.

## Open Questions

- Should direct event routing be a generic Bobi runtime feature or an eng-team
  package helper?
- Should `managed_repos` live in `agent.yaml`, a separate source-controlled
  routing file, or runtime state updated by onboarding commands?
- Should status aggregation have a first-class CLI command so the director does
  not hand-roll GitHub/tracker queries in prompt space?
- Should merge conflict remediation get a dedicated workflow rather than using
  `adhoc`?
- How should one Linear team mapped to multiple repos be represented when an
  issue does not mention a repo?
