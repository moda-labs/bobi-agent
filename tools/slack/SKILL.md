# Messaging — Slack

This documents how to interact with Slack. Both the manager and engineers
use Slack to communicate with the human team.

## Setup

### 1. Create a Slack App

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
2. Name it "Modabot" (or whatever you prefer)
3. Pick your workspace

### 2. Add Bot Scopes

Go to **OAuth & Permissions** → **Bot Token Scopes** and add:

- `chat:write` — post messages
- `channels:history` — read messages in public channels
- `channels:read` — list channels
- `groups:history` — read messages in private channels
- `groups:read` — list private channels
- `im:history` — read DMs to the bot
- `im:read` — list DM conversations
- `users:read` — resolve user names

### 3. Install to Workspace

Click **Install to Workspace** at the top of OAuth & Permissions.
Copy the **Bot User OAuth Token** (starts with `xoxb-`).

### 4. Configure in modastack

```bash
modastack setup --slack-token xoxb-your-token-here
```

Or manually add to `~/.modastack/credentials.yaml`:

```yaml
your-project:
  linear_api_key: lin_api_...
  slack_bot_token: xoxb-...
```

### 5. Invite the bot to channels

In Slack, invite @Modabot to `#engineering` (or whatever channel you use):
```
/invite @Modabot
```

## Channels

Configure in `~/.modastack/config.yaml`:

```yaml
messaging:
  provider: slack
  channel: "#engineering"     # default channel for status updates
  dm_user: "U12345678"        # user ID for escalation DMs (optional)
```

## How the manager uses Slack

- Posts status updates to the channel: "Picked up BET-11"
- Escalates questions via DM: "@zach BET-11 needs product input"
- Reads DMs for replies: human answers a question the manager asked

## How the engineer uses Slack

Engineers don't use Slack directly. The manager handles all communication.
If an engineer needs human input, it tells the manager (by going idle with
a question), and the manager posts to Slack.

## Message format

- Always prefix with the ticket ID: `[BET-11] ...`
- Keep messages short — one line for updates, a few lines for questions
- Use threads for follow-up on the same topic
