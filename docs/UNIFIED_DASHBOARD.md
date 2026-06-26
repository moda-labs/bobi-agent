# Unified Agent Dashboard

**Status:** Design / decision record — awaiting approval. No implementation lands until approved.
**Audience:** modastack maintainers
**Question it answers:** Today modastack has two separate local web apps — a
creation/onboarding UI (`modastack setup`) and a monitoring/interaction UI
(`modastack ui`). How do we merge them into one app that opens as a dashboard of
your teams, leads into the existing onboarding flow, launches the team and
returns you home, and lets you click a team to monitor it — such that a user
never needs the CLI except to start the app, and the same web app can later be
hosted as a website?

---

## TL;DR

- **Merge the two UIs into one app** with two tiers: an **app-level** shell
  (dashboard, onboarding, monitor) and **team-level** views. The onboarding flow
  and the monitor become routes inside the shell, not separate processes. Both
  existing apps already share the DESIGN.md design system, so this is a merge,
  not a rewrite.
- **Give installed teams a canonical home:** `~/.modastack/agents/<team>/`
  containing the unchanged `.modastack/` runtime-state dir + `workspace/`. The
  per-project layout and self-containment principle are **unchanged** — we only
  relocate the project under a predictable root so the dashboard and CLI can find
  every team. One deployment per team per machine, keyed by team name.
- **Sources are configurable; runtime is fixed.** Team *design sources*
  consolidate under `~/.modastack/sources/` by default but are a configurable
  search-list (so a team can live in a version-controlled repo). The *runtime
  root* is fixed and predictable. The only new global config is **path
  resolution** (`~/.modastack/config.yaml`) — agent config stays per-project.
- **MVP = the spine:** Dashboard, Onboarding (rewired to install + launch +
  return home), and Agents-&-Chat (monitor). Everything else (events,
  transcripts, costs, workflows, monitors, KB, registry, settings UI) stays a
  power-user CLI surface until later phases.
- **Lower the barrier to entry** by addressing the three real dependencies
  (Python installer, Node event server, Claude CLI auth) and keeping the UI a
  **static client over a relocatable HTTP API**, so the same app ships as a
  bundled desktop install today and a hosted website later without a rewrite.

---

## Problem

The two UIs are disjoint products with disjoint lifecycles:

- **Creation UI** — `modastack setup` → `modastack/setup/webui/` (FastAPI +
  vanilla-JS SPA). Onboards a team through staged digestion, then ends at a
  static "here's the `modastack start` command" screen. `/api/finish` keeps the
  server alive and routes to a *team-design library* homepage; it never launches
  anything (`setup/webui/server.py:1089`, `setup/actions.py` install-only).
- **Monitoring UI** — `modastack ui` → `modastack/agentui/` (FastAPI + vanilla-JS
  SPA). Lists *running agent sessions* for **one** project's
  `.modastack/sessions/` and offers blocking chat per agent. Already supports a
  deployed/container mode (Fly 6PN + token file).

So there's a gap: nothing connects "I designed a team" → "it's running" → "let me
watch it." There are even two different notions of "home" (a library of *designs*
vs a roster of *running sessions*), and neither is the dashboard a user wants.
Worse, the install/run location is just whatever cwd you ran `modastack install`
in (`setup/actions.py:install_team` → `paths.modastack_dir(project)`), so there
is no root from which a dashboard could enumerate "all my teams."

## Goal

One web app that:

1. Opens as a **dashboard** of your teams and their state.
2. Leads into the **existing onboarding flow** to create or modify a team.
3. On finish, **installs, launches, and returns you to the dashboard**.
4. Lets you **click a team** to drop into the existing monitor/chat.
5. Requires the **CLI only to start the app** (power-user commands may stay CLI
   for MVP).
6. Is structured so it can be **hosted as a website later** without a rewrite.

---

## Directory model

### Canonical runtime root

Each installed team gets a predictable home keyed by team name. The
`.modastack/` runtime-state convention does **not** change — each `<team>/` is a
normal modastack project in today's sense, just relocated under a managed root:

