# /prepare-pr — Create or update the PR

You are creating or updating a pull request, then moving the ticket to In Review.

Refer to `practices/source-control-conventions` and `tools/github` for PR
conventions, `practices/ticketing-policy` and the task tracker tool skill for how to move tickets.

## Steps

### 1. Check current state

```bash
gh pr view --json url,state,isDraft 2>/dev/null
```

### 2. Create or update the PR

**No PR exists:** Invoke `/ship` — it handles test running, final review,
and PR creation. Give it the issue ID and title.

**Draft PR exists (from spec):** Convert and update:
```bash
git push
gh pr ready
```
Then invoke `/ship` to do the final review and update the body.

**Updating after feedback:**
```bash
git push
gh pr comment --body "Addressed review feedback: <summary>"
```

### 3. Move ticket to In Review

After the PR is created, follow `practices/ticketing-policy` and the task tracker tool skill to:
1. Move the ticket to "In Review"
2. Comment on the issue with the PR link:
   ```bash
   gh issue comment <NUMBER> --body "**PR:** <PR URL>"
   ```

This is YOUR responsibility as the engineer — the manager does not do this.

### 4. Update handoff and STOP

After the PR is created and the ticket is moved, update `~/.modastack/handoffs/<ISSUE_ID>.md`:
- Set `phase: pr_ready` (use this exact string)
- Add `pr_url: <PR URL>`

Then **STOP completely.** Your work is done. Do NOT proceed to any next step.

## Rules

- PR title format per `practices/source-control-conventions`: `[ISSUE-ID] type: description`
- **Move ticket to In Review** after creating the PR.
- **NEVER merge PRs.** Do NOT run `gh pr merge`, `git merge main`, or any
  merge operation. Humans merge PRs after review. Your job ends at creating
  the PR and moving the ticket to In Review.
- **NEVER run `/land-and-deploy`.** That skill merges and deploys — humans decide when to merge.
- `/ship` handles test running, review, and PR creation — let it do its job.
- After `/ship` creates the PR, **STOP.** Do not chain into any follow-up skill.


## Consulting the manager

When you need a decision or guidance from the manager:

```bash
modastack consult "your question"
```

Use for: architecture decisions, scope questions, priority calls,
requesting Slack notifications. The command blocks until the manager
responds. Use the response to guide your work.
