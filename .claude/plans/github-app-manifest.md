# Plan: Centralized Event Server + GitHub App

Replace the current polling-and-direct-webhook architecture with a centralized
event server on Cloudflare Workers. One GitHub App receives all webhook events.
Modastack deployments connect outbound via WebSocket to subscribe to their repos'
events, with automatic catch-up on missed events after downtime.

## Problem

Three problems, one solution:

1. **Webhook permissions** — moda-bot can't install webhooks because it lacks admin
   access. A centralized GitHub App gets webhook permissions via installation.
2. **Event loss during downtime** — when modastack restarts or crashes, all events
   during the gap are lost. The central server buffers events and replays on reconnect.
3. **Inbound port requirement** — current webhook server requires a public IP with
   open ports. Outbound WebSocket connections work behind NAT/firewalls.

## Architecture

```
GitHub ──webhook──┐
Linear ──webhook──┤
Slack ──webhook───┤
                  ▼
     Cloudflare Worker (event-server)
          │
          ├── receives webhooks, assigns sequence IDs
          ├── stores events in KV (48h buffer)
          └── Durable Object per deployment
                  │
                  ▼ WebSocket
          modastack deployment
              │
              └── "last_seen: 57" → replays 58, 59, 60... → live stream
```

## Components

### 1. Cloudflare Worker: `moda-event-server`

A single Worker with three responsibilities:

**Webhook ingestion (HTTP routes):**
- `POST /webhooks/github` — receives GitHub App webhook events
- `POST /webhooks/linear` — receives Linear webhook events  
- `POST /webhooks/slack` — receives Slack event API payloads

Each incoming event gets:
- A globally monotonic sequence ID (per-deployment namespace)
- Stored in KV with TTL of 48 hours
- Forwarded to the Durable Object for each matching deployment

**Deployment registration (HTTP routes):**
- `POST /deployments` — register a new deployment, returns API key
- `GET /deployments/:id/subscribe` — upgrade to WebSocket

**Event routing logic:**
- GitHub: route by `installation.id` or `repository.full_name`
- Linear: route by workspace ID or team key
- Slack: route by workspace/bot token identifier

### 2. Durable Object: `DeploymentSession`

One DO per registered deployment. Responsibilities:

- **Subscription state** — which repos/orgs/Linear teams this deployment cares about
- **Cursor tracking** — last-acknowledged event ID per deployment
- **WebSocket management** — holds the live connection to the deployment
- **Replay on reconnect** — when a deployment reconnects with `last_seen: N`,
  fetch events N+1..latest from KV and send them in order before switching to live

**Event delivery contract:**
- Events delivered in sequence order, no gaps
- Deployment sends `{"ack": 73}` to advance its cursor
- If WebSocket is disconnected, events buffer in KV (up to 48h)
- On reconnect, full replay from cursor position

### 3. GitHub App: `Modastack` (centralized)

One GitHub App registered under the moda-labs org:

**Permissions:**
- `contents: write` — read/write repo contents (for PRs, branches)
- `issues: write` — manage issues and labels
- `pull_requests: write` — create/update PRs, request reviews
- `checks: read` — read CI status
- Webhook events: `issues`, `issue_comment`, `pull_request`,
  `pull_request_review`, `check_run`, `workflow_run`

**Webhook URL:** `https://moda-events.<domain>/webhooks/github`

**Installation flow:**
1. User runs `modastack setup`
2. Opens `https://github.com/apps/modastack/installations/new`
3. User selects org and repos to grant access
4. GitHub sends `installation` webhook to the event server
5. Event server auto-registers the deployment's subscription

**Token generation stays local.** Each modastack deployment has a copy of the
app's private key (provisioned during `modastack setup`). It generates its own
installation tokens via JWT for GitHub API calls (creating PRs, managing issues).
The central server only handles webhook receipt and forwarding — it never needs
to call GitHub's API on behalf of deployments.

### 4. Modastack client changes

Replace the current webhook server + polling architecture in `modastack/manager/events/`
with an outbound WebSocket client:

**New file: `modastack/manager/events/event_client.py`**
- Connects to `wss://moda-events.<domain>/deployments/:id/subscribe`
- Authenticates with deployment API key
- Sends `last_seen` cursor on connect (persisted in `~/.modastack/cursor.json`)
- Receives events, feeds them into the existing event bus
- Acks events after successful processing
- Auto-reconnects with exponential backoff

