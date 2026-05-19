# Source Control — Git + GitHub

This documents how to interact with source control. Currently Git for version
control and GitHub for pull requests.

## Branching conventions

- Branch name: `agent/<issue-id-lowercase>` (e.g., `agent/bet-10`)
- One branch per ticket
- Branch from `main` (or whatever the default branch is)

## Worktree setup

Each ticket gets its own git worktree:

```bash
git worktree add -b agent/<issue-id> worktrees/<issue-id>
cd worktrees/<issue-id>
```

If the branch already exists: `git worktree add worktrees/<issue-id> agent/<issue-id>`
If the worktree already exists: just `cd` into it.

## Commit conventions

- Prefix with the ticket ID: `[BET-10] feat: add rate limiting`
- Format: `[ISSUE-ID] type: description`
- Types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`
- One logical change per commit

## Push conventions

```bash
git push -u origin HEAD    # first push (sets upstream)
git push                   # subsequent pushes
```

## Pull requests

### Create a new PR

```bash
gh pr create \
  --title "[ISSUE-ID] type: description" \
  --body "Fixes ISSUE-ID

<summary>

## Manual QA
<steps>"
```

### Create a draft PR (for specs)

```bash
gh pr create --draft \
  --title "[ISSUE-ID] spec: description" \
  --body "Design spec for ISSUE-ID. Review specs/ and reply 'approved' on Linear."
```

### Convert draft to ready

```bash
gh pr ready
```

### Check PR state

```bash
gh pr view --json url,state,isDraft
```

### Comment on a PR

```bash
gh pr comment --body "message"
```

## PR title format

Always: `[ISSUE-ID] type: description`

Examples:
- `[BET-10] feat: add rate limiting to API`
- `[AGD-22] fix: move LOG_DIR constant to config.py`
- `[BET-11] docs: rewrite README for new architecture`
