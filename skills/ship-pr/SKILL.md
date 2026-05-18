# /ship-pr — Create or update the PR

You are creating or updating a pull request. Focused, atomic step.

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
phase: in_review
pr_url: <PR_URL>
---

PR created/updated. Waiting for review.
HANDOFF_EOF
```

If blocked (conflict, push failure):

```bash
cat > .dispatch/handoff.md << 'HANDOFF_EOF'
---
issue_id: <ISSUE_ID>
title: <TITLE>
linear_id: <LINEAR_UUID>
worktree: <ABSOLUTE_WORKTREE_PATH>
branch: <BRANCH_NAME>
phase: blocked
question: <WHAT_WENT_WRONG>
---

<BODY>
HANDOFF_EOF
```

Write the handoff BEFORE exiting. The pipeline stalls without it.

## Inputs

Read `.dispatch/handoff.md` for current state and issue context.

## Steps

### 1. Check current state

```bash
gh pr view --json url,state,isDraft 2>/dev/null
```

### 2. Create or update the PR

**No PR exists:**

```bash
git push -u origin HEAD
gh pr create \
  --title "<issue-id>: <title>" \
  --body "Fixes <issue-id>

<summary from handoff>

## Manual QA
<QA steps from spec if available>"
```

**Draft PR exists (from spec phase):**

```bash
git push
gh pr ready
gh pr edit --body "<updated body with implementation summary>"
```

**PR exists, updating after feedback:**

```bash
git push
gh pr comment --body "Addressed review feedback: <summary>"
```

### 3. Write handoff and exit

Follow the EXIT CONTRACT above. Include the PR URL.

## Rules

- Never merge. Just create/update the PR.
- Do NOT invoke other skills.