**Modified: `modastack/manager/events/consumer.py`**
- `run()` starts the WebSocket client instead of (or alongside) pollers
- Events from the WebSocket feed into the same bus/batching pipeline
- Existing manager session injection unchanged

**Removed (or made optional):**
- `webhook_server.py` — no longer needed; events come via WebSocket
- GitHub/Linear pollers in `pollers.py` — replaced by webhooks through the
  central server. Slack Socket Mode can stay as-is or move to central server.

**Kept as fallback:**
- `gh` CLI auth for GitHub API calls (creating PRs, etc.)
- Local polling mode via `modastack start` (no `--webhooks`) for users who
  don't want the central server

## KV Schema

```
events:{deployment_id}:{sequence_id} → {event JSON}     TTL: 48h
cursor:{deployment_id}              → {last_acked_id}
deployments:{api_key}               → {deployment config}
subscriptions:{owner/repo}          → [deployment_id, ...]
```

## Setup Flow (updated)

```bash
modastack setup <repo>
```

1. Detect GitHub org from remote URL
2. Check if Modastack GitHub App is installed on that org
   - If not: open browser to install URL, wait for confirmation
3. Register deployment with central event server (gets API key)
4. Save API key + deployment ID to `~/.modastack/config.yaml`
5. Configure repo subscriptions on the event server
6. Existing setup steps: generate .modastack.yaml, install skills, hooks

## Auth Model

| Action | Auth method |
|---|---|
| Receive webhook events | Central server → deployment via WebSocket (API key) |
| Create PRs, manage issues | Deployment generates installation token locally (app private key) |
| Register deployment | One-time API key from central server |
| Install GitHub App on org | User clicks install in browser (GitHub handles auth) |

## Key Design Decisions

- **Central server is dumb relay.** It receives, stores, and forwards. It never
  calls external APIs. All GitHub/Linear API calls happen on the deployment side.
  This keeps the Worker simple and stateless (except for KV/DO).
- **48-hour event buffer.** Covers weekends, maintenance windows, EC2 crashes.
  After 48h, the deployment falls back to polling-based reconciliation (scan
  Linear/GitHub for current state), which already exists.
- **Private key distributed to deployments.** Each deployment gets a copy during
  setup. This means deployments can make GitHub API calls independently. The
  alternative (central server proxies all API calls) adds latency and complexity.
- **Sequence IDs per deployment namespace.** Not global. Each deployment gets its
  own monotonic counter. Simpler, no cross-deployment ordering needed.
- **`gh` CLI stays as fallback** for GitHub API calls. Existing users don't break.

## What This Replaces

| Current | New |
|---|---|
| `webhook_server.py` (inbound HTTP) | WebSocket client (outbound) |
| GitHub polling in `pollers.py` | GitHub webhooks via central server |
| Linear polling in `pollers.py` | Linear webhooks via central server |
| `moda-bot` user account + PAT | Modastack GitHub App + installation tokens |
| Events lost on restart | 48h buffer with replay |
| Public IP required | Works behind NAT |

## Implementation Sequence

### Phase 1: Central event server (Cloudflare Worker)
1. Scaffold Worker project with Durable Objects + KV
2. GitHub webhook ingestion endpoint
3. Deployment registration + API key management
4. Durable Object: WebSocket handling, cursor tracking, replay
5. Deploy to Cloudflare

### Phase 2: GitHub App
1. Register Modastack app under moda-labs org
2. Configure permissions and webhook URL → central server
3. Install on moda-labs org
4. Test: webhook events arrive at central server

### Phase 3: Modastack client
1. New `event_client.py` — WebSocket client with reconnect + replay
2. Wire into consumer.py event loop
3. Update `modastack setup` to register with central server
4. Update `modastack start` to use WebSocket mode by default
5. Keep polling mode as `modastack start --local` fallback

### Phase 4: Linear + Slack integration
1. Add Linear webhook ingestion to central server
2. Add Slack event API endpoint (or keep Socket Mode local)
3. Route Linear events to deployments by team key
4. Remove polling loops from modastack client

## Testing

- Unit tests: event serialization, cursor logic, subscription matching
- Integration test: Worker receives webhook → DO buffers → client replays
- Miniflare for local Worker testing
- Manual end-to-end: install app, push to repo, verify event arrives at deployment
