# The reference image

`ghcr.io/moda-labs/bobi` is a published container image that runs one Bobi
Agent under supervision. It exists so that standing up Bobi on your own
infrastructure needs nothing from us but a public pull — no repository grant, no
vendored Dockerfile, no copy of a recipe that drifts from ours.

**What is in it:** everything needed to stand up an agent in a VM — the `bobi`
wheel, the knowledge-base dependencies, the pinned brain CLIs (`claude`,
`codex`, `aichat`), the runtime tools, and the supervisor sidecar. **And nothing
else.** There is no team baked in, no credentials, and no operator tooling. One
image serves every tenant; identity arrives at runtime through the volume and
the environment.

For the wire protocol an external control plane speaks to the supervisor, see
[`ADMIN_PROTOCOL.md`](ADMIN_PROTOCOL.md). For the event server the agent
connects to, see [`EVENT_SERVER.md`](EVENT_SERVER.md) and
[`SELF_HOSTED_EVENT_SERVER.md`](SELF_HOSTED_EVENT_SERVER.md).

## Pull it

```bash
docker pull ghcr.io/moda-labs/bobi:latest      # newest non-prerelease
docker pull ghcr.io/moda-labs/bobi:0.51.1      # a specific bobi version
```

The tag is the `bobi` version it contains, and `:latest` tracks the newest
non-prerelease. Both are multi-arch (`linux/amd64` + `linux/arm64`), each arch
built natively — never under emulation, because the runtime stage executes the
brain binaries it installs and the Bun-compiled `claude` binary segfaults under
`qemu-user`. Per-arch tags (`:<version>-amd64`, `:<version>-arm64`) exist as
manifest inputs; pull the plain version tag.

The image is public and anonymously pullable. It needs no GitHub token.

## Run it

**`--init` is required.** The image deliberately ships without an init system.
The entrypoint `exec`s the supervisor, which spawns the agent manager as a
child, and a supervisor running as PID 1 does not reap orphaned grandchildren —
over a long-lived deployment those accumulate as zombies. Rather than bake an
init (baking one is a documented boot-failure trigger on some orchestrators that
inject their own), the image expects the platform to supply it:

| Platform | How |
|---|---|
| `docker run` | `--init` |
| Docker Compose | `init: true` on the service |
| Kubernetes | `shareProcessNamespace: true` on the pod spec, or an init-providing runtime |
| Fly Machines | nothing — Fly injects its own init |

```bash
docker run --init --rm -v bobi-eng:/data \
  -e BOBI_INSTANCE=eng \
  -e BOBI_TEAM=eng-team \
  -e BOBI_AUTH=api_key \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e BOBI_EVENT_SERVER=https://your-worker.example.workers.dev \
  ghcr.io/moda-labs/bobi:latest
```

The volume at `/data` is the agent's identity and its only durable state. Reuse
it and the agent resumes; discard it and you get a fresh agent. Nothing
identifying lives in the image.

### First boot

The entrypoint runs as root just long enough to prepare the volume, then drops
to the unprivileged `bobi` user and `exec`s:

```
bobi agent "$AGENT_NAME" supervise -- --foreground
```

Everything after `--` is forwarded verbatim to the manager's start command.
`--foreground` keeps the manager a supervisable child rather than letting it
daemonize, which is what makes `SIGTERM` reach it for a graceful shutdown.

On an empty volume the entrypoint installs the team named by `BOBI_TEAM` or
fetched from `BOBI_TEAM_URL`. With **neither** set it enters a *wait-for-team*
state and polls for `run/package/agent.yaml` instead of crashing — that is the
hook for pushing a team onto the volume out of band.

The instance self-mints its event-bus bubble and self-registers its sessions.
No provisioning step writes credentials into it.

### Environment

`BOBI_HOME` (default `/data/.bobi`) and the `/data` volume are the layout. The
agent to run is selected by the first of these that is set:

| Var | Meaning |
|---|---|
| `BOBI_AGENT` | agent name, highest precedence |
| `BOBI_INSTANCE` | agent name |
| `BOBI_ROOT` | a run root; the agent name is its parent directory |

At least one is required — the entrypoint refuses to boot without a selection
rather than guessing.

| Var | Required | Meaning |
|---|---|---|
| `BOBI_AUTH` | no (default `api_key`) | `api_key` or `subscription` |
| `BOBI_TEAM` | on first boot* | team to install, by bundled/registry name |
| `BOBI_TEAM_URL` | on first boot* | public `.tar.gz` URL of one team package; takes precedence over `BOBI_TEAM` |
| `ANTHROPIC_API_KEY` | native Claude, `api_key` mode | **must be absent** in `subscription` mode |
| `ANTHROPIC_AUTH_TOKEN` | authenticated Claude gateway | bearer token, accepted *instead of* `ANTHROPIC_API_KEY`; omit when the gateway accepts Claude subscription OAuth |
| `BOBI_GATEWAY_API_KEY` | Codex gateway | key consumed by Codex's configured provider |
| `BOBI_EVENT_SERVER` | yes | event-server URL (`https://`); the client derives `wss://` from it |
| `BOBI_FLEET` | no | operator/fleet namespace stamp |
| `CLAUDE_CONFIG_DIR` | no (default `/data/claude`) | Claude's durable state on the volume; `~/.claude` is linked to it |
| `DATA_DIR`, `BOBI_HOME` | no | layout overrides (`/data`, `/data/.bobi`) |

