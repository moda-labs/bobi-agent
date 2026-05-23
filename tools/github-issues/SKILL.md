# GitHub Issues

Mechanical reference for GitHub Issues via the `gh` CLI. For ticketing workflow
policies and responsibilities, see `practices/ticketing-policy`.

The `gh` CLI is authenticated for all engineer sessions.

## Workflow states (label-based)

GitHub Issues doesn't have built-in columns/states. We use labels:

| Label | Meaning | Linear equivalent |
|-------|---------|-------------------|
| `status:todo` | Ready to be picked up | Todo |
| `status:in-progress` | Engineer actively working | In Progress |
| `status:blocked` | Waiting for human input | Blocked |
| `status:in-review` | PR created, awaiting review | In Review |

Done = issue closed (no label needed).

The `agent` label marks issues that should be picked up by modastack.

## How to list issues

```bash
# All open issues
gh issue list

# Filter by a single label
gh issue list --label "status:todo"

# Filter by multiple labels
gh issue list --label "status:todo" --label "agent"

# Filter by assignee
gh issue list --assignee "@me"

# Combine filters
gh issue list --label "status:in-progress" --assignee "@me"
```

## How to create an issue

```bash
gh issue create --title "Title here" --body "Description here"

# With labels
gh issue create --title "Title here" --body "Description here" \
  --label "status:todo" --label "agent"

# With assignee
gh issue create --title "Title here" --body "Description here" \
  --label "status:todo" --assignee "@me"
```

## How to move a task between states

Swap labels using `gh issue edit`. Remove the old status label, add the new one.

```bash
# Todo → In Progress
gh issue edit ISSUE_NUMBER --remove-label "status:todo" --add-label "status:in-progress"

# In Progress → In Review
gh issue edit ISSUE_NUMBER --remove-label "status:in-progress" --add-label "status:in-review"

# In Progress → Blocked
gh issue edit ISSUE_NUMBER --remove-label "status:in-progress" --add-label "status:blocked"

# Blocked → In Progress
gh issue edit ISSUE_NUMBER --remove-label "status:blocked" --add-label "status:in-progress"
```

Replace `ISSUE_NUMBER` with the issue number (e.g., 42).

## How to comment on an issue

```bash
gh issue comment ISSUE_NUMBER --body "Your message here"
```

## How to close an issue

```bash
gh issue close ISSUE_NUMBER

# Close with a comment
gh issue close ISSUE_NUMBER --comment "Completed in PR #123"
```
