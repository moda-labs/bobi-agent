# Linear

Receive research requests from Linear and report results back, via
Linear's GraphQL API. `$LINEAR_API_KEY` is in your environment (declared
in agent.yaml, loaded from `.modastack/.env`).

All calls are POSTs to one endpoint:

```bash
curl -s https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "<graphql>"}'
```

## Get issue details

`issue(id:)` accepts the human identifier (e.g. `MOD-166`) or UUID. This
is how you read a research-request ticket into a brief:

```graphql
query { issue(id: "MOD-166") {
  id identifier title description
  state { name } assignee { name } labels { nodes { name } }
} }
```

Capture the returned `id` (UUID) — mutations below need it.

## Comment results back on an issue

Post the research summary and a link/path to the full brief when a
`linear-research` run completes:

```graphql
mutation { commentCreate(input: {
  issueId: "<issue-uuid>", body: "<TL;DR + brief path>"
}) { success } }
```

## Update issue state

Move a ticket to `In Progress` when you pick it up and `Done` (or
`In Review`) once results are commented. States are per-team — look up
the target state's UUID first:

```graphql
query { issue(id: "MOD-166") { team { states { nodes { id name } } } } }
```

```graphql
mutation { issueUpdate(id: "<issue-uuid>", input: {
  stateId: "<state-uuid>"
}) { success } }
```

## What counts as a research request

A ticket assigned to this agent, or labeled for research (e.g. `research`),
whose body asks a market/landscape/PMF question. Parse the body into a
research brief, run the appropriate workflow, then comment the result and
move the ticket.
