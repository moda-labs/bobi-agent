# Design System — bobbi setup

> Source of truth for the `bobbi setup` web UI. Read this before any visual or
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
- **What:** a local web wizard a developer runs (`bobbi setup`, served on
  `127.0.0.1`) to design, build, install, and later edit a portable
  **agent-team package**. The team source lives in a folder the user chooses
  (default `bobbi/`); **Finish** authors the source there and installs a frozen
  image into `.bobbi/` (`.modastack/`).
- **Who:** developers/engineers comfortable in IDEs and terminals. v1 of this
  product was a terminal REPL.
- **Hard constraints:** fully **offline** — system fonts only, no CDN, no web
  fonts; vanilla HTML/CSS/JS, no build step; inline SVG only.
- **Brand:** rebrand from `modastack` to **bobbi** ("team bobbi", "we are
  legion"), after the Bobiverse novels — one engineer's mind forking into a
  legion of autonomous copies, which is the product's pitch. See
  `memory/project_bobbi_rebrand.md`.

## North star
**Surprisingly approachable** — "standing up an agent team was way easier than I
expected." Warmer and more guided than a stark IDE, closer to Linear/Vercel
onboarding, but still credible to developers. Chosen over the "craftable
artifact / workbench" framing — which is why files are *not* a persistent hero
surface during design (they become the hero at the very end, see UX
architecture).

The brand voice (Bob: dry, witty, geeky, reassuring even when things break)
serves the copy layer, not the chrome.

## Experience principles (pacing & feel) — the governing metric
**Shortest time-to-magic wins.** New-tool attention is short; the flow must feel
fun from screen one and reach a delightful, visible result fast. Everything else
is subordinate to this.
- **The intro is a short on-ramp, not a heavy chooser.** Three ways in (create /
  modify / registry — see below) on one calm screen, then you're talking to
  bobbi within seconds. Create needs no name and no decisions up front — bobbi
  names the team for you as you describe it.
- **Three escalating magic beats — front-load the first:** (1) **Design
  reflection** — type a rough line, bobbi instantly reflects back a smart, alive
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
- **Done is a launchpad, not a finish line** — "bobbi's live, change it anytime."
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
disposes:* you talk to bobbi on the left; the team materializes as cards on the
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

- **Create new** — author from scratch. No name field (bobbi auto-names the team
  from the goal as you talk; rename anytime). One field: the **location**,
  defaulting to `bobbi/`, with a **Browse…** button.
- **Modify existing** — pick a local team (any `agents/` or `agent-teams/` folder
  with an `agent.yaml`). Copies it into the working location and **reverse-fills**
  the cards so they show what's already there.
- **From a registry** — lazily lists teams from configured registries, downloads
  the chosen one into the working location, then reverse-fills like modify.

**Location & the folder picker.** A localhost page can't open a native OS folder
dialog, so **Browse…** opens a small **server-side directory lister** (`/api/browse`,
project-scoped) to navigate and pick. Default location is `bobbi/` everywhere —
create lands at `bobbi/`, modify and registry at `bobbi/<team-name>`. Source dirs
are confined out of `.bobbi/`.

### The one screen — chat + the team as cards
- **Left: the conversation.** A single centered chat. You say what you want in
  your own words; bobbi reflects it back (typed out) and asks at most one good
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
is a card; setup classifies it into one of three kinds and handles it
accordingly:

- **native** — the framework ships an ingestion adapter (github / slack /
  linear). A direct token captured into `.env`; events arrive by webhook.
- **venn** — reached through the Venn gateway with one shared `VENN_API_KEY`.
  Venn-backed services are grouped under a single **"Set up Venn"** popup with a
  per-service live verification badge. Whether a service *is* venn-backed is
  decided against Venn's **real catalog** (see below), not a guess.
- **custom** — a service that's neither native nor on Venn (e.g. PostHog). bobbi
  captures a service-specific API key (`<SVC>_API_KEY`) **and authors a
  `tools/<svc>.md` usage guide** at build, so the agent knows how to call that
  service's API. Tagged "custom · bobbi writes a guide" in the card.

**The real Venn catalog (not guessing).** Classification uses the live list of
services Venn supports, sourced **CLI-first**: `modastack/setup/venn_cli.py`
shells out to the canonical `venn` binary (`venn --json help list_servers`),
falling back to the REST client in `modastack/venn.py` when the CLI is absent.
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
framing, not "monitors": *"anything bobbi should do on its own?"* Maps to
`monitors/` (description-only checks) + scheduled/triggered workflows.
- **Per-behavior leash:** each item carries **notify** (tells you) / **ask
  first** (proposes, waits) / **act** (does it, reports). (Named **Automate**
  2026-06-14 over "Autopilot", which read as unsupervised; the leash plus the
  plain verb keep "you're in control".)
- **bobbi suggests, doesn't just collect.** A dedicated suggestion prompt
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
  `bobbi start` command. Open-folder/file-browsing happen *before* Finish, while
  the server is alive.

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
modastack/setup/
├── state.py        # SetupState: stage enum + hard-floor gating + 4-slot spec
│                   #   (goal/roles/autonomous/services) + soft readiness; JSON
│                   #   persistence, source_tree_hash, validated-hash freeze.
├── digestion.py    # the brain: assemble_context() + DIGESTION_SYSTEM_PROMPT,
│                   #   parse → apply_deltas (routes turn into spec, sets name).
├── authoring.py    # build the pack from the spec. compute_manifest (structure),
│                   #   create = author from scratch; open = non-lossy edit/merge;
│                   #   custom services → tools/<svc>.md guides.
├── services.py     # connector catalog + classification (native/venn/custom),
│                   #   live Venn catalog, pure card-status logic.
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
3. **Command name.** `bobbi setup` reads one-shot but the UI is also the
   re-entrant editor — may want a broader command later.
4. **`bobbi/` vs `.bobbi/` proximity.** Source in `bobbi/`, installed image in
   `.bobbi/` — one dotfile apart. Kept deliberately (user call, 2026-06-16) for
   symmetry; watch for confusion.

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
| 2026-06-16 | Default team folder `bobbi/` everywhere; keep `.bobbi/` install target | one consistent, obvious location |
```