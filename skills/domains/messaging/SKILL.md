# Messaging — Slack

This documents how to interact with our real-time messaging system.
Currently Slack.

**Note:** This integration is not yet fully implemented. Messages are
currently logged but not sent.

## Channels

- `#engineering` — general engineering updates, PR announcements
- `#builds` — CI/CD status, deploy notifications
- DMs — direct communication with team members

## How the engineer uses messaging

- Post when picking up a task: "Working on BET-11, should have a PR in ~20 min"
- Ask for help when stuck: "@zach the CI is failing on a test I didn't touch"
- Announce PRs: "PR up for BET-11: <link>"

## How the manager uses messaging

- Post status updates: "BET-11 assigned to engineer, ETA ~20 min"
- Escalate when an engineer is stuck: "@zach BET-11 needs product input — should the rate limit apply to free users?"
- Daily summary: "3 PRs shipped today, 1 blocked"

## Message format

Keep messages short and actionable:
- Start with the ticket ID: "BET-11: ..."
- Include links (PR, Linear ticket) when relevant
- Use threads for follow-up discussion

## Configuration

When implemented, configure in `.dispatch.yaml`:

```yaml
messaging:
  provider: slack            # or teams, discord
  channel: "#engineering"
  # Provider-specific config
```

## API (when implemented)

```bash
# Send a message (via Slack API)
curl -X POST https://slack.com/api/chat.postMessage \
  -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"channel": "#engineering", "text": "BET-11: PR ready for review"}'
```
