# /ship-pr — Create or update the PR

You are creating or updating a pull request, then moving the ticket to In Review.

Refer to `domains/source-control` for PR conventions,
`domains/ticketing` for how to move tickets.

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

After the PR is created, follow `domains/ticketing` to:
1. Move the ticket to "In Review"
2. Comment on the ticket with the PR link

This is YOUR responsibility as the engineer — the manager does not do this.

## Rules

- PR title format per `domains/source-control`: `[ISSUE-ID] type: description`
- **Move ticket to In Review** after creating the PR.
- Never merge. Humans merge PRs.
- `/ship` handles test running, review, and PR creation — let it do its job.
