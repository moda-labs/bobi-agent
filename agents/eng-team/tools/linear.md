# Linear

Interact with Linear via its GraphQL API. `$LINEAR_API_KEY` is in your
environment (declared in agent.yaml, loaded from `.modastack/.env`).

All calls are POSTs to one endpoint:

```bash
curl -s https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "<graphql>"}'
```

## Get issue details

`issue(id:)` accepts the human identifier (e.g. `MOD-123`) or UUID:

```graphql
query { issue(id: "MOD-123") {
  id identifier title description
  state { name } assignee { name } labels { nodes { name } }
} }
```

Capture the returned `id` (UUID) — mutations below need it.

## Comment on an issue

```graphql
mutation { commentCreate(input: {
  issueId: "<issue-uuid>", body: "PR: https://github.com/org/repo/pull/42"
}) { success } }
```

## Update issue state

States are per-team — look up the target state's UUID first:

```graphql
query { issue(id: "MOD-123") { team { states { nodes { id name } } } } }
```

```graphql
mutation { issueUpdate(id: "<issue-uuid>", input: {
  stateId: "<state-uuid>"
}) { success } }
```

## Link a PR to an issue

Include the issue identifier in the PR title or body — Linear auto-links
when it sees it (e.g. `MOD-123`). A comment with the PR URL also works.
