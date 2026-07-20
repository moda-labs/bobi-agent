# Tool Library

How a team declares the tools, skills, and services it depends on, and how those
dependencies get baked into the team's container image and verified. This is the
unified dependency model (#428): a CLI tool, a skill library, a font, and an MCP
server are all the same thing - a **dependency with a verifiable success
condition** - so there is one way to declare them, not one per kind.

## The one concept: a dependency

A dependency has a name and a required `success` contract; everything else is
optional:

| field | required | what it is |
|---|---|---|
| `success` | **yes** | The contract a preflight verifies (prose or shell). Without it, an agent or a build could declare victory on a half-install. |
| `guide` | no | How to materialize / use it (a link or text). Becomes `tools/<name>.md`. |
| `install` | no | Explicit **pinned** steps (`apt`/`npm`/`run_root`/`run`) - "do exactly this". |
| `host` | no | A host capability the container cannot grant itself (a kernel sysctl, a device). Runtime wiring, never baked. |
| `mcp` | no | An MCP server's connection spec (the SDK-native `{name: spec}` shape), rendered into each brain's config and verified by an `initialize` handshake. |
| `why` / `fix` | no | Documentation + a runtime repair hint carried to the `requires:` doctor surface. |

## Declaring a dependency on a team

A team lists its dependencies under `tool_library:` in `agent.yaml`. Each item is
either a **string** (a reference to a named catalog entry) or an **inline
mapping** (a dependency declared directly on the team):

```yaml
tool_library:
  - venn                       # a named catalog entry (see below)
  - name: gstack               # an inline dependency
    guide: https://github.com/<owner>/gstack/blob/<sha>/README.md
    success: |
      The agent can run the gstack `gstack-browse` skill and get a screenshot back.
    host:
      - sysctl: kernel.apparmor_restrict_unprivileged_userns=0
```

`tool_library:` is consumed at compose time - it never appears in the frozen
`agent.yaml`. It merges across the `from:` chain, so a base team's dependency is
inherited by everything built on it (de-duped by name, first occurrence wins).

## Two ways to declare a tool

Every dependency is materialized one of two ways. The difference is only whether
you hand the framework a recipe or a description.

### 1. Pinned `install:` - deterministic, no agent

You write the exact steps. Compose merges them into the team's `build:` and the
existing build layer bakes them (`bobi/build_render.py`). Reproducible byte for
byte. Use this for the brain's own CLI, security-sensitive installs, or anything
bit-pinned.

```yaml
- name: codex
  success: command -v codex && codex --version
  install:
    apt: [nodejs, npm]
    npm: ["@openai/codex@0.142.0"]
```

### 2. Guide-only `guide:` + `success:` - agent-bootstrapped, snapshotted

You describe it loosely; a **bootstrap agent** materializes it at image-build time
(in CI, on a fresh base image with the brain installed), pins the versions it
chose, and reports the exact steps as a recipe. That recipe is frozen back
through the same renderer as a pinned `install:` - there is one install code
path. Use this when maintaining a hand-written recipe would be churn (a tool that
changes often, adapts to the host, or is easier to describe than to script).

|  | Pinned `install:` | Guide-only `guide:` |
|---|---|---|
| Who writes the recipe | You | The bootstrap agent, once, in CI |
| Reproducible | Byte-for-byte | The **snapshot** is the pinned artifact; two bootstraps can resolve different upstream versions |
| Cost | None | A one-time agent run per declared dependency set (see the private deploy repo's `CONTAINERIZED_DEPLOYMENT.md` §2.6.1) |
| `bobi deploy` source-build | Works | Refused - deploy never runs the agent; build + push the image in CI and deploy it by `image:` / `team-url:` |
| When to use | Pinned / security-sensitive / a brain CLI | Loose, human-readable, or fast-moving deps |

Both are verified against `success` before the image layer is trusted, and both
skip on a warm boot (no agent runs at runtime).

## The catalog: declare once, reuse by name

A **catalog entry** is a dependency the framework ships as reusable package data,
so any team can pull it in with a single line. Entries live under
`bobi/tool_library/<name>/`:

```
bobi/tool_library/venn/
  tool.yaml     # the fields: success / install / guide / host / mcp / why / fix
  guide.md      # becomes tools/venn.md in the team (the agent-facing usage doc)
```

Once an entry exists, a team author writes `tool_library: [venn]` and gets, for
free:

- the **dispatch/doctor `requires:` gate** (`name` + `success` + `why` + `fix`),
- the **build recipe** merged into the image (`install`, or the guide-resolved
  recipe),
- the **usage doc** at `tools/venn.md` (from `guide.md`),
- any **host** capability surfaced to deploy/doctor.

The pin lives **once**, in the catalog entry. Compose's build de-dupe collapses
repeats across `from:` layers, so declaring `[venn]` in three teams that share a
base still bakes venn once. A team can still override any surface by declaring it
inline (an explicit team `requires:` / `build:` / `host:` wins).

## What a dependency contributes at compose

`tool_library.expand()` splices each dependency's surfaces into the merged
`agent.yaml`, then drops the `tool_library:` key:

- **`requires:`** - a `{name, why, check: success, fix}` entry (unless the team
  already declares that name), so the runtime dispatch gate and `bobi agent
  <name> doctor` verify it.
- **`build:`** - `install` steps accreted + de-duped via the one build merge.
- **`tools/<name>.md`** - the guide, unless the team already ships that file.
- **`host:`** - emitted as a top-level list so deploy surfaces it and doctor
  checks it (see `bobi/host_caps.py`); never materialized into the image.
- **`mcp_servers:`** - each dependency's `mcp:` spec merged into a top-level
  `mcp_servers:` dict (leaf-wins per server name), rendered per brain at runtime
  (see below).

## Cookbook: agent.yaml recipes

Every recipe below is the `tool_library:` an author writes on a team's
`agent.yaml`. `tool_library:` is consumed at compose and never appears frozen -
the "yields" block is the surface(s) it splices into the composed `agent.yaml`
(and `tools/`). Mix and match fields freely; a single dependency can carry any
combination.

### 1. A pinned CLI tool

Deterministic install, no agent. Reference a catalog entry by name, or inline the
same fields. Both forms are identical after compose.

```yaml
# by catalog name (the pin lives once, in bobi/tool_library/venn/tool.yaml)
tool_library: [venn]

# or inline, declared directly on the team
tool_library:
  - name: codex
    success: command -v codex && codex --version
    install:
      apt: [nodejs, npm]
      npm: ["@openai/codex@0.142.0"]
```

Yields a `build:` (baked into the image) and a `requires:` doctor gate. See
[recipe 8](#8-inheritance-dedup-and-overrides) for how repeats across the `from:`
chain collapse to one.

### 2. A guide-only tool (agent-bootstrapped)

No pinned recipe - you describe it and a bootstrap agent materializes it in CI,
pins what it chose, and snapshots the result. At compose there is no `build:`
yet; only the doctor gate and the usage doc are emitted.

```yaml
tool_library:
  - name: gstack
    why: "Headless browser QA via the gstack skill."
    guide: "Install gstack per https://github.com/example/gstack and confirm `gstack browse` returns a screenshot."
    success: "The agent can run `gstack browse https://example.com` and get a screenshot back."
```

```yaml
# yields (pre-bootstrap):
requires:
  - name: gstack
    why: Headless browser QA via the gstack skill.
    check: The agent can run `gstack browse https://example.com` and get a screenshot back.
# + tools/gstack.md  (the guide text, the agent-facing usage doc)
```

`success` may be **prose** (an agent judges it, as here) or **shell** (run
directly). Use prose when the check is "the agent can actually do X"; use shell
when a command exit code settles it.

### 3. A non-CLI asset (font, data file, ...)

A dependency is anything with a verifiable `success`, not just CLIs.

```yaml
tool_library:
  - name: inter-font
    why: "Brand font for rendered PDFs."
    success: "fc-list | grep -qi Inter"
    install:
      apt: [fontconfig]
      run_root:
        - mkdir -p /usr/share/fonts/inter && curl -fsSL https://example.com/Inter.ttc -o /usr/share/fonts/inter/Inter.ttc && fc-cache -f
```

### 4. A stdio MCP server (install + connection)

A local MCP server has two halves: **install** bakes the server binary, **mcp**
wires the connection. `success` verifies the binary; the per-brain `initialize`
handshake (over the emitted `mcp_servers:`) verifies the server actually answers.
Declaring `mcp:` without `install:` fails preflight - nothing on PATH.

```yaml
tool_library:
  - name: pirate-weather
    why: "Current conditions + forecast via the Pirate Weather MCP server (tools/pirate-weather.md)."
    success: >-
      command -v pirate-weather-mcp >/dev/null 2>&1 &&
      pirate-weather-mcp --version >/dev/null 2>&1
    install:
      apt: [python3-venv]
      run_root:
        - >-
          python3 -m venv /opt/pirate-weather &&
          /opt/pirate-weather/bin/pip install --no-cache-dir pirate-weather-mcp==1.4.0 &&
          ln -sf /opt/pirate-weather/bin/pirate-weather-mcp /usr/local/bin/pirate-weather-mcp
    mcp:
      weather:                       # the MCP SERVER name (a name -> spec map)
        type: stdio
        command: /usr/local/bin/pirate-weather-mcp
        args: ["--stdio"]
        env:
          PIRATE_WEATHER_API_KEY: ${PIRATE_WEATHER_API_KEY}
```

Yields `build:` + `requires:` + a top-level `mcp_servers.weather`. Note the
dependency name (`pirate-weather`) and the server name (`weather`) are different
namespaces - they need not match.

### 5. A hosted MCP server (HTTP/SSE, no install)

Nothing to materialize - just the URL and any auth headers. `${VAR}`
interpolates from `run/.env` at load.

```yaml
tool_library:
  - name: linear-mcp
    success: "the linear MCP answers initialize"
    mcp:
      linear:
        type: http                   # or `sse`
        url: https://mcp.linear.app/mcp
        headers:
          Authorization: Bearer ${LINEAR_MCP_TOKEN}
```

### 6. One dependency, several MCP servers

`mcp:` is a `{server-name: spec}` map, so one dependency can bring more than one
server - that is why the servers are nested rather than implied by the dep name.

```yaml
tool_library:
  - name: acme-suite
    success: "both acme servers answer initialize"
    mcp:
      acme-crm:
        type: http
        url: https://acme.example/crm/mcp
      acme-billing:
        type: http
        url: https://acme.example/billing/mcp
```

### 7. A dependency needing a host capability

Some deps need a capability the container cannot grant itself (a kernel sysctl, a
device). It is runtime wiring, surfaced to `bobi deploy` and verified by doctor,
never baked into the image.

```yaml
tool_library:
  - name: gstack
    success: "the browser launches"
    guide: "Headless browser QA."
    host:
      - sysctl: kernel.apparmor_restrict_unprivileged_userns=0
```

### 8. Inheritance, dedup, and overrides

`tool_library:` unions across the `from:` chain, so a base team's dependency is
inherited; the build de-dupe collapses a pin declared in several layers to one:

```yaml
# base/agent.yaml       ->  tool_library: [venn]
# leaf/agent.yaml       ->  from: base
#                            tool_library: [venn]
# composed leaf: venn baked once (one build recipe, one requires entry).
```

Any surface the framework would emit can be **overridden** by declaring it
explicitly on the team - the leaf wins:

```yaml
# The dep would wire a stdio weather server, but this team points the same
# server name at a hosted endpoint. The explicit mcp_servers.weather wins;
# the dep's spec for that name is dropped.
mcp_servers:
  weather:
    type: http
    url: https://team.example/mcp
tool_library:
  - name: pirate-weather
    success: "true"
    mcp:
      weather: { type: stdio, command: /usr/local/bin/pirate-weather-mcp }
```

The same leaf-wins rule holds for `requires:` (an explicit `requires: [{name:
...}]` is neither duplicated nor clobbered) and for `host:` (per sysctl key). Two
*dependencies* clashing on the same MCP server name or sysctl resolve first-wins
in resolve order.

## MCP servers, rendered per brain

An `mcp:` field is an MCP server's connection spec in the SDK-native shape - a
`{name: spec}` mapping where each spec is `type: stdio` (`command`/`args`/`env`)
or `type: http|sse` (`url`/`headers`). Compose merges every dependency's `mcp:`
into the composed `agent.yaml`'s top-level `mcp_servers:`, so a team-brought MCP
server is declared once (in a catalog entry or inline) and wired into whichever
brain the team runs on. An explicit team `mcp_servers.<name>` overrides a
dependency's spec for that name (leaf wins); two dependencies contributing the
same name resolve first-wins.

Each brain consumes that one `mcp_servers:` differently:

- **Claude** reads it from a per-session option - `subagent.py` splats
  `cfg.mcp_servers` into the SDK at every agent spawn. The compose-time emission
  is all Claude needs.
- **Codex** reads MCP servers from `~/.codex/config.toml` (nothing rides the CLI
  invocation), so the codex brain renders the effective `mcp_servers` into that
  file before the first `codex exec` (`bobi/brain/codex_config.py`). Only the
  bobi-owned `mcp_servers` block is managed; any other config keys survive.

Verification is a real `initialize` handshake, per brain, so a broken server
fails preflight instead of silently degrading:

- **Claude** is probed through the SDK's `get_mcp_status` (existing path).
- **Codex** exposes a `get_mcp_status` that runs a direct `initialize` +
  `tools/list` against each configured server (`bobi/mcp_handshake.py`), keeping
  `validate._async_probe_mcp` a single loop across brains. A stdio server whose
  binary isn't installed, or an unreachable URL, is a blocking failure.

A team-brought stdio server may also need its server binary installed - declare
that with the same `install:` / guide-bootstrap the rest of the dependency model
uses.

## Lifecycle and snapshot

1. **Declare** the dependency (catalog ref or inline).
2. **Bootstrap** (cold path, CI): a pinned `install:` is baked by the build
   layer; a guide-only dep is materialized by the bootstrap agent, which reports
   a recipe.
3. **Preflight**: each `success` is verified in the build tier
   (`BOBI_VERIFY_PHASE=build`), per target brain. The snapshot is trusted only
   when every dependency passes.
4. **Snapshot**: the materialized layer is frozen into the team image.
5. **Warm boot**: replay the snapshot - no agent runs in production.
6. **Re-bootstrap**: only when the declared set changes. The image stamps the
   declared-set hash (`/opt/bobi/dep-list.hash`, from
   `tool_library.dependency_list_hash`); `bobi deploy` compares it over `fly ssh`
   and rebuilds when it drifts.

## Running a team locally (`bobi agents install --with-deps`)

The lifecycle above is the **deploy** story: bootstrap in CI, snapshot, warm-boot
in production. Locally there is no image - `bobi agents install` only *composes*
the package, so a locally-run team blocks at the `requires:`/MCP dispatch gate
until its tools exist on the machine. `--with-deps` closes that gap: the brain
**already present on the dev machine** installs the declared dependencies the
same way a human would, driven by the same `guide`/`install`/`success` contract.

```bash
bobi agents install agents/eng-team --name eng --with-deps
```

After composing, it:

1. **Resolves** the team's full declared dependency set and **verifies** each
   `success` in the runtime tier. An already-satisfied dependency is skipped
   (idempotent - re-runs are cheap and safe).
2. **Previews a plan**: what will be materialized, what is already satisfied,
   which steps may need `sudo`, and which `host:` capabilities must be
   provisioned by hand.
3. **Confirms**, then drives the team's brain (its declared `brain:`, else the
   local default) to install each remaining dependency on THIS host. The agent
   **adapts** container-shaped `install:` recipes to the real host (brew / apt /
   pipx / a binary into `~/.local/bin`) - the recipe is a version-pin reference,
   not verbatim commands.
4. **Re-verifies** each `success` (the source of truth, not the agent's own
   claim) and prints a transcript of the commands the agent ran.

Because it mutates the developer's real machine, it is confirm-gated and never
runs `sudo` silently: a step that needs root is surfaced behind an explicit
"Allow sudo?" prompt, and `host:` capabilities stay a guided fix (like `doctor
--fix`), never attempted by the agent. Partial failure is non-fatal - doctor and
the dispatch preflight still gate - so you can fix a straggler and re-run. There
is no local snapshot; idempotency comes from re-verifying `success`. Without
`--with-deps`, install is compose-only and unchanged.

## `host:` capabilities

Some dependencies need a capability the in-container agent cannot grant itself -
a kernel sysctl, a device. It is **runtime wiring**, not baked:

```yaml
host:
  - sysctl: kernel.apparmor_restrict_unprivileged_userns=0
```

`bobi deploy` surfaces required host caps to the operator, and `bobi agent <name>
doctor` verifies each one on the host. gstack's Chromium-sandbox sysctl is one
instance of this model (`bobi/browser.py` builds on `bobi/host_caps.py`).

## Adding a new dependency

**As a catalog entry (reusable by any team):**

1. Create `bobi/tool_library/<name>/tool.yaml` with a required `success` and
   either a pinned `install:` or a `guide:` (and optional `host` / `why` / `fix`).
2. Add `bobi/tool_library/<name>/guide.md` - the agent-facing usage doc.
3. Reference it from a team: `tool_library: [<name>]`.

**Inline on a single team:** add a mapping under the team's `tool_library:` with
`name` + `success` + the fields it needs.

**Then:** if you edit a team package's content, bump that team's `version:` in its
`agent.yaml` - exact-pin consumers otherwise keep pulling the stale immutable
tarball.

## Where the code lives

- `bobi/tool_library.py` - the dependency model, catalog loader, `expand()`, and
  `dependency_list_hash`.
- `bobi/dep_bootstrap.py` - the bootstrap-agent harness, the `render_team_deps`
  build seam, and the CLI (`python -m bobi.dep_bootstrap <team> --render`).
- `bobi/local_deps.py` - local materialization (`install --with-deps`): plan /
  idempotency, the host-adapting install prompt, and re-verify-is-truth. Reuses
  `dep_bootstrap`'s agent/shell runners and `preflight` (runtime tier).
- `bobi/build_render.py` - the one renderer that bakes `install`/recipes into the
  Docker hook layer.
- `bobi/host_caps.py` - the generic `host:` capability model (doctor + deploy).
- The private deploy repo's `CONTAINERIZED_DEPLOYMENT.md` §2.6 / §2.6.1 - how
  the image is built and when the bootstrap agent runs.
