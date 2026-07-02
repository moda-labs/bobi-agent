# Design System — bobi setup

> Source of truth for the `bobi setup` web UI. Read this before any visual or
> UX decision.
>
> **Status (2026-06-16):** shipped and iterating. The original 8-stage rail
> machine was reshaped into **one screen** — an objective-guided conversation
> with the team materializing as cards beside it — plus a proper intro and a
> built-in file browser at the end. **Open mode (edit an existing team) is
> landed**, not deferred. The design *direction* below (aesthetic, color, type,
> motion, retro, the digestion-prompt philosophy) is unchanged from the
> 2026-06-13 agreement; the **UX architecture** section is the current model and
> supersedes any "stage rail" description elsewhere.

## Product context
- **What:** a local web wizard a developer runs (`bobi setup`, served on
  `127.0.0.1`) to design, build, install, and later edit a portable
  **agent-team package**. The editable team source lives in the named agent's
  `src/` directory by default (or another user-selected source directory), so a
  team isn't tied to the cwd; **Finish** authors the source there and installs
  a frozen image into that agent's `run/package/`.
- **Who:** developers/engineers comfortable in IDEs and terminals. v1 of this
  product was a terminal REPL.
- **Hard constraints:** fully **offline** — system fonts only, no CDN, no web
  fonts; vanilla HTML/CSS/JS, no build step; inline SVG only.
- **Brand:** ships as **bobi** for now. A rebrand to **bobbi** ("team
  bobbi", "we are legion") — after the Bobiverse novels, one engineer's mind
  forking into a legion of autonomous copies, which is the product's pitch — is
  the intended direction but **deferred** until the whole-codebase rename lands.
  Until then, all user-facing copy, commands, and paths say `bobi`. The
  retro-futurist *aesthetic* below already nods to that lineage. See
  `memory/project_bobbi_rebrand.md`.

## North star
**Surprisingly approachable** — "standing up an agent team was way easier than I
expected." Warmer and more guided than a stark IDE, closer to Linear/Vercel
onboarding, but still credible to developers. Chosen over the "craftable
artifact / workbench" framing — which is why files are *not* a persistent hero
surface during design (they become the hero at the very end, see UX
architecture).

The setup assistant's voice (dry, witty, geeky, reassuring even when things
break) serves the copy layer, not the chrome.

## Experience principles (pacing & feel) — the governing metric
**Shortest time-to-magic wins.** New-tool attention is short; the flow must feel
fun from screen one and reach a delightful, visible result fast. Everything else
is subordinate to this.
- **The intro is a short on-ramp, not a heavy chooser.** Three ways in (create /
  modify / registry — see below) on one calm screen, then you're talking to
  bobi within seconds. Create needs no name and no decisions up front — bobi
  names the team for you as you describe it.
- **Three escalating magic beats — front-load the first:** (1) **Design
  reflection** — type a rough line, bobi instantly reflects back a smart, alive
  understanding *and the cards on the right start filling in* (~seconds, the
  early hook); (2) **Automate suggestions** — "it thought of things I didn't";
  (3) **the Build pour** — the payoff. Don't make the user climb to (3) to feel
  anything.
- **Housekeeping never gates magic.** Connect/auth is **always deferrable** so
  OAuth is never a wall between the user and the pour. Auth happens once they're
  hooked, or just before they Finish.
- **Fun and forgiving.** Rough input is genuinely fine *because the team is alive
  and evolvable* (below) — no pressure to get it perfect up front.

**The team is alive and evolving, not set-once** (the openclaw/hermes feeling).
The product is a living workshop you return to, built on the re-entrant editor
(open/edit any team = same screen).
- **Done is a launchpad, not a finish line** — "bobi's live, change it anytime."
- **A fast evolve loop for small changes** — open your team, say "also post a
  Friday recap," it adapts; modify mode is **non-lossy** so you never lose the
  team's existing depth (below).

Corollary: time-to-magic is largely a **digestion-prompt latency + taste**
problem (how fast/smart the reflection is) — see the digestion-prompt section.

## Aesthetic direction
- **Warm workshop, not sci-fi lab.** Soft daylight surfaces, precise dark
  tooling, one sharp accent.
