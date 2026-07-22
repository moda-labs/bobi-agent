# Slack Setup

Create a Slack bot that bobi uses to receive work requests (mentions, DMs, thread replies) and post status updates.
The bot talks to your **event server** (Cloudflare Worker or local Node.js) through one of two transports.
HTTP Events API is the default: Slack POSTs to `<event-server>/webhooks/slack` and requires public HTTPS ingress.
Socket Mode is an opt-in for the local Node.js event server: the server opens an outbound connection to Slack, so no public Request URL is required.
Bobi replies through the Web API in either mode.

**Time:** ~2 minutes - the app is stamped out from a manifest, so you don't hand-pick scopes or wire the event URL yourself.

Built the team with `bobi setup` and chose Slack as its chat? The setup
completion screen walks this whole flow for you: it shows the required scopes,
links the app-creation walkthrough, saves the team's dedicated channel, and
posts a test message end to end. This page is the manual/CLI path and the
reference.

## 1. Generate a manifest and create the app

```bash
# HTTP Events API (default)
bobi create-slack-bot --app-name "Agent Dispatch"

# Socket Mode for a self-hosted local Node event server
bobi create-slack-bot --socket-mode --app-name "Agent Dispatch"
```

Both commands print a Slack app manifest plus a one-click create link.
When the HTTP command runs interactively, it asks for the app name and event server URL before rendering anything.
Press Enter to use the bobi cloud event server, or enter your own URL.
If the agent runs on your own machine with the local event server, Slack can't reach localhost, so put a public tunnel (cloudflared or ngrok) in front of `localhost:8080` and enter the tunnel URL.
Both that tunnel topology and a standalone server on your own box are covered in `docs/SELF_HOSTED_EVENT_SERVER.md`.

Scripted or piped HTTP generation has no prompts.
The request URL comes from the project config when run inside an install and otherwise uses the bobi cloud event server.
Override either default with `--event-server https://…` or `--app-name <name>`.
Socket Mode never prompts for or emits an event server URL.

Then create the app one of three ways:

- **One click:** open the printed
  `https://api.slack.com/apps?new_app=1&manifest_json=…` link, pick your
  workspace, and click **Create**. Scopes + event subscriptions are prefilled.
