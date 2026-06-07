# GitHub

Interact with GitHub repositories, pull requests, and issues via the `gh` CLI.

## Check PR status

```bash
gh pr view <number> --json state,mergeable,statusCheckRollup,reviews
```

## Create a pull request

```bash
gh pr create --base main --title "[ISSUE-ID] type: description" --body "## Summary\n..."
```

Always target `main` unless explicitly told otherwise.

## List open PRs

```bash
gh pr list --state open --json number,title,author,updatedAt
```

## Check failing CI

```bash
gh pr checks <number>
```

## Request review

```bash
gh pr edit <number> --add-reviewer <handle>
```

## Merge a PR

```bash
gh pr merge <number> --squash --delete-branch
```

## Issues

```bash
gh issue view <number> --json title,body,state,labels,assignees
gh issue comment <number> --body "message"
gh issue close <number>
```

## Search code

```bash
gh search code "query" --repo <owner/repo> --json path,textMatches
```
