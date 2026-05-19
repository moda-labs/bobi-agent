# /ship-pr — Create or update the PR

You are creating or updating a pull request using the `/ship` workflow.

## EXIT CONTRACT — READ THIS FIRST

Your task is NOT complete until you update `.dispatch/handoff.md`:

```bash
cat > .dispatch/handoff.md << 'HANDOFF_EOF'
---
issue_id: <ISSUE_ID>
title: <TITLE>
linear_id: <LINEAR_UUID>
worktree: <ABSOLUTE_WORKTREE_PATH>
branch: <BRANCH_NAME>
phase: in_review
pr_url: <PR_URL>
---

PR created/updated. Waiting for review.
HANDOFF_EOF
```

If blocked (conflict, push failure):

```bash
cat > .dispatch/handoff.md << 'HANDOFF_EOF'
---
issue_id: <ISSUE_ID>
title: <TITLE>
linear_id: <LINEAR_UUID>
worktree: <ABSOLUTE_WORKTREE_PATH>
branch: <BRANCH_NAME>
phase: blocked
question: <WHAT_WENT_WRONG>
---

<BODY>
HANDOFF_EOF
```

Write the handoff BEFORE exiting. The pipeline stalls without it.

## Inputs

Read `.dispatch/handoff.md` for current state and issue context.

## Steps

### 1. Check current state

```bash
gh pr view --json url,state,isDraft 2>/dev/null
```

### 2. Ship it

**If no PR exists or updating an existing one:**

Invoke `/ship` to handle the full ship workflow. `/ship` will:
- Detect and merge the base branch
- Run tests
- Review the diff one final time
- Create or update the PR with a proper description

Give `/ship` the issue ID and title for the PR description.

**If a draft PR exists (from spec phase) and just needs converting:**

```bash
git push
gh pr ready
```

Then invoke `/ship` to do the final review and update the PR body.

**If updating after feedback:**

```bash
git push
gh pr comment --body "Addressed review feedback: <summary>"
```

### 3. Write handoff and exit

Follow the EXIT CONTRACT above. Include the PR URL from `/ship`'s output.

### 4. Move ticket to In Review

After the PR is created, move the Linear ticket to In Review and post
the PR link. Use the LINEAR_API_KEY env var:

```bash
# Get the "In Review" state ID for your team
curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "{ teams(filter: { key: { eq: \"TEAM\" } }) { nodes { states { nodes { id name } } } } }"}' \
  | python3 -c "import sys,json; states=json.load(sys.stdin)['data']['teams']['nodes'][0]['states']['nodes']; print(next(s['id'] for s in states if s['name']=='In Review'))"

# Move the issue
curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "mutation { issueUpdate(id: \"LINEAR_UUID\", input: { stateId: \"IN_REVIEW_STATE_ID\" }) { success } }"}'

# Comment with the PR link
curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "mutation($id: String!, $body: String!) { commentCreate(input: { issueId: $id, body: $body }) { success } }", "variables": {"id": "LINEAR_UUID", "body": "PR ready for review: PR_URL"}}'
```

Replace TEAM, LINEAR_UUID, IN_REVIEW_STATE_ID, and PR_URL with actual values.
The team key and linear UUID are in the `.dispatch/handoff.md` from the previous phase.

## Rules

- **PR title format**: `[ISSUE-ID] type: description` — e.g., `[AGD-22] feat: add LOG_DIR constant`. Always include the issue ID in brackets.
- **Move ticket to In Review** after creating the PR. This is YOUR responsibility.
- Never merge. `/ship` creates the PR, humans merge it.
- `/ship` handles test running, review, and PR creation — let it do its job.
