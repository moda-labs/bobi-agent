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

### 2. Classify with /frontdoor

Invoke `/frontdoor` to classify the work. It will:
- Classify the ask as **update**, **inquiry**, or **bug**
- Capture problem, scope, and UX decisions into `.context/intake.md`
- Route recommendation (investigate vs build vs brainstorm)

If `/frontdoor` classifies as **bug**, note this in the handoff — the
`/implement` phase should use `/investigate` for root cause analysis.

### 3. For complex or ambiguous issues: /office-hours

If the issue is vague, ambitious, or could go multiple directions, invoke
`/office-hours` to run a structured diagnostic:
- What's the real problem?
- Who is desperate for this?
- What's the narrowest wedge?

This produces a design doc that feeds into the spec phase.

### 4. Decide what this needs

Based on the classification:
- **Trivial/Small** (typo, config, single-file fix): `needs_spec: false`
- **Bug**: `needs_spec: false` (will use `/investigate` in implement phase)
- **Medium/Large update**: `needs_spec: true`

### 5. Write the handoff and exit

Follow the EXIT CONTRACT above. Include in the body:
- `/frontdoor` classification (update / inquiry / bug)
- Issue summary (condensed)
- Triage rationale (2-3 sentences)
- Relevant files list
- If bug: note "use /investigate for root cause"
- If `/office-hours` ran: reference the design doc path

## Rules

- Do NOT implement anything. Triage and setup only.
- Do NOT invoke other skills besides `/frontdoor` and `/office-hours`.
- Do NOT create a PR.
- If you can't determine complexity, default to `needs_spec: true`.
