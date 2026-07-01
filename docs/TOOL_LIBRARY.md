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
      The agent can run the gstack `browse` skill and get a screenshot back.
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
| Cost | None | A one-time agent run per declared dependency set (see `docs/CONTAINERIZED_DEPLOYMENT.md` §2.6.1) |
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
- `bobi/build_render.py` - the one renderer that bakes `install`/recipes into the
  Docker hook layer.
- `bobi/host_caps.py` - the generic `host:` capability model (doctor + deploy).
- `docs/CONTAINERIZED_DEPLOYMENT.md` §2.6 / §2.6.1 - how the image is built and
  when the bootstrap agent runs.
