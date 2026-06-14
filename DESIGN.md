# Design System — bobbi setup

> Source of truth for the `bobbi setup` web UI. Read this before any visual or
> UX decision. Live mockups: `docs/design/bobbi-setup-mockup.html` (the generate
> screen, accent + retro switchers) and `docs/design/bobbi-setup-flow.html`
> (clickable stage prototype). Open either in a browser.
>
> **Status:** design direction agreed 2026-06-13. Two picks still open — see
> [Open decisions](#open-decisions). This supersedes the visual/UX assumptions
> in `~/.claude/plans/sleepy-crunching-pnueli.md` (that plan needs a rewrite).

## Product context
- **What:** a local web wizard a developer runs (`bobbi setup`, served on
  `127.0.0.1`) to design, generate, install, and later edit a portable
  **agent-team package**.
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
surface (see UX architecture).

The brand voice (Bob: dry, witty, geeky, reassuring even when things break)
serves the copy layer, not the chrome.

## Experience principles (pacing & feel) — the governing metric
**Shortest time-to-magic wins.** New-tool attention is short; the flow must feel
fun from screen one and reach a delightful, visible result fast. Everything else
is subordinate to this.
- **First screen IS the on-ramp to magic, not a chooser.** For the scratch path,
  collapse **Start → Sketch**: open with a warm, Bob-voiced invitation to just say
  what you want; "start from a template / open one of yours" is a quiet secondary.
  Talking to bobbi within seconds of launch.
- **Three escalating magic beats — front-load the first:** (1) **Sketch
  reflection** — type a rough line, bobbi instantly reflects back a smart, alive
  understanding (~seconds, the early hook); (2) **Autopilot suggestions** — "it
  thought of things I didn't"; (3) **the Build pour** — the payoff. Don't make the
  user climb to (3) to feel anything.
- **Housekeeping never gates magic.** Connect/auth must be **always deferrable**
  ("connect later" ever-present) so OAuth is never a wall between the user and the
  pour. Auth happens once they're hooked, or just before Install.
- **Fun and forgiving.** Rough input is genuinely fine *because the team is alive
  and evolvable* (below) — no pressure to get it perfect up front.

**The team is alive and evolving, not set-once** (the openclaw/hermes feeling).
The product is a living workshop you return to, built on the re-entrant editor
(open/edit any pack = same flow).
- **Done is a launchpad, not a finish line** — "bobbi's live, change it anytime,"
  one-tap re-entry, never terminal.
- **A fast evolve loop for small changes** — open your team, say "also post a
  Friday recap," it adapts; don't re-walk 8 steps for a tweak. The lightweight
  path is the everyday path.

Corollary: time-to-magic is largely a **digestion-prompt latency + taste**
problem (how fast/smart the reflection is) — see the digestion-prompt section.

## Aesthetic direction
- **Warm workshop, not sci-fi lab.** Soft daylight surfaces, precise dark
  tooling, one sharp accent.
- **The signature move: split lighting.** Warm **light** chrome for the guided
  surfaces (stage rail, node panels, conversation); a single **dark** CRT slab
  for the generated artifact (the pack files). *You author in daylight; the
  machine writes in the dark.* The dark slab is the **only** dark surface, which
  is what makes it read as "the artifact" rather than "a theme."
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

**Accent — ONE color (LOCKED: amber, 2026-06-13), used only on:** the current
stage marker, the primary button, the slab top-edge, and the streaming caret.
```css
/* amber phosphor — LOCKED */
--accent:#C8612B; --accent-2:#D86E33; --slab-accent:#E0843F;
/* green — considered, not chosen (classic-terminal but cooler against the paper):
   --accent:#177B52; --accent-2:#1E8E60; --slab-accent:#29A36A; */
```
Amber skews "warning", so amber is **reserved for the brand accent only**
(progress, primary action, active stage, streaming) — success and error states
get their own distinct hues so the accent never sends a mixed signal. Avoid
purple/violet entirely (the generic-AI-builder tell).

## Typography — system fonts only
```css
--font-sans: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
             "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
--font-mono: ui-monospace, "SF Mono", "SFMono-Regular", Menlo, "Cascadia Mono",
             "Segoe UI Mono", Consolas, "Liberation Mono", monospace;
```
**Mono-accented, not mono-led.** Personality comes from the *contrast* between
engraved mono and conversational sans — free with system fonts:
- **Mono** (uppercase, tracked, small): stage labels, file paths, status,
  eyebrows, numerals/counters (`tabular-nums`). The "instrument" voice.
- **Sans** (generous, 15–16px): body, questions, helper copy, chat. The
  conversational voice. This is the *only* sans and it stays quiet.
- **Mono** again for code in the slab.

`system-ui` as the body face is normally the "gave up on typography" tell; here
it's a hard offline constraint, countered by giving mono the identity role.

## Spacing & density
- 8px base. **Density gradient across the panes:** tight rail → breathable
  center/conversation → dense slab. The asymmetry is intentional; uniform
  spacing reads as templated.
- Comfortable in the chrome (the approachable lever), compact in code/tables.
- Card radius 12px; inputs ~40px tall; rail items ~38px.

## Motion
- **Intentional, not decorative.** Signature moment is the **generation "pour"**:
  files stream token-by-token into the slab behind a soft accent block-caret,
  the rail ticks through stages. Short, ease-out, reassuring.
- All motion respects `prefers-reduced-motion`.

## Space-retro layer (static)
A *slight* retro-futurist lean (Bobiverse / Apollo / Nostromo / phosphor CRT),
concentrated where it belongs and **static** — no animation (a sweeping scan
beam and flicker were tried and removed as distracting).
- **The dark slab is the ship's computer:** corner-bracket frames, CRT vignette
  (deep inset shadow), faint static scanlines (~.42 opacity), a glowing accent
  top-edge, and code pulled toward **phosphor** (syntax tinted toward the accent
  — amber-cream or terminal-green).
- **Chrome gets a whisper:** faint instrument grid behind the paper, a `//`
  telemetry prefix on the mono counter, a starfield in the rail footer.
- **No web fonts** — retro is CSS treatment over system mono, never a pixel
  typeface. The light chrome stays clean so "approachable" survives.
- Brand mark: a small inline-SVG satellite/probe.

## Anti-slop (hard no)
Purple/violet gradients, aurora blobs, centered hero composition, 3-column
icon-card grids inside the wizard, gradient CTA buttons, glassmorphism, "AI
sparkle" iconography, oversized empty cards. Left-aligned workflow, real forms
and artifacts on screen, buttons that look like tools.

---

## UX architecture (the state machine)

One state machine, **mode-aware**, not two parallel flows. `Start` sets a mode:

- **Create** (from scratch) — the only path that authors from blank.
- **Open** (edit an existing pack) — *source is just a loader*: a registry/GitHub
  ref, a local pack from a prior session, or the team installed in this project.
  All resolve to a local working copy, then run the same stages. This unifies
  "customize a template", "fetch from GitHub and edit", and "go back and edit the
  team I made last week" into one path. The wizard is therefore also the
  **team editor**, and re-entrant. Edit the *source* (`agents/<name>/`); Install
  re-freezes into `.bobbi/` (the existing `source_tree_hash` reinstall model).

Stages (rail, renamed from the old plan):

| stage | create | open |
|---|---|---|
| **Start** | from scratch | pick a source → fetch to local working copy |
| **Sketch** | you sketch what it does → bobbi designs | bobbi summarizes the pack → "use as-is or customize?" |
| **Autopilot** | bobbi *suggests* proactive behaviors → pick + set leash | confirm the pack's autopilot + add/remove |
| **Connect** | connect the services the sketch + autopilot imply | connect services the pack declares (pre-filled) |
| **Build** | author all files (the "pour") | as-is → auto-pass; customize → regenerate affected files |
| **Review** | review/edit authored files | browse/edit fetched files |
| **Install** | freeze to `.bobbi/` + write `.env` | same |
| **Done** | `bobbi start` + recap | same |

Principles:
- **Stable rail.** Always show all stages; auto-pass rather than hide (e.g. Build
  in "open/as-is" shows "nothing to build — pack is ready").
- **Files appear in exactly two places:** Build (watch, hands-off) and Review
  (opt-in edit). Everywhere else is calm single-column chrome. This follows from
  the approachable north star — no persistent code-editor pane.
- **Magic in-process, transparency at the end.** The conversation stays clean;
  the agent's structuring happens behind the curtain. Trust comes from inspecting
  the finished pack — in the Review UI *or* the raw files in `agents/<name>/`
  (it's just a folder).
- **Connect is the deliberate exception to the magic.** It stays a dedicated step
  because (a) auth is an unavoidable user action, (b) credential/access grants
  must be *visible and approved* — a developer wants "this team gets read/write on
  your GitHub issues" stated plainly (invisible connector grants = the wrong
  magic), and (c) inference is incomplete. So Connect is **inferred → confirmed +
  augmented**: connectors that fell out of the conversation (or the pack's declared
  services, in "open" mode) come **pre-checked**; an explicit catalog lets the user
  add what was missed and remove what they don't want. Each is a card: granted
  scopes + connect/authorize (Venn OAuth or token) + live connection check.
  Ambiguous-but-implied tools ("triage issues", no tracker named) get asked
  conversationally in Sketch when possible, else surface as an explicit pick here.
- **Autopilot is the twin of Connect — the other boundary decision.** Principle:
  *expose the team's boundary with the world (Connect = what it can reach) and with
  time (Autopilot = when it acts on its own); keep internal structure (roles,
  workflows, policies) invisible.* It's an explicit step because **granting
  initiative is a trust decision** — proactive behavior (message you each morning,
  poll an outside source, launch agents on a schedule) should be consciously opted
  into, never sprung on the user — and because people under-specify the proactive
  dimension, so asking unlocks the most distinctive part of the product (bobbi is
  event-driven autonomous agents; this is where the team feels *alive*, not a
  passive responder). Human framing, not "monitors":
  **"Are there things you want bobbi to do on its own, without you asking?"**
  Maps to the existing `monitors/` (scheduled checks) + scheduled/triggered
  workflows. Three design points:
  - **Before Connect, not after.** Autopilot is about *capability/intent* (what the
    team does), which flows from Sketch; Connect is *housekeeping* (plumbing the
    access those capabilities need). Putting it first keeps the intent-shaped steps
    together (Sketch → Autopilot) and makes Connect the *consequence* of intent —
    its connector list is seeded by both Sketch and Autopilot ("you asked for
    morning stale-PR alerts → connect GitHub + Slack").
  - **Per-behavior leash, to defuse the "Autopilot = no oversight" fear.** Each item
    carries an autonomy level: **notify** (bobbi tells you, you act) / **ask first**
    (bobbi proposes, waits for approval) / **act** (bobbi does it, reports). The
    user is always in control of what's on and how much leash each gets. (The name
    "Autopilot" is provisional and a known risk for implying unsupervised autonomy —
    validate in testing; fallbacks: "On its own", "Standing orders".)
  - **bobbi *suggests*, it doesn't just collect.** Autopilot runs a **dedicated LLM
    generation with a specialized prompt** that *ideates* high-value proactive
    behaviors from the team's intent (distinct from the digestion prompt: this is
    generative ideation, not routing). The prompt is tuned to propose concrete,
    genuinely-useful, non-spammy behaviors with sane cadence — "think right" about
    what's worth doing unprompted. The user toggles, edits, adds, or skips. This is
    a big "it did more than I expected" magic moment.
