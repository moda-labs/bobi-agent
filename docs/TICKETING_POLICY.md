# Ticketing Policy

This policy defines how we track work between Linear and GitHub to keep project
planning and implementation aligned.

## Scope

- **Linear** is the project/product-management surface.
  - Source of truth for user outcomes, priorities, epics, and status tracking.
  - Issue descriptions should be user-centric (for example, “As a user, I want…”
    with clear expected behavior).
- **GitHub** is the implementation surface.
  - Source of truth for technical work, execution tasks, and PR-level changes.
  - Issue descriptions should be implementation-focused with concrete design and
    delivery steps.

## Issue levels

Use this structure consistently in both systems:

### 1) Epic / high-level
- Large cross-cutting initiative or product track.
- Keep epics in Linear.
- In GitHub, represent each epic as an issue labeled as an `epic` (or equivalent
  tag), with technical decomposition.

### 1a) Plan-born initiatives (`plans/` convention)
- Initiative-sized work designed as a plan artifact `plans/<slug>.md` in the
  implementation repo (see `AGENTS.md`, Development Lifecycle), with a
  GitHub tracking issue labeled `plan`.
- The Linear epic points at the plan: link the plan file on `main` and the
  GitHub tracking issue in the epic description — do not duplicate the design
  into Linear or into GitHub issue bodies. The plan file is the technical
  decomposition and the source of truth for scope; the tracking issue
  replaces the GitHub `epic` issue for these initiatives.
- Rule 2a applies to the tracking issue: when a Linear epic exists, the
  tracking issue title carries the `[MOD-nnn]` prefix and both sides
  backlink.
- Work cut from an approved plan is routed by **dispatch issues**: thin
  task-level GitHub issues (level 2, below) that point into the plan
  instead of restating it. The plan stays the spec; the issue body carries
  the pointer (the plan file's path and the slice of the plan it routes)
  plus links and labels, nothing more. Dispatch issues are titled with the
  initiative's bracket prefix (`[<slug>] …`) and get individual `[MOD-nnn]`
  keys only when Linear tracks them individually - then rule 2a applies to
  them unchanged.
- Standalone (non-plan-born) work is the opposite: its design lives in the
  issue body directly, per the Scope section above.

### 2) Task-level implementation
- Technical work to execute the epic.
- Lives in GitHub issues.
- PRs should be approximately one-to-one with GitHub issues.

### 3) One-off / triage / cleanup
- Standalone tasks should be clearly labeled and triaged.
- If not tied to an active roadmap objective, close/retire them from active queues.

## Core rules

1. **Sync cadence**
   - Before starting work, confirm open states in both systems.
   - Keep GitHub the source of truth: update issue state, labels
     (`status:*`, readiness), assignees, and sub-issue links in-session as work
     changes — don't maintain a separate mirror.

2. **Linear → GitHub mapping**
   - Every active non-duplicate Linear ticket should map to:
     - one or more GitHub implementation issues, or
     - an explicit close/retire decision with reason.

2a. **Title and backlink convention (required)**
   - Linked GH<->Linear pairs must use this GitHub issue title prefix pattern:
     - `"[MOD-212] <descriptive title>"` where the bracketed token is the Linear
       ticket key.
   - The corresponding Linear ticket should include a backlink to the GH issue.
   - The corresponding GitHub issue body (or top comment) should include a backlink
     to the Linear ticket.

3. **Epic handling**
   - Linear epics are user-facing/PM-level and should define outcomes.
   - GitHub epics should contain technical scope and checkpoints.
   - Children in Linear epics should map to matching or planned technical
     children in GitHub.

4. **PR linkage**
   - Prefer one PR per GitHub issue where feasible.
   - PR titles/bodies should reference the GitHub issue.
   - Close only when the mapped implementation is complete.

5. **Assignment hygiene**
   - Keep assignees current.
   - When pausing work, set clear owner status and record the reason in notes.

6. **Account/identity hygiene**
   - Use current account/username conventions in all active tickets.
   - Retire stale references during active triage passes.

7. **Duplicate handling**
   - Mark exact duplicates explicitly in both systems and link canonical tickets.
   - Do not keep duplicate epics as active backlog.

8. **Scope cleanup**
   - Remove non-project tickets and stale off-topic items from active lanes.
   - Always leave closure context to keep future reconciliation lossless.

## Reconciliation acceptance criteria

Reconciliation is considered healthy when:

- All active Linear epics have a visible GitHub implementation owner.
- All active GitHub implementation issues have a Linear tracking context.
- Active queues are free of obvious duplicates and stale off-scope work.
- GitHub issue state, labels, and sub-issue links reflect reality (state is the
  source of truth — no separate overview doc to keep in sync).

## Pause / deferred work

If reconciliation is paused (for example, missing approver):

- Record a checkpoint as a comment on the relevant epic/tracking GitHub issue
  with a snapshot, mapping, and open ambiguities.
- Avoid destructive state changes until alignment is restored.
- Resume directly from the checkpoint.