- **The signature move: split lighting.** Warm **light** chrome for the guided
  surfaces (conversation, the team panel, popups); a single **dark** CRT slab
  for generated artifact content (file contents in the built-in browser). *You
  author in daylight; the machine writes in the dark.* The dark slab is the
  **only** dark surface, which is what makes it read as "the artifact" rather
  than "a theme."
- **Decoration:** minimal-to-intentional. Hairline borders, warm paper, no
  gradients-as-CTA, no glassmorphism.

## Color

Light chrome (primary):
```css
--bg:            #F4F1EA;  /* warm bone/paper — the "approachable" promise */
--surface:       #FBFAF6;  /* panels */
--raised:        #FFFDF9;  /* cards, inputs */
--text:          #1F1B16;  /* near-black, brown bias — never #000 */
--muted:         #7A7062;
--faint:         #A89E90;
--border:        #E2DCD0;
--border-strong: #D6CCBD;
```

Dark slab (the one dark surface):
```css
--slab-bg:      #181410;   /* warm near-black, not blue-black */
--slab-surface: #1E1A15;
--slab-text:    #E8E2D6;   /* paper color inverted */
--slab-muted:   #948A7B;
--slab-border:  #2A241D;
```

Restrained syntax tints (never the accent):
```css
--syn-key: #AFC0D2;  --syn-str: #CBBA8B;  --syn-punc: #7E776B;  --syn-com: #6E6354;
```

**Accent — ONE color (LOCKED: amber, 2026-06-13), used only on:** the primary
button, the active selection, the slab top-edge, slot check-marks, and the
streaming caret.
```css
/* amber phosphor — LOCKED */
--accent:#C8612B; --accent-2:#D86E33; --slab-accent:#E0843F;
/* green — considered, not chosen (classic-terminal but cooler against the paper):
   --accent:#177B52; --accent-2:#1E8E60; --slab-accent:#29A36A; */
```
Amber skews "warning", so amber is **reserved for the brand accent only**
(progress, primary action, active selection, streaming) — success and error
states get their own distinct hues so the accent never sends a mixed signal.
Avoid purple/violet entirely (the generic-AI-builder tell).

## Typography — system fonts only
```css
--font-sans: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
             "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
--font-mono: ui-monospace, "SF Mono", "SFMono-Regular", Menlo, "Cascadia Mono",
             "Segoe UI Mono", Consolas, "Liberation Mono", monospace;
```
**Mono-accented, not mono-led.** Personality comes from the *contrast* between
engraved mono and conversational sans — free with system fonts:
- **Mono** (uppercase, tracked, small): card labels, file paths, status,
  eyebrows, numerals/counters (`tabular-nums`). The "instrument" voice.
- **Sans** (generous, 15–16px): body, questions, helper copy, chat. The
  conversational voice. This is the *only* sans and it stays quiet.
- **Mono** again for code in the slab.

`system-ui` as the body face is normally the "gave up on typography" tell; here
it's a hard offline constraint, countered by giving mono the identity role.

## Spacing & density
- 8px base. **Density gradient across the panes:** breathable conversation →
  denser team panel → dense slab (file contents). The asymmetry is intentional;
  uniform spacing reads as templated.
- Comfortable in the chrome (the approachable lever), compact in code/tables.
- Card radius 12px; inputs ~40px tall.

## Motion
- **Intentional, not decorative.** Signature moment is the **build "pour"**:
  files stream token-by-token into the slab behind a soft accent block-caret;
  the chat reply types out char-by-char. Short, ease-out, reassuring.
- All motion respects `prefers-reduced-motion`.

## Space-retro layer (static)
A *slight* retro-futurist lean (Bobiverse / Apollo / Nostromo / phosphor CRT),
concentrated where it belongs and **static** — no animation (a sweeping scan
beam and flicker were tried and removed as distracting).
- **The dark slab is the ship's computer:** a glowing accent top-edge and code
  pulled toward **phosphor** (syntax tinted toward the accent — amber-cream).
- **Chrome gets a whisper:** faint instrument texture behind the paper, a mono
  telemetry prefix on counters.
- **No web fonts** — retro is CSS treatment over system mono, never a pixel
  typeface. The light chrome stays clean so "approachable" survives.