\* Set exactly one of `BOBI_TEAM` / `BOBI_TEAM_URL`.

Service tokens the team's `agent.yaml` references as `${VAR}` (for example
`SLACK_BOT_TOKEN`, `GITHUB_TOKEN`, `LINEAR_API_KEY`) are passed the same way.

### Health

The manager serves `GET /health` on a port written to
`/data/project/run/state/manager-health.port`. It binds `127.0.0.1` on an
ephemeral port by default, which the image's own `HEALTHCHECK` reads and probes:

```bash
docker inspect -f '{{.State.Health.Status}}' <container>
```

Kubernetes probes originate from the kubelet against the pod IP, so a k8s
deployment needs a fixed, non-loopback bind:

```yaml
env:
  - name: BOBI_HEALTH_BIND
    value: "0.0.0.0"
  - name: BOBI_HEALTH_PORT
    value: "8081"
livenessProbe:
  httpGet: { path: /health, port: 8081 }
readinessProbe:
  httpGet: { path: /ready, port: 8081 }
```

`/health` is a cheap in-process liveness check; `/ready` returns `503` until the
director session reports `running` or `idle`. Both are served concurrently, and
a connection that does not complete a request is dropped after 10s — one
stalled or half-open client cannot queue every probe behind it. **Keep the
health port private to the pod network** — `/health` reports process and session
status for operators, so it does not belong behind a public Service or Ingress.

## Build it yourself

You do not need to; the published image is the supported path. If you do:

```bash
# From a released version on PyPI — what the release pipeline publishes.
docker build --build-arg BOBI_BUILD=pypi --build-arg BOBI_VERSION=0.51.1 -t bobi:pypi .

# From this checkout, via a locally built wheel.
python -m build --wheel --outdir dist/     # needs Node 20 exactly
docker build --build-arg BOBI_BUILD=wheel -t bobi:dev .
```

`BOBI_BUILD=wheel` installs exactly one prebuilt `dist/*.whl`. Building that
wheel requires **Node.js 20 specifically** — the build hook compiles the
embedded event server and rejects other majors, including newer ones.

> The Dockerfile's nominal default, `BOBI_BUILD=source`, does not work and is
> not a supported mode: `.dockerignore` excludes both `.git` and
> `event-server/dist`, so the build hook inside the image finds neither a VCS
> checkout nor a prebuilt artifact and tries to rebuild the event server in a
> stage that has no Node. Pass `wheel` or `pypi` explicitly.

### Baking team tools (`TEAM_DEPS`)

Some teams need host tools present in the image. The `TEAM_DEPS` build-arg
points at a shell hook that runs during the build; the default is a no-op, so an
image built without it is byte-identical to one built with the default:

```bash
docker build --build-arg TEAM_DEPS=path/to/rendered-hook.sh -t bobi:eng .
```

The same hook can instead be applied as an overlay on the published image, which
avoids rebuilding the recipe at all:

```dockerfile
FROM ghcr.io/moda-labs/bobi:0.51.1
COPY rendered-hook.sh /tmp/team-deps.sh
RUN /tmp/team-deps.sh
```

Both forms render from the same public renderer (`bobi/build_render.py`). If a
hook needs a secret, pass it as a BuildKit secret
(`--mount=type=secret,id=NAME`) and never as a build-arg — a build-arg persists
in `docker history`.

## Publishing (maintainers)

`.github/workflows/release-image.yml` builds and pushes the image. It installs
`bobi==<version>` **from PyPI**, so it runs *after* the public release is live —
the published image must contain the exact bytes PyPI serves, not a
separately-rebuilt wheel that merely claims the same version.

```bash
gh workflow run release-image.yml \
  -f version=0.51.1 -f claude-version=<pinned> -f source-sha=<sha>
```

Dispatching any released version re-publishes that version's image from the
current Dockerfile, which is also the hotfix path. `:latest` moves only when the
version being published is this repository's newest non-prerelease release, so
re-running an old release's job can never move it.

Pushing to GHCR authenticates with the workflow's `GITHUB_TOKEN` and requires
this repository to have write access to the `ghcr.io/moda-labs/bobi` package.
That access is granted in the package's settings and is **not** implied by the
`org.opencontainers.image.source` label, which only links the package to a
repository for display.
