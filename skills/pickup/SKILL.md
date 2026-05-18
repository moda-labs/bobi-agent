# /pickup — Take a ticket and triage

You are picking up a Linear ticket. Your job: set up the workspace, understand
the problem, decide what kind of work this is, then write the handoff for the
next phase. You do NOT implement anything or write specs. Just triage and setup.

## EXIT CONTRACT — READ THIS FIRST

Your task is NOT complete until you run this exact sequence:

```bash
mkdir -p .dispatch
cat > .dispatch/handoff.md << 'HANDOFF_EOF'
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

<BODY — issue summary, triage rationale, relevant files, next steps>
HANDOFF_EOF
```

If you exit without writing `.dispatch/handoff.md`, the daemon cannot advance
this issue. The entire pipeline stalls. Write the handoff BEFORE anything else
in your exit sequence.

## Inputs

You will be given an issue ID and its description. The `LINEAR_API_KEY` env var
is set. Read `.dispatch.yaml` in the repo root for project config.

## Steps

### 1. Create a worktree

```bash
git worktree add -b agent/<issue-id-lowercase> worktrees/<issue-id-lowercase>
```

If a worktree for this issue already exists, `cd` into it and reuse it.

### 2. Explore the codebase

Spawn a sub-agent to understand the codebase. Give it the issue title and
description. Ask it to return: relevant files, architecture summary, risk
areas, and a complexity assessment (trivial / small / medium / large).

### 3. Decide what this needs

Based on the sub-agent's assessment:
- **Trivial/Small** (typo, config, single-file fix): `needs_spec: false`
- **Medium/Large** (new feature, multi-file, API change): `needs_spec: true`

### 4. Write the handoff and exit

Follow the EXIT CONTRACT above. Include in the body:
- Issue summary (condensed)
- Triage rationale (2-3 sentences)
- Relevant files list
- What the next agent should do

## Rules

- Do NOT implement anything. Triage and setup only.
- Do NOT invoke other skills.
- Do NOT create a PR.
- If you can't determine complexity, default to `needs_spec: true`.
