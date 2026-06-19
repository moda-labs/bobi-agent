# Deployment — how it actually works (Fly build + GitOps)

How a modastack agent team becomes a running, self-managing instance on Fly,
and the hard-won nuances that make it work. This is the **operational** companion
to the design docs: `docs/design/CONTAINERIZED_INSTANCES.md` is *why/what*, this is
*how it works today + the gotchas*. Current as of the C22 GitOps work (2026-06-19).

Pieces: `Dockerfile` (image) · `scripts/provision-instance.sh` + `destroy-instance.sh`
· `scripts/fleet.sh` (fleet helper) · `scripts/build-team-tarballs.sh` ·
`.github/workflows/{team-packages,gitops-teams,gitops-release}.yml`.

---

## 1. Mental model

**Git is what should be running; Fly is what is running; three workflows close
the gap.** Push a team to `main` → its instance appears. Edit a team → the matching
instance updates in place. Publish a release → the whole fleet rolls to the new
image. There is **no database and no manifest** — the Fly API is the only state
store, and the GitHub Actions log is the deploy log.

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

## 3. The provisioner (`scripts/provision-instance.sh`)

Stands up one instance: `fly apps create` → 15 GB volume → stage secrets →
generate a per-app `fly.toml` (identity in `[env]`, 4 GB/shared-2x, **no
`[http_service]`** = dark/always-on) → `fly deploy`. **Idempotent** — re-running
redeploys, so it doubles as "redeploy this instance."

Key flags:
- Exactly one of `--team <name>` (bundled/registry) or `--team-url <.tar.gz URL>`
  (the dark-instance injection seam — pulled at first boot).
- `--env-file` — KEY=VALUE; `MODASTACK_*` keys become plaintext `[env]` identity,
  **everything else becomes a Fly secret**. This routing is what lets one blob
  carry both (see §5).
- `--fleet <prefix>` — stamps `MODASTACK_FLEET` into `[env]` (see §4). Defaults to
  the app name's leading dash-segment.
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

One **GitHub Environment per team**, holding a single secret `MODASTACK_ENV` = the
team's entire KEY=VALUE env-file body. The provision job binds
`environment: ${{ matrix.team }}`, writes `$MODASTACK_ENV` to a temp file under
`umask 077`, and passes `--env-file`. The provisioner's routing (`MODASTACK_*` →
`[env]`, rest → Fly secrets) means the single blob loses no expressiveness, and it
is the exact seam a token broker fills later (it emits the same blob).

- Use a **secret**, not a variable (masking). Never `echo`/`cat` it — `printf` to
  disk under `umask 077`.
- Team Environments must have **no required-reviewer protection rule** — it would
  pause the provision matrix waiting on a human.
- Per-fleet config lives in repo **variables/secrets**: `vars.FLEET_PREFIX`,
  optional `vars.MODASTACK_EVENT_SERVER`, and `secrets.FLY_API_TOKEN` (an
  org-scoped Fly deploy token: `fly tokens create deploy`).

---

## 6. GitHub Ops (the three workflows)

```
push to main
   │
   ▼
team-packages.yml ── builds each team -> teams-latest/<team>.tar.gz (sole publisher)
   │ (on success, main)
   ▼
gitops-teams.yml (workflow_run):
   diff: git changed agents/*/  ->  fleet.sh classify (by Fly state)
   ├─ added   -> provision-instance.sh  (env from the team's MODASTACK_ENV)
   └─ changed -> modastack install <url> + fly machine restart   (in place)

publish a GitHub Release
   │
   ▼
gitops-release.yml (release: published):
   build image once -> roll every fleet app to that digest (config preserved)
```

### team-packages.yml
On push to main (path-filtered to `agents/**` + the smoke fixture), builds each
team into `<team>.tar.gz` and **publishes to a rolling `teams-latest` GitHub
Release** → stable public URL
`https://github.com/<owner>/<repo>/releases/download/teams-latest/<team>.tar.gz`.
It is the **sole publisher** of that release; nothing else should `--clobber` it
(two writers race).