- Brand mark: a small inline-SVG satellite/probe in the titlebar.

## Anti-slop (hard no)
Purple/violet gradients, aurora blobs, centered hero composition, 3-column
icon-card grids inside the wizard, gradient CTA buttons, glassmorphism, "AI
sparkle" iconography, oversized empty cards. Left-aligned workflow, real forms
and artifacts on screen, buttons that look like tools.

---

## UX architecture — one screen

There is **one screen**, not a stage rail. *Conversation proposes, the panel
disposes:* you talk to bobi on the left; the team materializes as cards on the
right. Special, deliberate steps (credential capture, Venn, Slack-as-chat) open
as **popup overlays** so they never derail the conversation. Three render routes
bracket the one screen: **intro** (before), the **build pour** (the automated
middle), and the **file browser** (after).

> The underlying `Stage` enum still exists and drives the build/install pipeline
> and its hard floors (goal non-empty to build, a fresh validation to install,
> an install to finish). The four "talk" stages just no longer have separate
> screens — they collapse into the one conversation. Don't reintroduce a rail.

### The intro — three ways in
One calm screen, three tabs, each landing in the same chat+cards editor:

- **Create new** — author from scratch. No name field (bobi auto-names the team
  from the goal as you talk; rename anytime). One field: the **location**,
  defaulting to the `~/bobi-agents/` library, with a **Browse…** button.
- **Modify existing** — always available. A **scan-directory field** (default the
  `~/bobi-agents/` library, changeable + Browse) asks which folder holds your
  teams; bobi lists every `agent.yaml`-bearing folder it finds there (the
  folder itself or its children). Pick one to copy into the working location and
  **reverse-fill** the cards from it. Stays enabled even when the default library
  is empty, so you can point the scan elsewhere.
- **From a registry** — lazily lists teams from configured registries, downloads
  the chosen one into the working location, then reverse-fills like modify.

**Location & the folder picker.** A localhost page can't open a native OS folder
dialog, so **Browse…** opens a small **server-side directory lister** (`/api/browse`),
**rooted at the user's home** (the library and most dev repos live there; confined
to home so the page can't list the whole filesystem) and returning absolute paths.
Default source location is `$BOBI_HOME/agents/<name>/src/`; install targets the
same named agent's `run/package/`. Anything outside home can still be typed into
the location field. Source dirs are kept separate from `run/`.

### The one screen — chat + the team as cards
- **Left: the conversation.** A single centered chat. You say what you want in
  your own words; bobi reflects it back (typed out) and asks at most one good
  follow-up. **Contextual quick-add chips** (emitted by the digestion brain, not
  hardcoded) sit by the input as one-tap conversational adds.
- **Right: the team panel.** Five cards fill in and check off live:
  **Goal · Roles · Automations · Connections · Chat.** The panel holds state +
  structured controls + secret inputs — the things a freeform chat shouldn't.
- **Auto-name + rename.** The team's name shows in the panel header and is
  click-to-edit (a ✎ pencil makes it discoverable → `/api/rename`). Renaming a
  team whose source folder is named after it **moves the folder to match** and
  updates the `agent:` field, so the name actually sticks.
- **The Finish gate.** A **Finish →** button appears only once all five are
  gathered — the four spec slots self-scored "enough" plus a chosen chat channel
  (a `N/5 gathered` meter shows progress). Clicking it starts the build.

### Connections — the deliberate exception to the magic
Connect stays explicit because auth is an unavoidable user action and
credential/access grants must be **visible and approved**. Each implied service
is a card; setup classifies it through a **deliberate cascade** (native → venn →
mcp → custom) and handles it accordingly:

- **native** — the framework ships an ingestion adapter (github / slack /
  linear). A direct token captured into `.env`; events arrive by webhook.
- **venn** — reached through the Venn gateway with one shared `VENN_API_KEY`
  ("one key, every service"). Venn-backed services are grouped under a single
  **"Set up Venn"** popup with a per-service live verification badge. Whether a
  service *is* venn-backed is decided against Venn's **real catalog** (see
  below), not a guess. Venn wins ahead of the MCP registry — it's the 1-click
  path.
