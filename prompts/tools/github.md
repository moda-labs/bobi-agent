## GitHub CLI (gh)

Use the `gh` CLI for all GitHub operations.

### Create a branch and push

```bash
git checkout -b agent/<issue-id>-<short-slug>
# ... make changes ...
git add -A
git commit -m "<message>"
git push -u origin HEAD
```

### Create a draft PR (spec review)

```bash
gh pr create --draft \
  --title "[SPEC] <issue title>" \
  --body "## Design Review for <ISSUE_ID>

Review the spec in specs/<filename>.md.

**Reply 'approved' on the Linear issue to start implementation.**"
```

### Create an implementation PR

```bash
gh pr create \
  --title "<issue title>" \
  --body "Fixes <ISSUE_ID>

<description of what was implemented>

## Manual QA Checklist

<Level 3 verification steps from the spec>"
```

### Check PR review status

```bash
gh pr view --json reviewDecision,reviews
```

Returns `reviewDecision`: `APPROVED`, `CHANGES_REQUESTED`, or `REVIEW_REQUIRED`.

### Push updates to existing PR

Just push to the same branch — the PR updates automatically:

```bash
git add -A
git commit -m "<message>"
git push
```

Do NOT create a new PR when addressing feedback.
