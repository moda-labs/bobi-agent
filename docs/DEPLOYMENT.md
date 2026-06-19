# Deployment — how it actually works (Fly build + GitOps)

How a modastack agent team becomes a running, self-managing instance on Fly,
and the hard-won nuances that make it work. This is the **operational** companion
to the design docs: `docs/design/CONTAINERIZED_INSTANCES.md` is *why/what*,
`docs/design/DEPLOY_INTERFACE.md` is the *deploy-primitive design*, and this is
*how it works today + the gotchas*. Current as of the `modastack deploy` refactor
(2026-06-19).

Pieces:

- `modastack deploy <name>` / `destroy <name>` (`modastack/deploy.py`) — **the
  one-instance primitive**; everything else is mechanics it drives or
  orchestration that calls it
- `deployments/<name>.yaml` (+ `defaults.yaml`) — per-instance operator config
- `Dockerfile` — the image
- `scripts/provision-instance.sh` + `scripts/destroy-instance.sh` — stand up / tear down one instance
- `scripts/fleet.sh` — fleet enumeration helper
- `scripts/build-team-tarballs.sh` — package teams into `.tar.gz`
- `.github/workflows/gitops-teams.yml` — thin client: reconcile `deployments/*` on release/tag
- `.github/workflows/gitops-release.yml` — fleet image rollout on release
- `.github/workflows/team-packages.yml` — publishes team tarballs (for `team-url` delivery)

---

## 1. Mental model

**Git is what should be running; Fly is what is running; `modastack deploy` closes
the gap, one instance at a time.** A `deployments/<name>.yaml` is one instance an
operator runs. Add/edit one and run `modastack deploy <name>` (or let the GitOps
Action do it on the next release) → the instance appears or updates in place.
Delete one → its Fly app is surfaced for a human `modastack destroy`. There is
**no database and no manifest** — the Fly API is the only state store.

The layering that keeps this operator-agnostic:

```
orchestration (per operator) : GitHub Action │ Terraform │ SaaS plane │ a for-loop
the primitive (modastack)    : modastack deploy <name> / destroy <name>   ← ONE instance
mechanics                    : provision-instance.sh · fleet.sh · install · fly
```

`modastack deploy` is **idempotent** — no Fly app yet ⇒ provision, app exists ⇒
update — so the caller never decides provision-vs-update; it just names the
instance. Anything that loops or diffs across instances is orchestration on top.

An **instance** = one Fly app + one persistent volume (mounted at `/data`, holding
both the project and `$HOME`) + env vars + an **outbound-only** WebSocket to one
event server (a Cloudflare Worker). Nothing reaches in; it reaches out. All
instances share **one image**; identity lives entirely in the volume + env.

---

## 2. The image (Fly build)

`Dockerfile` at the repo root, two-stage:
- **builder**: build the `modastack` wheel; install into a venv.
- **runtime**: slim Debian + the venv, the native pinned `claude` CLI (no Node),
  `git`/`curl`/`gosu`, the fastembed embedding model **baked at build time**, and
  `docker-entrypoint.sh`. Runs as non-root uid 10001 (`modastack`).

