# Slack Setup

Create a Slack bot that modastack uses to scan for work requests and post status updates.

**Time:** ~5 minutes. No code, no server, no OAuth redirect URI.

## 1. Create the app

1. Go to https://api.slack.com/apps
2. Click **Create New App** → **From scratch**
3. Name it something like `Agent Dispatch` (or whatever you want)
4. Pick your workspace
5. Click **Create App**

## 2. Add bot scopes

1. In the left sidebar: **OAuth & Permissions**
2. Scroll to **Scopes** → **Bot Token Scopes**
3. Add these scopes:

| Scope | Why |
|-------|-----|
| `channels:history` | Read messages in public channels |
| `channels:read` | List public channels |
| `chat:write` | Post status updates |
| `files:read` | Access files shared in conversations |
| `files:write` | Upload files and images to channels |
| `im:history` | Read DMs to the bot (work requests) |
| `im:read` | List DM conversations |
| `users:read` | Resolve user names in messages |

That's it. No user scopes needed.

## 3. Install to workspace

1. Scroll up to **OAuth Tokens for Your Workspace**
2. Click **Install to Workspace**
3. Review and **Allow**
4. Copy the **Bot User OAuth Token** (starts with `xoxb-`)

## 4. Add the token to modastack

```bash
modastack init
# Paste the xoxb- token when prompted for Slack bot token
```

Or for named credentials (multiple workspaces):

```bash
# Edit directly
cat >> ~/.config/modastack/credentials.yaml << 'EOF'
my-workspace:
  linear_api_key: "lin_api_..."
  slack_bot_token: "xoxb-..."
EOF
```

## 5. Invite the bot to channels

The bot can only post to (and read from) channels it's a member of.

In Slack, go to each channel you want to use (e.g., `#eng-agents`) and type:

```
/invite @Agent Dispatch
```

## 6. Enable DMs for work requests

For the bot to receive work via DM:

1. In your Slack app settings: **App Home** (left sidebar)
2. Under **Show Tabs**, enable **Messages Tab**
3. Check **Allow users to send Slash commands and messages from the messages tab**

Now anyone can DM the bot with "fix the login bug on repo-x" and dispatch will pick it up on the next cycle.

## That's it

No OAuth redirect server. No ngrok. No callback URL. The bot token is a static credential that works with the Slack Web API. Dispatch polls on each cron cycle — no WebSocket or Events API needed.

## Multiple workspaces

If you have repos across different Slack workspaces, create one app per workspace and store each token as a named credential:

```yaml
# ~/.config/modastack/credentials.yaml
workspace-a:
  slack_bot_token: "xoxb-workspace-a-token"
  linear_api_key: "lin_api_team_a"

workspace-b:
  slack_bot_token: "xoxb-workspace-b-token"
  linear_api_key: "lin_api_team_b"
```

Then reference the appropriate workspace when configuring your project.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Bot can't post to channel | `/invite @Agent Dispatch` in that channel |
| Bot can't read DMs | Enable Messages Tab in App Home settings |
| `not_authed` error | Token expired or wrong — regenerate in OAuth & Permissions |
| Bot posts but no one sees it | Check the channel name in your Slack config |
