# /feedback — Address review comments

You are addressing feedback from a human reviewer. Fix what they asked for,
then review your fixes before pushing.

## EXIT CONTRACT — READ THIS FIRST

Your task is NOT complete until you do ALL THREE of these, in order:

```bash
# 1. Push your fixes
git push

# 2. Update the handoff
cat > .dispatch/handoff.md << 'HANDOFF_EOF'
---
issue_id: <ISSUE_ID>
title: <TITLE>
linear_id: <LINEAR_UUID>
worktree: <ABSOLUTE_WORKTREE_PATH>
branch: <BRANCH_NAME>
phase: feedback_addressed
pr_url: <PR_URL_FROM_PREVIOUS_HANDOFF>
---

Feedback addressed. Ready for re-review.
HANDOFF_EOF

# 3. Verify
cat .dispatch/handoff.md
```

If blocked (can't understand feedback, need clarification):

```bash
cat > .dispatch/handoff.md << 'HANDOFF_EOF'
---
issue_id: <ISSUE_ID>
title: <TITLE>
linear_id: <LINEAR_UUID>
worktree: <ABSOLUTE_WORKTREE_PATH>
branch: <BRANCH_NAME>
phase: blocked
question: <SPECIFIC_QUESTIONS>
---

<BODY>
HANDOFF_EOF
```

Write the handoff BEFORE exiting. The pipeline stalls without it.

## Inputs

Read `.dispatch/handoff.md` for context. You will also receive the human's
reply text as additional context from the daemon.

## Steps

### 1. Understand the feedback

Read the human's reply. Categorize:
- **Fix**: something wrong → change it
- **Question**: needs clarification → set `phase: blocked`
- **Suggestion**: nice-to-have → use judgment

### 2. For bugs in existing code: /investigate

If the feedback points to a bug (not just a style issue), invoke `/investigate`
to find the root cause before attempting a fix. Don't guess at fixes.

### 3. Make changes

Spawn a sub-agent with the feedback items and relevant source files.
It makes fixes and commits them.

### 4. Review fixes with /review

Invoke `/review` on the new changes. Give it just the diff since the last
push (`git diff origin/HEAD..HEAD`). Fix anything it finds.

This catches regressions introduced while addressing feedback.

### 5. Run tests

Run the project's test command.

### 6. Push, write handoff, exit

Follow the EXIT CONTRACT above. Push first, then write the handoff, then exit.

## Rules

- Only change what was requested. Don't refactor unrelated code.
- If feedback contradicts the spec, follow the feedback (human overrides spec).
- `/review` on your fixes is mandatory. Don't skip it.
