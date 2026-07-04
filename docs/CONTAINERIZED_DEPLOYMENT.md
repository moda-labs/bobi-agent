# Deployment (image + Fly + GitOps)

How a bobi agent team becomes a running, self-managing instance on Fly, and the
hard-won nuances that make it work. This is the end-to-end operational runbook:
the image, building and running it, the `bobi deploy` primitive, the Fly
provisioner, fleet identity, secrets, and GitOps. The why/what of containerized
instances, and the remaining scale-to-zero design, is tracked in issue #395.

Pieces:

- `bobi deploy <name>` / `destroy <name>` (`bobi/deploy.py`) — **the
  one-instance primitive**; everything else is mechanics it drives or
  orchestration that calls it
- `deployments/<name>.yaml` (+ `defaults.yaml`) — per-instance operator config
- `Dockerfile` — the image
- `scripts/provision-instance.sh` + `scripts/destroy-instance.sh` — stand up / tear down one instance
- `scripts/fleet.sh` — fleet enumeration helper
- `scripts/build-team-tarballs.sh` — package teams into `.tar.gz`
- `.github/workflows/release.yml` — the single gated framework release pipeline:
  build + smoke both brain canaries (`ci-canary` Claude + `ci-codex-smoke` Codex),
  then publish the proven wheel
- `.github/workflows/deploy-agent-teams.yml.example` — example generic
  `deployments/*` reconciler for fleet-owning repos; `bobi deploy-init`
  generates the standalone version
- `.github/workflows/team-packages.yml` — publishes team tarballs (for `team-url` delivery)

---

## 1. Mental model

**Git is what should be running; Fly is what is running; `bobi deploy` closes
the gap, one instance at a time.** A `deployments/<name>.yaml` is one instance an
operator runs. Add/edit one and run `bobi deploy <name>` (or let the GitOps
Action do it on the next release) → the instance appears or updates in place.
Delete one → its Fly app is surfaced for a human `bobi destroy`. There is
**no database and no manifest** — the Fly API is the only state store.

The layering that keeps this operator-agnostic:

```
orchestration (per operator) : GitHub Action │ Terraform │ SaaS plane │ a for-loop
the primitive (bobi)    : bobi deploy <name> / destroy <name>   ← ONE instance
mechanics                    : provision-instance.sh · fleet.sh · install · fly
```

`bobi deploy` is **idempotent** — no Fly app yet ⇒ provision, app exists ⇒
update — so the caller never decides provision-vs-update; it just names the
instance. Anything that loops or diffs across instances is orchestration on top.

An **instance** = one Fly app + one persistent volume (mounted at `/data`, holding
both the project and `$HOME`) + env vars + an **outbound-only** WebSocket to one
event server (a Cloudflare Worker). Nothing reaches in; it reaches out. First KB
use may also fetch the fastembed model over HTTPS into the mounted volume cache.
All instances share **one image**; identity lives entirely in the volume + env.

---

## 2. The image

