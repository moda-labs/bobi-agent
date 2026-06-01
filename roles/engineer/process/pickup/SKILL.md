# /pickup — Take a ticket and triage

You are an engineer picking up a new ticket. Set up the workspace, understand
the problem deeply, and decide what kind of work this is. You do NOT implement
anything. Your job is triage only.

Refer to `practices/source-control-conventions` and `tools/git` for
git/branching conventions, and `practices/ticketing-policy` for ticket policies.

## Steps

### 1. Create a worktree

Follow the worktree setup in `tools/git`. Branch name: `agent/<issue-id>`.

### 2. Deeply explore the codebase

Before classifying anything, spend real time understanding the codebase:
- Read the CLAUDE.md and any architecture docs
- Understand the existing patterns, conventions, and abstractions
- Find the specific files and modules related to this ticket
- Understand how similar features were implemented before
- Identify dependencies, edge cases, and potential risks

Do NOT rush this step. A human engineer would spend 30-60 minutes reading
code before forming an opinion. You should too.

### 3. Classify with /triage

Invoke `/triage` to classify the work (update / inquiry / bug).
For complex or ambiguous issues, invoke `/office-hours` for a structured diagnostic.

### 4. Decide complexity

- **Trivial** (typo fix, config change, one-line fix): `needs_spec: false`
- **Small** (single-file bug fix, add a test): `needs_spec: false`
- **Medium** (multi-file change, new feature, new API): `needs_spec: true`
- **Large** (architectural change, new subsystem): `needs_spec: true`
- **Bug**: `needs_spec: false` ONLY if the fix is obvious and localized.
  If the bug requires investigation across multiple files, `needs_spec: true`.

**When in doubt, write a spec.** The cost of a spec is 10 minutes. The cost
of implementing the wrong thing is hours of wasted work and a rejected PR.

### 5. Write the handoff

Write `~/.modastack/handoffs/<ISSUE_ID>.md` with the triage results:

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

## Codebase understanding
<what you learned from exploring — key patterns, relevant modules,
how similar things are done in this codebase>

## Triage
<complexity rationale — 2-3 sentences>

## Relevant files
- <files that need changes, with brief notes on why>

## Risks and edge cases
- <anything that could go wrong or needs careful handling>

## Next
<what the next phase should do>
```

## Rules

- Do NOT implement anything. Triage and setup only.
- Do NOT create a PR.
- If you can't determine complexity, default to `needs_spec: true`.
- Medium and large tasks ALWAYS need a spec. No exceptions.

