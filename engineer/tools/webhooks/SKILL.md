# Webhooks — Setup Guide

This documents how to programmatically set up webhooks for all event sources.
Webhooks deliver real-time events to the modabot event bus via an HTTP endpoint.

## Prerequisites

- **ngrok** (local dev) or a public IP (EC2): the webhook server needs a reachable URL
- **Webhook server running**: `modastack start --webhooks --port 8080`

## Start the tunnel (local dev only)

```bash
ngrok http 8080
```

Get the public URL:

```bash
NGROK_URL=$(curl -s http://localhost:4040/api/tunnels | python3 -c "import sys,json; print(json.load(sys.stdin)['tunnels'][0]['public_url'])")
echo $NGROK_URL
```

On EC2, use the public IP directly: `NGROK_URL=http://your-ec2-ip:8080`

## GitHub Webhooks

One webhook per repo. Subscribes to PR events, reviews, and comments.

### Create via gh CLI

```bash
# For repos on your primary GitHub account
gh api repos/OWNER/REPO/hooks --method POST \
  -f "config[url]=${NGROK_URL}/webhooks/github" \
  -f "config[content_type]=json" \
  -F "active=true" \
  -f "events[]=pull_request" \
  -f "events[]=pull_request_review" \
  -f "events[]=issue_comment"
```

If the repo is on a different GitHub account, switch first:

```bash
gh auth switch --user OTHER_ACCOUNT
gh api repos/ORG/REPO/hooks --method POST \
  -f "config[url]=${NGROK_URL}/webhooks/github" \
  -f "config[content_type]=json" \
  -F "active=true" \
  -f "events[]=pull_request" \
  -f "events[]=pull_request_review" \
  -f "events[]=issue_comment"
gh auth switch --user PRIMARY_ACCOUNT
```

**Required scope**: `admin:repo_hook`. If you get a 404, run:
```bash
gh auth refresh -h github.com -s admin:repo_hook
```

### Verify

```bash
gh api repos/OWNER/REPO/hooks --jq '.[].config.url'
```

### Delete

```bash
HOOK_ID=$(gh api repos/OWNER/REPO/hooks --jq '.[0].id')
gh api repos/OWNER/REPO/hooks/$HOOK_ID --method DELETE
```

### Events received

| GitHub event | Event bus type | When |
|---|---|---|
| `pull_request` (opened/closed/merged) | `github.pr.<action>` | PR created, merged, closed |
| `pull_request_review` | `github.pr.review` | Someone approves or requests changes |
| `issue_comment` | `github.comment` | Comment on a PR or issue |
| `ping` | (logged only) | Webhook created successfully |

## Linear Webhooks

One webhook per Linear workspace. Covers all teams in that workspace.

### Create via GraphQL API

```bash
LINEAR_API_KEY="lin_api_..."

curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"query\": \"mutation(\$url: String!, \$resourceTypes: [String!]!) { webhookCreate(input: { url: \$url, resourceTypes: \$resourceTypes, allPublicTeams: true, enabled: true }) { success webhook { id url } } }\",
    \"variables\": {
      \"url\": \"${NGROK_URL}/webhooks/linear\",
      \"resourceTypes\": [\"Issue\", \"Comment\"]
    }
  }"
```

Or via Python:

```python
import httpx

api_key = "lin_api_..."
url = f"{NGROK_URL}/webhooks/linear"

r = httpx.post("https://api.linear.app/graphql",
    headers={"Authorization": api_key, "Content-Type": "application/json"},
    json={
        "query": 'mutation($url: String!, $resourceTypes: [String!]!) { webhookCreate(input: { url: $url, resourceTypes: $resourceTypes, allPublicTeams: true, enabled: true }) { success webhook { id url } } }',
        "variables": {"url": url, "resourceTypes": ["Issue", "Comment"]},
    })
print(r.json())
```

**Key**: use `allPublicTeams: true` to cover all teams in the workspace.
Each Linear workspace (API key) needs its own webhook.

### List existing webhooks

```bash
curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "{ webhooks { nodes { id url enabled resourceTypes } } }"}' | python3 -m json.tool
```

### Delete

```bash
curl -s -X POST https://api.linear.app/graphql \
  -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "mutation { webhookDelete(id: \"WEBHOOK_ID\") { success } }"}'
```

### Events received

