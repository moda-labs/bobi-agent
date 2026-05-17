## Linear API

Use `curl` to interact with Linear. The API key is in the environment as `LINEAR_API_KEY`.

### Move issue to a state

```bash
curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "mutation { issueUpdate(id: \"ISSUE_ID\", input: { stateId: \"STATE_ID\" }) { success } }"}'
```

### Common state IDs for this project

Read these from `.dispatch/state.md` or look them up:

```bash
# Get all states for the team
curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "{ teams(filter:{key:{eq:\"TEAM_KEY\"}}){nodes{states{nodes{id name}}}} }"}' | python3 -c "import sys,json; [print(f'{s[\"name\"]}: {s[\"id\"]}') for s in json.load(sys.stdin)['data']['teams']['nodes'][0]['states']['nodes']]"
```

### Post a comment

```bash
curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "mutation { commentCreate(input: { issueId: \"ISSUE_ID\", body: \"YOUR_COMMENT\" }) { success } }"}'
```

### Create a sub-issue

```bash
curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "mutation { issueCreate(input: { teamId: \"TEAM_ID\", title: \"TITLE\", description: \"DESC\", parentId: \"PARENT_ISSUE_ID\" }) { success issue { id identifier } } }"}'
```

### Get issue comments (to check for replies)

```bash
curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "{ issue(id: \"ISSUE_ID\") { comments { nodes { body createdAt } } } }"}'
```

### Comment format

Always prefix agent comments with the robot emoji:
- Status updates: `🤖 **Status:** <message>`
- Questions: `🤖 **Question:** <question>`
- Completion: `🤖 **Done.** <summary>`
