# Custom agent dependencies — team-flavored images (C24)

Status: **MVP SHIPPED (2026-06-19).** Layered on C8 (the container image) and
C22 (provision/update/release automation). Tracking: `[containerized-24]`.

## What shipped (MVP) — read this first

Two design choices were refined during implementation; they supersede the
"FROM modastack-base" framing in §1/§2 below:

1. **Wheel-on-top via a team-deps HOOK, not `FROM modastack-base`.** A team that
   declares `build:` is rendered to a shell hook (`modastack/build_render.py` →
   `team-deps.sh`) that the ONE Dockerfile runs via a `TEAM_DEPS` build-arg, as a
   stable layer **below** the volatile framework-wheel copy. So a code-only
   framework release rebuilds only the wheel layer — the team's tools stay
   cached. (FROM-base would re-run every team's apt/npm/run each release, because
   the FROM digest moves.) The default `TEAM_DEPS=docker/noop-deps.sh` makes a
   no-build team byte-identical to the generic image. A published `modastack-base`
   is therefore NOT needed for declarative teams (deferred — only the raw-
   Dockerfile escape hatch + solo `install <url>` pulls would want one).

2. **`$HOME` on the image + `CLAUDE_CONFIG_DIR` on the volume (revised
   2026-06-20 — supersedes the original HOME-seed).** The MVP shipped a
   "HOME-seed": runtime `$HOME` was the VOLUME (`/data/home`), tools baked into a
   seed dir, and the entrypoint `cp -a`'d that seed onto the volume at boot,
   gated by a content stamp. That worked but was brittle — most acutely, the
   build's `verify: requires` ran against the seed HOME, NOT the post-copy volume
   HOME the agent actually uses, so a copy that dropped a file passed CI and
   broke production (build path ≠ runtime path).

   The revised model keeps **`$HOME` on the IMAGE** (`/home/modastack`) — the
   SAME path at build and runtime, so `verify` proves the live paths and any
   `$HOME`-relative tool (`~/dev/gstack`) is read in place, never copied. Only
   Claude's DURABLE state (creds, transcripts, settings) is redirected to the
   volume via **`CLAUDE_CONFIG_DIR=/data/claude`** (the supported config-dir
   override). To make this invisible to tools, the entrypoint points the WHOLE
   `~/.claude` at that volume dir (`~/.claude -> /data/claude`), so a tool keyed
   off `~/.claude/{projects,settings.json,skills,…}` sees Claude's real state —
   one coherent home tree, split underneath only by storage lifecycle. Personal
   skills bake at `/opt/modastack/skills` (immutable image content, OUTSIDE
   `~/.claude` so the repoint can't clobber them) via a build-time
   `~/.claude/skills -> /opt/modastack/skills` symlink, and are surfaced under
   the config dir's `skills/` entry. No seed, no stamp, no `cp -a`, no drift.
   (codex/gh differ: system installs land in `/usr/local/bin`, on PATH, outside
   HOME entirely.) See `build_render.py` + `docker-entrypoint.sh` §2b.

3. **Built on Fly during deploy, not pushed to a registry (MVP).** The intended
   "build once in CI → push to a registry → deploy many by ref" hit Fly friction:
   Fly's registry rejects a push to a never-deployed ("pending") app, and GHCR
   needs `write:packages`. So `modastack deploy` renders the team-deps hook into
   the build context and builds the team-flavored image **on Fly's remote
   builder** during deploy (`deploy.py:_render_team_deps_into_context` →
   `--build-arg TEAM_DEPS`). Fly creates app + registry + machine together, and
   its builder caches the tool layers, so re-deploys are cheap. `--image <ref>`
   still short-circuits to a prebuilt pull when one exists; `team-images.yml` is
   a build-only **verify gate**. Build-once-deploy-many (per-team digest, pushed
   to **Fly's registry** — we standardized on `registry.fly.io`, not GHCR) is
   deferred as a SaaS-scale optimization — tracked as **#378** (the foundation),
   which in turn unblocks **#379** (deps-vs-definition trigger split, §6 below).

4. **A `run_root` build phase.** Some tools need root steps `apt` can't express
   (gstack's browse drives Playwright Chromium → ~30 system libs). `run_root`
   runs as root before the user `run` steps; eng-team uses
   `npx playwright install-deps chromium` so Playwright resolves the right
   packages for the running Debian instead of hand-listing `t64` names.

Pieces: `build:` schema (`modastack/config.py` `BuildSpec`, incl. `run_root`),
renderer (`build_render.py`), Dockerfile `TEAM_DEPS` hook + `docker/noop-deps.sh`,
entrypoint `~/.claude` coincidence (`docker/docker-entrypoint.sh` §2b), build-on-deploy
(`deploy.py:_render_team_deps_into_context`) + `--image` short-circuit +
`provision-instance.sh --image`, CI verify gate
(`scripts/build-team-images.sh` + `.github/workflows/team-images.yml`),
eng-team's `build:` block, and `deployments/eng-team.yaml` (activated). codex
auth = `OPENAI_API_KEY` in the env blob (flows as a Fly secret; build only
verifies the binary). Tests: `tests/test_build_spec.py`,
`tests/test_build_render.py`, `tests/integration/test_team_image.py` (gated
build+seed proof).

The original design follows (FROM-base framing kept for context).

---

## Original design

Layered on C8 (the container image) and C22 (provision/update/release automation).

## Problem

A team is more than prompts — a real one needs **host tools** in its container.
`eng-team` is the canonical case: its `agent.yaml` declares

```yaml
requires:
  - name: gstack
    check: "test -e ~/.claude/skills/browse/SKILL.md && test -x .../browse"
    fix:   "git clone .../gstack ~/dev/gstack && cd ~/dev/gstack && ./setup"
  - name: codex
    check: "command -v codex && (OPENAI_API_KEY set OR codex --version)"
    fix:   "npm install -g @openai/codex && codex auth login"
```

Today this team **provisions but cannot work**:

1. `requires[].check` runs at **dispatch time** (`subagent.py:check_requires`) and
   *blocks agent launch* if it fails — so the tools must genuinely be present.
2. `requires[].fix` is only ever run **interactively** (`modastack doctor` →
   `click.confirm`, `cli.py:1303`). Nothing auto-runs it; a dark container has no
   TTY, so that path can never fire.
3. The C8 image **deliberately ships no Node/npm** (`Dockerfile`: the `claude` CLI
   is a native binary, "no Node"). But `codex` is `npm install -g`, and `gstack`'s
   `./setup` is Node-based too — neither can install even if we did run `fix`.

Net: secrets already materialize (the C22 `MODASTACK_ENV` blob → Fly secrets/env,
unchanged), but **dependency binaries don't materialize at all.** And this isn't
eng-team-specific — *anyone building a custom agent* will hit it. It needs a
first-class, team-agnostic mechanism.

## Principles (what must hold)

- **Framework stays topology-free** (CLAUDE.md): the base image must not bake in
  any team's tools. The dependency declaration travels *with the team*, in the
  team directory, like prompts and workflows.
- **Operator-agnostic** (design §9.1): works for the moda-labs GitOps fleet **and**
  the solo `modastack install <url>` + `provision-instance.sh` path.
- **Build once, deploy many** (§2): the per-team image is the unit; the
  provisioner/release deploy it by reference — which the C22 release flow already
  does (`fly deploy --image`).

## Key insight: three clocks

Three things change at very different rates. Keep them on separate paths, ordered
fastest-changing to slowest so the cheap update stays cheap:

| | Changes | Lives in | On change |
|---|---|---|---|
| **Definition** (prompts, workflows) | constantly | the **volume** | hot `install <url>` + restart (~30 s, no rebuild) |
| **Framework** (the `modastack` wheel) | per release | the **image** (a thin top layer) | rebuild the last layer → redeploy |
| **Tool deps** (codex, gstack, node…) | rarely | the **image** (cached lower layers) | rebuild image → redeploy |

This preserves the C22 "changed team" flow: a prompt edit is a hot update with
**no rebuild**. The team image carries **tools + framework only**; the team
**definition keeps flowing through the tarball** (never bake the definition into
the image — that turns every prompt tweak into a multi-minute build).

**Layer ordering is load-bearing.** The image is a single `Dockerfile`
(`MODASTACK_BUILD={source|pypi|wheel}`, shipped in C22/#365). Today it inverts the
order — the `claude` install and the **fastembed model bake** sit *after* the
modastack venv, so any framework change re-bakes the model every build (~minutes).
A team image must order layers **stable → volatile**: (1) base OS + sys pkgs →
(2) **tool deps** (node, codex, gstack) → (3) `claude` CLI + **model bake** →
(4) modastack's python deps → (5) **the modastack wheel** as the LAST thin layer.
Then a framework-version bump rebuilds only the final layer (seconds), and a
tool-deps change rebuilds from (2). That's what keeps image-baked fast enough to
keep its immutability/atomic-deploy/rollback guarantees instead of mutating live
machines. (Install `fastembed` explicitly, never the `[kb]` extra — some published
`[kb]` stale-lists `sentence-transformers` → torch + ~2 GB CUDA the CPU instance
never uses.)

## Design

### 1. Base image becomes a published artifact

The unified C8/C22 `Dockerfile` (`MODASTACK_BUILD=pypi`, version-pinned, lean
`fastembed` — shipped in #365) is published as `modastack-base` to a registry,
tagged per framework release (e.g. `ghcr.io/moda-labs/modastack-base:<version>`).
Team images do `FROM ghcr.io/moda-labs/modastack-base:<version>` and add only
their tool-deps layers (the framework wheel is already baked, version-matched).
(Registry choice is an open question — see below; GHCR is the leaning.)

### 2. Team build spec — declarative front door + Dockerfile escape hatch

A team dir **optionally** declares its build. No declaration → it deploys on the
generic base image (today's behavior; smoke-team, market-research need nothing).

**Front door — declarative `build:` in `agent.yaml`** (covers ~90%):

```yaml
build:
  base: ghcr.io/moda-labs/modastack-base   # optional pin; default = matching release
  apt:  [nodejs, npm]                       # root, build-time
  npm:  ["@openai/codex"]                   # global installs
  run:                                      # arbitrary build steps, as the modastack user
    - "git clone https://github.com/garrytan/gstack ~/dev/gstack && cd ~/dev/gstack && ./setup"
  verify: requires                          # run requires[].check at build; fail build on miss
```

The framework renders this to a Dockerfile fragment appended to `FROM <base>`,
choosing the right `USER`, `HOME`, and `WORKDIR` (the renderer owns the
volume-shadows-WORKDIR trap from C10 — build-time has no volume, but `HOME`/paths
must match runtime). `apt` runs as root; `npm`/`run` as the `modastack` user.

**Escape hatch — a raw `Dockerfile` in the team dir.** If present, it wins; the
framework only asserts `FROM …/modastack-base…` and builds it. The team owns
everything. For the long tail the declarative block can't express.

**Pin deps for reproducible rebuilds (#380).** Pin `npm` to exact versions
(`@openai/codex@0.141.0`), clone-then-`git checkout <sha>` for `run` clones, and
pin `npx playwright@<ver> install-deps`. `team_deps_hash` keys on the spec TEXT,
so this is also the *only* knob that moves the image identity: a deliberate pin
bump is a one-line diff that rebuilds; an unpinned dep can drift upstream without
moving the hash (cache serves a stale image) — exactly the failure pinning
prevents. (Resolved-version folding into the hash was considered and rejected:
with everything pinned, the spec text already *is* the resolved set.) `apt` is the
exception — exact Debian versions get pruned from the mirror and break rebuilds,
so it tracks the pinned base image's Debian release instead. A lint test
(`tests/test_eng_team_deps_pinned.py`) fails if a pin is dropped.

### 3. CI builds + publishes per-team images

Extend `team-packages.yml`: for each team with a build spec (declarative or
Dockerfile), build `modastack-<team>:<sha>` (+ rolling `:latest`) and push to the
registry. The **definition tarball still builds unchanged** (deps and definition,
two artifacts). `requires[].check` runs as the **final build step** so a missing
tool fails CI, not production. Teams with no build spec publish no image (they use
the base).

### 4. Provisioner / release deploy the team image

- `provision-instance.sh` gains **`--image <ref>`**: deploy a prebuilt image
  instead of building the generic one from the local Dockerfile. The C22 provision
  job passes `--image modastack-<team>:latest` when the team has one; otherwise the
  generic path (unchanged).
- `gitops-release.yml`: a framework release rebuilds each team image `FROM` the new
  base, then rolls. The C22 release loop already deploys by image ref — it iterates
  team images instead of one shared image. (Build-once-per-team, deploy-many.)

### 5. `requires` stays the single contract

- `check` → build-time **verify** (new) + dispatch-time **gate** (exists).
- `fix` → dev-box/`doctor` path (unchanged), and the *source the declarative
  `build.run` can reuse* so the install is written once.
- Secrets unchanged: `OPENAI_API_KEY` etc. flow through the C22 env blob.

### 6. GitOps trigger split (deps vs definition) — SHIPPED (#379)

> **BUILT.** `modastack deploy` of an already-running ssh-push app now **decides**
> between a hot-push and an in-place rebuild *in the deploy primitive* — so the
> behavior is identical from a laptop and from the `gitops-teams` reconcile, and a
> deps edit to a live instance self-heals instead of being a silent no-op.

**Mechanism — a deps-identity stamp.** The team-deps hook stamps
`team_deps_hash(build:)` into the image at `/opt/modastack/team-deps.hash`
(`build_render.py`). On an in-place update the deploy primitive reads that stamp
off the running instance over `fly ssh` (`deploy._running_team_deps_hash`) and
compares it to the hash a fresh build would bake (`_local_team_deps_hash`):

- **deps changed** (running ≠ rebuilt) → **rebuild in place**: re-run the
  idempotent `provision-instance.sh` (rebuilds the image on the existing app,
  never touches the volume's project files), then hot-push the definition + reload.
- **deps unchanged** → the **hot-push fast path** (`push_team(..., restart=True)`),
  exactly as before. Most pushes are prompt-only and stay here.
- **no stamp** (image built before #379) → can't tell deps apart; warn + hot-push.
  `modastack deploy --rebuild` forces the rebuild path (and re-stamps).

`--rebuild` also lets an operator force a rebuild unconditionally. After #378, the
team image's Fly-registry digest becomes the deps identity, so the same decision
collapses to a digest comparison (no `fly ssh` probe needed).

## What changes vs stays

**New:** base-image publish; `build:` spec parsing + Dockerfile rendering; per-team
image build/push in CI; `provision-instance.sh --image`; the deps-vs-definition
trigger split.

**Unchanged:** the C22 secret model, fleet enumeration (`MODASTACK_FLEET`), the
changed-team hot-install flow for prompt edits, and the release loop's
deploy-by-ref mechanic. This is purely additive.

## Open questions

- **Registry: DECIDED — Fly's registry (`registry.fly.io`), not GHCR.** We
  standardized on Fly (one less service; native `fly auth docker`; Fly pulls it
  with no public-package/token dance). In the MVP nothing is pushed at all —
  team images build on Fly *during deploy*. The build-once → push → deploy-many
  path (Fly registry) is #378; its constraint is that Fly repos are app-scoped
  and only exist after an app's first deploy (pushing to a "pending" app 404s).
- **Trust / supply chain:** CI building images from team-declared `run:`/Dockerfile
  is fine for the first-party `agents/` repo. For *third-party* teams (future SaaS),
  arbitrary build steps are a supply-chain surface → sandbox the build or restrict
  to an allowlisted declarative subset. **Note now, don't solve in MVP.**
- **Dev-box parity:** the same `build:` spec could drive `modastack doctor --fix`
  (run the install steps locally), so a contributor's laptop matches the container.
  Nice-to-have, not MVP.
- **Base-image size:** adding Node to `modastack-base` for the common case vs
  keeping it lean and letting each team's `apt` pull Node. Leaning lean base +
  per-team `apt` (keeps the no-Node promise for teams that don't need it).

## MVP slice

1. Declarative `build:` block (`apt` + `npm` + `run`) + raw-Dockerfile escape hatch.
2. Per-team image build/push in `team-packages.yml` → GHCR.
3. `provision-instance.sh --image`.
4. `requires[].check` run at build (verify).
5. **eng-team** as the proving case: declare `build:` (node + codex + gstack),
   build its image, provision, watch `requires.check` pass and an agent actually
   dispatch.

## Acceptance

- A team with **no** build spec still deploys on the generic base image (regression).
- `eng-team` declares `build:` → CI builds `modastack-eng-team` → provision →
  `requires.check` passes → the dispatch gate no longer blocks → a real eng
  workflow runs end-to-end on a Fly instance.
- A **prompt-only** edit to `eng-team` hot-installs via the existing C22
  changed-team flow (no image rebuild).
- A **deps** edit (bump codex, add a tool) rebuilds the image and redeploys, volume
  + sessions intact.
