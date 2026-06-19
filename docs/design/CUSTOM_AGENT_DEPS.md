# Custom agent dependencies — team-flavored images (C24)

Status: **design, ready to ticket (2026-06-19).** Layered on C8 (the container
image) and C22 (provision/update/release automation). Tracking: `[containerized-24]`.

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

## Key insight: two clocks

Deps and definition change at very different rates. Keep them on separate paths:

| | Changes | Lives in | On change |
|---|---|---|---|
| **Deps** (codex, gstack, node…) | rarely | the **image** | rebuild image → redeploy |
| **Definition** (prompts, workflows) | constantly | the **volume** | hot `install <url>` + restart |

This preserves the C22 "changed team" flow: a prompt edit is a ~30 s hot update
with **no rebuild**. Only an actual *dependency* change triggers an image rebuild.
Corollary: the team image carries **tools only**; the team **definition keeps
flowing through the tarball** (do not bake the definition into the image — that
would turn every prompt tweak into a 10-minute build).

## Design

### 1. Base image becomes a published artifact

The current C8 `Dockerfile` is published as `modastack-base` to a registry,
tagged per framework release (e.g. `ghcr.io/moda-labs/modastack-base:<version>`).
Team images do `FROM ghcr.io/moda-labs/modastack-base:<version>`. (Registry
choice is an open question — see below; GHCR is the leaning for portability.)

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

### 6. GitOps trigger split (deps vs definition)

`gitops-teams.yml` must distinguish a **deps change** (rebuild image → redeploy)
from a **prompt change** (hot `install <url>`). Diff the build spec / team
`Dockerfile` (or a deps-hash stamped as an image label): changed → rebuild +
`deploy --image`; unchanged → the existing hot-install path. Most pushes are
prompt-only and stay on the fast path.

## What changes vs stays

**New:** base-image publish; `build:` spec parsing + Dockerfile rendering; per-team
image build/push in CI; `provision-instance.sh --image`; the deps-vs-definition
trigger split.

**Unchanged:** the C22 secret model, fleet enumeration (`MODASTACK_FLEET`), the
changed-team hot-install flow for prompt edits, and the release loop's
deploy-by-ref mechanic. This is purely additive.

## Open questions

- **Registry:** GHCR (public, repo-scoped, portable — solo operators pull it too)
  vs the Fly registry (already in the deploy path, org-scoped). Leaning GHCR, with
  Fly as a mirror in the deploy path. Decide in implementation.
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