The instance image packages the framework and a pinned native `claude` CLI. The
embedding model downloads on first KB use into the mounted volume. Tenant identity
lives entirely in the mounted volume and env vars (the full instance contract is
tracked in issue #395).
The image is built **for Fly**, so several choices below are Fly-driven; the same
image also runs under plain `docker run` for a local contract test.

### What's in it

| Property | Why |
|---|---|
| `python:3.11-slim` base | small, matches `requires-python` |
| Non-root `bobi` user (uid 10001) | Claude Code refuses `bypassPermissions` as root unless `IS_SANDBOX=1`; we drop privileges with `gosu` instead |
| Native `claude` CLI (no Node) | the local Node event server is never run in deployed instances; the CLI is the standalone binary |
| `DISABLE_AUTOUPDATER=1` | freeze the CLI at the built version (the image is the unit of update) |
| `FASTEMBED_CACHE_PATH=/data/.bobi/cache/fastembed` | first KB use downloads the model into durable writable state instead of slowing image builds |
| `gosu` (privilege drop); no `tini` | Fly injects its own PID-1 init (reaps zombies, forwards signals); tini-on-Fly is a known boot-failure trigger. For other runtimes, use `docker run --init` |
| `bobi agent <name> start --foreground` entrypoint | container mode |

The agent's `$HOME` stays on the **image** (`/home/bobi`), so baked team tools
(`~/dev/gstack`, skills) are read in place. Claude's durable state is redirected to
the **volume** via `CLAUDE_CONFIG_DIR=/data/claude`, and the entrypoint points the
whole `~/.claude` at it, so `~/.claude/.credentials.json` and `~/.claude/projects/`
(session transcripts, required for resume) persist across image updates while
remaining reachable at their usual `~/.claude` paths.

### Build

`Dockerfile` at the repo root, two-stage: **builder** builds the `bobi` wheel into
a venv; **runtime** is slim Debian + the venv, the native pinned `claude` CLI (no
Node), `git`/`curl`/`gosu`, a volume-backed fastembed cache path, and
`docker-entrypoint.sh`.
Runs as non-root uid 10001.

```bash
# default: 'stable' channel of the claude CLI
docker build -t bobi:dev .

# reproducible production build: pin an exact claude CLI version
docker build -t bobi:dev --build-arg CLAUDE_VERSION=2.1.89 .
```

Build args: `CLAUDE_VERSION` (default `stable`), `BOBI_UID` (default `10001`).

### Run it with Docker (local contract test)

The image needs a volume at `/data`, an auth mode, the team to install, the
event-server URL, and the service tokens the team uses. `docker run` is the local
contract test; Fly provisioning (below) sets the same env + secrets.

**api_key mode (fleet default):**

```bash
docker run --rm -v bobi-a:/data \
  -e BOBI_AUTH=api_key \
  -e BOBI_TEAM=eng-team \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e BOBI_EVENT_SERVER=https://your-worker.example.workers.dev \
  -e SLACK_BOT_TOKEN=xoxb-... \
  -e GITHUB_TOKEN=ghp_... \
  bobi:dev
```

**subscription mode (internal dogfood only):** uses OAuth credentials on the
volume (`/data/claude/.credentials.json`) instead of an API key.
**`ANTHROPIC_API_KEY` must be unset** - it silently outranks subscription auth and
bills the API; the image refuses to start if both are set.

```bash
docker run --rm -v bobi-a:/data \
  -e BOBI_AUTH=subscription \
  -e BOBI_TEAM=eng-team \
  -e BOBI_EVENT_SERVER=https://your-worker.example.workers.dev \
  -e BOBI_LOGIN_CHANNEL=C0PRIVATE \
  -e SLACK_BOT_TOKEN=xoxb-... \
  bobi:dev
```

**First-boot login is automated.** When the volume has no credentials, the
entrypoint runs `bobi agent <name> login-bootstrap` before starting the manager:
it drives `claude auth login --claudeai` under a pty, posts the OAuth URL to the
private Slack channel `BOBI_LOGIN_CHANNEL`, and waits for you to paste the auth
code back in that channel (it arrives as a normal Slack->Worker->deployment
event). The channel **must be private**: the code is single-use but grants login
to whoever pastes it first. Refresh-token rotation makes this a once-per-machine
ceremony. Manual fallback if the event bus isn't wired yet:

```bash
docker run --rm -it -v bobi-a:/data \
  -e CLAUDE_CONFIG_DIR=/data/claude --entrypoint claude bobi:dev auth login --claudeai
```

Never copy a `.credentials.json` between machines; shared refresh chains
invalidate each other.

### First boot (the entrypoint)

The entrypoint creates the volume layout, installs the team (`BOBI_TEAM_URL` or
`BOBI_TEAM`), then `exec gosu`es to the `bobi` user running
`bobi agent <name> start --foreground` (PID 1). The instance **self-mints its
event-bus bubble and self-registers every session** - the provisioner never
touches `deployment_id`/`api_key`.

With **neither** team var set on an empty volume the entrypoint enters a
**wait-for-team** state: it polls for `run/package/agent.yaml` instead of
crashing. That is the ssh-push hook - the instance boots blank and holds while
`bobi deploy` pushes a local team onto the volume (next section); the moment the
team lands, it proceeds to start.

### Environment variables

| Var | Required | Meaning |
|---|---|---|
| `BOBI_AUTH` | no (default `api_key`) | `api_key` or `subscription` |
| `BOBI_TEAM` | on first boot* | team to install into an empty volume, by bundled/registry name |
| `BOBI_TEAM_URL` | on first boot* | public `.tar.gz` URL of one team package, fetched at first boot; takes precedence over `BOBI_TEAM`. *Set exactly one of `BOBI_TEAM` / `BOBI_TEAM_URL`.* |
| `ANTHROPIC_API_KEY` | api_key mode | **must be absent** in subscription mode |
| `BOBI_LOGIN_CHANNEL` | subscription mode | private Slack channel ID for the first-boot login bootstrap |
| `BOBI_EVENT_SERVER` | yes | the Worker URL (`https://`) the team config references via `${BOBI_EVENT_SERVER}`; the client derives `wss://` from it |
| `BOBI_FLEET` | no (default: app-name prefix) | operator/fleet namespace stamp; the authoritative fleet-membership key the GitOps automation enumerates by. The app name is only a discovery hint |
| `SLACK_BOT_TOKEN`, `GITHUB_TOKEN`, `LINEAR_API_KEY`, ... | per team | service tokens (`${VAR}` refs in `agent.yaml`) |
| `DATA_DIR` / `BOBI_PROJECT` / `BOBI_HOME` | no | layout overrides (default `/data`, `/data/project`, `/home/bobi`). `BOBI_HOME` is the IMAGE home - baked tools live there |
| `CLAUDE_CONFIG_DIR` | no (default `/data/claude`) | Claude's durable config dir on the volume (creds, transcripts, settings); the entrypoint links `~/.claude` to it |

### Health

The manager exposes `GET /health` on a port written to
`/data/project/run/state/manager-health.port`. By default it binds
`127.0.0.1` on an ephemeral port, preserving the image `HEALTHCHECK` and Fly
script checks that read the port file and probe the endpoint:

```bash
docker inspect -f '{{.State.Health.Status}}' <container>
```

Kubernetes `httpGet` probes originate from the kubelet against the pod IP, so
set a fixed port and non-loopback bind address for k8s deployments. Use
`127.0.0.1` or `0.0.0.0` for `BOBI_HEALTH_BIND`; the bundled Docker/Fly
healthcheck probes `127.0.0.1`, so binding to a specific pod IP is not
compatible with that script.

```yaml
env:
  - name: BOBI_HEALTH_BIND
    value: "0.0.0.0"
  - name: BOBI_HEALTH_PORT
    value: "8081"
ports:
  - name: health
    containerPort: 8081
livenessProbe:
  httpGet:
    path: /health
    port: health
readinessProbe:
  httpGet:
    path: /ready
    port: health
```

`/health` is a cheap in-process liveness check. `/ready` returns `503` until the
director session reports `running` or `idle`, then returns `200`.

Keep the health port private to the pod network. `/health` includes process and
session status for operators, so do not expose it through a public Service or
Ingress. For Docker and Fly, leave the default loopback bind in place; the
bundled `HEALTHCHECK` probes `127.0.0.1` via the port file.

### Fly build gotchas (each one cost real debugging - do not regress)

1. **`WORKDIR` must NOT be under the volume mount.** The volume mounts at `/data`
   and **shadows** anything beneath it, so `WORKDIR /data/project` makes the
   container's cwd not exist at runtime → Fly's init can't `exec` *any* binary
   (your entrypoint *and* Fly's own `hallpass`) → `No such file or directory (os
   error 2)`, crash-loop to max-restart 10. Fix: **`WORKDIR /`**; the entrypoint
   `cd`s into `${BOBI_PROJECT}` itself.
2. **No `tini`.** Fly Machines inject their own PID-1 init; shipping `tini` is a
   documented Fly boot-failure trigger. Keep it out of the ENTRYPOINT and apt.
   (For non-Fly runtimes, use `docker run --init`.)
3. **`--depot=false` on `fly deploy`.** Depot's default zstd OCI layers can't be
   extracted by Fly's machine init → incomplete rootfs → ENOENT on exec. The
   classic builder (gzip layers) boots fine. Load-bearing, not optional.
4. **`--ha=false`.** Fly defaults to HA = a spare machine, which needs a *second*
   volume and fails the deploy against our single volume.
5. **`--dockerfile <repo>/Dockerfile` explicitly.** The per-app `fly.toml` is
   generated in a temp dir; a relative `[build] dockerfile` key would resolve
   against *that* dir (no Dockerfile there). So pass `--dockerfile` and never put a
   `[build]` key in the generated config.
6. **`--wait-timeout 10m`.** First boot installs the team and can run live auth or
   startup checks past the default 5-minute machine-state wait.
7. **`[[mounts]]` array form** in the generated config (canonical).
8. **`fly ssh` admin lands in `/`**, and runtime identity is explicit. Admin
   commands should set the same home/root environment as the instance and call
   the named-agent CLI as the volume's uid-10001 owner:
   ```
   fly ssh console -a <app> --command \
     'gosu bobi env HOME=/home/bobi BOBI_HOME=/data/bobi CLAUDE_CONFIG_DIR=/data/claude bobi agent <name> status'
   ```
9. **Concurrent `fly deploy --remote-only` builds race on the org's single shared
   remote builder** (`failed to parse daemon host "unix:///var/run/docker.sock":
   missing hostname`). One build grabs the builder; the other dies. **Serialize
   provisions** (or deploy a prebuilt `image:` ref, §2.6, so the builder leaves
   the path entirely).

---

## 2.5. The primitive (`bobi deploy <name>`)

`bobi deploy <name>` resolves one instance's config, validates its secrets,
stamps identity, picks a delivery mode, and applies — idempotently. It is the
single entry point the CLI, CI, and any future control plane share.

**Config precedence** (merged by the command itself, so it works standalone):

```
CLI flags  ›  deployments/<name>.yaml  ›  deployments/defaults.yaml  ›  built-ins
```

- `deployments/<name>.yaml` = one instance (name = filename). `defaults.yaml` =
  shared operator *values* (fleet, event server, region) — **not** a deploy list;
  the deploy list is the set of `deployments/*.yaml` files.
- App name = `<fleet>-<name>`; stamps `BOBI_FLEET` + `BOBI_INSTANCE`
  (the per-instance/SaaS-tenant key) into `[env]`.
- A bare `<name>` with no file falls back to the local package `agents/<name>`
  (ssh-push) — the minimal dev path.

**Two delivery modes**, picked by the team source:

| `team: <name>` → **ssh-push** | `team-url: <url>` → **HTTPS-fetch** |
|---|---|
| a LOCAL package (`agents/<name>`) | a PUBLISHED `.tar.gz` |
| provision **blank** → build a tarball → push it onto the volume over `fly ssh` → the waiting entrypoint installs it and starts | provision with `--team-url` → the dark instance pulls the tarball at first boot (today's path) |
| "I built it, ship it" — no hosting (single dev, or CI from its own checkout) | enterprise / SaaS / anyone publishing tarballs |

The ssh-push push: `base64` the built tarball onto `/data` over `fly ssh`, then
`bobi agents install <tarball> --non-interactive` as the volume owner (reads secrets
from the Fly-injected env, fails loudly on a gap). On a **new** instance this
releases the wait-for-team loop (no restart); on an **existing** one it's a
workspace-safe reinstall + `fly machine restart` to reload.

**Secrets** come from `secrets.env-file:` (a local path) or the process env (the
CI seam — the Action exports the team's GitHub-Environment blob and runs
`bobi deploy`). For a local team the required `${VAR}`s are validated up
front; `BOBI_*` refs are identity (stamped from flags), never demanded as
secrets.

`bobi destroy <name>` resolves `<name>` → `<fleet>-<name>` and runs
`destroy-instance.sh` (Fly app + volume, typed-confirm; `--yes` for automation).

---

## 2.6. Team-flavored images — baked host tools

Some teams need **host tools** in the container, not just prompts. `eng-team`
declares `requires: [gstack, codex]`; the generic image ships neither (no Node),
and a dark container can't run `requires.fix` interactively — so it would
provision but never dispatch. A team fixes this by declaring a `build:` block in
its `agent.yaml`:

```yaml
build:
  apt: [nodejs, npm]              # installed as root (system-wide)
  npm: ["@openai/codex"]          # global → /usr/local/bin, on PATH
  run:                            # as the bobi user, into the image HOME
    - "git clone …/gstack ~/dev/gstack && cd ~/dev/gstack && ./setup"
  verify: requires                # re-run requires[].check at build → fail CI on a miss
```

**Two clocks.** Deps live in the **image**; the team **definition**
(prompts/workflows) keeps flowing through the volume (ssh-push / team-url). A
prompt edit is still a hot update — only a deps change rebuilds an image.

**How it builds (built on Fly during deploy).** `bobi deploy` renders the
`build:` spec to a shell hook (`bobi/build_render.py` →
`deploy.py:_render_team_deps_into_context`) into the build context and builds the
ONE Dockerfile **on Fly's remote builder** with `--build-arg TEAM_DEPS=<rendered>`
— Fly creates app + registry + machine together (no separate registry push), and
its builder caches the tool layers. The hook runs as a stable layer **below** the
framework-wheel copy, so a code-only framework release rebuilds only the wheel and
re-deploys stay cheap. No `build:` → the default `docker/noop-deps.sh` →
byte-identical to the generic image. `run_root:` steps run as root for tools `apt`
can't express (e.g. `npx playwright install-deps chromium`). CI
(`.github/workflows/team-images.yml` → `scripts/build-team-images.sh`) is a
build-only **verify gate** — it builds each team image (running the `requires`
check) but does not push. (A "build once → push to a registry → deploy many by
ref" optimization is deferred: Fly's registry rejects pushes to a never-deployed
app, GHCR needs `write:packages`.)

**Image HOME + volume config dir (no build/runtime split).** `$HOME` stays on the
**image** (`/home/bobi`) at build AND runtime, so `run:` steps bake
~-relative tools in place and the build's `verify` checks the exact paths the
agent uses. Claude's durable state lives on the **volume** via
`CLAUDE_CONFIG_DIR=/data/claude`, and the entrypoint points the whole `~/.claude`
at it — so any tool keyed off `~/.claude/{projects,settings.json,skills,…}` sees
Claude's real state. Personal skills bake at `/opt/bobi/skills` (immutable
image content, outside `~/.claude`) and are surfaced under the config dir. No
seed, no stamp, no copy. codex/gh need none of this (`npm i -g`/apt →
`/usr/local/bin`, on PATH).

**Deploying a prebuilt image (optional).** If a pullable image ref exists, add
`image: <ref>` to `deployments/<name>.yaml`; `bobi deploy` then passes
`--image` to the provisioner and **skips the build entirely** (no remote builder,
no race per gotcha #9). Without `image:`, a `build:`-declaring team is built on
Fly during deploy (above). The definition always flows via `team:`/`team-url:`.
For codex (and any service the tools call) put its key — e.g. `OPENAI_API_KEY` —
in the env blob; it becomes a Fly secret and the tool reads it at runtime (the
build only verifies the binary, which needs no auth). Referenced-but-optional
scoping vars (e.g. `channels: ${SLACK_CHANNELS}`, empty = whole workspace) may be
declared empty in the env blob without blocking the deploy.

### 2.6.1. Agent-bootstrapped dependencies + snapshot (#428)

A `tool_library:` dependency (the unified model) can declare a loose `guide:` +
required `success:` instead of pinned `install:` steps. A **guide-only** dep is
materialized by a **bootstrap agent** at image-build time (in CI, not
production): the agent reads the guide, installs the dependency with pinned
versions, and reports the exact steps as a machine-readable recipe. That recipe
feeds back through the same `build_render` renderer as an inline `build:`, so the
snapshot is a normal image layer and there is one install code path. Every dep is
verified against its `success` contract (per brain, build tier) before the layer
is trusted. See `bobi/dep_bootstrap.py` and issue #428.

- **One seam, agent-free for pinned teams.** `scripts/build-team-images.sh` and
  the release rollout render each team through `python -m bobi.dep_bootstrap
  --render`; a team with only pinned installs renders with no agent (a drop-in for
  the old `build_render` render). A guide-only dep is materialized by the bootstrap
  agent **inside a fresh base image** (`docker run` the base, so the recipe is
  faithful to the image, not the CI host), gated on the brain key in the CI env
  (`BOBI_BOOTSTRAP_BRAINS`, default `claude`). `bobi deploy` never runs the agent
  (it refuses to source-build a guide-only team and directs you to the CI `image:`).
  The full path (guide dep → live agent → frozen recipe → working image) is
  exercised end-to-end by `tests/integration/test_dep_bootstrap_e2e.py` in the
  container/claude CI suite.
- **Re-bootstrap detection.** The image stamps the DECLARED dependency-set hash
  (`/opt/bobi/dep-list.hash`, from `tool_library.dependency_list_hash`) alongside
  the #379 build-deps stamp. `bobi deploy` reads it over `fly ssh`: a matching set
  skips re-bootstrap; a changed set (a guide dep, a bumped pin, a `host:`/`mcp:`
  change) rebuilds in place to re-bootstrap. A warm boot runs no agent.
- **`host:` capabilities.** A dep can declare a host capability the container
  cannot grant itself (a kernel sysctl, a device):
  `host: [{sysctl: kernel.apparmor_restrict_unprivileged_userns=0}]`. It is
  runtime wiring, never baked: `bobi deploy` surfaces it to the operator and
  `bobi agent <name> doctor` verifies it on the host (see `bobi/host_caps.py`).
  gstack's `/browse` sandbox sysctl is one instance of this model.

---

## 3. The provisioner (`scripts/provision-instance.sh`)

Stands up one instance: `fly apps create` → 15 GB volume → stage secrets →
generate a per-app `fly.toml` (identity in `[env]`, 4 GB/shared-2x, **no
`[http_service]`** = dark/always-on) → `fly deploy`. **Idempotent** — re-running
redeploys, so it doubles as "redeploy this instance."

Most operators drive this through `bobi deploy` (§2.5), which fills these
flags from `deployments/<name>.yaml`. Key flags:
- Exactly one of `--team <name>` (bundled/registry), `--team-url <.tar.gz URL>`
  (the dark-instance injection seam — pulled at first boot), or `--blank` (no team
  source: boot into the wait-for-team state for ssh-push delivery).
- `--env-file` — KEY=VALUE; `BOBI_*` keys become plaintext `[env]` identity,
  **everything else becomes a Fly secret**. This routing is what lets one blob
  carry both (see §5).
- `--fleet <prefix>` — stamps `BOBI_FLEET` into `[env]` (see §4). Defaults to
  the app name's leading dash-segment.
- `--instance <name>` — stamps `BOBI_INSTANCE` (the per-instance/SaaS-tenant
  key, enumerable next to the fleet). Defaults to the app name minus `<fleet>-`.
- `--event-server <https URL>` — defaults to the shared moda Worker; the bubble key
  is refused over cleartext remote URLs, so it must be `https://` (or loopback).
- `--auth api_key|subscription` — api_key **requires** `ANTHROPIC_API_KEY` in the
  env-file; subscription **forbids** it (the key silently outranks subscription
  OAuth and bills the API).

What it deliberately does **not** do: pre-register a deployment, or write the
volume's `agent.yaml`. After first boot the volume config is the source of truth;
a reprovision sets only env + secrets, never project files.

`BOBI_EVENT_SERVER` is the var name (an `https://` value; the client derives
`wss://`). Tear down with `destroy-instance.sh --app <app>` — **removes the volume**
(the only copy of state); human-only, never automated.

> Fly account note: a new personal org may be flagged high-risk; clear it at
> `fly.io/high-risk-unlock` (card verify) before `fly apps create`.

---

## 4. Fleet identity & enumeration (`scripts/fleet.sh`)

The Fly API is the state store. A **fleet** is the set of instances sharing one
operator namespace, stamped `BOBI_FLEET=<prefix>` in each app's `[env]`.

- **App name = `<prefix>-<team>`** — a deterministic *discovery hint*.
- **The `BOBI_FLEET` stamp is the authoritative membership key** (name is only
  a hint). This is the SaaS-extensible primitive: two fleets can share one Fly org,
  and a future `BOBI_TENANT` filter slots into the same query.

`fleet.sh` (sourceable lib + CLI):
- `fleet.sh app <prefix> <team>` → `<prefix>-<team>`.
- `fleet.sh list <prefix>` → member app names. Candidates from a single
  `fly apps list --json` name-prefix filter, **each confirmed by its stamp** (so an
  unrelated `<prefix>-website` can't sneak in).
- `fleet.sh classify <prefix> <team>…` → `added=[…]` / `changed=[…]`, partitioned by
  whether `<prefix>-<team>` exists on Fly (added = provision, changed = update).
- `fleet.sh fleet-of <app>` → the app's stamp.

> flyctl gotcha: `fly config show -a <app>` outputs **JSON by default** — passing
> `--json` errors ("unknown flag"). `fleet.sh` reads `.env.BOBI_FLEET` from it.

---

## 5. Secrets model

**Fly secrets are the runtime store; `agent.yaml` is the schema; the env-file is
ephemeral transport.** The four roles:

| role | what |
|---|---|
| `agent.yaml` `${VAR}` refs | **schema** — which secrets a team needs (the declared set + prune authority) |
| GitHub Environment / shell | **values** — per-key, editable, transient |
| `--env-file` | **transport** — ephemeral, never authoritative |
| live Fly secrets | **runtime store** — the one durable source the instance reads |

**One GitHub Environment per TENANT** (not per deployment). Production deployments
default to the `modalabs` Environment (`tenant:` in `deployments/defaults.yaml`);
the canary is its own tenant. Within an Environment, secrets are **per-key**, named
`<TEAM>__<KEY>` — e.g. `ENG_TEAM__SLACK_BOT_TOKEN`. The `<TEAM>__` prefix
(deployment name, slug-normalized) namespaces multiple teams in one tenant; the
tenant lives only in the Environment name, never in the key.

The deploy job binds `environment: <tenant>`, dumps `toJSON(secrets)`, selects keys
with the `<TEAM>__` prefix, strips it, writes a temp env-file under `umask 077`, and
hands it to `bobi deploy … --env-file`.

**The reconcile** (`bobi/deploy.py`): on an existing app, deploy reads the live
Fly secret names (`fly secrets list`), then:
- a live secret **satisfies the required check** — an update needn't re-supply what
  Fly already holds (kills the "re-paste the whole blob" friction);
- supplied values are **set** (Fly no-ops identical ones — steady-state is quiet);
- live, non-`BOBI_*` secrets **not in the team's declared set are pruned**
  (`--no-prune` to disable) — so the store converges on what `agent.yaml` declares;
- it sets **only declared keys** — an undeclared key in the env-file (a `toJSON`
  dump's `FLY_API_TOKEN`, or a typo) is dropped with a warning, never provisioned.

This closes the drift hole that took `moda-eng-team` down: an `ANTHROPIC_API_KEY`
manually unset in `api_key` mode is **restored** on the next deploy (it's required),
not perpetuated; a stray one in `subscription` mode is pruned.

Notes:
- Editing one secret = one `gh secret set <TEAM>__<KEY> --env <tenant>` (or
  `fly secrets set <KEY>=… -a <app>` directly). No blob re-paste.
- A secret a team consumes at runtime but doesn't `${VAR}`-reference (e.g. the gh
  CLI's `GH_TOKEN`) must still be **declared** — add it to `agent.yaml` (eng-team
  wires `GH_TOKEN` as the github service credential), or the reconcile will prune it.
- Use a **secret**, not a variable (masking). The job `printf`s to disk under
  `umask 077`; the engine redacts secret values from its own logs.
- Tenant Environments must have **no required-reviewer protection rule** — it would
  pause the deploy matrix. The `<tenant>` prefix is *organization, not isolation*:
  `toJSON(secrets)` in any cell sees every secret in scope. True multi-tenant
  isolation needs an Environment (or repo) per tenant with no shared secrets.
- Self-service (no CI) points `secrets.env-file:` at a local file, or just relies on
  live Fly secrets + interactive supply.
- Fleet/tenant config lives in **`deployments/defaults.yaml`** (`fleet:`, `tenant:`,
  `event_server:`, sizing), not repo variables. The only repo secret is
  `secrets.FLY_API_TOKEN` (`fly tokens create deploy`); absent it, the workflows no-op.

---

## 6. GitHub Ops (thin clients over the primitive)

```
publish a GitHub Release   ─▶ release.yml  (the single gated pipeline)
   │                              subscription-login-smoke   (gate)
   │                                 │
   │                              build-wheel                (one artifact for all)
   │                                 python -m build -> upload the wheel/sdist
   │                                 │
   │                              build-canary               (THE gate)
   │                                 for each brain canary (ci-canary Claude,
   │                                 ci-codex-smoke Codex): build image FROM the
   │                                 wheel + `ask` it -> assert CANARY-OK e2e
   │                                 ├──────────────┐
   │                              publish
   │                              same wheel -> PyPI + Homebrew
   │
fleet repo deploy tag / dispatch ─▶ deploy-agent-teams.yml   (standalone, NO framework release)
          plan   : list ACTIVE deployments/<name>.yaml (defaults excluded) + tenant
          deploy : matrix over {name,tenant}, environment=<tenant>
                   └─ toJSON(secrets) | filter <TEAM>__ -> env-file -> `bobi deploy <name>`
          orphans: Fly apps with no deployments/ file -> warn (human `bobi destroy`)
```

**A release is the deploy gate** — an edit pushed to `main` does NOT auto-deploy;
you cut a release to ship the framework. **One functional gate guards the
framework artifact**: `release.yml` builds the wheel once, builds both brain
canaries (`ci-canary` Claude + `ci-codex-smoke` Codex) *from that wheel* and
smokes each (`CANARY-OK`), and only then publishes the same wheel to PyPI and
Homebrew. Codex is a hard gate at parity with Claude (#428); its instance is
`bootstrap`-tolerant in `release.yml` (warn+skip until first provisioned, then a
hard gate). Production agent rollout is owned by fleet repos
such as `moda-agents`; they bump their pinned `bobi` version and run their own
deploy workflow.

A team-definition or secret edit in a fleet repo can ship without a framework
release via that repo's `deploy-agent-teams.yml`. The reconcile **business logic
lives in `bobi deploy`**, not the YAML — the Action only orchestrates: list the
active deployments, hand each its secrets, loop the primitive. That is why the
same engine runs from a laptop, a fleet Action, Terraform, or a SaaS plane — see
§7.2 B (*Bring your own repo*).

### deploy-agent-teams.yml — fleet repo reconcile
The framework repo keeps `.github/workflows/deploy-agent-teams.yml.example` as
reference material only; it is not an active workflow here. Fleet-owning repos
use `bobi deploy-init` or copy/adapt the example as an active
`.github/workflows/deploy-agent-teams.yml`. Run standalone by a **`deploy-*` tag
push** (team/secret update) or **`workflow_dispatch`** (optional `only:` to scope
to one deployment). Jobs:
- **plan**: list every **active** `deployments/<name>.yaml` (`defaults.yaml`
  excluded; an inactive deployment is a non-`.yaml` like `<name>.yaml.example`).
  No git-diff; `bobi deploy` is idempotent. Gates the rest on
  `secrets.FLY_API_TOKEN` being set.
- **deploy** (matrix over the active `{name, tenant}`, `environment: <tenant>`):
  install the CLI, filter this deployment's per-key `<TEAM>__<KEY>` secrets out of
  `toJSON(secrets)` → env-file, then `bobi deploy <name> --env-file …`. One
  idempotent path — `deploy` itself decides provision-vs-update by Fly state and
  reconciles secrets to the declared set (§5).
- **orphans**: enumerate the fleet (`fleet.sh list`, fleet from `defaults.yaml`),
  warn on any app with no `deployments/` file (including a removed/inactivated
  deployment). **Never auto-destroys** (the volume is the only copy of state).

> **Delivery in CI.** Both delivery modes run from CI. **ssh-push (`team:`)** works
> with an **org-scoped** Fly token (`fly tokens create org` *can* `fly ssh`) — this
> is how moda-agents reconciles eng-team in place (`updating instance
> 'moda-eng-team' in place (ssh-push)`), pushing the team definition to the volume
> and reloading. **`team-url` (HTTPS-fetch)** is the alternative when you'd rather
> not give CI ssh, or to first-boot a dark instance with no SSH at all
> (`team-packages.yml` publishes the tarballs). The two always-on pipeline
> canaries exercise the `team-url` path: `deployments/canary.yaml` (Claude,
> app ci-canary) via `claude-smoke.tar.gz` and `deployments/codex-smoke.yaml`
> (Codex, app ci-codex-smoke) via `codex-smoke.tar.gz`.

### team-packages.yml (only for `team-url` delivery)
On push to main (path-filtered to `agents/**` + the smoke fixtures), builds each
team into `<team>.tar.gz` and **publishes to a rolling `teams-latest` GitHub
Release** → stable public URL
`https://github.com/<owner>/<repo>/releases/download/teams-latest/<team>.tar.gz`.
Sole publisher of that release; nothing else should `--clobber` it. Only needed
when a deployment uses `team-url:`; pure ssh-push (`team:`) deployments ignore it.

### release.yml — the release pipeline
Triggered by **`release: published`**. One gated pipeline; the canary, running the
exact wheel we publish, is the single functional gate for PyPI/Homebrew:
- **subscription-login-smoke** — gate the release on a verified subscription-login
  bootstrap (a hermetic mock-code smoke; #388).
- **build-wheel** — `python -m build` the wheel/sdist **once** and upload it, so the
  canary and PyPI run the identical artifact. A fail-fast
  `pip install dist/*.whl && bobi --version` rejects an obviously-broken wheel
  before the expensive canary build.
- **build-canary** — for each brain canary (`ci-canary` Claude, `ci-codex-smoke`
  Codex), deploy from an image built **from that wheel** (`--build-arg
  BOBI_BUILD=wheel`, the artifact staged into `dist/`), then a functional `ask`
  asserts `CANARY-OK` end-to-end. **This is the gate** - both brains at parity.
- **publish** — `needs: build-canary`. Uploads the **same** wheel to PyPI via
  trusted publishing (`environment: pypi`).
- **update-homebrew** — `needs: publish`. Bumps the tap and smokes bottle URLs.

Production fleet rollout is deliberately outside this workflow. Fleet repos bump
their pinned `bobi` version and run their own deployment reconcile.

> **PyPI trusted publishing.** The publish step must run in the top-level workflow
> the trusted-publisher config names (PyPI rejects a reusable workflow with
> `invalid-publisher`), so it's a native job in `release.yml`. Configure the PyPI
> trusted publisher as: repo `<owner>/<repo>`, workflow `release.yml`, environment
> `pypi`. (If you migrate from a prior `publish-pypi.yml` publisher, update it
> **before** the next release or the upload fails.)

> **flyctl gotchas:**
> - `fly config save` writes via **`-c <path>`**, not `-o` (which it rejects).
> - `fly image show -a <app> --json` returns `Ref`/`Reference`/`FullImageRef` as
>   **null** — construct the pull ref yourself:
>   `registry.fly.io/<Repository>@<Digest>`.
> - Deploying app B with app A's `registry.fly.io/<A>@<digest>` works (org-scoped
>   registry) — that's how one build rolls the whole fleet.

---

## 7. Playbook — stand up your own agents

Everything here is driven by the **`bobi` binary** — `uv tool install
bobi` and you're done; no repo checkout required (for hosting too, the
instance image installs bobi from PyPI). Pick where it runs:

- **7.1 Run it on your machine** — the simplest thing. Build a team, run it. No
  cloud, no Fly. This is the friends-and-family default. Start here.
- **7.2 Host it on Fly** — always-on, off your machine. One command; the binary
  walks you through Fly setup.

| | event server | drive it with |
|---|---|---|
| **7.1 Local** | bundled, loopback (no cloud) | `bobi agent <name> start` |
| **7.2 Fly, self-service** | a Cloudflare Worker | `bobi deploy` from your laptop |
| **7.2 Fly, CI** | a Cloudflare Worker | a release / `deploy-*` tag → GitHub Actions |

(There's no "local event server + Fly" cell: a hosted instance is dark and reaches
*out*, so it needs an internet-reachable event server — a Worker, not loopback.)

### 7.1. Run it on your machine (start here)
```
uv tool install bobi
bobi setup                  # design + install a team in a browser UI…
#   …or grab a bundled one:   bobi agents install eng-team
bobi agent <name> start                  # runs your agent — and a local event server
                                 # (loopback) by default. No cloud, no accounts.
```
The only credential you need is your Anthropic auth (`ANTHROPIC_API_KEY`, or a
Claude subscription) — `bobi agents install` prompts for whatever a team requires.
Talk to it with `bobi agent <name> ask "…"` / `bobi agent <name> message`; add `monitors` for
scheduled reactions. (Inbound webhooks from GitHub/Slack need a public URL — host
it on Fly for that, or front the local server with a tunnel.)

### 7.2. Host it on Fly (always-on)
For 24/7 operation off your machine. Still just the binary — `bobi deploy`
builds the instance image from PyPI, so no checkout is needed. The image pins
**the same bobi version you're running**, so run a *released* version
(`uv tool install bobi` — the normal case): the instance image and the CLI
that deployed it match. (Deploying from an unreleased dev checkout pins the last
*published* version, which can lag the entrypoint and crash-loop the instance —
release first.) A hosted instance is **dark** (reaches out over WSS), so its
event server is a **Cloudflare Worker**:
the built-in shared moda Worker (set nothing), your own (`cd event-server && npx
wrangler deploy` → set `event_server:`), or any reachable `https://` server.

**First time on Fly?** `bobi deploy` preflights your setup and prints exactly
what to do — install `flyctl`, `fly auth signup`/`login`, and (for a new org)
the one-time `fly.io/high-risk-unlock`. The guidance is step-by-step, so a human
*or* an agent can get from zero to a deployable account.

**A — Self-service (one developer).** From your laptop:
```
printf 'ANTHROPIC_API_KEY=sk-ant-…\n' > ./my-team.env
bobi deploy my-team --team my-team --env-file ./my-team.env   # ssh-push
bobi destroy my-team                                          # tear down
```
`--team` ssh-pushes your **local** team (no hosting to set up); edit + re-run to
update in place. Or commit a `deployments/my-team.yaml` (`team: my-team`,
`secrets.env-file: ./my-team.env`) and just `bobi deploy my-team`. Prefer a
published tarball? Use `team-url:` instead.

**B — CI (GitHub Actions, always-fresh).** Cut a release (or push a `deploy-*`
tag) and the Action deploys every active deployment.

> **One command:** from your agent-teams repo root, `bobi deploy-init`
> scaffolds all of this — it writes the standalone `deploy-agent-teams.yml`
> (already PyPI-pinned to your installed bobi, with the inline-orphans
> adaptation) + a `deployments/` skeleton for every team under `agents/`, then
> **prints the exact `fly`/`gh` commands** for steps 3–4 below, with each team's
> per-key secret list derived from its declared `${VAR}`s. `--fleet/--tenant/
> --auth/--event-server` set the defaults; it's non-destructive (`--force` to
> overwrite). The manual steps below are what it automates.

Wire your repo once (or let `deploy-init` do 1–2 and print 3–4):
1. Generate `.github/workflows/deploy-agent-teams.yml` + `deployments/`; set
   `fleet:` + `event_server:` in `defaults.yaml`. Do **not** copy `release.yml`
   — that's the framework's own wheel-publish pipeline; you adopt new framework
   versions by bumping the pin in the generated workflow.
2. `deployments/<team>.yaml` with `team:` (local package → **ssh-push**) **or**
   `team-url:` (published `.tar.gz` → **HTTPS-fetch**). Both work in CI: an
   **org-scoped** Fly token (`fly tokens create org`) *can* `fly ssh`, so ssh-push
   reconciles in place from the Action — proven by moda-agents updating
   eng-team (`updating instance 'moda-eng-team' in place (ssh-push)`). Reach for
   `team-url` when you'd rather not give CI ssh, or to provision a dark instance
   with no SSH at all. Set `tenant:` (or inherit `defaults.yaml`).
3. Repo secret `FLY_API_TOKEN` = `fly tokens create org -o <your-org>` — a standing
   production credential (long-lived, rotate periodically).
4. A GitHub Environment named after the **tenant** (e.g. `modalabs`), holding this
   team's **per-key** secrets named `<TEAM>__<KEY>` — e.g. `MY_TEAM__SLACK_BOT_TOKEN`
   (`<TEAM>` = the deployment name slug-normalized: lowercase+hyphen → upper+
   underscore). Editable/diffable per key in the UI; the engine reconciles them to
   the team's declared `agent.yaml` set (§5). **No** required-reviewer rule.

No `FLEET_PREFIX` var, no manifest, no database — the Fly API is the state store.

**Bring your own repo (teams developed *independently* of the framework).** A
team is pure config (role prompts, workflows, monitors, `agent.yaml`) with **zero
framework imports**, and `bobi deploy` has a **binary mode**: outside a
bobi checkout it falls back to the deploy assets bundled in the wheel
(`bobi/_deploy`: a PyPI `Dockerfile` + provision/destroy/fleet scripts), so
`pip install bobi==<pin>` is fully self-sufficient. That means your teams can
live in their **own private repo** that never carries framework source — the
"outside user runs their own teams on Fly" shape. The reference example is
**`moda-labs/moda-agents`** (it owns the `moda` fleet; this framework repo
keeps only its `ci` self-gate canary). The split model:

- **One Fly org, two (or more) fleets.** Fleets are distinguished by the exact
  `BOBI_FLEET` stamp (§4), so repos that share an org never cross-enumerate.
- **Adopt vs. fresh.** Keep the same `fleet:` as the live app to **adopt** it at
  cutover (idempotent reconcile, no data migration — volume/login/identity
  preserved); pick a new `fleet:` to provision fresh.
- **Prune-safety at cutover.** The reconcile sets the supplied secrets and
  **prunes any live secret not in the declared set** (§5). Before the first reconcile
  of an existing app, populate the tenant Environment with **every** declared key
  (source values from the live app — `fly ssh … printenv <KEY>` — so the digests
  don't change and nothing is pruned), and confirm the live set == declared set.
- **Framework upgrades** are a version-pin bump in the team repo (rebuilds the
  team image from the newly-pinned wheel on the next deploy), decoupled from the
  framework's own release cadence.

### Manual ops
```
bobi deploy <name> [--env-file ./x.env]        # provision or update (idempotent)
bobi destroy <name> [--yes]                     # tear down (removes volume!)
scripts/fleet.sh list <fleet>                        # what's running
fly logs -a <app> ; fly status -a <app>              # observe
fly ssh console -a <app> --command "gosu bobi env HOME=/home/bobi BOBI_HOME=/data/bobi CLAUDE_CONFIG_DIR=/data/claude bobi agent <name> status"
```
Both GitOps workflows also accept `workflow_dispatch` for manual re-runs.

**Troubleshooting:**
- *Crash-loop, "No such file or directory (os error 2)"* → a `WORKDIR`/path under
  the volume mount, or a zstd/Depot image. See §2.1, §2.3.
- *Deploy fails on volumes* → missing `--ha=false` (§2.4).
- *Two new teams, one fails with `docker.sock missing hostname`* → concurrent
  builds racing the shared builder; serialize (§2.9).
- *Changed `team-url` team didn't update* → confirm the in-place path used
  `install <url>`, not `agents update`, and that `teams-latest` republished (§6).
- *ssh-push instance stuck "waiting for a pushed team"* → the blank provision
  succeeded but the push didn't land `run/package/agent.yaml`; check `fly logs` and
  re-run `bobi deploy <name>` (idempotent — it re-pushes).
- *Instance boots but agents won't dispatch* → a team with a `requires:` gate whose
  tools aren't in the image (e.g. eng-team's gstack/codex). Declare the tools in
  the team's `build:` block (§2.6).

---

## 7.3. Many teams on one workspace / org — event routing

Several team instances can share **one Slack workspace + one GitHub org** without
triaging each other's events. Routing is **targeted, not broadcast-and-filter**:
each instance subscribes to resource topics and the event server delivers an
event only to subscribers of the topics it carries (`events/subscriptions.py`
builds the keys; the Worker matches them in `subscriptionKeysForEvent` / `deliver`).

**The contract — scope each team:**
- **Slack:** the detector resolves the bot's `api_app_id` and subscribes to
  app-qualified topics (`slack:<TEAM>:app:<APP>`). Set `channels:` on the
  team's slack service (`agent.yaml`) when a bot should only handle specific
  channels; the detector then subscribes per app+channel
  (`slack:<TEAM>:app:<APP>:<CHANNEL>`). IDs (`C0ABC123`) or names (`#support`)
  both work (names resolve via the Slack API). If the app id cannot be resolved,
  the detector falls back to legacy workspace/channel keys for single-bot
  compatibility.
- **GitHub:** already per-repo (`github:<org>/<repo>`), auto-detected from each
  repo's remote — a director watching a parent dir detects each child repo. An
  org webhook fans out only to the repo's subscriber, never the whole org.
- **DMs** are app-scoped, not channel-scoped: a DM event carries
  `api_app_id`, so it routes to `slack:<TEAM>:app:<APP>`. That keeps Bobbers,
  eng-team, and other bots in the same Slack workspace from receiving each
  other's DMs.

**Isolation proof:** end-to-end no-cross-delivery tests in
`event-server/test/index.spec.ts` (two deployments, disjoint channels/repos →
each event reaches exactly its subscriber, an unscoped channel/repo reaches
nobody) plus the key-building tests in `tests/test_adapters.py`. The live
two-instance `events.jsonl` check is the final acceptance.

**Scope vs. tenancy:** this is channel/repo *delivery scoping* within one trust
domain. Webhook topics are still **global across bubbles** in v1 (an accepted
cross-tenant read hole) — true multi-tenant isolation (bind inbound webhooks to a
bubble/account) is #239 (auth-v2), part of the multitenant phase, not this.

---

## 8. Testing

Image contract + live round-trip: `tests/integration/test_container_image.py`
(non-root, no Node, fastembed cache path, auth guards; the live `ask` is skipped
unless `ANTHROPIC_API_KEY` is set). Manual acceptance smoke:

```bash
docker run -d --name smoke -v "$(mktemp -d):/data" \
  -e BOBI_AUTH=api_key -e ANTHROPIC_API_KEY=sk-ant-... \
  -e BOBI_TEAM=eng-team -e BOBI_EVENT_SERVER=https://... \
  bobi:dev
# wait for healthy, then:
docker exec smoke bobi agent <name> ask "Reply with: pong"
```

Smoke targets: `tests/fixtures/claude-smoke` and `tests/fixtures/codex-smoke`
(zero-secret; each needs only `BOBI_EVENT_SERVER` + its brain's key for the `ask`
round-trip - an Anthropic key for Claude, an OpenAI key for Codex).
Structural/unit coverage: `tests/test_gitops_c22.py`. Both GitOps workflows pass
`actionlint` (+ shellcheck on run blocks).

> flyctl gotcha: `fly machine restart -a <app>` errors "a machine ID must be
> specified" outside a TTY; `bobi deploy` resolves IDs via
> `fly machine list --json` and restarts each by ID.