- **mcp** — a service Venn doesn't cover but that ships a **hosted MCP server**
  (`bobi/setup/mcp_registry.py`, a curated seed of public hosted MCPs).
  Wired straight into the team's `agent.yaml` `mcp_servers:` block, so the agent
  connects at runtime — no authored guide. A static-key server captures one
  `<SVC>_API_KEY` (sent as a `${VAR}` auth header); an OAuth/public server
  captures nothing and reads as "✓ wired" (authorized at first connect). Tagged
  "hosted MCP · 1-click".
- **custom** — neither native, on Venn, nor in the MCP registry (e.g. PostHog).
  bobi captures a service-specific API key (`<SVC>_API_KEY`) **and authors a
  `tools/<svc>.md` usage guide** at build, so the agent knows how to call that
  service's API. This is the "you'll need to build an MCP for this" terminal
  state. Tagged "custom · bobi writes a guide".

> The full ticket cascade (MOD-203) has two further rungs between mcp and
> custom — a **live web search** for a hosted MCP, then a **CLI fallback** —
> deferred to follow-ups. Today the registry is a static seed; a miss falls
> straight through to custom.

**The real Venn catalog (not guessing).** Classification uses the live list of
services Venn supports, sourced **CLI-first**: `bobi/setup/venn_cli.py`
shells out to the canonical `venn` binary (`venn --json help list_servers`),
falling back to the REST client in `bobi/venn.py` when the CLI is absent.
A small static seed covers the pre-key case; the live list (unioned on top when
a `VENN_API_KEY` is present) is authoritative. Connection status for venn
services is verified live against the user's connected servers.

Saved secrets show `✓ saved · Edit · Copy` (Copy reads the value back over a
loopback+nonce endpoint — it already lives in plaintext in `.env`). A no-secret
method (e.g. the GitHub App) is never auto-satisfied, since it can't be verified
locally — only a captured token or a live Venn connection flips a card to
connected.

### Automate — the twin of Connect
*Expose the team's boundary with the world (Connect = what it can reach) and
with time (Automate = when it acts on its own); keep internal structure (roles,
workflows) invisible.* Automate is its own card because **granting initiative is
a trust decision** — proactive behavior should be consciously opted into. Human
framing, not "monitors": *"anything bobi should do on its own?"* Maps to
`monitors/` (description-only checks) + scheduled/triggered workflows.
- **Per-behavior leash:** each item carries **notify** (tells you) / **ask
  first** (proposes, waits) / **act** (does it, reports). (Named **Automate**
  2026-06-14 over "Autopilot", which read as unsupervised; the leash plus the
  plain verb keep "you're in control".)
- **bobi suggests, doesn't just collect.** A dedicated suggestion prompt
  ideates concrete, non-spammy proactive behaviors from the team's intent — a
  "did more than I expected" beat. The user toggles, edits, adds, or skips;
  committing is explicit even when the answer is "nothing".

### Build → file browser → finish
- **Build pour.** Finish kicks off `author → validate → install`. Deterministic
  files (`agent.yaml`, `workflows/adhoc.yaml`, monitors) are written verbatim;
  prose files (`agent.md`, each `ROLE.md`, custom `tools/*.md`) stream
  token-by-token to disk. The file list and `entry_point` are **computed by the
  wizard, never LLM-decided**.
- **The post-build screen IS a built-in file browser.** Not a tiny overlay — the
  whole screen: a success banner, the generated files in a tree, their contents
  in the dark slab (read live from disk), an **Open folder** action (reveals the
  real folder via the OS file manager — works while the local server is alive),
  and **Finish**. If no files are found it says so with the path, instead of a
  silent void.
- **Finish ends cleanly.** `/api/finish` marks the state complete and **stops the
  local setup server**, then the page transitions to a **static completion
  screen** (no server-dependent buttons left to strand the user) with the
  named `start` command. Open-folder/file-browsing happen *before* Finish,
  while the server is alive.

### Modify mode is non-lossy
Editing an existing team must never flatten the work that's already there.
- Open/registry **copy the whole source** into the working location (the
  original is left untouched until install) and **reverse-fill** the cards from
  it; the conversation opens with a **recap of what the team already does**
  rather than a blank greeting.
