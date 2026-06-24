# Slack Setup

Create a Slack bot that modastack uses to receive work requests (mentions, DMs,
thread replies) and post status updates. The bot talks to your **event server**
(Cloudflare Worker or local Node.js) over the HTTP Events API — Slack POSTs
events to `<event-server>/webhooks/slack`, and modastack replies via the Web API.

**Time:** ~2 minutes — the app is stamped out from a manifest, so you don't
hand-pick scopes or wire the event URL yourself.

## 1. Generate a manifest and create the app

```bash
modastack create-slack-bot --app-name "Agent Dispatch"
```

This prints a Slack app manifest plus a one-click create link. The request URL
is filled in from your project config when run inside an install, otherwise the
modastack cloud event server (override with `--event-server https://…`).

Then create the app one of three ways:

- **One click:** open the printed
  `https://api.slack.com/apps?new_app=1&manifest_json=…` link, pick your
  workspace, and click **Create**. Scopes + event subscriptions are prefilled.
- **Slack CLI:** see [Create with the Slack CLI](#create-with-the-slack-cli).
- **Manual:** https://api.slack.com/apps → **Create New App** → **From a
  manifest**, paste the YAML.

The manifest pins exactly the scopes and bot events the modastack Slack adapter
consumes, so you never have to reason about them:

| Bot event | Becomes | Scope(s) |
|-----------|---------|----------|
| `app_mention` | `slack.mention` | `app_mentions:read` |
| `message.im` | `slack.dm` | `im:history`, `im:read` |
| `message.mpim` | `slack.dm` | `mpim:history` |
| `message.channels` / `message.groups` (with `thread_ts`) | `slack.thread_reply` | `channels:history` / `groups:history` |

Plus `chat:write` (post replies), `files:read`/`files:write` (attachments), and
`users:read` (resolve names).

## 2. Install to workspace

1. On the app page: **Install App** → **Install to Workspace**
2. Review and **Allow**
3. Copy the **Bot User OAuth Token** (starts with `xoxb-`)
4. (Recommended) On **Basic Information**, copy the **Signing Secret** — set it
   as `SLACK_SIGNING_SECRET` on the event server so it verifies inbound requests.

> Slack verifies the event **Request URL** with a `url_verification` challenge
> the moment you create or reinstall the app, so your event server must be
> reachable then. For local dev, start it first: `modastack event-server start`.

## 3. Add the token to modastack

```bash
modastack init
# Paste the xoxb- token when prompted for the Slack bot token
```

Or store named credentials directly (multiple workspaces):

```yaml
# ~/.config/modastack/credentials.yaml
my-workspace:
  slack_bot_token: "xoxb-..."
  linear_api_key: "lin_api_..."
```

In `agent.yaml`, the bot token is referenced as a `${VAR}`:

```yaml
services:
  - name: slack
    events: true
    credentials:
      bot_token: ${SLACK_BOT_TOKEN}
```

## 4. Invite the bot to channels

The bot only sees (and posts to) channels it's a member of. In each channel:

```
/invite @Agent Dispatch
```

DMs work out of the box — the manifest enables the Messages tab.

## Create with the Slack CLI

Useful if you live in the [Slack CLI](https://api.slack.com/automation/cli) or
want to script app creation. Write the manifest to a file, then create from it:

```bash
modastack create-slack-bot --app-name "Agent Dispatch" --format json -o manifest.json

# Validate, then create the app from the manifest:
slack manifest validate --manifest manifest.json
slack create agent-dispatch --manifest manifest.json
```

The same `manifest.json` is the single source of truth — regenerate it whenever
your event server URL changes and re-apply, rather than editing scopes by hand
in the Slack UI.

### Provision many apps programmatically

For multitenant setups (one Slack app per deployment, all pointed at one shared
event server), generate a manifest per tenant and POST it to the Slack
[App Manifest API](https://api.slack.com/reference/manifests#apps_manifest) with
a configuration token — no clicks:

```bash
modastack create-slack-bot --app-name "Tenant A" \
  --event-server https://modastack-events.modalabs.workers.dev \
  --format json -o tenant-a.json
# then: POST tenant-a.json to apps.manifest.create with your config token
```

## Multiple workspaces

Create one app per workspace (each gets its own `xoxb-` token) and store each as
a named credential:

```yaml
# ~/.config/modastack/credentials.yaml
workspace-a:
  slack_bot_token: "xoxb-workspace-a-token"
workspace-b:
  slack_bot_token: "xoxb-workspace-b-token"
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Slack rejects the Request URL on create | Event server unreachable — start it (`modastack event-server start`) and reinstall the app |
| Bot receives nothing | Event subscriptions disabled, or the bot isn't in the channel — reinstall from the manifest and `/invite` it |
| Bot can't post to a channel | `/invite @your-bot` in that channel |
| `not_authed` error | Token expired or wrong — reinstall and copy a fresh `xoxb-` token |
| Inbound events `401`/ignored | `SLACK_SIGNING_SECRET` on the event server doesn't match the app's signing secret |