### gitops-teams.yml — added + changed
Triggered by **`workflow_run` after "Team packages" completes on `main`** (so the
tarball is fresh before any instance pulls it). Jobs:
- **diff**: checks out `workflow_run.head_sha` (`fetch-depth: 2`), computes changed
  top-level `agents/<team>` dirs (`git diff --diff-filter=d` to **exclude
  deletions** — deleting a team is human-only), drops slugs whose `agent.yaml` is
  gone, then `fleet.sh classify` against **Fly state** → emits `added`/`changed`.
  Classifying by Fly state (not git status) makes failed-provision retries and
  re-add-after-destroy self-heal.
- **provision** (matrix over `added`, `environment: <team>`): materialize
  `MODASTACK_ENV` → env-file → `provision-instance.sh --app <prefix>-<team> --fleet
  <prefix> --team-url <teams-latest/team.tar.gz> --env-file … --yes`.
- **update** (matrix over `changed`, no environment binding — secrets already on
  the volume): `fly ssh … modastack install "<url>"` then `fly machine restart`.

> **Why `install <url>`, not `agents update`.** A container first-boots from
> `MODASTACK_TEAM_URL`, so its installed pack records `source = url:…`
> (`registry.py:318`). `modastack agents update` resolves via GitHub *registry*
> repos (`source = github:…`) and won't match a URL source. `install <url>` re-runs
> the workspace-safe reinstall against the refreshed tarball (reseeds
> roles/tools/workflows, keeps existing workspace files). `restart` (stop+start,
> not `--fresh`) reloads config and resumes the session.

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

**One-time, per repo:**
1. `vars.FLEET_PREFIX` (e.g. `moda`), optional `vars.MODASTACK_EVENT_SERVER`.
2. `secrets.FLY_API_TOKEN` = `fly tokens create deploy`.

**One-time, per team:** a GitHub **Environment** named `<team>` with one secret
`MODASTACK_ENV` = the full env-file (all `ANTHROPIC_API_KEY`/service tokens/
`MODASTACK_*`). No protection rules.

**Then it's automatic:** push the team → instance appears; edit prompts → hot
update; publish a release → fleet rolls.

**Manual ops (mirror the workflows):**
```
scripts/provision-instance.sh --app <prefix>-<team> --fleet <prefix> \
  --team-url <teams-latest/team.tar.gz> --env-file ./team.env --yes
scripts/fleet.sh list <prefix>                       # what's running
scripts/fleet.sh classify <prefix> <team>…           # added vs changed
fly logs -a <app> ; fly status -a <app>              # observe
fly ssh console -a <app> --command 'gosu modastack env HOME=/data/home \
  bash -c "cd /data/project && modastack status"'    # admin
scripts/destroy-instance.sh --app <app>              # tear down (removes volume!)
```
Both GitOps workflows also accept `workflow_dispatch` for manual re-runs.

**Troubleshooting:**
- *Crash-loop, "No such file or directory (os error 2)"* → a `WORKDIR`/path under
  the volume mount, or a zstd/Depot image. See §2.1, §2.3.
- *Deploy fails on volumes* → missing `--ha=false` (§2.4).
- *Two new teams, one fails with `docker.sock missing hostname`* → concurrent
  builds racing the shared builder; serialize (§2.9).
- *Changed team didn't update* → confirm the workflow used `install <url>`, not
  `agents update`, and that `teams-latest` republished first (§6).
- *Instance boots but agents won't dispatch* → a team with a `requires:` gate whose
  tools aren't in the image (e.g. eng-team's gstack/codex). That's the C24 gap —
  see `docs/design/CUSTOM_AGENT_DEPS.md`.

---

## 8. What's verified

C10 + C22 were verified **live on Fly**, then torn down:
- Single instance: empty volume → image build → boot → first-boot team install from
  URL → healthy manager → `modastack ask` self-registers on the real Worker →
  `pong`.
- Two-instance fleet (C22): provision both, `fleet.sh list`/`classify` against real
  Fly state, changed-team update (`install <url>` + restart — role content updated,
  workspace file preserved), release rollout (cross-app image pull, config
  preserved, `pong`).

Smoke target: `tests/fixtures/smoke-team` (zero-secret; only needs
`MODASTACK_EVENT_SERVER` + an Anthropic key for the `ask` round-trip).
Structural/unit coverage: `tests/test_gitops_c22.py`. Both workflows pass
`actionlint` (+ shellcheck on run blocks).