- At Finish, authoring **edits in place** rather than regenerating from scratch:
  existing prose files go through an *edit* prompt ("make the minimal change,
  preserve everything the spec doesn't touch; return unchanged if it already
  matches"); `agent.yaml` and `monitors` **merge** (union services/monitors by
  name, keep hand-written keys; `agent`/`entry_point` are setup-managed and do
  update); files the manifest never models are left untouched; **nothing is
  deleted** (remove a role by deleting its folder, not via chat).

---

## Customization / policy model
- **The conversation is primary; structure is its output.** The UI guides
  unstructured thoughts into structured artifacts. We do **not** predeclare an
  exhaustive customization schema.
- **The agent is a router.** Its core job is classifying each emergent intent
  into the right pack artifact — invisibly:

  | user says (messy) | routes to | lives in |
  |---|---|---|
  | "watch GitHub, post to Slack" | connector | `tools/` + services |
  | "when an issue lands, triage + assign" | workflow | `workflows/` |
  | "ping me about stale PRs each morning" | monitor (automation) | `monitors/` |
  | "a triage lead and some engineers" | role | `roles/` |

- **Prose-first.** Nuance a dropdown can't hold lives in the authored role/base
  prompts the LLM agents read and obey at runtime. A dedicated `POLICY.md`
  spec slot is **not** implemented today (the spec is the four slots below); it
  folds into goal/roles prose at build. Revisit as structural policy matures.

## The magic is in the digestion prompt
The screens are a calm, stable vessel. The intelligence lives in **one rich,
stateful digestion prompt** that holds the whole conversation and the
spec-in-progress and turns everything the user says into well-formed artifacts:
the routing, pack-format fluency, the taste to ask one good follow-up. You
iterate the prompt, not the screens.

It's a *small family* of prompts: the main **digestion prompt** (routes the
conversation → the four-slot spec, refreshes a rolling summary, self-scores
readiness, emits quick-add chips and the auto-name) plus **specialized
generators** — the **Automate suggester** (ideates proactive behaviors) and the
**file authoring / editing** prompts. The digestion brain is the through-line;
the specialists are tuned for jobs the brain shouldn't do inline.

---

## Architecture (as built)

```
bobi/setup/
├── state.py        # SetupState: stage enum + hard-floor gating + 4-slot spec
│                   #   (goal/roles/autonomous/services) + soft readiness; JSON
│                   #   persistence, source_tree_hash, validated-hash freeze.
├── digestion.py    # the brain: assemble_context() + DIGESTION_SYSTEM_PROMPT,
│                   #   parse → apply_deltas (routes turn into spec, sets name).
├── authoring.py    # build the pack from the spec. compute_manifest (structure),
│                   #   create = author from scratch; open = non-lossy edit/merge;
│                   #   custom services → tools/<svc>.md guides.
├── services.py     # connector catalog + classification (native/venn/mcp/custom),
│                   #   live Venn catalog, pure card-status logic.
├── mcp_registry.py # curated seed of public hosted MCP servers (the cascade's
│                   #   third rung) → agent.yaml mcp_servers: entries.
├── venn_cli.py     # canonical `venn` CLI wrapper: run_venn + list_servers.
├── open_mode.py    # list local + registry teams, copy_into, reverse_fill, recap.
├── actions.py      # deterministic ops: team_source_dir, validate, install,
│                   #   credential save, env helpers, preflight.
├── automate.py     # the Automate suggester (one-shot generative prompt).
├── llm.py          # stateless one-shot streaming (hermetic fake in tests).
└── webui/
    ├── server.py   # FastAPI on 127.0.0.1. def deterministic routes (threadpooled)
    │               #   vs async SSE (message/build). nonce + Host guard.
    └── static/     # vanilla HTML/CSS/JS, no build step (app.js / app.css).
```

**Brain state = stateless one-shot per turn + rolling summary.** No long-lived
SDK session. Each turn is a fresh streaming call fed `spec-so-far +
rolling-summary + last-N raw msgs`; the digestion output emits a refreshed
summary alongside the deltas. Trivial resume, hermetic tests.

