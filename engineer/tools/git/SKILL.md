# Git CLI

Mechanical reference for git commands used in the dispatch workflow.
For branching and commit conventions, see `practices/source-control-conventions`.

## Worktree setup

Each ticket gets its own git worktree:

```bash
git worktree add -b agent/<issue-id> worktrees/<issue-id>
cd worktrees/<issue-id>
```

If the branch already exists: `git worktree add worktrees/<issue-id> agent/<issue-id>`
If the worktree already exists: just `cd` into it.

## Push

```bash
git push -u origin HEAD    # first push (sets upstream)
git push                   # subsequent pushes
```
