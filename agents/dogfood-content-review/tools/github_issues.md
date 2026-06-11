# GitHub Issues

Track content tasks via GitHub Issues in this repository.

## View an issue

```bash
gh issue view <number> --json title,body,state,labels,assignees
```

## Comment on an issue

```bash
gh issue comment <number> --body "message"
```

## Move between states (label-based)

```bash
# Start work
gh issue edit <number> --add-label "in-progress" --remove-label "todo"

# Ready for review
gh issue edit <number> --add-label "in-review" --remove-label "in-progress"

# Done
gh issue close <number>
```

## Create a PR linked to an issue

Reference the issue number in the PR body to auto-link:

```bash
gh pr create --base main --title "docs: description" --body "Closes #<number>"
```