**Security.** Loopback bind only; a per-launch **nonce** every `/api` call must
present; a **Host guard** (DNS-rebinding defense). Secrets never enter the LLM
loop — credential values arrive on a dedicated `/api/credential` POST and go
straight to `.env`; a secret-redaction scan scrubs anything secret-shaped a user
pastes into the chat before it reaches the model or the rolling summary.

**TLS.** Venn calls (CLI and REST) verify against the **OS system trust store**
via `truststore`, falling back to certifi — so they work behind corporate
inspecting proxies (Zscaler, etc.) whose root is in the keychain but not certifi.

**Key endpoints.** `/api/intro`,`/api/registry`,`/api/browse`,`/api/start`
(create/open/registry) · `/api/message` (SSE) · `/api/rename` · `/api/connect`,
`/api/credential`(+`/value`),`/api/chat`,`/api/automate` · `/api/advance` ·
`/api/build`(SSE),`/api/validate`,`/api/install` · `/api/files`,`/api/file`,
`/api/reveal` · `/api/finish`.

---

## Open decisions
1. ~~Phosphor accent~~ — **RESOLVED: amber** (2026-06-13).
2. ~~Stage names / is the rail right~~ — **RESOLVED: no rail** (2026-06-15). The
   8-stage rail collapsed into one screen; the stage enum survives only as the
   build/install pipeline.
3. **Command name.** `bobi setup` reads one-shot but the UI is also the
   re-entrant editor — may want a broader command later.
4. ~~source/install proximity~~ — **SUPERSEDED (2026-06-26):** editable source
   and runtime output now live in separate `src/` and `run/` directories under
   the named agent slot.

## Agent UI (runtime dashboard — named `ui` command)

A second, separate surface from the `setup` wizard: a minimal dashboard for a
*running* team. One card per active agent session (manager + workers, read from
the on-disk session registry), and clicking a card opens a chat panel to talk to
that agent directly. The chat is **blocking request/response** — a message goes
out via `inbox.deliver(wait=True)` and the agent's full reply comes back as one
block; there's no token-streaming surface to expose. It **reuses this design
language verbatim**: warm light chrome for the roster + composer, the single dark
CRT slab for the chat transcript (the machine writes in the dark), amber accent,
mono labels, system fonts, no build step.

- **Two run modes, one app.** Local `bobi agent <name> ui` binds `127.0.0.1` + a
  per-launch token and opens a browser, exactly like setup. In-container it's
  **on by default** (the entrypoint sets `BOBI_UI=1`; disable with
  `BOBI_UI=0`) — the manager binds the Fly **6PN** address in a daemon
  thread, and the named `ui` command resolves the app, reads the token off
  the machine, runs `fly proxy`, and opens the browser. Being image behavior
  (not a per-instance flag) means existing instances get it on their next deploy
  — which is what lets the release canary gate on UI reachability.
- **No public ingress.** The Fly box stays dark (no `[http_service]`); 6PN
  reachability via `fly proxy` is the trust boundary, and a token (env
  `BOBI_UI_TOKEN`, else auto-written to `run/state/ui.token`) is
  defense-in-depth. In both modes the browser talks to *localhost*, so the same
  loopback Host guard + token check as setup applies unchanged.

## Unified web app (`bobi app`) — #525

One machine-scoped app over everything above. `bobi app start` runs a
background daemon (state under `$BOBI_HOME/webapp/`, persisted token, default
port 8642) serving a shell with a hash router:

- **`#/` Dashboard** - every agent slot on the machine (running / stopped /
  design-only) with start/stop/open actions. Subsumes the "two homes" problem:
  the setup hub's design library and the runtime roster share this one home.
- **`#/agents/<name>`** - the Agent UI above, as a route; endpoints are
  team-scoped (`/api/agents/<name>/subagents`, `.../chat`) and resolve the
  runtime per request. Chat is submit-then-poll (no held-open request).
- **`#/setup`** - a create-team form that hands off to the full setup app,
  mounted unmodified under `/setup/` (the SPA prefixes URLs with a `{{BASE}}`
  mount prefix; standalone `bobi setup` passes an empty prefix and is
  unchanged). In hosted mode Finish installs, **launches**, and returns to
  `#/agents/<name>` instead of printing a start command.

