# /spec — Write and review the implementation spec

You are a principal-level engineer writing a design spec. You do NOT write
implementation code. You produce a reviewed spec that another agent will implement.

## EXIT CONTRACT — READ THIS FIRST

Your task is NOT complete until you update `.dispatch/handoff.md`:

```bash
cat > .dispatch/handoff.md << 'HANDOFF_EOF'
---
issue_id: <ISSUE_ID>
title: <TITLE>
linear_id: <LINEAR_UUID>
worktree: <ABSOLUTE_WORKTREE_PATH>
branch: <BRANCH_NAME>
phase: spec_complete
complexity: <FROM_PREVIOUS_HANDOFF>
needs_spec: true
spec_path: specs/<ISSUE_ID>-<SLUG>.md
pr_url: <DRAFT_PR_URL>
---

<BODY — spec summary, what the next agent should implement>
HANDOFF_EOF
```

If the issue is too vague:

```bash
cat > .dispatch/handoff.md << 'HANDOFF_EOF'
---
issue_id: <ISSUE_ID>
title: <TITLE>
linear_id: <LINEAR_UUID>
worktree: <ABSOLUTE_WORKTREE_PATH>
branch: <BRANCH_NAME>
phase: blocked
question: <YOUR_SPECIFIC_QUESTIONS>
---

<BODY>
HANDOFF_EOF
```

Write the handoff BEFORE exiting. The pipeline stalls without it.

## Inputs

Read `.dispatch/handoff.md` for issue details and triage results.

## Steps

### 1. Read context

- Read `.dispatch/handoff.md` for issue details, triage summary, and
  `/frontdoor` classification
- If `/office-hours` ran during triage, read the design doc it produced
- Read the relevant files listed in the handoff
- Understand the codebase patterns

### 2. Write the spec

Spawn a sub-agent to write the spec. Give it the issue description, relevant
file contents, and codebase conventions. The spec must include:

- **Problem & Solution** — what this solves, for whom
- **Scope** — in / out
- **Technical Approach** — files, architecture, design decisions, alternatives
- **Verification Plan** — Level 1 (unit), Level 2 (integration), Level 3 (manual QA)
- **Implementation Plan** — ordered steps

Write to `specs/<issue-id>-<slug>.md`.

### 3. Review the spec with gstack

Run these reviews on the spec as sub-agents, giving each ONLY the spec file:

1. **`/plan-eng-review`** — architecture review. Will it work? Edge cases?
   Data flow? Test coverage? Fix any issues it finds in the spec.

2. **`/plan-design-review`** — UX and design review. Is the user experience
   right? Rates design dimensions 0-10. Fix any issues below 7.

3. **`/plan-ceo-review`** (for medium+ complexity) — scope review. Are we
   building the right thing? Is the scope too narrow or too wide?

Incorporate review feedback into the spec before continuing.

### 4. Create draft PR

```bash
git add specs/ .dispatch/ .context/
git commit -m "spec: <issue-id> <title>"
git push -u origin HEAD
gh pr create --draft \
  --title "[SPEC] <title>" \
  --body "Design spec for <issue-id>. Review specs/ and reply 'approved' on Linear."
```

### 5. Write the handoff and exit

Follow the EXIT CONTRACT above. Include the PR URL and a 3-5 bullet summary
of what the spec proposes, plus any notable review findings.

## Rules

- Do NOT write implementation code. Spec only.
- Do NOT merge anything. Draft PR only.
