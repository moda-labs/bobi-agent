# /pickup — Take a ticket and triage

You are an engineer picking up a new ticket. Set up the workspace, understand
the problem, decide what kind of work this is. You do NOT implement anything.

Refer to `domains/source-control` for git/branching conventions and
`domains/ticketing` for ticket policies.

## Steps

### 1. Create a worktree

Follow the worktree setup in `domains/source-control`. Branch name: `agent/<issue-id>`.

### 2. Classify with /frontdoor

Invoke `/frontdoor` to classify the work (update / inquiry / bug).
For complex or ambiguous issues, invoke `/office-hours` for a structured diagnostic.

### 3. Decide complexity

- **Trivial/Small** (typo, config, single-file fix): `needs_spec: false`
- **Bug**: `needs_spec: false` (note "use /investigate" for implement phase)
- **Medium/Large update**: `needs_spec: true`

### 4. Write the handoff

Write `.dispatch/handoff.md` with the triage results:

```markdown
---
issue_id: <ISSUE_ID>
title: <TITLE>
linear_id: <LINEAR_UUID>
worktree: <ABSOLUTE_WORKTREE_PATH>
branch: <BRANCH_NAME>
phase: triage_complete
complexity: <trivial|small|medium|large>
needs_spec: <true|false>
---

## Issue
<condensed summary>

## Triage
<complexity rationale — 2-3 sentences>

## Relevant files
- <files that need changes>

## Next
<what the next phase should do>
```

## Rules

- Do NOT implement anything. Triage and setup only.
- Do NOT create a PR.
- If you can't determine complexity, default to `needs_spec: true`.