```
~/.modastack/
├── config.yaml                       # NEW: machine-level, PATH RESOLUTION ONLY
├── sources/                          # default (consolidated) design library
│   └── <team>/                       #   editable source — what setup writes by default
└── agents/
    └── <team>/                       # canonical runtime, one per team per machine
        ├── .modastack/               #   runtime state — UNCHANGED convention
        └── workspace/                #   user-owned domain files + work products
```

What changes is **resolution only**: "which directory is the project" goes from
"wherever cwd is" to "`~/.modastack/agents/<team>/`". Everything that reads or
writes `.modastack/` — sessions, state, `.env`, the runtime resolver, monitors,
the watchdog — is untouched.

- **One deployment per team per machine.** Keyed by team name; reinstall updates
  the dir in place. Multi-instance is explicitly out of scope.
- **Runtime root is fixed**, not user-relocatable (an env override such as
  `MODASTACK_HOME` is sufficient for tests/CI). Predictability is the point — the
  dashboard and CLI rely on it.

### Sources are configurable; runtime is fixed

Design **sources** have the opposite requirement from runtime: users want them
wherever they keep their work, including a version-controlled repo.

- Default source location: `~/.modastack/sources/` (consolidated).
- Resolved as a **search-list** so a user can add a git-controlled directory and
  have setup + dashboard see those teams too.
- **Install always reads from a source (wherever it lives) and freezes into the
  canonical runtime root.** Source location and run location are decoupled:
  "source-controlled if you want it" and "runtime always predictable" both hold.

This consolidates the two confusingly-similar roots that exist today —
`~/modastack-agents/` (design sources, `setup/webui/server.py:98`) and the
scattered per-cwd `.modastack/` runtime — into one obvious `~/.modastack/` tree.
`~/modastack-agents/` is renamed to `~/.modastack/sources/`.

### The global-config boundary

