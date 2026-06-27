# Deployment — how it actually works (Fly build + GitOps)

How a bobi agent team becomes a running, self-managing instance on Fly,
and the hard-won nuances that make it work. This is the **operational** companion
to the design docs: `docs/design/CONTAINERIZED_INSTANCES.md` is *why/what*,
`docs/design/DEPLOY_INTERFACE.md` is the *deploy-primitive design*, and this is
*how it works today + the gotchas*. Current as of the `bobi deploy`
primitive + binary-only deploy, **merged to main 2026-06-19** (PR #365). Next:
layered-deps (C24 #368) → eng-team on Fly → EC2 decommission
(`HANDOFF-layered-deps-eng-team-fly.md`).

Pieces:

- `bobi deploy <name>` / `destroy <name>` (`bobi/deploy.py`) — **the
  one-instance primitive**; everything else is mechanics it drives or
  orchestration that calls it
- `deployments/<name>.yaml` (+ `defaults.yaml`) — per-instance operator config
- `Dockerfile` — the image
- `scripts/provision-instance.sh` + `scripts/destroy-instance.sh` — stand up / tear down one instance
- `scripts/fleet.sh` — fleet enumeration helper
- `scripts/build-team-tarballs.sh` — package teams into `.tar.gz`
- `.github/workflows/release.yml` — the single gated release pipeline: build + roll fleet images, then deploy teams
- `.github/workflows/deploy-agent-teams.yml` — reconcile `deployments/*` (called by `release.yml`; standalone on a `deploy-*` tag)
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
event server (a Cloudflare Worker). Nothing reaches in; it reaches out. All
instances share **one image**; identity lives entirely in the volume + env.

---

## 2. The image (Fly build)

`Dockerfile` at the repo root, two-stage:
- **builder**: build the `bobi` wheel; install into a venv.
- **runtime**: slim Debian + the venv, the native pinned `claude` CLI (no Node),
  `git`/`curl`/`gosu`, the fastembed embedding model **baked at build time**, and
  `docker-entrypoint.sh`. Runs as non-root uid 10001 (`bobi`).

First boot (the entrypoint): create the volume layout, install the team
(`BOBI_TEAM_URL` or `BOBI_TEAM`), then `exec gosu` to the `bobi`
user running `bobi agent <name> start --foreground` (PID 1). The instance **self-mints its
event-bus bubble and self-registers every session** (#240) — the provisioner never
touches `deployment_id`/`api_key`.

With **neither** team var set on an empty volume the entrypoint enters a
**wait-for-team** state: it polls for `run/package/agent.yaml` instead of crashing.
That is the ssh-push hook — the instance boots blank and holds while
`bobi deploy` pushes a local team onto the volume (next section); the moment
the team lands, it proceeds to start. (This is the C9-adjacent first-boot change.)

### Fly build gotchas (each one cost real debugging — do not regress)

1. **`WORKDIR` must NOT be under the volume mount.** The volume mounts at `/data`
   and **shadows** anything beneath it, so `WORKDIR /data/project` makes the
   container's cwd not exist at runtime → Fly's init can't `exec` *any* binary
   (your entrypoint *and* Fly's own `hallpass`) → `No such file or directory (os
   error 2)`, crash-loop to max-restart 10. Fix: **`WORKDIR /`**; the entrypoint
   `cd`s into `${BOBI_PROJECT}` itself. *This was THE on-Fly boot bug.*
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
   provisions** (or, post-C24, deploy prebuilt images by ref so the builder leaves
   the path entirely). Found in the C22 live e2e.

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

## 2.6. Team-flavored images — baked host tools (C24)

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

## 5. Secrets model (#385)

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

### 5.1. Migrating an Environment from the old blob (runbook)

Pre-#385 Environments held a single opaque `BOBI_ENV` blob. To migrate one
deployment to per-key (non-destructive — do this *before* deleting the blob):

```bash
ENV=modalabs                 # the tenant Environment
TEAM=ENG_TEAM                # deployment name, slug-normalized (eng-team → ENG_TEAM)
APP=moda-eng-team            # the live Fly app (source of current values)

# Add per-key secrets, sourced from the live Fly app (values never printed):
for k in SLACK_BOT_TOKEN LINEAR_API_KEY OPENAI_API_KEY GH_TOKEN SLACK_CHANNELS; do
  v=$(fly ssh console -a "$APP" -C "printenv $k" | tr -d '\r\n')
  [ -n "$v" ] && printf '%s' "$v" | gh secret set "${TEAM}__${k}" --env "$ENV" -R <owner>/<repo>
done
```

The per-key secrets and the old `BOBI_ENV` blob **coexist safely** — the
pre-#385 workflow reads the blob, the new one reads per-key. **At cutover** (after
the per-key workflow has merged and a deploy is verified green), delete the blob:

```bash
gh secret delete BOBI_ENV --env "$ENV" -R <owner>/<repo>
```

A subscription team (e.g. eng-team) has **no** `ANTHROPIC_API_KEY` — don't migrate
one; the reconcile prunes a stray live key. An old per-deployment Environment (e.g.
`eng-team`) becomes vestigial once its keys live in the tenant Environment.

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
   │                                 build canary image FROM the wheel + `ask`
   │                                 it -> assert CANARY-OK end-to-end
   │                                 ├──────────────┐
   │                              publish        roll-fleet  (parallel; both need canary)
   │                              same wheel      reuse canary digest (generic);
   │                              -> PyPI (+      team-flavored rebuild own image
   │                              event-server,   from the wheel + TEAM_DEPS
   │                              Homebrew)            │
   │                                              deploy-teams   (packages/secrets last)
   │                                                └─ uses: deploy-agent-teams.yml
   │
push a `deploy-*` tag / dispatch ─▶ deploy-agent-teams.yml   (standalone, NO image roll)
          plan   : list ACTIVE deployments/<name>.yaml (defaults excluded) + tenant
          deploy : matrix over {name,tenant}, environment=<tenant>
                   └─ toJSON(secrets) | filter <TEAM>__ -> env-file -> `bobi deploy <name>`
          orphans: Fly apps with no deployments/ file -> warn (human `bobi destroy`)
```

**A release is the deploy gate** — an edit pushed to `main` does NOT auto-deploy;
you cut a release (or push a `deploy-*` tag) to ship. **One functional gate guards
everything**: `release.yml` builds the wheel once, builds the canary *from that
wheel* and smokes it (`CANARY-OK`), and only then — in parallel — publishes the
same wheel to PyPI and rolls the fleet, finally reconciling each deployment's
package content + secrets (`deploy-teams`). Publish is gated on the **canary**, not
the fleet roll, so a flaky non-canary instance can't block an already-proven
publish. A team-definition or secret edit that needs **no** image rebuild ships on
its own via a `deploy-*` tag, which runs `deploy-agent-teams.yml` standalone. Either
way the reconcile **business logic lives in `bobi deploy`**, not the YAML — the
Action only orchestrates: list
the active deployments, hand each its secrets, loop the primitive. That is why the
same engine runs from a laptop, this Action, Terraform, or a SaaS plane — see §7.2
B (*Bring your own repo*).

### deploy-agent-teams.yml — the reconcile
Invoked by **`release.yml` via `workflow_call`** (its final step, after the image
roll — `secrets: inherit` forwards the Fly token + per-tenant Environment secrets,
and `ref` pins it to the released commit), or run standalone by a **`deploy-*` tag
push** (an image-free team/secret update — also how the GitOps path is e2e'd from a
branch, since tag pushes run from the tagged commit) or **`workflow_dispatch`**
(optional `only:` to scope to one deployment). Jobs:
- **plan**: list every **active** `deployments/<name>.yaml` (`defaults.yaml`
  excluded; an inactive deployment is a non-`.yaml` like `<name>.yaml.example`).
  No git-diff — a release reconciles the whole set, and `bobi deploy` is
  idempotent. Gates the rest on `secrets.FLY_API_TOKEN` being set.
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
> is how moda-agent-teams reconciles eng-team in place (`updating instance
> 'moda-eng-team' in place (ssh-push)`), pushing the team definition to the volume
> and reloading. **`team-url` (HTTPS-fetch)** is the alternative when you'd rather
> not give CI ssh, or to first-boot a dark instance with no SSH at all
> (`team-packages.yml` publishes the tarballs). `deployments/canary.yaml` (the
> always-on pipeline canary) exercises the `team-url` path via the published
> `smoke-team.tar.gz`.

### team-packages.yml (only for `team-url` delivery)
On push to main (path-filtered to `agents/**` + the smoke fixture), builds each
team into `<team>.tar.gz` and **publishes to a rolling `teams-latest` GitHub
Release** → stable public URL
`https://github.com/<owner>/<repo>/releases/download/teams-latest/<team>.tar.gz`.
Sole publisher of that release; nothing else should `--clobber` it. Only needed
when a deployment uses `team-url:`; pure ssh-push (`team:`) deployments ignore it.

### release.yml — the release pipeline
Triggered by **`release: published`**. One gated pipeline; the canary, running the
exact wheel we publish, is the single functional gate for both PyPI and the fleet:
- **subscription-login-smoke** — gate the release on a verified subscription-login
  bootstrap (a hermetic mock-code smoke; #388).
- **build-wheel** — `python -m build` the wheel/sdist **once** and upload it, so the
  canary, the fleet, and PyPI all run the identical artifact. A fail-fast
  `pip install dist/*.whl && bobi --version` rejects an obviously-broken wheel
  before the expensive canary build.
- **build-canary** — deploy the canary from an image built **from that wheel**
  (`--build-arg BOBI_BUILD=wheel`, the artifact staged into `dist/`), then a
  functional `ask` asserts `CANARY-OK` end-to-end. **This is the gate.** It resolves
  and outputs the built image digest for `roll-fleet` to reuse.
- **publish** — `needs: build-canary` (the canary, **not** the fleet roll). Uploads
  the **same** wheel to PyPI via trusted publishing (`environment: pypi`), then
  `deploy-event-server` + `update-homebrew`.
- **roll-fleet** — `needs: build-canary`, in parallel with publish. Generic
  instances reuse the canary's image digest (build-once-deploy-many); a team-flavored
  app rebuilds its **own** image from the wheel + its TEAM_DEPS hook. Each app keeps
  its volume/sessions/env/secrets (round-trip live config, swap image only). Per-app
  failures are isolated and reported; re-run to retry (idempotent; C7 guards skew).
- **deploy-teams** — `needs: roll-fleet`, then **calls `deploy-agent-teams.yml`** to
  reconcile each deployment's package content + secrets onto the rolled image. Image
  always lands before the package/secret reconcile.

> **PyPI trusted publishing.** The publish step must run in the top-level workflow
> the trusted-publisher config names (PyPI rejects a reusable workflow with
> `invalid-publisher`), so it's a native job in `release.yml`. Configure the PyPI
> trusted publisher as: repo `<owner>/<repo>`, workflow `release.yml`, environment
> `pypi`. (If you migrate from a prior `publish-pypi.yml` publisher, update it
> **before** the next release or the upload fails.)

> **flyctl gotchas (found in the C22 e2e):**
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
1. Copy `.github/workflows/deploy-agent-teams.yml` + `deployments/`; set `fleet:`
   + `event_server:` in `defaults.yaml`. Two one-line adaptations for a repo with
   **no bobi source** (the recommended shape — see *Bring your own repo*
   below): `pip install -e .` → `pip install "bobi==<pin>"` (track a
   *published* framework version), and have the `orphans` job enumerate the fleet
   inline (`fly apps list` + the `BOBI_FLEET` stamp) since `scripts/fleet.sh`
   isn't present. Do **not** copy `release.yml` — that's the framework's own
   wheel-publish pipeline; you adopt new framework versions by bumping the pin.
2. `deployments/<team>.yaml` with `team:` (local package → **ssh-push**) **or**
   `team-url:` (published `.tar.gz` → **HTTPS-fetch**). Both work in CI: an
   **org-scoped** Fly token (`fly tokens create org`) *can* `fly ssh`, so ssh-push
   reconciles in place from the Action — proven by moda-agent-teams updating
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
**`moda-labs/moda-agent-teams`** (it owns the `moda` fleet; this framework repo
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
  tools aren't in the image (e.g. eng-team's gstack/codex). That's the C24 gap —
  see `docs/design/CUSTOM_AGENT_DEPS.md`.

---

## 7.3. Many teams on one workspace / org — event routing (#341)

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

## 8. What's verified

C10 + C22 were verified **live on Fly**, then torn down:
- Single instance: empty volume → image build → boot → first-boot team install from
  URL → healthy manager → `bobi agent <name> ask` self-registers on the real Worker →
  `pong`. (The `team-url` delivery path, unchanged by the deploy refactor except
  the added `BOBI_INSTANCE` stamp.)
- Two-instance fleet (C22): provision both, `fleet.sh list`/`classify` against real
  Fly state, changed-team update (`install <url>` + restart — role content updated,
  workspace file preserved), release rollout (cross-app image pull, config
  preserved, `pong`).

The deploy refactor adds the **ssh-push** path, **verified live on Fly**
(`bobi deploy sshe2e --team smoke-team`, then torn down):
- Blank provision (app + 15 GB volume + staged secret); `fly deploy` returns on the
  blank machine reaching **started** — it does *not* hang on the image healthcheck.
- `BOBI_INSTANCE` confirmed in the live `[env]` (next to `BOBI_FLEET`).
- base64 push over `fly ssh` → `bobi agents install /data/…tar.gz --non-interactive`
  (secrets read from the Fly-injected env — confirmed available in the ssh session)
  → the wait-for-team entrypoint detects the team and starts the manager (`status`
  shows it running).
- In-place re-deploy: re-push → workspace-safe reinstall (a role marker propagated)
  → `fly machine restart <id>` → manager back up.
- `bobi destroy sshe2e --yes` removes the app + volume.

> **flyctl gotcha (found in this e2e):** `fly machine restart -a <app>` errors
> "a machine ID must be specified" outside a TTY. `deploy` resolves IDs via
> `fly machine list --json` and restarts each by ID.

The **team-url** path is verified live two ways:
- **CI / GitOps:** a `deploy-canary-1` tag fired `deploy-agent-teams.yml` from the branch
  → `bobi deploy canary` → provisioned `moda-canary` (1 GB/1 vCPU) via team-url;
  manager healthy, `BOBI_INSTANCE` stamped.
- **Binary-only (no repo):** from a directory with no bobi checkout,
  `bobi deploy` resolved the bundled wheel assets ("binary mode"), built the
  image from PyPI (`BOBI_BUILD=pypi`, version-pinned), and provisioned
  `bintest-bsmoke` — incl. the **re-provision-on-failure** fork (a half-built app
  with no started machine re-provisions instead of erroring "no started VMs").

> **Release gate (found in the binary e2e):** the PyPI image pins the *installed*
> bobi version, so the instance runs **published** code while the entrypoint
> ships with the *operator's* version. Deploying from an unreleased dev checkout
> (entrypoint ahead of the pinned published package — e.g. an entrypoint that calls
> `install --non-interactive` before that option was published) crash-loops the
> instance. **Release these changes before binary-mode deploy boots cleanly**; a
> released `uv tool install bobi` is always self-consistent.

> **Lean image (found in the binary e2e):** install the kb deps the code uses
> (fastembed) **explicitly**, not via the `[kb]` extra — some published releases
> stale-list `sentence-transformers` there, pulling torch + ~2 GB of CUDA the dark
> CPU instance never uses (and blowing the build). Fixed in the Dockerfile +
> pyproject; the published `[kb]` should be re-released lean too.
<!-- e2e-status: ssh-push + canary(team-url) + binary-mode verified 2026-06-19 -->

Smoke target: `tests/fixtures/smoke-team` (zero-secret; only needs
`BOBI_EVENT_SERVER` + an Anthropic key for the `ask` round-trip).
Structural/unit coverage: `tests/test_gitops_c22.py`. Both workflows pass
`actionlint` (+ shellcheck on run blocks).
