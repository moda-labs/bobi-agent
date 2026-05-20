# Linear API

Mechanical reference for Linear GraphQL API calls. For ticketing workflow
policies and responsibilities, see `practices/ticketing-policy`.

The `LINEAR_API_KEY` env var is set for all engineer sessions.

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
