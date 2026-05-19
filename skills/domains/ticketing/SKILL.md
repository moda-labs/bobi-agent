# Ticketing System — Linear

This documents how to interact with our ticketing system. Currently Linear.
The `LINEAR_API_KEY` env var is set for all engineer sessions.

## Ticket states

Our workflow uses these states, in order:

| State | Meaning |
|-------|---------|
| Todo | Ready to be picked up |
| In Progress | Engineer is actively working |
| Blocked | Waiting for human input |
| In Review | PR created, waiting for human review |
| Done | PR merged, work complete |

## Your responsibilities as an engineer

- **Do NOT move tickets to In Progress** — the manager does this when assigning
- **Move to In Review** when you create a PR
- **Move to Blocked** if you have a question you can't answer yourself
- **Do NOT move to Done** — the manager does this when the PR is merged

## How to move a ticket

```bash
# Step 1: Get the state ID for the target state
STATE_ID=$(curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "{ teams(filter: { key: { eq: \"TEAM_KEY\" } }) { nodes { states { nodes { id name } } } } }"}' \
  | python3 -c "import sys,json; states=json.load(sys.stdin)['data']['teams']['nodes'][0]['states']['nodes']; print(next(s['id'] for s in states if s['name']=='TARGET_STATE'))")

# Step 2: Move the ticket
curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"query\": \"mutation { issueUpdate(id: \\\"LINEAR_UUID\\\", input: { stateId: \\\"$STATE_ID\\\" }) { success } }\"}"
```

Replace `TEAM_KEY` with the project prefix (e.g., BET, AGD), `TARGET_STATE`
with the state name (e.g., In Review), and `LINEAR_UUID` with the issue's
UUID from the handoff.

## How to comment on a ticket

```bash
curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "mutation($id: String!, $body: String!) { commentCreate(input: { issueId: $id, body: $body }) { success } }", "variables": {"id": "LINEAR_UUID", "body": "Your message here"}}'
```

## Where to find ticket info

The handoff file (`.dispatch/handoff.md`) contains:
- `issue_id`: the ticket identifier (e.g., BET-10)
- `linear_id`: the UUID needed for API calls
- `title`: the ticket title

The team key is the prefix of the issue ID (BET-10 → BET).
