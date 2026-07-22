# Self-Hosted Event Server

The event server is bobi's pub/sub bus: provider events land on it, and agents receive them over an outbound WebSocket (architecture, topics, and the security model live in [EVENT_SERVER.md](EVENT_SERVER.md)).
GitHub, Linear, WhatsApp, and Slack's default HTTP Events API deliver webhooks only to a public HTTPS URL, so those paths need public ingress in front of an event server you run.
Slack Socket Mode is the exception: the local Node event server dials out to Slack and needs no public Request URL.
This guide covers both transport choices and two self-hosted server shapes:

- **Tunnel** - everything on one machine. The embedded local server that
  `bobi agent <name> start` launches automatically, plus a public tunnel
  (cloudflared, ngrok) in front of it for webhooks. Right for a single box
  you already keep running.
- **Standalone** - a small always-on server (any VPS) runs the event server
  behind TLS; agents on any machine point at it. Right when agent machines
  lack stable ingress (laptops), when more than one machine runs agents, or
  when you want webhook ingress to outlive any one agent host.

Both run the same single-node Node server (`event-server/src/local.ts`) with
the same webhook verification as any production deployment. The trade against
a managed/durable tier is operational: state is in memory, so a server
restart drops registrations and buffered replay (see
[What a restart means](#what-a-restart-means)).

## Slack Socket Mode: no public ingress

Socket Mode is an opt-in for the local Node runtime, whether that runtime is embedded beside one agent or runs standalone on another box.
The Cloudflare Worker does not hold persistent Slack sockets.

Generate the Socket Mode manifest, then create an app-level token with the `connections:write` scope:

```bash
bobi create-slack-bot --socket-mode --app-name "Agent Dispatch"
```

Save the bot and app tokens in the agent runtime:

```dotenv
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

The Slack service in `agent.yaml` must reference both values, with the app token optional so webhook mode stays the default:

```yaml
services:
  - name: slack
    events: true
    credentials:
      bot_token: ${SLACK_BOT_TOKEN}
      app_token: ${SLACK_APP_TOKEN:-}
```

On startup, Python verifies that the target `/health` payload reports `mode: local`, then includes the app token only in the bubble-signed `POST /slack/workspaces` registration.
There is no event-server environment fallback for this credential, and the hosted Worker never receives it.
A standalone Node server reached over a tailnet, LAN, tunnel, or public TLS URL still reports `mode: local` and can run Socket Mode.

After starting the agent, run `bobi agent <name> doctor` and require the `Slack Socket Mode` check to report `connected`.
The check reports unsupported, not registered, retrying, and fatal states without printing the app token.

Slack switches one app exclusively between HTTP and WebSocket delivery, so the same app cannot overlap both transports during migration.
Prepare the app token and bobi configuration while HTTP remains active, keep the existing Request URL and signing secret, and schedule a quiet cutover window.
Toggle Socket Mode on, immediately start or restart the agent, and wait for doctor to report `connected` before sending a test event.
Events that arrive after the toggle but before the socket connects can be lost.
Slack also has no Discord-style paste-back readiness gate, so a login DM sent before the socket connects can be lost.
To roll back, toggle Socket Mode off first so Slack resumes the saved HTTP Request URL, then verify webhook delivery.
Revoke the app-level token, remove `SLACK_APP_TOKEN`, restart the local event server, and immediately restart every agent pointed at it because the server restart clears registrations.
Events arriving between the server restart and agent re-registration are dropped, so use a quiet window.

## Tunnel: expose the embedded server

`bobi agent <name> start` already runs an event server on
`127.0.0.1:8080`. Put a tunnel in front of it:

```bash
# quick start (URL rotates every run - fine for trying it out)
cloudflared tunnel --url http://localhost:8080

# durable (named tunnel with your own hostname; ngrok reserved domains work too)
cloudflared tunnel create bobi-events
cloudflared tunnel route dns bobi-events events.example.com
cloudflared tunnel run --url http://localhost:8080 bobi-events
```

Then:

1. Enter the tunnel URL as the webhook/request URL when configuring each
   provider (see [Wiring the providers](#wiring-the-providers)). The
   **Webhook ingress** row in `bobi setup`'s Connections tab persists it so
   generated configs and manifests use it.
2. Put the verification secrets in the agent's runtime `.env`. The launcher
   passes `SLACK_SIGNING_SECRET` and `LINEAR_WEBHOOK_SECRET` through to the
   server it spawns; GitHub verification reads `BOBI_ES_WEBHOOK_SECRET`
   directly from the environment.

Providers hold the URL you gave them, so a quick-tunnel URL stops working
when the tunnel restarts. Use a named tunnel or reserved domain for anything
you keep.

## Standalone: run the server on its own box

Requirements on the box: Node 20+, a DNS name, TLS in front (below). No
Python and no bobi install needed - the server is plain Node.

```bash
git clone https://github.com/moda-labs/bobi-agent.git
cd bobi-agent/event-server
npm install
npm run build:local          # esbuild bundle -> dist/local.js
BOBI_ES_SLACK_SIGNING_SECRET=... \
BOBI_ES_LINEAR_WEBHOOK_SECRET=... \
BOBI_ES_WEBHOOK_SECRET=... \
node dist/local.js
```

(A pip install of bobi bundles the same sources under
`site-packages/bobi/event-server/` if you prefer not to clone.)

### Configuration

All configuration is environment variables, read at startup:

| Variable | Default | Purpose |
|---|---|---|
| `BOBI_ES_PORT` | `8080` | Listen port |
| `BOBI_ES_BIND` | `127.0.0.1` | Listen address. Keep loopback and terminate TLS on the same box |
| `BOBI_ES_WEBHOOK_SECRET` | unset | GitHub webhook secret (`X-Hub-Signature-256`) |
| `BOBI_ES_SLACK_SIGNING_SECRET` | unset | Slack app signing secret |
| `BOBI_ES_LINEAR_WEBHOOK_SECRET` | unset | Linear webhook signing secret |
| `BOBI_ES_WHATSAPP_APP_SECRET` | unset | Meta app secret for WhatsApp signatures |
| `BOBI_ES_WHATSAPP_VERIFY_TOKEN` | unset | WhatsApp GET-subscribe handshake token |
| `BOBI_ES_INGEST_TOKENS` | unset | Boot-seeded `topic=token` ingest bindings, comma-separated |

An unset provider secret admits that provider's webhooks **unverified**
(zero-config local development). On a public server set every secret for a
provider you use; `/health` counts unverified admissions as
`webhook_unverified` so you can see the gap.

### TLS in front

Agents refuse to mint their trust-bubble credential over a cleartext remote
URL, so a standalone server is only reachable-by-agents at an `https://` URL.
Keep the server bound to loopback and put the TLS terminator on the same box.
The proxy must pass WebSocket upgrades - agents hold a long-lived `wss://`
socket on `/deployments/<id>/subscribe`.

Caddy does both with two lines:

```
events.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

A named cloudflared tunnel on the box works equally well and needs no open
inbound port. For nginx, forward the `Upgrade`/`Connection` headers.

### Keep it running

The server is one small process; run it under your init system.

```ini
# /etc/systemd/system/bobi-events.service
[Unit]
Description=bobi event server
After=network-online.target

[Service]
WorkingDirectory=/opt/bobi-agent/event-server
ExecStart=/usr/bin/node dist/local.js
Restart=always
EnvironmentFile=/etc/bobi-events.env   # the BOBI_ES_* variables, mode 0600

[Install]
WantedBy=multi-user.target
```

### Point agents at it

In the project's `agent.yaml`:

```yaml
event_server: https://events.example.com
```

Setup-authored configs reference `${BOBI_EVENT_SERVER:-}`, so exporting
`BOBI_EVENT_SERVER=https://events.example.com` in the runtime `.env` does the
same. On start the agent skips launching a local server, mints or joins its
trust bubble over TLS, registers its subscriptions, and holds an outbound
WebSocket. Nothing connects inbound to the agent machine.

## Wiring the providers

Point each provider at the route on your public URL; the setup skills cover
the provider-side clicks and scopes:

| Provider | Request URL | Server-side secret | Guide |
|---|---|---|---|
| Slack HTTP Events API | `https://<host>/webhooks/slack` | `BOBI_ES_SLACK_SIGNING_SECRET` | `skills/slack-setup.md` |
| Slack Socket Mode | none; local Node server dials out | signed `SLACK_APP_TOKEN` registration | `skills/slack-setup.md` |
| GitHub | `https://<host>/webhooks/github` | `BOBI_ES_WEBHOOK_SECRET` | repo webhook settings |
| Linear | `https://<host>/webhooks/linear` | `BOBI_ES_LINEAR_WEBHOOK_SECRET` | `skills/linear-setup.md` |
| WhatsApp | `https://<host>/webhooks/whatsapp` | `BOBI_ES_WHATSAPP_APP_SECRET` + verify token | `skills/whatsapp-setup.md` |
| Anything else | `https://<host>/webhooks/ingest/<topic>` | scoped ingest token | `docs/EVENT_SERVER.md` |

Slack and WhatsApp verify the URL the moment you save it (Slack's
`url_verification` challenge, Meta's GET handshake), so the server and
ingress must be up first.

### Verify the path end to end

```bash
curl -s https://events.example.com/health | jq
```

Check `webhook_unverified` is 0 and stays 0, and that `webhook_bad_signature`
increments when you send garbage:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://events.example.com/webhooks/github \
  -H 'Content-Type: application/json' \
  -H 'X-Hub-Signature-256: sha256=bad' -d '{}'   # expect 401
```

Then use each provider's own test delivery (GitHub webhook "Redeliver",
Linear's webhook test button, a Slack mention) and watch it arrive in the
agent's inbox.
For Socket Mode, also confirm `bobi agent <name> doctor` reports the Slack app as connected before sending the mention.

## What a restart means

The standalone server is the **single-node, in-memory tier** - that is the
design, not an accident. Its state (trust bubbles, deployment registrations
and subscriptions, resource grants, non-env ingest tokens, and the replay
buffer of the last 10,000 events per deployment) lives in process memory.

- **While server and agents stay up**, delivery is at-least-once with
  cursor-based replay: an agent that disconnects, crashes, or restarts
  catches up from its cursor on reconnect.
- **When the server restarts**, webhooks flow again immediately but every
  registration is gone. Running sessions cannot receive events until they
  restart; on its next start an agent detects the lost bubble, re-mints, and
  re-registers automatically. So the operational rule is: **if you restart
  the event server, restart the agents pointed at it.** Events delivered to
  the server between those two restarts are dropped.

If you need durable replay across server restarts, that is the managed
deployment tier, not this one.

## Security notes for a public server

- HTTPS only, loopback bind, TLS terminator on the same box.
- Set every provider secret you use; watch `webhook_unverified` on `/health`.
- Generic ingress goes through scoped ingest tokens (topic-bound, hash-stored,
  revocable), never through a provider route.
- Tenancy on a shared server is coarse (a global topic fans out to every
  granted bubble). Run one server per trust domain. The full model - trust
  bubbles, resource grants, verification pipeline - is in
  [EVENT_SERVER.md](EVENT_SERVER.md).