Same design language verbatim: tokens.css, warm chrome, one dark slab, amber
accent. The standalone `bobi setup` and container `ui` surfaces keep working
during the transition.

## Decisions log
| date | decision | rationale |
|---|---|---|
| 2026-06-13 | Warm light chrome + single dark CRT slab | "author in daylight, machine writes in dark"; approachable + IDE-credible |
| 2026-06-13 | Mono-accented identity, system fonts only | offline constraint; personality from mono/sans contrast |
| 2026-06-13 | Static space-retro in the slab; motion removed | Bobiverse lineage without distraction/kitsch |
| 2026-06-13 | Accent LOCKED: amber phosphor | warmest, cohesive with paper; reserve success/error hues |
| 2026-06-13 | Agent-as-router, invisible in-process; transparency at the end | magic in-process, inspect the finished pack |
| 2026-06-13 | One stateful digestion prompt, not per-field extractors | holds full picture; the prompt is the product |
| 2026-06-13 | Automate before Connect, per-behavior leash, dedicated suggester | capability before housekeeping; defuse "no oversight"; "did more than I expected" |
| 2026-06-14 | Eng-review: 4-slot spec (goal/roles/autonomous/services), route-then-author | conversation guides messy→structured; wizard owns the manifest |
| 2026-06-14 | Eng-review: stateless one-shot per turn + rolling summary | clean def/async split; trivial resume + hermetic tests |
| 2026-06-14 | Eng-review: soft non-blocking readiness; floor = goal non-empty | forgiving north star; certainty signal guides, never walls |
| 2026-06-15 | **Reshape to one screen** (rail deleted) | conversation proposes, panel disposes; special steps → popups; faster, less templated |
| 2026-06-15 | **Open mode landed** (no longer M2): copy + reverse-fill + non-lossy edit-in-place | modify must never flatten existing depth; create and open share the editor |
| 2026-06-15 | **Three-way intro** (create / modify / registry) + server-side folder picker | one on-ramp; localhost can't open a native dialog |
| 2026-06-15 | **Post-build screen = built-in file browser**; Finish → static completion | "view files does nothing" was an empty overlay; finish stops the server, so leave nothing stranded |
| 2026-06-16 | **Real Venn catalog via the `venn` CLI** (CLI-first, REST fallback); non-Venn services → custom + authored `tools/*.md` | stop guessing what Venn supports; give custom services a real usage guide |
| 2026-06-16 | **Auto-name from goal; rename moves the folder + updates `agent:`** | the name has to actually stick on disk |
| 2026-06-16 | **OS system trust store (truststore) for Venn TLS** | works behind Zscaler-style inspecting proxies; certifi alone fails |
| 2026-06-16 | Default team folder `bobi/` everywhere; later superseded by named-agent `src/` + `run/` slots | one consistent, obvious location |
| 2026-06-16 | **Editable source → machine-wide library**; later superseded by `$BOBI_HOME/agents/<name>/src/` | a team isn't tied to where it installs; stop littering the cwd |
| 2026-06-16 | **Modify asks which folder to scan** (`/api/teams`); tab always enabled; folder picker re-rooted at `$HOME`, absolute paths | teams can live anywhere; pick the scan dir even when the library is empty |
| 2026-06-16 | **Server-disconnect overlay** (ping heartbeat + fetch/SSE failure) + **Escape closes popups** | the page must stop pretending to be live when the local server dies |
| 2026-06-16 | **Branding reverted to `bobi`** in the shipping UI; `bobbi` rebrand deferred to the whole-codebase rename | don't ship `bobbi` ahead of the rename; also fixed wrong command/path examples |
| 2026-06-23 | **Runtime Agent UI**: cards per live agent + blocking click-to-chat; reuses the setup design language | a running team had no visual surface; private-via-`fly proxy` keeps the Fly box dark (no public ingress) |
| 2026-06-18 | **Connections cascade gains an `mcp` rung** (native → venn → mcp → custom); hosted MCPs from a static registry wire into `agent.yaml` `mcp_servers:` (MOD-203) | a service Venn doesn't cover often ships a hosted MCP — wire it in directly instead of dropping to a hand-authored guide; live-search + CLI rungs deferred |
```