First boot (the entrypoint): create the volume layout, install the team
(`MODASTACK_TEAM_URL` or `MODASTACK_TEAM`), then `exec gosu` to the `modastack`
user running `modastack start --foreground` (PID 1). The instance **self-mints its
event-bus bubble and self-registers every session** (#240) — the provisioner never
touches `deployment_id`/`api_key`.

With **neither** team var set on an empty volume the entrypoint enters a
**wait-for-team** state: it polls for `.modastack/agent.yaml` instead of crashing.
That is the ssh-push hook — the instance boots blank and holds while
`modastack deploy` pushes a local team onto the volume (next section); the moment
the team lands, it proceeds to start. (This is the C9-adjacent first-boot change.)

### Fly build gotchas (each one cost real debugging — do not regress)

1. **`WORKDIR` must NOT be under the volume mount.** The volume mounts at `/data`
   and **shadows** anything beneath it, so `WORKDIR /data/project` makes the
   container's cwd not exist at runtime → Fly's init can't `exec` *any* binary
   (your entrypoint *and* Fly's own `hallpass`) → `No such file or directory (os
   error 2)`, crash-loop to max-restart 10. Fix: **`WORKDIR /`**; the entrypoint
   `cd`s into `${MODASTACK_PROJECT}` itself. *This was THE on-Fly boot bug.*
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
6. **`--wait-timeout 10m`.** First boot installs the team and warms the model past
   the default 5-minute machine-state wait.
7. **`[[mounts]]` array form** in the generated config (canonical).
8. **`fly ssh` admin lands in `/`**, but `modastack` finds its project by walking
   up from cwd — so admin commands must `cd` first, as the volume's uid-10001
   owner:
   ```
   fly ssh console -a <app> --command \
     'gosu modastack env HOME=/data/home bash -c "cd /data/project && modastack <cmd>"'
   ```
9. **Concurrent `fly deploy --remote-only` builds race on the org's single shared
   remote builder** (`failed to parse daemon host "unix:///var/run/docker.sock":
   missing hostname`). One build grabs the builder; the other dies. **Serialize
   provisions** (or, post-C24, deploy prebuilt images by ref so the builder leaves
   the path entirely). Found in the C22 live e2e.

---

## 2.5. The primitive (`modastack deploy <name>`)

`modastack deploy <name>` resolves one instance's config, validates its secrets,
stamps identity, picks a delivery mode, and applies — idempotently. It is the
single entry point the CLI, CI, and any future control plane share.

**Config precedence** (merged by the command itself, so it works standalone):

```
CLI flags  ›  deployments/<name>.yaml  ›  deployments/defaults.yaml  ›  built-ins
```

- `deployments/<name>.yaml` = one instance (name = filename). `defaults.yaml` =
  shared operator *values* (fleet, event server, region) — **not** a deploy list;
  the deploy list is the set of `deployments/*.yaml` files.
- App name = `<fleet>-<name>`; stamps `MODASTACK_FLEET` + `MODASTACK_INSTANCE`
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
`modastack install <tarball> --non-interactive` as the volume owner (reads secrets
from the Fly-injected env, fails loudly on a gap). On a **new** instance this
releases the wait-for-team loop (no restart); on an **existing** one it's a
workspace-safe reinstall + `fly machine restart` to reload.

**Secrets** come from `secrets.env-file:` (a local path) or the process env (the
CI seam — the Action exports the team's GitHub-Environment blob and runs
`modastack deploy`). For a local team the required `${VAR}`s are validated up
front; `MODASTACK_*` refs are identity (stamped from flags), never demanded as
secrets.

`modastack destroy <name>` resolves `<name>` → `<fleet>-<name>` and runs
`destroy-instance.sh` (Fly app + volume, typed-confirm; `--yes` for automation).

---

## 3. The provisioner (`scripts/provision-instance.sh`)

Stands up one instance: `fly apps create` → 15 GB volume → stage secrets →
generate a per-app `fly.toml` (identity in `[env]`, 4 GB/shared-2x, **no
`[http_service]`** = dark/always-on) → `fly deploy`. **Idempotent** — re-running
redeploys, so it doubles as "redeploy this instance."

Most operators drive this through `modastack deploy` (§2.5), which fills these
flags from `deployments/<name>.yaml`. Key flags:
- Exactly one of `--team <name>` (bundled/registry), `--team-url <.tar.gz URL>`
  (the dark-instance injection seam — pulled at first boot), or `--blank` (no team
  source: boot into the wait-for-team state for ssh-push delivery).
- `--env-file` — KEY=VALUE; `MODASTACK_*` keys become plaintext `[env]` identity,
  **everything else becomes a Fly secret**. This routing is what lets one blob
  carry both (see §5).
- `--fleet <prefix>` — stamps `MODASTACK_FLEET` into `[env]` (see §4). Defaults to
  the app name's leading dash-segment.
- `--instance <name>` — stamps `MODASTACK_INSTANCE` (the per-instance/SaaS-tenant
  key, enumerable next to the fleet). Defaults to the app name minus `<fleet>-`.
- `--event-server <https URL>` — defaults to the shared moda Worker; the bubble key
  is refused over cleartext remote URLs, so it must be `https://` (or loopback).
- `--auth api_key|subscription` — api_key **requires** `ANTHROPIC_API_KEY` in the
  env-file; subscription **forbids** it (the key silently outranks subscription
  OAuth and bills the API).

What it deliberately does **not** do: pre-register a deployment, or write the
volume's `agent.yaml`. After first boot the volume config is the source of truth;
a reprovision sets only env + secrets, never project files.

`MODASTACK_EVENT_SERVER` is the var name (an `https://` value; the client derives
`wss://`). Tear down with `destroy-instance.sh --app <app>` — **removes the volume**
(the only copy of state); human-only, never automated.

> Fly account note: a new personal org may be flagged high-risk; clear it at
> `fly.io/high-risk-unlock` (card verify) before `fly apps create`.

---

## 4. Fleet identity & enumeration (`scripts/fleet.sh`)

The Fly API is the state store. A **fleet** is the set of instances sharing one
operator namespace, stamped `MODASTACK_FLEET=<prefix>` in each app's `[env]`.

- **App name = `<prefix>-<team>`** — a deterministic *discovery hint*.
- **The `MODASTACK_FLEET` stamp is the authoritative membership key** (name is only
  a hint). This is the SaaS-extensible primitive: two fleets can share one Fly org,
  and a future `MODASTACK_TENANT` filter slots into the same query.

`fleet.sh` (sourceable lib + CLI):
- `fleet.sh app <prefix> <team>` → `<prefix>-<team>`.
- `fleet.sh list <prefix>` → member app names. Candidates from a single
  `fly apps list --json` name-prefix filter, **each confirmed by its stamp** (so an
  unrelated `<prefix>-website` can't sneak in).
- `fleet.sh classify <prefix> <team>…` → `added=[…]` / `changed=[…]`, partitioned by
  whether `<prefix>-<team>` exists on Fly (added = provision, changed = update).
- `fleet.sh fleet-of <app>` → the app's stamp.

> flyctl gotcha: `fly config show -a <app>` outputs **JSON by default** — passing
> `--json` errors ("unknown flag"). `fleet.sh` reads `.env.MODASTACK_FLEET` from it.

---

## 5. Secrets model

One **GitHub Environment per deployment**, holding a single secret `MODASTACK_ENV`
= the instance's entire KEY=VALUE env-file body. The deploy job binds
`environment: ${{ matrix.name }}`, writes `$MODASTACK_ENV` to a temp file under
`umask 077`, and hands it to `modastack deploy … --env-file`. `deploy`'s secret
resolution + the provisioner's routing (`MODASTACK_*` → `[env]`, rest → Fly
secrets) mean the single blob loses no expressiveness, and it is the exact seam a
token broker fills later (it emits the same blob).

- Use a **secret**, not a variable (masking). Never `echo`/`cat` it — `printf` to
  disk under `umask 077`.
- Deployment Environments must have **no required-reviewer protection rule** — it
  would pause the deploy matrix waiting on a human.
- Self-service (no CI) points `secrets.env-file:` at a local file instead.
- Fleet config now lives in **`deployments/defaults.yaml`** (`fleet:`,
  `event_server:`, sizing), not repo variables — a single source of truth shared by
  both workflows. The only repo secret is `secrets.FLY_API_TOKEN` (an org-scoped
  Fly deploy token: `fly tokens create deploy`); absent it, the workflows no-op.

---

## 6. GitHub Ops (thin clients over the primitive)

```
publish a GitHub Release   (or push a `deploy-*` tag / dispatch — the deploy gate)
   │
   ├─▶ gitops-teams.yml:
   │      plan   : list ACTIVE deployments/<name>.yaml (defaults excluded)
   │      deploy : matrix over names, environment=<name>
   │               └─ materialize MODASTACK_ENV -> env-file -> `modastack deploy <name>`
   │      orphans: Fly apps with no deployments/ file -> warn (human `modastack destroy`)
   │
   └─▶ gitops-release.yml:
          build image once -> roll every fleet app to that digest (config preserved)
```

**A release is the deploy gate** — an edit pushed to `main` does NOT auto-deploy;
you cut a release (or push a `deploy-*` tag) to ship. The reconcile **business
logic lives in `modastack deploy`**, not the YAML. The Action only orchestrates:
list the active deployments, hand each its secrets, loop the primitive. That is why
the same engine runs from a laptop, this Action, Terraform, or a SaaS plane — see
§7.1 (bring your own repo).

### gitops-teams.yml — the reconcile
Triggered by **`release: published`**, a **`deploy-*` tag push** (manual deploy
without a formal release — also how the GitOps path is e2e'd from a branch, since
tag pushes run the workflow from the tagged commit), or **`workflow_dispatch`**
(optional `only:` to scope to one deployment). Jobs:
- **plan**: list every **active** `deployments/<name>.yaml` (`defaults.yaml`
  excluded; an inactive deployment is a non-`.yaml` like `<name>.yaml.example`).
  No git-diff — a release reconciles the whole set, and `modastack deploy` is
  idempotent. Gates the rest on `secrets.FLY_API_TOKEN` being set.
- **deploy** (matrix over the active names, `environment: <name>`): install the
  CLI, materialize `MODASTACK_ENV` → env-file, then `modastack deploy <name>
  --env-file …`. One idempotent path — `deploy` itself decides provision-vs-update
  by Fly state.
- **orphans**: enumerate the fleet (`fleet.sh list`, fleet from `defaults.yaml`),
  warn on any app with no `deployments/` file (including a removed/inactivated
  deployment). **Never auto-destroys** (the volume is the only copy of state).

> **Delivery in CI.** moda's internal `deployments/eng-team.yaml` uses
> **`team-url` (HTTPS-fetch) is the CI delivery mode** — a CI Fly token deploys but
> doesn't `fly ssh`, so CI uses the published-tarball path (`team-packages.yml`
> publishes them). **ssh-push (`team:`) is the logged-in-dev path** (`fly ssh`
> needs your full creds). `deployments/ci-smoke.yaml` exercises the CI path via
> the published `smoke-team.tar.gz`.

### team-packages.yml (only for `team-url` delivery)
On push to main (path-filtered to `agents/**` + the smoke fixture), builds each
team into `<team>.tar.gz` and **publishes to a rolling `teams-latest` GitHub
Release** → stable public URL
`https://github.com/<owner>/<repo>/releases/download/teams-latest/<team>.tar.gz`.
Sole publisher of that release; nothing else should `--clobber` it. Only needed
when a deployment uses `team-url:`; pure ssh-push (`team:`) deployments ignore it.

### gitops-release.yml — fleet rollout
Triggered by **`release: published`** (independent of PyPI — the Fly image builds
from source). Build the image **once** against the first fleet app, resolve the
image it now runs, and reuse that exact reference for every other app
(build-once-deploy-many; all instances share one image). Each app keeps its
volume/sessions/env: round-trip the live config with `fly config save` and only
swap the image. Per-app failures are isolated and reported; re-run to retry
(idempotent; C7 guards format-version skew).

> **flyctl gotchas (found in the C22 e2e):**
> - `fly config save` writes via **`-c <path>`**, not `-o` (which it rejects).
> - `fly image show -a <app> --json` returns `Ref`/`Reference`/`FullImageRef` as
>   **null** — construct the pull ref yourself:
>   `registry.fly.io/<Repository>@<Digest>`.
> - Deploying app B with app A's `registry.fly.io/<A>@<digest>` works (org-scoped
>   registry) — that's how one build rolls the whole fleet.

---

## 7. Operator runbook

### Self-service (one developer, no CI)
From a modastack checkout, with your team under `agents/<team>/`:
```
# 1. point a local env-file at your secrets (ANTHROPIC_API_KEY + service tokens)
printf 'ANTHROPIC_API_KEY=sk-ant-…\nSLACK_BOT_TOKEN=xoxb-…\n' > ./my-team.env

# 2. deploy — ssh-push builds your local team and ships it; no hosting needed
modastack deploy my-team --team my-team --env-file ./my-team.env

# 3. iterate: edit agents/my-team/…, redeploy (in-place, workspace-safe)
modastack deploy my-team --team my-team --env-file ./my-team.env

# 4. tear down (removes the volume!)
modastack destroy my-team
```
Or commit a `deployments/my-team.yaml` (`team: my-team`, `secrets.env-file:
./my-team.env`) and just run `modastack deploy my-team`.

### 7.1. Bring your own repo (CI for your own teams)
The two workflows are operator-agnostic — wire your repo in four steps:
1. **Copy** `.github/workflows/gitops-teams.yml` (+ `gitops-release.yml` if you
   want release rollouts) and `deployments/defaults.yaml` into your repo. Set
   `fleet:` (your namespace) and `event_server:` in `defaults.yaml`.
2. **Add a deployment**: `deployments/<team>.yaml` with `team-url: <published
   .tar.gz>` (the CI delivery mode — `team-packages.yml` publishes these). One file
   per instance. (`team:` ssh-push is for logged-in-dev `modastack deploy`, not CI —
   a CI Fly token can't `fly ssh`.)
3. **Repo secret**: `secrets.FLY_API_TOKEN` = `fly tokens create org -o <your-org>`
   (org-scoped so CI can create apps/volumes). It's a standing production
   credential — long-lived, rotate periodically (re-mint + re-set).
4. **Per-deployment GitHub Environment** named `<team>`, one secret `MODASTACK_ENV`
   = the full KEY=VALUE env-file body. **No** required-reviewer rule.

**Cut a release** (or push a `deploy-*` tag) → `gitops-teams.yml` runs
`modastack deploy <team>` for every active deployment. That's it; no FLEET_PREFIX
var, no manifest, no database.

### Manual ops
```
modastack deploy <name> [--env-file ./x.env]        # provision or update (idempotent)
modastack destroy <name> [--yes]                     # tear down (removes volume!)
scripts/fleet.sh list <fleet>                        # what's running
fly logs -a <app> ; fly status -a <app>              # observe
fly ssh console -a <app> --command 'gosu modastack env HOME=/data/home \
  bash -c "cd /data/project && modastack status"'    # admin
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
  succeeded but the push didn't land `.modastack/agent.yaml`; check `fly logs` and
  re-run `modastack deploy <name>` (idempotent — it re-pushes).
- *Instance boots but agents won't dispatch* → a team with a `requires:` gate whose
  tools aren't in the image (e.g. eng-team's gstack/codex). That's the C24 gap —
  see `docs/design/CUSTOM_AGENT_DEPS.md`.

---

## 8. What's verified

C10 + C22 were verified **live on Fly**, then torn down:
- Single instance: empty volume → image build → boot → first-boot team install from
  URL → healthy manager → `modastack ask` self-registers on the real Worker →
  `pong`. (The `team-url` delivery path, unchanged by the deploy refactor except
  the added `MODASTACK_INSTANCE` stamp.)
- Two-instance fleet (C22): provision both, `fleet.sh list`/`classify` against real
  Fly state, changed-team update (`install <url>` + restart — role content updated,
  workspace file preserved), release rollout (cross-app image pull, config
  preserved, `pong`).

The deploy refactor adds the **ssh-push** path, **verified live on Fly**
(`modastack deploy sshe2e --team smoke-team`, then torn down):
- Blank provision (app + 15 GB volume + staged secret); `fly deploy` returns on the
  blank machine reaching **started** — it does *not* hang on the image healthcheck.
- `MODASTACK_INSTANCE` confirmed in the live `[env]` (next to `MODASTACK_FLEET`).
- base64 push over `fly ssh` → `modastack install /data/…tar.gz --non-interactive`
  (secrets read from the Fly-injected env — confirmed available in the ssh session)
  → the wait-for-team entrypoint detects the team and starts the manager (`status`
  shows it running).
- In-place re-deploy: re-push → workspace-safe reinstall (a role marker propagated)
  → `fly machine restart <id>` → manager back up.
- `modastack destroy sshe2e --yes` removes the app + volume.

> **flyctl gotcha (found in this e2e):** `fly machine restart -a <app>` errors
> "a machine ID must be specified" outside a TTY. `deploy` resolves IDs via
> `fly machine list --json` and restarts each by ID.

The **team-url** path is unchanged from C22 (live-verified there) except the added
`MODASTACK_INSTANCE` stamp; it is covered here by the engine unit tests pending a
fresh live run. <!-- e2e-status: ssh-push verified 2026-06-19 -->

Smoke target: `tests/fixtures/smoke-team` (zero-secret; only needs
`MODASTACK_EVENT_SERVER` + an Anthropic key for the `ask` round-trip).
Structural/unit coverage: `tests/test_gitops_c22.py`. Both workflows pass
`actionlint` (+ shellcheck on run blocks).
