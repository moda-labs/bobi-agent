# /ship-pr — Create or update the PR

You are creating or updating a pull request using the `/ship` workflow.

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

### 2. Ship it

**If no PR exists or updating an existing one:**

Invoke `/ship` to handle the full ship workflow. `/ship` will:
- Detect and merge the base branch
- Run tests
- Review the diff one final time
- Create or update the PR with a proper description

Give `/ship` the issue ID and title for the PR description.

**If a draft PR exists (from spec phase) and just needs converting:**

```bash
git push
gh pr ready
```

Then invoke `/ship` to do the final review and update the PR body.

**If updating after feedback:**

```bash
git push
gh pr comment --body "Addressed review feedback: <summary>"
```

### 3. Write handoff and exit

Follow the EXIT CONTRACT above. Include the PR URL from `/ship`'s output.

## Rules

- **PR title format**: `[AGD-XX] type: description` — e.g., `[AGD-22] feat: add LOG_DIR constant to config.py`. Always include the issue ID in brackets.
- Never merge. `/ship` creates the PR, humans merge it.
- `/ship` handles test running, review, and PR creation — let it do its job.
