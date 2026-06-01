# /merge-conflict — Resolve a PR's merge conflicts

You were spawned because a background monitor detected that a PR can no longer
merge cleanly into its base branch (`monitor/pr.conflict_detected`). Your job
is to rebase the conflict away — merge the base branch into the PR branch,
resolve every conflict correctly, verify the result builds and passes tests,
and push. If the conflict needs a human decision, escalate instead of guessing.

Refer to `tools/git` for git commands, `tools/github` for `gh` usage, and
`practices/source-control-conventions` for commit conventions.

The manager's prompt gives you the PR: `repo`, `pr_number`, `branch`, and
`url`. Use them.

## Steps

### 1. Check out the PR branch

Work in an isolated worktree (see `tools/git`). Fetch and check out the PR's
head branch:

```bash
gh pr checkout <pr_number> --repo <owner/repo>
```

### 2. Identify the base branch

The base branch is usually `main`, but confirm — never assume:

```bash
gh pr view <pr_number> --repo <owner/repo> --json baseRefName -q .baseRefName
```

### 3. Merge the base branch in

```bash
git fetch origin
git merge origin/<base_branch>
```

This surfaces the conflicts. List them:

```bash
git diff --name-only --diff-filter=U
```

### 4. Resolve each conflict

For every conflicting file, understand *both* sides before editing — never
blindly keep one side:

- **Use history for intent.** `git log --oneline -5 origin/<base_branch> -- <file>`
  and `git log --oneline -5 <branch> -- <file>` show what each side was trying
  to do. The conflict is two intents colliding; your resolution must honor both.
- **Prefer keeping both sides** when the changes are additive and independent
  (e.g. both sides added a different import, function, list entry, or test).
  Combine them.
- **Mechanical conflicts** — imports, formatting, adjacent-but-unrelated edits,
  generated files, lockfiles — resolve by taking the union or regenerating.
- After editing, remove every conflict marker (`<<<<<<<`, `=======`,
  `>>>>>>>`) and `git add` the file.

If a file is genuinely a both-sides logic collision you cannot reconcile with
confidence, **stop and escalate** (step 6) — do not guess.

### 5. Verify, then push

Conflicts resolved is not done — the merge must be correct:

```bash
git add -A
git commit --no-edit    # completes the merge commit
```

Run the project's build and test commands (see CLAUDE.md). If anything fails,
fix it as part of this resolution — a green-but-conflicted tree is the same as
broken. Only when build and tests pass:

```bash
git push
```

Then leave a short PR comment noting the conflict was auto-resolved and tests
pass, so the human reviewer knows what changed:

```bash
gh pr comment <pr_number> --repo <owner/repo> \
  --body "Resolved merge conflicts with \`<base_branch>\` (files: …). Build and tests pass."
```

### 6. Escalate when you can't resolve

Escalate — do **not** push a partial or risky resolution — when:

- Both sides change the *same logic* in incompatible ways.
- Resolving correctly needs an architectural or product decision.
- Tests fail after resolution and the fix isn't obvious.
- The base branch has diverged so far the merge isn't mechanically tractable.

To escalate:

1. Abort the merge so the branch is left clean, not half-merged:
   ```bash
   git merge --abort
   ```
2. Post a PR comment explaining exactly which files/conflicts you couldn't
   resolve and *why* (the competing intents), so a human can decide:
   ```bash
   gh pr comment <pr_number> --repo <owner/repo> \
     --body "⚠️ Could not auto-resolve merge conflicts. <file>: <both sides do X / why a human decision is needed>. Leaving for human review."
   ```
3. **Exit with an error** so the manager knows resolution failed and must
   notify the human:
   ```bash
   exit 1
   ```

## Rules

- Never push a tree with conflict markers or failing tests.
- Resolution is not "pick a side" — understand both intents and honor both
  where possible.
- A clean abort + clear comment + non-zero exit is the correct outcome for a
  hard conflict. Escalating is success, not failure.
- Only touch what the merge requires. Don't refactor or fix unrelated code.
- Commit/merge messages follow `practices/source-control-conventions`.
