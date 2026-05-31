# Git CLI

Mechanical reference for git commands used in the dispatch workflow.
For branching and commit conventions, see `practices/source-control-conventions`.

## Worktree setup

Each ticket gets its own git worktree located inside the modastack repo.
This ensures Claude Code resolves modastack skills automatically (they live
in a parent directory of the worktree).

The worktree base path is provided in your prompt as `Worktree base: <path>`.
Use it to create worktrees:

```bash
# Create worktree in the modastack repo (skills auto-resolve)
WORKTREE_BASE="<worktree-base-from-prompt>"
mkdir -p "$WORKTREE_BASE"
git worktree add -b agent/<issue-id> "$WORKTREE_BASE/<issue-id>"
cd "$WORKTREE_BASE/<issue-id>"
```

If the branch already exists: `git worktree add "$WORKTREE_BASE/<issue-id>" agent/<issue-id>`
If the worktree already exists: just `cd` into it.

## Push

```bash
git push -u origin HEAD    # first push (sets upstream)
git push                   # subsequent pushes
```