- **Layout adapts per stage** (not a fixed 3-pane shell): single column for
  Start/Sketch/Autopilot/Connect/Install/Done (+ optional brainstorm drawer),
  slab-as-hero for Build, rail|tree|editor for Review.

## Customization / policy model
- **The conversation is primary; structure is its output.** The UI's job is to
  guide unstructured thoughts into structured output. We do **not** predeclare an
  exhaustive customization schema — we can't anticipate everything.
- **`POLICY.md` is the primary artifact:** the agreed preferences in *prose*,
  which the LLM agents read and obey at runtime. Nuance a dropdown can't hold
  ("strict on logic, lenient on style") lives here.
- **`policy.yaml` is at most a generated index / a few seed topics** the pack
  ships as conversation starters ("teams usually tune review strictness, ticket
  flow, merge rules") — priors, not a cage. When bobbi authors a team it jots a
  few seed topics so future edits aren't a blank page.
- **The agent is a router.** Its core job is classifying each emergent intent
  into the right pack artifact — invisibly:

  | user says (messy) | routes to | lives in | actioned at |
  |---|---|---|---|
  | "watch GitHub, post to Slack" | connector | `tools/` + services | Connect |
  | "when an issue lands, triage + assign" | workflow | `workflows/` | Build |
  | "ping me about stale PRs each morning" | monitor | `monitors/` | Build |
  | "a triage lead and some engineers" | role | `roles/` | Build |
  | "strict on logic, easy on style" | policy | `POLICY.md` | Build/Review |

  Blurry cases (e.g. "mark tickets done after deploy" = policy vs monitor vs
  workflow) get bobbi's best call; it's correctable, but at Review, not via a
  live in-process panel.
- **v1 = prose policies** (LLM agents obey `POLICY.md`). **v2 = structural
  policies** that regenerate workflows/monitors. Routing *to* structural is v1
  (the classification always happens); maturing the *authoring quality* is what
  takes time.

## The magic is in the digestion prompt
The screens are a calm, stable vessel. The intelligence lives in **one rich,
stateful digestion prompt** that holds the whole conversation *and* the
pack-in-progress and turns everything the user says into well-formed,
inspectable artifacts: the routing, pack-format fluency, the taste to ask one
good follow-up. This revises the old plan's many narrow per-field
extractors (`InterpretSpec` per node) → a single brain that always sees the full
picture. You iterate the prompt, not the screens.

It's a *small family* of prompts, not literally one: the main **digestion prompt**
(routes the conversation → pack artifacts) plus **specialized generators** with
their own jobs and quality bars — notably the **Autopilot suggester** (ideates
proactive behaviors from intent) and the **file authoring** prompt(s). The
digestion brain is the through-line; the specialists are tuned for tasks the brain
shouldn't do inline.

---

## Implementation handoff

What's still valid from `~/.claude/plans/sleepy-crunching-pnueli.md` (reuse it):
the **engine-reuse** map (`state.py` as-is, `tools.py` bodies → `actions.py`,
`validate`/`venn`/`config` callees), the **HTTP/SSE** design (FastAPI on
`127.0.0.1`, `def` deterministic routes threadpooled vs `async def` SSE), the
**socket→uvicorn** foreground launcher, and the **security** model (nonce + Host
guard, loopback-only, secrets never in the LLM loop). The module layout
(`actions.py`, `services.py`, `llm.py`, `authoring.py`, `webui/`) still holds.

What this doc CHANGES vs that plan (build to these):
- **Adaptive per-stage layout**, not a persistent 3-pane shell. Stages:
  `Start · Sketch · Autopilot · Connect · Build · Review · Install · Done`.
- **One mode-aware machine** (create / open) with a **source resolver** (scratch /
  registry / local pack / installed) → local working copy; the wizard is the
  re-entrant editor. Reuse `registry.py` for fetch.
- **A small prompt family**, not per-node `InterpretSpec` extractors: the stateful
  **digestion prompt** (routes conversation → pack artifacts), the **Autopilot
  suggester** (ideates proactive behaviors), and **file-authoring** prompt(s).
- **Policy:** `POLICY.md` (prose, roles reference it) is primary; `policy.yaml` =
  seed topics / generated index. Build authors both for scratch packs.
- **Autopilot** maps to `monitors/` + scheduled/triggered workflows, with a
  per-behavior leash (notify / ask-first / act).
- **Time-to-magic** governs: first-screen-as-conversation (collapse Start→Sketch
  for scratch), instant Sketch reflection, Connect always deferrable.

Build order (refined post-eng-review 2026-06-14; reflects the locks above):
1. **`actions.py`** — extract the pure deterministic bodies from `tools.py`
   (validate, install, preflight, credential save, env helpers,
   `_resolve_or_fetch`, `_validate_pack`, `SECRET_SHAPES`/`PACK_SLUG`). NO
   stage-gating inside `actions` — gating becomes a server/state concern. Keep
   `tools.py`'s `@tool` wrappers calling the new functions so
   `test_setup_tools.py` stays green; add `test_setup_actions.py`. The old REPL
   machine still runs after this step.
2. **`state.py` rewrite** (lock #2) — new 8-stage `Stage`,
   `can_advance`/`require_stage`/`advance_blocker` for the create machine, the
   4-slot spec fields (`goal`/`roles`/`autonomous`/`services`) + per-slot
   readiness; keep `save/load/clear`, `source_tree_hash`, validated-hash freeze.
   Rewrite `test_setup_state.py`. ⚠️ This is where the old `tools.py`/`repl.py`
   gating diverges (they reference the old enum). Cleanest path: do steps 2→5 as
   one push so the new machine immediately has the web server as its consumer,
   then delete `repl.py`/`tools.py` in step 8 — don't try to keep the dead REPL
   gating green against the new enum.
3. **`services.py`** (Venn/Slack catalog) + **`llm.py`** (stateless one-shot
   streaming per lock #4; hermetic fake-client test).
4. **Digestion prompt + `authoring.py`** — the brain (lock #3): route each turn
   → 4-slot spec deltas + refreshed rolling summary + readiness; Build authors
   files from the spec (wizard computes the manifest). The long pole — prototype
   against real sessions early.
5. **`webui/server.py`** — `build_app`, `serialize_state` (exposes
   `advance_blocker`), SSE `def`/`async def` split, nonce + Host guard,
   foreground socket→uvicorn launcher.
6. **`webui/static/*`** — port from `docs/design/bobbi-setup-flow.html` (the
   adaptive stage machine) + `bobbi-setup-mockup.html` (slab/retro).
7. **Autopilot suggester** prompt (separate one-shot, auto-runs on stage enter)
   + **Connect** cards.
8. **Rewire `cli.py`**, delete `repl.py`/`tools.py` + their tests, adapt
   `tests/integration/test_setup_flow.py` to drive the HTTP API.

Deferred to **M2** (do NOT build in v1): open-mode source resolver +
summarize-existing + regenerate-affected; `POLICY.md` as a first-class spec slot.
Do not re-litigate the six locked decisions above without explicit approval.

Before coding, a fresh `/plan-eng-review` pass against this doc is worth it — the
old plan predates the model.

The design artifacts (`docs/design/bobbi-setup-mockup.html`,
`bobbi-setup-flow.html`) are working vanilla HTML/CSS/JS and are a direct head
start on `webui/static/` — not throwaway.

### Eng-review locks (2026-06-14)

The `/plan-eng-review` pass against this doc settled the build-defining
decisions. These are now binding for the implementation:

- **v1 cut = create-only spine.** Ship all 8 stages for the scratch/create
  path. Defer **open mode** (source resolver, summarize-existing, regenerate-
  affected) to milestone 2 — it reuses the same stages, so it's additive, and
  the build order already lists it last.
- **`state.py`: rewrite the machine, keep the persistence.** Rewrite the
  `Stage` enum to `Start·Sketch·Autopilot·Connect·Build·Review·Install·Done`
  and `can_advance`/`require_stage`/`advance_blocker` to the new gates. Drop
  `INTERVIEW_KEYS`/`REQUIRED_INTERVIEW_KEYS`, `discovery_skipped_reason`, and
  the use-as-is jump (returns with open mode). Keep `save/load/clear`,
  `source_tree_hash`, and the `validated`/`validated_hash` freeze model as-is.
  (The handoff's "state.py as-is" was wrong for the gating; this corrects it.)
- **Digestion contract = 4-slot accumulating spec, route-then-author.** The
  conversation fills a server-owned spec with four slots — `goal`, `roles`,
  `autonomous` (events/monitors), `services` — that accumulates across stages.
  Each turn the digestion prompt **routes** the user's message into slot
  update(s); `SetupState` is the source of truth. At **Build**, the wizard
  computes the file manifest (file list + `entry_point` are computed, never
  LLM-decided) and per-file authoring prompts stream raw md/yaml to disk. LLM
  owns routing + content; the wizard owns structure. (`POLICY.md` is **not** a
  v1 slot — it folds into goal/roles prose at Build; revisit in M2.)
- **Brain state = stateless one-shot per turn + rolling summary.** No
  long-lived SDK session. Each turn is a fresh streaming call fed
  `spec-so-far + rolling-summary + last-N raw msgs`. The digestion output
  emits a **refreshed rolling summary** alongside the deltas (no extra
  summarize round-trip). The context assembler is one tunable function.
- **Readiness = soft, non-blocking, trajectory-based.** Each conversation
  stage is a **sustained multi-turn dialogue**, not a one-shot prompt — the
  stage does not auto-advance; it loops until the user clicks on. The brain
  self-scores each slot (`enough`/`thin`/`empty`) against a rubric (goal: one
  sentence naming what-it-does + outcome; roles: ≥1 named role w/ responsibility;
  autonomous: explicitly confirmed even if "none"; services: each implied
  service named + connected-or-deferred). That signal drives bobbi's one good
  follow-up and a calm "got it ✓ / still fuzzy" cue — it never gates. The only
  hard floor is `goal` non-empty (so Build has something to author).
- **Sketch keeps structure behind the curtain** — a quiet readiness cue at
  most, no live spec panel (per "magic in-process, transparency at the end").
- **Parked (real, not dropped):** redacting secrets a user pastes into the
  freeform Sketch chat before they reach the LLM / rolling summary. The
  `SECRET_SHAPES` scan today only covers generated files; the freeform surface
  is a new credential-leak vector. Decide before Sketch chat ships.

## Open decisions
1. ~~Phosphor accent~~ — **RESOLVED: amber** (2026-06-13). Reserve distinct
   success/error hues since amber is the brand accent.
2. **Step names.** Working set (8): `Start · Sketch · Autopilot · Connect ·
   Build · Review · Install · Done`. Provisional picks to validate: **Sketch**
   (was Describe — wanted something more fun/less dry; alts: Shape, Conjure,
   Craft) and **Autopilot** (the proactive step — fun + on-theme, but risks
   implying unsupervised autonomy; alts: On its own, Standing orders; mitigated
   by the per-behavior leash). Still gut-check `Build` vs `Forge`, `Done` vs
   `Launch`; whether **Review** is its own step; and whether Connect + Autopilot
   stay two steps (current) or merge.
3. **Command name.** `bobbi setup` reads one-shot but the UI is also the
   re-entrant editor — may want a broader command later.

## Decisions log
| date | decision | rationale |
|---|---|---|
| 2026-06-13 | Warm light chrome + single dark CRT slab | "author in daylight, machine writes in dark"; approachable + IDE-credible |
| 2026-06-13 | Mono-accented identity, system fonts only | offline constraint; personality from mono/sans contrast |
| 2026-06-13 | Green vs purple → neither; warm accent (amber/green) | dodge generic-AI purple; serve warm north star |
| 2026-06-13 | Static space-retro in the slab; motion removed | Bobiverse lineage without distraction/kitsch |
| 2026-06-13 | One mode-aware state machine (create/open) | unifies customize/fetch/edit-old-team; shrinks scope |
| 2026-06-13 | Files only at Build + Review | approachable north star; no persistent editor |
| 2026-06-13 | `POLICY.md` (prose) primary; `policy.yaml` = seeds/generated | can't predeclare everything; structure is output |
| 2026-06-13 | Agent-as-router, invisible in-process | magic in-process, inspect at end |
| 2026-06-13 | One stateful digestion prompt, not per-field extractors | holds full picture; the prompt is the product |
| 2026-06-13 | Accent LOCKED: amber phosphor | warmest, cohesive with paper, most distinctive; reserve success/error hues |
| 2026-06-13 | Add proactive step, named **Autopilot**, BEFORE Connect | capability/intent before housekeeping; Connect becomes consequence of intent |
| 2026-06-13 | Per-behavior leash on Autopilot (notify/ask/act) | defuses "Autopilot = no oversight"; user controls how much initiative each gets |
| 2026-06-13 | Autopilot has a dedicated suggestion-generation prompt | ideates proactive behaviors from intent; "did more than I expected" magic |
| 2026-06-13 | Rename Describe → **Sketch** (provisional) | warmer/lower-pressure, contrasts with Build (rough idea → built thing) |
| 2026-06-13 | **Time-to-magic** is the governing metric; fun from screen 1 | short attention; front-load magic, defer housekeeping, keep it alive/evolvable |
| 2026-06-14 | Eng-review: v1 = create-only spine; open mode → M2 | prove time-to-magic on create first; open mode is additive, reuses same stages |
| 2026-06-14 | Eng-review: rewrite `state.py` machine, keep persistence/hash/freeze | stages changed wholesale; gating is the spine — don't map dead gates onto a new machine |
| 2026-06-14 | Eng-review: 4-slot spec (goal/roles/autonomous/services), route-then-author | conversation guides messy→structured; `SetupState` authoritative; wizard owns the manifest |
| 2026-06-14 | Eng-review: stateless one-shot per turn + rolling summary (no live session) | "stateful" satisfied by server-held spec; clean def/async split; trivial resume + hermetic tests |
| 2026-06-14 | Eng-review: soft non-blocking readiness over a multi-turn conversation | forgiving north star; certainty signal guides the follow-up, never walls; floor = goal non-empty |
