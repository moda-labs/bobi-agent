# GitHub CLI

Mechanical reference for `gh` CLI commands. For PR title format and
conventions, see `practices/source-control-conventions`.

## Create a new PR

**Always target `main`** with `--base main` unless explicitly instructed to use
a different base branch. Never open a PR against a feature branch — see
`practices/source-control-conventions`.

```bash
gh pr create \
  --base main \
  --title "[ISSUE-ID] type: description" \
  --body "Fixes ISSUE-ID

<summary>

## Manual QA
<steps>"
```

## Create a draft PR (for specs)

```bash
gh pr create --draft \
  --base main \
  --title "[ISSUE-ID] spec: description" \
  --body "Design spec for ISSUE-ID. Review specs/ and reply 'approved' on Linear."
```

## Convert draft to ready

```bash
gh pr ready
```

## Check PR state

```bash
gh pr view --json url,state,isDraft
```

## Comment on a PR

```bash
gh pr comment --body "message"
```