- **Slack CLI:** see [Create with the Slack CLI](#create-with-the-slack-cli).
- **Manual:** https://api.slack.com/apps → **Create New App** → **From a
  manifest**, paste the YAML.

The manifest pins exactly the scopes and bot events the bobi Slack adapter
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
4. For HTTP Events API, on **Basic Information**, copy the **Signing Secret** and set it as `SLACK_SIGNING_SECRET` on the event server so it verifies inbound requests.
5. For Socket Mode, open **Basic Information → App-Level Tokens**, generate an `xapp-` token with the `connections:write` scope, and keep it for the next step.

> Slack verifies the event **Request URL** with a `url_verification` challenge
> the moment you create or reinstall the app, so your event server must be
> reachable then. For local dev, start it first:
> `bobi agent <name> event-server start`.

## 3. Add the token to bobi

Install or reinstall the Bobi Agent that needs Slack. `bobi agents install`
prompts for any missing `${VAR}` credentials and writes them to that named
agent's `run/.env`.

```bash
bobi agents install <source> --name <name>
# Paste the xoxb- token when prompted for SLACK_BOT_TOKEN.
# Socket Mode also accepts the optional xapp- SLACK_APP_TOKEN.
```

For non-interactive installs, provide the value in the environment:

```bash
SLACK_BOT_TOKEN=xoxb-... \
SLACK_APP_TOKEN=xapp-... \
  bobi agents install <source> --name <name> --non-interactive
```

Omit `SLACK_APP_TOKEN` for HTTP webhook mode.

In `agent.yaml`, the bot token is referenced as a `${VAR}`:

```yaml
services:
  - name: slack
    events: true
    credentials:
      bot_token: ${SLACK_BOT_TOKEN}
      app_token: ${SLACK_APP_TOKEN:-}
```

The `:-` makes the app token optional for webhook-only installs.
Existing packs enabling Socket Mode must add the `credentials.app_token` mapping shown above and save `SLACK_APP_TOKEN` in the named agent's `run/.env`.
The token is forwarded only through bubble-signed registration after the target event server reports `mode: local`; the hosted Worker never receives it.

## 4. Invite the bot to channels

The bot only sees (and posts to) channels it's a member of. In each channel:

```
/invite @Agent Dispatch
```

DMs work out of the box — the manifest enables the Messages tab.

To scope the bot to one dedicated channel, save it as `SLACK_CHANNELS` in the
agent's `run/.env`. Setup-authored `agent.yaml` files read it via
`channels: ${SLACK_CHANNELS:-}` (unset means no scoping); the setup completion
screen saves it for you, resolving a `#name` to its channel ID via the bot
token, and can post a test message to prove token + channel + membership.

## Verify Socket Mode and migrate safely

After Socket Mode is enabled and `SLACK_APP_TOKEN` is saved, start or restart the agent, then run:

```bash
bobi agent <name> doctor
```

`Slack Socket Mode` must report the configured app as `connected`.
If it reports unsupported, not registered, backoff, or fatal, fix that state before relying on Socket Mode.
During an HTTP migration, toggle Socket Mode off immediately if the connection does not become healthy so Slack resumes the saved Request URL.

[Slack switches one app exclusively between HTTP and WebSocket delivery](https://docs.slack.dev/apis/events-api/using-socket-mode/); the same app cannot overlap both transports.
Prepare the app token and `credentials.app_token` mapping while HTTP remains active, keep the existing Request URL and signing secret, and schedule a quiet cutover window.
Toggle Socket Mode on, immediately start or restart the agent, and wait for doctor to report `connected` before sending a test event.
Events that arrive after the toggle but before the socket connects can be lost.
Slack also has no Discord-style paste-back readiness gate: a login DM emitted before the socket connects can be lost.

To roll back, toggle Socket Mode off first so Slack resumes the saved HTTP Request URL, then verify webhook delivery.
Revoke the app-level token, remove `SLACK_APP_TOKEN`, restart the local event server, and immediately restart every agent pointed at it because the server restart clears registrations.
Events arriving between the server restart and agent re-registration are dropped, so use a quiet window.

## Create with the Slack CLI

Useful if you live in the [Slack CLI](https://api.slack.com/automation/cli) or
want to script app creation. Write the manifest to a file, then create from it:

```bash
bobi create-slack-bot --app-name "Agent Dispatch" --format json -o manifest.json

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
bobi create-slack-bot --app-name "Tenant A" \
  --event-server https://bobi-events.modalabs.workers.dev \
  --format json -o tenant-a.json
# then: POST tenant-a.json to apps.manifest.create with your config token
```

## Multiple workspaces

Create one app per workspace (each gets its own `xoxb-` token) and store each as
a named credential:

```yaml
# ~/.config/bobi/credentials.yaml
workspace-a:
  slack_bot_token: "xoxb-workspace-a-token"
workspace-b:
  slack_bot_token: "xoxb-workspace-b-token"
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Slack rejects the Request URL on create | Event server unreachable — start it (`bobi agent <name> event-server start`) and reinstall the app |
| Bot receives nothing | Event subscriptions disabled, or the bot isn't in the channel — reinstall from the manifest and `/invite` it |
| Bot can't post to a channel | `/invite @your-bot` in that channel |
| `not_authed` error | Token expired or wrong — reinstall and copy a fresh `xoxb-` token |
| Inbound events `401`/ignored | `SLACK_SIGNING_SECRET` on the event server doesn't match the app's signing secret |
| Socket Mode is unsupported or not registered | Confirm the target is the local Node event server, add `credentials.app_token`, then restart the agent |
| Socket Mode is `fatal` | Generate a fresh `xapp-` token with `connections:write`, save it as `SLACK_APP_TOKEN`, and restart |
