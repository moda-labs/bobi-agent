# Linear

Interact with Linear issue tracking via the `modastack` CLI.

## Update issue state

```bash
modastack linear move <issue-id> <state>
```

States: `Todo`, `In Progress`, `In Review`, `Done`, `Blocked`

## Comment on an issue

```bash
modastack linear comment <issue-id> "message"
```

## Get issue details

```bash
modastack linear issue <issue-id>
```

Returns title, description, state, assignee, labels, and linked PRs.

## List team issues

```bash
modastack linear issues --state "In Progress"
modastack linear issues --assignee "@me"
```

## Link a PR to an issue

Include the issue ID in the PR title or body. Linear auto-links when it
sees the identifier (e.g., `BET-10`). Alternatively:

```bash
modastack linear comment <issue-id> "PR: https://github.com/org/repo/pull/<number>"
```
