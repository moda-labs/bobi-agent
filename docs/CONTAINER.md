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
| `gosu` (privilege drop); no `tini` | Fly injects its own PID-1 init (reaps zombies / forwards signals); tini-on-Fly is a known boot-failure trigger. For other runtimes, use `docker run --init`. |
| `modastack start --foreground` entrypoint | container mode (C2) |

The agent's `$HOME` stays on the **image** (`/home/modastack`), so baked team
tools (`~/dev/gstack`, skills) are read in place. Claude's durable state is
redirected to the **volume** via `CLAUDE_CONFIG_DIR=/data/claude`, and the
entrypoint points the whole `~/.claude` at it — so `~/.claude/.credentials.json`
and `~/.claude/projects/` (session transcripts, required for resume) persist
across image updates while remaining reachable at their usual `~/.claude` paths.

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
  -e MODASTACK_EVENT_SERVER=https://your-worker.example.workers.dev \
  -e SLACK_BOT_TOKEN=xoxb-... \
  -e GITHUB_TOKEN=ghp_... \
  modastack:dev
```

### subscription mode (internal dogfood only)

Uses OAuth credentials on the volume (`/data/claude/.credentials.json`, reachable
as `~/.claude/.credentials.json`) instead of an API key. **`ANTHROPIC_API_KEY` must be unset** — it silently
outranks subscription auth and bills the API (§6.1). The image refuses to
start if both are set.

```bash
docker run --rm -v modastack-a:/data \
  -e MODASTACK_AUTH=subscription \
  -e MODASTACK_TEAM=eng-team \
  -e MODASTACK_EVENT_SERVER=https://your-worker.example.workers.dev \
  -e MODASTACK_LOGIN_CHANNEL=C0PRIVATE \
  -e SLACK_BOT_TOKEN=xoxb-... \
  modastack:dev
```

**First-boot login is automated (C23).** When the volume has no credentials,
the entrypoint runs `modastack login-bootstrap` before starting the manager:
it drives `claude auth login --claudeai` under a pty, posts the OAuth URL to
the private Slack channel `MODASTACK_LOGIN_CHANNEL`, and waits for you to paste
the auth code back **in that channel** — the code arrives as a normal
Slack→Worker→deployment event over the event bus. The channel **must be
private**: the code is single-use but grants the login to whoever pastes it
first. Refresh-token rotation makes this a once-per-machine ceremony.

Manual fallback (if Slack/event-bus isn't wired yet):

```bash
# one-time, interactive, writes /data/claude/.credentials.json
docker run --rm -it -v modastack-a:/data \
  -e CLAUDE_CONFIG_DIR=/data/claude --entrypoint claude modastack:dev auth login --claudeai
```

Never copy a `.credentials.json` between machines — shared refresh chains
invalidate each other.

## Environment variables

| Var | Required | Meaning |
|---|---|---|
| `MODASTACK_AUTH` | no (default `api_key`) | `api_key` or `subscription` |
| `MODASTACK_TEAM` | on first boot* | team to install into an empty volume, by bundled/registry name |
| `MODASTACK_TEAM_URL` | on first boot* | public `.tar.gz` URL of one team package, fetched at first boot; takes precedence over `MODASTACK_TEAM`. *Set exactly one of `MODASTACK_TEAM` / `MODASTACK_TEAM_URL`.* |
| `ANTHROPIC_API_KEY` | api_key mode | **must be absent** in subscription mode |
| `MODASTACK_LOGIN_CHANNEL` | subscription mode | private Slack channel ID for the first-boot login bootstrap (C23) |
| `MODASTACK_EVENT_SERVER` | yes | the Worker URL (`https://`) the team config references via `${MODASTACK_EVENT_SERVER}`; the client derives `wss://` from it |
| `MODASTACK_FLEET` | no (default: app-name prefix) | operator/fleet namespace stamp; the authoritative fleet-membership key the GitOps automation enumerates by (C22). The app name is only a discovery hint |
| `SLACK_BOT_TOKEN`, `GITHUB_TOKEN`, `LINEAR_API_KEY`, … | per team | service tokens (`${VAR}` refs in `agent.yaml`) |
| `DATA_DIR` / `MODASTACK_PROJECT` / `MODASTACK_HOME` | no | layout overrides (default `/data`, `/data/project`, `/home/modastack`). `MODASTACK_HOME` is the IMAGE home — baked tools live there |
| `CLAUDE_CONFIG_DIR` | no (default `/data/claude`) | Claude's durable config dir on the volume (creds, transcripts, settings); the entrypoint links `~/.claude` to it |

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
  -e MODASTACK_TEAM=eng-team -e MODASTACK_EVENT_SERVER=https://... \
  modastack:dev
