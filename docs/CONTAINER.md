# Container image (instance deployment)

The modastack instance image packages the framework, a pinned native `claude`
CLI, and the embedding model into one immutable image. Tenant identity lives
entirely in the mounted volume and env vars — see
[`docs/design/CONTAINERIZED_INSTANCES.md`](design/CONTAINERIZED_INSTANCES.md)
§2 for the full instance contract. This doc is the build/run runbook for C8
(#338); first-boot logic (C9), the Fly provision script (C10), and GitOps
(C22) build on top of it.

## What's in the image

| Property | Why |
|---|---|
| `python:3.11-slim` base | small, matches `requires-python` |
| Non-root `modastack` user (uid 10001) | Claude Code refuses `bypassPermissions` as root unless `IS_SANDBOX=1` (§5); we drop privileges instead |
| Native `claude` CLI (no Node) | the local Node event server is never run in deployed instances (C6); the CLI is the standalone binary |
| `DISABLE_AUTOUPDATER=1` | freeze the CLI at the built version (the image is the unit of update) |
| fastembed model baked at `HF_HOME=/opt/modastack/models` | cold-start speed; no first-run download |
| `tini` (PID 1) + `gosu` | signal forwarding / zombie reaping; privilege drop |
| `modastack start --foreground` entrypoint | container mode (C2) |

The agent's `$HOME` is set to `/data/home` (on the volume) so
`~/.claude/.credentials.json` and `~/.claude/projects/` (session transcripts,
required for resume) persist across image updates.

## Build

```bash
# default: 'stable' channel of the claude CLI
docker build -t modastack:dev .

# reproducible production build: pin an exact claude CLI version
docker build -t modastack:dev --build-arg CLAUDE_VERSION=2.1.89 .
```

Build args: `CLAUDE_VERSION` (default `stable`), `MODASTACK_UID` (default
`10001`).

## Run

The image needs: a volume at `/data`, an auth mode, the team to install, the
event-server URL, and the service tokens the team uses.

### api_key mode (fleet default)

```bash
docker run --rm -v modastack-a:/data \
  -e MODASTACK_AUTH=api_key \
  -e MODASTACK_TEAM=eng-team \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e MODASTACK_EVENT_SERVER_URL=wss://your-worker.example.workers.dev \
  -e SLACK_BOT_TOKEN=xoxb-... \
  -e GITHUB_TOKEN=ghp_... \
  modastack:dev
```

### subscription mode (internal dogfood only)

Uses OAuth credentials on the volume (`/data/home/.claude/.credentials.json`)
instead of an API key. **`ANTHROPIC_API_KEY` must be unset** — it silently
outranks subscription auth and bills the API (§6.1). The image refuses to
start if both are set.

```bash
docker run --rm -v modastack-a:/data \
  -e MODASTACK_AUTH=subscription \
  -e MODASTACK_TEAM=eng-team \
  -e MODASTACK_EVENT_SERVER_URL=wss://your-worker.example.workers.dev \
  -e SLACK_BOT_TOKEN=xoxb-... \
  modastack:dev
```

Getting the credentials onto the volume the first time is the **C23** login
bootstrap (post the `claude /login` URL to a private Slack channel, read the
pasted code back over the event bus). Until C23 lands, the manual fallback is:

```bash
# one-time, interactive, writes /data/home/.claude/.credentials.json
docker run --rm -it -v modastack-a:/data \
  -e HOME=/data/home --entrypoint claude modastack:dev /login
```

Refresh-token rotation makes this a once-per-machine ceremony. Never copy a
`.credentials.json` between machines — shared refresh chains invalidate each
other.

## Environment variables

| Var | Required | Meaning |
|---|---|---|
| `MODASTACK_AUTH` | no (default `api_key`) | `api_key` or `subscription` |
| `MODASTACK_TEAM` | on first boot | team to install into an empty volume |
| `ANTHROPIC_API_KEY` | api_key mode | **must be absent** in subscription mode |
| `MODASTACK_EVENT_SERVER_URL` | yes | the Worker WSS URL the team config references |
| `SLACK_BOT_TOKEN`, `GITHUB_TOKEN`, `LINEAR_API_KEY`, … | per team | service tokens (`${VAR}` refs in `agent.yaml`) |
| `DATA_DIR` / `MODASTACK_PROJECT` / `MODASTACK_HOME` | no | volume layout overrides (default `/data`, `/data/project`, `/data/home`) |

## Health

The manager exposes `GET /health` on a localhost port written to
`/data/project/.modastack/state/manager-health.port`. The image `HEALTHCHECK`
(and Fly script checks) read that file and probe the endpoint:

```bash
docker inspect -f '{{.State.Health.Status}}' <container>
```

## Acceptance smoke (C8)

```bash
# api_key: empty volume -> healthy manager -> one ask round-trip
docker run -d --name c8 -v "$(mktemp -d):/data" \
  -e MODASTACK_AUTH=api_key -e ANTHROPIC_API_KEY=sk-ant-... \
  -e MODASTACK_TEAM=eng-team -e MODASTACK_EVENT_SERVER_URL=wss://... \
  modastack:dev
# wait for healthy, then:
docker exec c8 modastack ask "Reply with: pong"
```

`tests/integration/test_container_image.py` automates the image-contract
checks (non-root, no Node, baked model, auth guards) and the live round-trip
(skipped unless `ANTHROPIC_API_KEY` is set).