| Linear event | Event bus type | When |
|---|---|---|
| Issue created | `linear.issue.create` | New ticket |
| Issue updated | `linear.issue.update` | State change, assignment, etc. |
| Comment created | `linear.comment` | Someone comments on a ticket |

## Slack — Socket Mode (no webhook needed)

Slack uses Socket Mode instead of webhooks. No public URL required.

### Setup

1. Go to api.slack.com/apps → your app → **Socket Mode** → toggle ON
2. Generate **App-Level Token** with `connections:write` scope
3. Go to **Event Subscriptions** → toggle ON → subscribe to bot events:
   - `message.im` — DMs to the bot
   - `message.channels` — public channel messages
   - `message.groups` — private channel messages
   - `app_mention` — @mentions
4. Save the `xapp-` token in `~/.modastack/credentials.yaml`:

```yaml
your-project:
  slack_bot_token: xoxb-...
  slack_app_token: xapp-...
```

Socket Mode starts automatically when `slack_app_token` is configured.

### Events received

| Slack event | Event bus type | When |
|---|---|---|
| DM to bot | `slack.dm` | Someone messages Modabot directly |
| @mention | `slack.mention` | Someone @Modabot in a channel |
| Thread reply | `slack.thread_reply` | Reply in a thread Modabot participated in |

## Full setup script

Run this to set up all webhooks for all registered repos:

```bash
# Get ngrok URL
NGROK_URL=$(curl -s http://localhost:4040/api/tunnels | python3 -c "import sys,json; print(json.load(sys.stdin)['tunnels'][0]['public_url'])")

# GitHub — for each repo in ~/.modastack/config.yaml
for repo in underminedsk/modastack underminedsk/bettertab; do
  gh api repos/$repo/hooks --method POST \
    -f "config[url]=${NGROK_URL}/webhooks/github" \
    -f "config[content_type]=json" \
    -F "active=true" \
    -f "events[]=pull_request" \
    -f "events[]=pull_request_review" \
    -f "events[]=issue_comment"
done

# Linear — for each unique API key in ~/.modastack/credentials.yaml
python3 -c "
import yaml, httpx
from pathlib import Path

NGROK_URL = '${NGROK_URL}'
creds = yaml.safe_load((Path.home() / '.dispatch' / 'credentials.yaml').read_text())
seen = set()
for name, entry in creds.items():
    key = entry.get('linear_api_key', '')
    if not key or key in seen:
        continue
    seen.add(key)
    r = httpx.post('https://api.linear.app/graphql',
        headers={'Authorization': key, 'Content-Type': 'application/json'},
        json={'query': 'mutation(\$url: String!, \$r: [String!]!) { webhookCreate(input: { url: \$url, resourceTypes: \$r, allPublicTeams: true, enabled: true }) { success webhook { id } } }',
              'variables': {'url': f'{NGROK_URL}/webhooks/linear', 'r': ['Issue', 'Comment']}})
    print(f'{name}: {r.json()}')
"
```

## Cleanup

When tearing down (e.g., ngrok URL changed), delete old webhooks:

```bash
# GitHub
for repo in underminedsk/modastack underminedsk/bettertab; do
  HOOK_IDS=$(gh api repos/$repo/hooks --jq '.[].id')
  for id in $HOOK_IDS; do
    gh api repos/$repo/hooks/$id --method DELETE
  done
done

# Linear — list and delete
python3 -c "
import yaml, httpx
from pathlib import Path
creds = yaml.safe_load((Path.home() / '.dispatch' / 'credentials.yaml').read_text())
seen = set()
for name, entry in creds.items():
    key = entry.get('linear_api_key', '')
    if not key or key in seen: continue
    seen.add(key)
    r = httpx.post('https://api.linear.app/graphql',
        headers={'Authorization': key, 'Content-Type': 'application/json'},
        json={'query': '{ webhooks { nodes { id url } } }'})
    for wh in r.json().get('data', {}).get('webhooks', {}).get('nodes', []):
        print(f'Deleting {wh[\"id\"]}: {wh[\"url\"]}')
        httpx.post('https://api.linear.app/graphql',
            headers={'Authorization': key, 'Content-Type': 'application/json'},
            json={'query': f'mutation {{ webhookDelete(id: \"{wh[\"id\"]}\") {{ success }} }}'})
"
```