# wait for healthy, then:
docker exec c8 modastack ask "Reply with: pong"
```

`tests/integration/test_container_image.py` automates the image-contract
checks (non-root, no Node, baked model, auth guards) and the live round-trip
(skipped unless `ANTHROPIC_API_KEY` is set).

## Deploying to Fly (C10)

`docker run` above is the local contract test. To stand up a real instance on
Fly Machines — app + persistent volume + secrets + a remotely-built image — use
the provisioner, which wraps everything above (it sets these same env vars and
secrets, never local Docker):

```bash
scripts/provision-instance.sh --app <operator-namespaced-name> --team eng-team \
  --env-file ./instance.env --region sjc
# subscription mode and bring-your-own-event-server are covered in the script header:
scripts/provision-instance.sh --help
```

Tear down with `scripts/destroy-instance.sh --app <name>` (removes the volume —
back up first). The script header documents the operator-agnostic flags and the
"deploy your own event server" runbook (design §9.1).

### Smoke testing

`tests/fixtures/smoke-team` is the zero-secret smoke target — it needs only
`MODASTACK_EVENT_SERVER` (no service tokens, no `requires`, no monitors), so an
instance reaches a healthy manager and answers one `modastack ask` with nothing
but an Anthropic credential. The `team-packages` workflow publishes it to the
rolling `teams-latest` release, so a full provisioner → boot → ask round-trip is:

```bash
scripts/provision-instance.sh --app <you>-modastack-smoke \
  --team-url https://github.com/moda-labs/modastack/releases/download/teams-latest/smoke-team.tar.gz \
  --env-file ./smoke.env --region sjc          # smoke.env: just ANTHROPIC_API_KEY
```

(Real teams like `eng-team` carry `requires:`/service secrets; the smoke team
deliberately doesn't, so it isolates the container/Fly path from team setup.)

## GitOps automation (C22)

Provisioning above is the manual seam. C22 reconciles `agents/` to a live Fly
**fleet** automatically — push a team to `main` and its instance appears; tag a
release and the whole fleet rolls to the new image. The Fly API is the only
state store (no DB, no manifest); `scripts/fleet.sh` is the enumeration helper,
and the same contract a future provisioner service inherits (design §9).

**Fleet identity.** An app is named `<fleet>-<team>` and stamped
`MODASTACK_FLEET=<fleet>` in its `[env]`. The stamp — not the name — is the
membership key, so two fleets can share one Fly org and a later
`MODASTACK_TENANT` filter slots into the same query for multitenant SaaS.

**One-time repo setup** (in `moda-labs/modastack` settings):

| What | Where | Value |
|---|---|---|
| `FLEET_PREFIX` | repo **variable** | operator namespace, e.g. `moda` (apps become `moda-<team>`) |
| `MODASTACK_EVENT_SERVER` | repo **variable** (optional) | Worker URL; omit to use the provisioner default |
| `FLY_API_TOKEN` | repo **secret** | an org-scoped Fly deploy token (`fly tokens create deploy`) |
| `MODASTACK_ENV` | **per-team** GitHub *Environment* named `<team>` | the team's entire KEY=VALUE env-file as one secret blob (the same content you'd pass to `--env-file`) |

The single-blob `MODASTACK_ENV` secret is the only secret interface — it routes
exactly like `--env-file` (`MODASTACK_*` → `[env]`, everything else → Fly
secrets) and is the seam a token broker later fills. Team Environments must have
**no required-reviewer protection rule** (it would pause the provision matrix).

**The three flows:**

- **Add a team** → push a new `agents/<team>/` to `main`. `team-packages.yml`
  publishes its tarball; `deploy-agent-teams.yml` (triggered on that completing) sees
  no `<fleet>-<team>` app yet → runs the provisioner with the team's
  `MODASTACK_ENV`. No manual step beyond the one-time Environment secret.
- **Edit a team** → push changes under an existing `agents/<team>/`. The matching
  instance is updated in place: `modastack install <teams-latest-url>` over
  `fly ssh` (workspace-safe reinstall — *not* `agents update`, which can't
  resolve a `url:`-sourced pack) then `fly machine restart`. The volume's
  `agent.yaml` and workspace edits survive.
- **Release** → publish a GitHub Release. `release.yml` builds the wheel once,
  builds the canary image **from that wheel** and smokes it (`CANARY-OK`), and only
  then — gated on the canary — publishes the same wheel to PyPI and rolls the fleet:
  generic apps reuse the canary's image digest (`fly config save` + `fly deploy
  --image`), team-flavored apps rebuild their own image from the wheel. Each app's
  volume/sessions/env is preserved; per-app failures are isolated; re-run to retry.

- **Delete a team** → nothing automatic. Run `scripts/destroy-instance.sh --app
  <fleet>-<team>` by hand (it removes the volume — back up first).

Manual fleet ops mirror the workflows: `scripts/fleet.sh list <prefix>` lists the
fleet, `scripts/fleet.sh classify <prefix> <team>…` shows added-vs-changed, and
both GitOps workflows accept a `workflow_dispatch` for manual re-runs.
