# Discord Setup

Connect a Discord bot so people can chat with your agent (#2). Unlike Slack
and WhatsApp, Discord does not push message events over HTTP webhooks -
inbound messages arrive over the **Gateway**, a persistent WebSocket the
event server opens and maintains. The **local event server** holds that
connection; a remote/Cloudflare event server does not receive Discord
messages yet (outbound replies work from either).

**Time:** ~10 minutes.

**Scope (v1):** the agent receives DMs, messages that @mention the bot, and
replies to the bot's own messages. It does not read the full channel
firehose. Replies go out through the Discord REST API (2000-character
messages, edits supported).

**Auth bootstrap:** Discord can be used as the private
`BOBI_LOGIN_CHANNEL` for subscription-login bootstrap, including Codex device
auth. Set it to a Discord conversation reference such as
`discord:<application_id>:dm:<channel_id>` (or `:channel:<channel_id>` for a
server channel). Bobi posts the login prompt through the channel gateway and,
for paste-back logins, listens on the app-wide `discord:<application_id>` topic
for the code pasted into that same conversation. Paste-back login requires the
event server to report a connected Discord Gateway for that application; with a
remote/Cloudflare event server, Discord can still carry Codex device-auth
prompts because Codex does not wait for a pasted chat message.

## 1. Create the application and bot

1. Open https://discord.com/developers/applications → **New Application**.
2. Copy the **Application ID** from General Information. This becomes
   `DISCORD_APPLICATION_ID` and the agent's subscription topic
   `discord:<application_id>`.
3. Under **Bot**, click **Reset Token** and copy the bot token. This is
   `DISCORD_BOT_TOKEN`.
4. Under **Bot**, turn **Public Bot off** (it defaults to on). With it on,
   anyone who has your Application ID can invite the bot to *their* server,
   and every member there can then @mention it and drive your agent - v1 has
   no guild allowlist, so the invite is the only gate.

The default (unprivileged) intents cover the v1 surface: DMs and messages
mentioning the bot are exempt from the privileged Message Content intent.
Only enable **Message Content** (Bot → Privileged Gateway Intents) if you
want message text on replies whose auto-mention was suppressed - then also
set `DISCORD_MESSAGE_CONTENT=1` so the server requests the intent.
Requesting the intent without enabling it in the portal makes Discord refuse
the connection (close code 4014, surfaced as `fatal` in `/health`).

## 2. Invite the bot to your server

1. Application → OAuth2 → **URL Generator**: check the `bot` scope, then the
   **Send Messages** and **Read Message History** permissions.
2. Open the generated URL and add the bot to your server. For DMs, no invite
   is needed - users can message the bot directly once they share a server.

## 3. Configure the team

Add the credentials to the runtime `.env`:

```bash
DISCORD_BOT_TOKEN=…
DISCORD_APPLICATION_ID=111222333444555666
```

And declare the service in `agent.yaml` with the credential mapping -
subscription detection and registration read `credentials:`, not the bare
environment:

```yaml
services:
  - name: discord
    events: true
    credentials:
      bot_token: ${DISCORD_BOT_TOKEN}
      application_id: ${DISCORD_APPLICATION_ID}
```

At session start the agent registers the app with the event server (a
bubble-signed `POST /discord/apps` that verifies the bot token upstream,
stores the send credential, and grants `discord:<application_id>` to this
instance), then subscribes to the app's topic. On the local server a
successful registration also starts the Gateway connection; the server
additionally connects at boot when `DISCORD_BOT_TOKEN` /
`DISCORD_APPLICATION_ID` are present in its environment (the launcher
forwards them automatically).

Discord subscriptions are app-wide in v1: the subscription key is exactly
`discord:<application_id>`. There is no `channels:` scoping knob like Slack.
Which guild channels can reach the agent is controlled by Discord permissions
and by the v1 normalizer filter: DMs, bot @mentions, and replies to the bot
are delivered; unrelated channel messages are ignored.

## 4. Test it

1. DM the bot, or @mention it in a channel it can read.
2. The agent receives a `discord.dm` / `discord.mention` / `discord.reply`
   event and replies into the same channel via
   `bobi reply discord:<application_id>:channel:<channel_id> "…"` (DMs use
   `:dm:<channel_id>`).
3. `bobi agent <name> event-server status` (or `GET /health`) shows a
   `discord_gateway` block with the connection state.

Discord renders a markdown subset (bold, italics, code blocks, lists);
messages cap at 2000 characters - the gateway chunks longer replies.

To use Discord for subscription-login bootstrap, first send the bot a DM (or
@mention it in the target server channel) and copy the `conversation:` value
from the received event. Put that exact value in `BOBI_LOGIN_CHANNEL`; the
legacy raw channel-id form remains Slack-only. For Claude paste-back login,
use an event server with the local Gateway driver; Bobi checks `/health` for a
connected `discord_gateway` entry before posting the login URL.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `/health` shows `fatal: authentication failed` | Bad bot token - reset it in the portal and update `DISCORD_BOT_TOKEN`, then re-register (restart the agent) |
| `/health` shows `fatal: disallowed intents` | `DISCORD_MESSAGE_CONTENT=1` is set but the Message Content intent is not enabled in the developer portal - enable it or unset the variable |
| `/health` shows `fatal: sharding required` | The bot is in 2500+ guilds; sharding is not supported yet |
| No inbound events | The event server must run locally (the hosted Worker has no Gateway driver yet); check `discord_gateway` in `/health`, and remember guild messages only arrive when the bot is @mentioned or replied to |
| Replies fail with `no send credential registered` | The app isn't registered for this instance - restart the agent so registration runs, and check the token/application id |
| Guild replies arrive as `[message content unavailable …]` | The message neither mentioned the bot nor was a DM-exempt payload - enable the Message Content intent (portal + `DISCORD_MESSAGE_CONTENT=1`) |
| A local server ignores new env vars | It reads them once at process start - `bobi agent <name> event-server restart` |