This reintroduces a small amount of global config, which CLAUDE.md today
forbids ("No global `~/.modastack/` directory — each project is fully
self-contained"). The boundary must be stated explicitly:

- **Global (`~/.modastack/config.yaml`):** path resolution only — where sources
  live, where the runtime root is, registries.
- **Per-project (unchanged):** all agent config — roles, services, monitors,
  workflows, `.env`. Still self-contained inside each team's `.modastack/`.

So the principle being revised is specifically *"no global root,"* not
*"agent config is per-project."* CLAUDE.md should be updated to reflect this
when the work lands.

### CLI selector

Once cwd is no longer the implicit project, commands need to pick a team:

- Infer from cwd when run inside a team dir (back-compat: an explicit
  `.modastack/` in cwd still wins).
- Otherwise accept a `--team <name>` selector.
- In the UI this is implicit — you click a team and the action targets it.

### Back-compat

Existing in-repo `.modastack/` installs keep working: explicit cwd `.modastack/`
wins if present; otherwise resolve `<team>` under `~/.modastack/agents/`. Old
installs run unchanged; new installs default to the canonical root.

---

## Screens

Two tiers: **app-level** (you, your machine, all teams) and **team-level** (one
team you've drilled into). Onboarding and the monitor are routes within the
shell.

```
App shell
├── Home / Dashboard                 ← merged: designs + installed + running
├── Onboarding (create/modify/clone) ← existing setup flow, as a route
├── Registry browser                 ← browse/add remote teams
├── Settings                         ← global config (paths, registries, harness/auth)
├── Health                           ← doctor + event-server + harness status
└── Team detail  (per team)
    ├── Agents & Chat                 ← existing monitor: roster + interact
    ├── Activity / Events
    ├── Transcripts (view + search)
    ├── Costs
    ├── Workflows
    ├── Monitors
    ├── Knowledge bases
    ├── Connections & Credentials     ← services, secrets, Slack bot
    └── Configure team                ← re-enter editor to modify an installed team
```

### Completeness — every CLI verb has a home

| CLI | Screen |
|---|---|
| `setup` | Onboarding |
| `install <path>` / `agents browse` / `agents update` | Registry browser / Onboarding import |
| `agents add-registry` | Settings → registries |
| `start` / `stop` / `restart` / `start --fresh` | Dashboard card + Agents & Chat run controls |
| `agents launch` / `list` / `show` / `cancel` | Agents & Chat (roster + launch + agent detail) |
| `ask` / `message` | Agents & Chat |
| `compact` | Agents & Chat (per-session action) |
| `status` | Dashboard / Team header |
| `events` | Activity / Events |
| `transcript show` / `search` | Transcripts |
| `costs [--by …]` | Costs |
| `doctor` | Health |
| `workflows list` / `status` / `validate` | Workflows |
| `monitors list` / `add` / `pause` / `remove` | Monitors |
| `roles list` | Configure team |
| `create-slack-bot` | Connections & Credentials |
| `kb *` | Knowledge bases |
| `event-server start` / `stop` | Managed by the app; status in Health |
| `skill` | Help/docs link (low priority) |

### What stays CLI by necessity

- **Starting the app** (`modastack` / `modastack home`) — the one allowed CLI
  touch (and it disappears entirely in the bundled desktop build).
- **Deploy / release** (`deploy.py`, Fly fleet, `gh release`) — CI/operator
  concern, not a daily-user screen. Out of scope unless a deploy panel is added
  later.

---

## MVP cut

Draw the line at the spine — create → run → watch → chat — and let the CLI keep
covering power-user surfaces.

### In

1. **Dashboard** — merged team list (design-only / installed / running) with
   Create team, per-card Start/Stop, Open. Harness/auth status shown inline (no
   separate Settings screen yet). Subsumes `status`.
2. **Onboarding** — existing setup flow, rewired to **install + launch + return
   to Dashboard**.
3. **Agents & Chat** — existing monitor: roster + chat/ask, plus basic run
   controls (start/stop/restart, `--fresh` with a confirm).
4. **Welcome / empty state** — when no teams exist (already exists in both UIs).

### Foundational (not screens, but required for MVP)

- **Canonical runtime root** `~/.modastack/agents/<team>/{.modastack,workspace}`
  + the `--team` selector. The dashboard cannot enumerate teams without this — it
  is the load-bearing change.
- **Default consolidated source** at `~/.modastack/sources`. Ship the layout; the
  configurable source-path *picker UI* is deferred (advanced users edit
  `config.yaml`).
- **Launch-and-return** wiring: a `/api/launch` that runs `modastack start`,
  polls the registry until the manager session appears, then routes the SPA back
  to the Dashboard. This is the one MVP piece the codebase does not have today
  (`/api/finish` currently only marks state and shows a copy-able command).

### Deferred → stays CLI for now

Registry browser, Settings/source-path UI, Health/doctor, Activity/Events,
Transcripts, Costs, Workflows, Monitors, Knowledge bases, ongoing
Connections/Credentials management, and **Configure-team re-entry editing**.

### Known MVP gap — secrets

Onboarding captures credentials at the Connect step, so a team launched straight
from onboarding is fine. A team that is *already installed* and needs a key
rotated still needs the CLI/`.env` until the Connections screen lands. Accepted
for MVP.

---

## Distribution: lowering the barrier to entry

Installing via brew/uv requires developer tools. The barrier is **three separate
dependencies**, and they come off in different ways:

1. **The installer itself** — brew/uv/pip + a Python runtime.
2. **The local event server** — Node.js (the Cloudflare Worker is the hosted
   alternative).
3. **The agent brain + its auth** — the Claude CLI, authenticated by the user
   running login in a terminal (`setup/harness.py` detects exactly this). This is
   the most "developer-y" dependency and the one most easily missed.

A one-click installer that still strands a non-developer at "now open a terminal
and log in" has not lowered the barrier. All three must be addressed.

### How each comes off

- **#1 — bundle it.** Replace brew/uv + Python with a downloadable artifact:
  - *Single self-contained binary* (PyInstaller / PyOxidizer / uv standalone
    Python): simplest to produce; needs code-signing + notarization to avoid
    Gatekeeper/SmartScreen warnings.
  - *Desktop wrapper* (Tauri preferred over Electron): a real installer, native
    window, auto-update, and a clean place to host the login OAuth window. More
    build infra; this is the version a non-developer recognizes as "an app," and
    it removes even the "start the app" CLI touch.
- **#2 — drop Node.** Default the consumer build to the **hosted Cloudflare event
  server** so there is no local Node. (Porting the local event server to Python
  is the alternative for fully-offline operation.)
- **#3 — kill terminal auth.** The pluggable brain (`docs/specs/pluggable-brain.md`)
  makes this possible: either **in-app OAuth** (the wrapper opens the login flow
  in a window and manages the token) or an **API-key / hosted brain** that talks
  to the model API directly and needs no local CLI at all.

### The discipline that keeps the website path open

The key point for "host it later": **you do not have to choose now, if the UI is
a static client over a clean HTTP API.** It largely already is —

- the frontend is vanilla JS with no build step (servable from a CDN unchanged);
- both backends already use a token + host guard;
- agentui already runs in a deployed mode (Fly 6PN + token file).

Hold that line — static client, *relocatable* API server, no `127.0.0.1`-only
assumptions in the client — and the **same UI** serves three deployments off one
codebase:

1. Bundled desktop app (agents run locally) — the near-term barrier-reduction win.
2. Self-hosted / `fly proxy` (agents in a container) — already works.
3. Hosted website (agents run in the cloud, multi-tenant) — the future SaaS
   (see `docs/OPEN_CORE_SAAS_STRATEGY.md`).

### The one decision that actually forks the product

Packaging is reversible; **where agents run is not.** Local agents operate on
your files, repos, and local credentials — modastack's current soul. A hosted
website means agents run in the cloud and reach your world through OAuth/connector
plumbing instead of your filesystem. That is a genuine fork in the
trust/credentials/filesystem model. The architecture already anticipates both
(pluggable brain + deployed agentui mode + Fly deploy), so the recommendation is
to **defer this decision but make it consciously** — the packaging choices above
cannot undo it.

### Recommendation

- **Near term:** Tauri desktop bundle + hosted event server by default + in-app /
  API-key auth → "download, open, log in, go." No terminal, no Python, no Node.
- **Architecture:** keep the UI a static client over a relocatable HTTP API so the
  website version is a deployment target, not a rewrite.
- **Defer** the local-vs-cloud-agents decision, consciously.

---

## Decisions locked

1. **Merge** the two UIs into one app shell with app-level + team-level tiers.
2. **Canonical runtime root** `~/.modastack/agents/<team>/{.modastack,workspace}`;
   `.modastack/` convention unchanged; CLI gains a `--team` selector.
3. **One deployment per team per machine**, keyed by team name; reinstall updates
   in place.
4. **Consolidate under `~/.modastack/`**; runtime fixed, **source path(s)
   configurable** via `~/.modastack/config.yaml` (default `~/.modastack/sources`,
   search-list so a git repo can be added). Global config is path-resolution only.
5. **MVP = Dashboard + Onboarding (install+launch+return) + Agents-&-Chat**;
   everything else stays CLI for now.
6. **Distribution:** address all three barrier dependencies; keep the UI a static
   client over a relocatable HTTP API so the same app ships bundled now and hosted
   later.

## Open questions

- **Ongoing edit-in-place** (Configure-team re-entry, Connections/Credentials
  management): making roles/automations/connections/secrets editable *after*
  install, idempotently, against a running team — a capability the codebase does
  not really have yet. The single biggest deferred item.
- **Local-vs-cloud agents** for the hosted website — deferred but must be made
  consciously (see Distribution).
- **CLAUDE.md update** for the global-config boundary, to land with the work.
