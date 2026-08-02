# Bobi Design System

The design system for **Bobi** — Moda Labs' open-source framework for agent
teams. Bobi is a lightweight CLI framework that runs a *team* of AI agents: a
director triages every webhook, message, and schedule, dispatches the right
agents in parallel, and reports back. It is Apache-2.0 licensed, self-hosted,
and runs on the Claude Code or OpenAI Codex subscription you already pay for.

This system exists so any new Bobi surface — the product UI, docs, a dashboard,
a deck — is visually indistinguishable from the Bobi that already ships.

---

## Sources

Everything here was derived from primary sources, not screenshots:

| Source | What was taken from it |
|---|---|
| `modalabswebsite` (local Next.js repo, buildmoda.ai) | The whole system. Key files: `src/app/bobi/page.tsx`, `src/components/bobi-art.tsx`, `bobi-icons.tsx`, `bobi-chrome.tsx`, `bobi-loop-figure.tsx`, `bobi-setup-walkthrough.tsx`, `bobi-install.tsx`, `src/app/globals.css` (the `.bobi-page` token block + figure machinery), `tailwind.config.ts`, `src/lib/bobi-links.ts` |
| `modalabswebsite/STYLEGUIDE.md` | The parent Moda Labs visual language Bobi inherits |
| `https://github.com/moda-labs/bobi-agent` | The product repo (Apache-2.0). Docs currently point at its README |
| `modalabswebsite/public/blog/`, `public/framer/`, `src/app/bobi/icon.svg` | Imagery + the favicon, copied into `assets/` |

**Bobi is a sub-brand, not a separate brand.** It inherits Moda Labs' palette,
type, spacing, dashed-rule system, and motion wholesale, and changes exactly one
thing: the accent. Moda's signature red `#FD4235` becomes **dusk violet**. Any
surface carrying the Bobi lockup must also carry the **"BY MODA LABS ↗"**
byline — that attribution is a requirement, not a decoration.

---

## Index

| Path | Contents |
|---|---|
| `styles.css` | The entry point. Link this one file; it `@import`s everything below |
| `tokens/` | `fonts` · `colors` · `typography` · `spacing` · `surfaces` · `motion` · `base` |
| `components/brand/` | `MarkProbe`, `BobiLockup`, `Constellation` |
| `components/icons/` | `Icon` (the full hand-drawn set), `GithubGlyph` |
| `components/core/` | `Button`, `Eyebrow`, `FileChip`, `Badge` |
| `components/forms/` | `Input`, `Select`, `Switch` |
| `components/surfaces/` | `CodeCard` (+ `Tok`, `GateLine`), `Terminal` (+ `TermCmd`, `TermOut`), `FigNode` (+ `FigWire`, `FigFlow`), `FileTree` |
| `components/product/` | `SideNav`, `PageHeader`, `Toolbar` (+ `KeyHint`, `IconButton`), `SectionLabel`, `MetricTile`, `StatusBadge`, `EventRow` (+ `EventRowHeader`), `GateApproval` |
| `ui_kits/control-plane/` | Interactive product recreation — queue, approvals, agents, config |
| `guidelines/` | Foundation specimen cards (colors, type, spacing, brand) |
| `assets/` | Favicon, logo PNGs, OG image, risograph imagery |
| `SKILL.md` | Agent-Skills entry point for use in Claude Code |

Each component directory holds `<Name>.jsx`, `<Name>.d.ts` (the props contract
and adherence rules), `<Name>.prompt.md` (what/when + usage), and one specimen
card.

---

## CONTENT FUNDAMENTALS

**The voice is calm, concrete, and technical.** It never sells; it states what
happens. Short declarative sentences. Present tense. Specific nouns over
abstractions — "a Linear ticket lands", not "work items are ingested".

**Person.** Address the reader as **you**; the product is **Bobi** or **it**.
Never "we" in product copy. Never first-person from the agent.

**Casing.** The wordmark is always lowercase `bobi`. In prose it is `Bobi` as a
sentence subject. Everything the system owns — agent names, roles, filenames,
CLI verbs, nav labels — is **lowercase mono**: `director`, `engineer agent`,
`agent.yaml`, `bobi agent my-agent start`. Eyebrows and plate labels are
UPPERCASE mono. Headings are sentence case.

**Numbers are real or absent.** `ENG-142`, `PR #488`, `02:00`, `23:59`,
`50 USD`, `12 tickets resolved`. Never a rounded fake metric, never a percentage
without a source.

**The signature heading move** is a two-clause headline where the second clause
flips to clay:

> One queue. One director. *<span>The whole team.</span>*
> Teams you can *<span>deploy today.</span>*
> Local, your cloud, or *<span>enterprise.</span>*

**The outcome line.** Explanations often close with a violet mono line prefixed
`↳` stating the payoff in one clause: `↳ No per-token bill. Your existing plan
is the engine.` · `↳ Work starts before you ask.` Use it sparingly — one per
step or section.

**Verbs that belong to Bobi:** land, triage, dispatch, run, work, hand off,
report, consolidate, wake, approve, halt. **Words to avoid:** harness (retired
in the rebrand), seamless, effortless, magic, unleash, revolutionize, simply,
just. **No emoji, ever** — the two glyphs the brand permits in copy are `↳` and
`☾` (the nightly cycle), plus the figure glyphs `◇ ▢ ▤ ◆ ✓`.

**Error and empty states** say what happened and what to do, in that order, and
never blame the user: "The run halted at `limit: spend`. Raise `max_usd` or
approve the overage." An empty queue reads "Nothing waiting. The team is
watching 2 monitors." — never "You're all caught up!"

---

## VISUAL FOUNDATIONS

### The aesthetic in one line
**Warm editorial print, wired for machines.** A cream paper canvas, ink
letterforms, terracotta highlights, hand-drawn architect rules — and a single
cool violet that means "the system is doing something, or waiting for you."

### Color
Cream `#FAF7EE` is the canvas; every section is paper or the tan band
`#EAE4D1`. Text is ink `#362E25` for primary, muted brown `#84725B` for
secondary. **Clay `#D67B55`** carries headline highlights, indices, and riso
offsets. **Dusk violet** (`oklch(0.53 0.17 285)`, bright `oklch(0.70 0.14 285)`)
is the accent and is strictly semantic: live, enforced, gated, focused. Warm
near-black `#221C15` is the dark surface (sidebar, footer). The single cool
surface is the terminal `#0F1226` — reserved for real shell output. `sky
#7DAABD` is a rare support tint. Moda's red never appears.

Never apply an `/opacity` modifier to the accent (it is a `var()` string) — use
`color-mix(in srgb, var(--bobi-acc) 5%, transparent)` for washes.

### Type
Three families, one tracking signature. **Geist semibold at -0.03em** for every
heading; **Inter** for body; **Geist Mono** for eyebrows, labels, code,
filenames, timestamps, nav, and status. Mono carries far more weight here than
on a typical marketing site, because the product's nouns are files and events.
The wordmark alone sets tighter, at **-0.045em**. Body is muted at 1.625
line-height with inline emphasis in `ink` + weight 500.

### Spacing & layout
1280px container, 24/40px gutters, sections 80→112px vertical. The rhythm inside
a section is fixed: eyebrow → 20px → heading → 20px → body → 48px → content.
Values are the exact ones in the source and are **not** snapped to a 4/8 grid —
if it says 30px or 12.5px, write that.

### Backgrounds
No gradients as decoration. Four treatments only: **paper**, the **tan band**
(alternating sections), **faded engineering grid-paper** (80px major + 16px
minor lines dissolving downward, at the top of light sections), and **full-bleed
risograph landscape photography** (the dusk sky, the ripple band, the mountain
CTA). Over dark imagery, a 48px paper-line grid at 5% sits above the photo and
below the content. The hero print is flipped vertically so its dark sea reads as
night sky and the ember band sits low on the horizon.

### Borders & corners
A **hairline system**. `#E5DED6` for soft dividers; `rgba(54,46,37,0.15)` for
figure plates and code cards; `rgba(54,46,37,0.12)` for floating nodes;
`rgba(54,46,37,0.25)` for inputs and file chips. The signature rule is **dashed
— 2px on, 4px gap, `rgba(132,114,91,0.55)`**, used to frame and separate like an
architect's drawing.

Radii are low and meaningful: **0** for figure plates, code cards, and terminal
blocks (they are documents); **6px** for buttons, chips, pills, switch tracks,
file rows; **8px** for a floating config card; **10px** for figure nodes and
icon tiles; **12px** for soft cards and the workspace panel. Nothing is a pill,
nothing is fully round except status dots and node circles.

### Shadows
Warm, ink-based, and **event-driven** — never ambient. Floating figure nodes
carry `0 10px 24px rgba(54,46,37,0.07)`; panels `0 14px 34px rgba(54,46,37,0.07)`;
hover adds `0 10px 26px rgba(54,46,37,0.08)`. Copy over imagery gets a text
shadow instead (`0 2px 18px rgba(0,0,0,0.4)` in the hero).

### Transparency & blur
Used in exactly one situation: a **glass chip over photography** —
`rgba(250,247,238,0.08)` fill, `rgba(250,247,238,0.18)` border, `blur(6px)`.
The install pill and the hero's secondary button are the canonical examples.
Never blur over paper.

### Motion
One curve for everything: **`cubic-bezier(0.16, 1, 0.3, 1)`**. Motion is slow,
confident, and diegetic — things *arrive*, wires *draw*, signals *travel a
path*. Nothing bounces or springs. The vocabulary: hero entrance cascade
(translateY 20px, 0.8s, staggered 110/165/220/275ms) · scroll reveal (24px,
1.05s, staggered 80ms per sibling) · node arrival (12px + scale 0.97, 0.6s,
staggered 300ms) · wire draw (pathLength dashoffset, 0.7s) · violet dashed flow
streaming along an active wire · ping rings off live nodes (4.2s) · a signal dot
traveling an `offset-path` (7s) · ambient constellation drift (26s alternate).

**Reduced motion is non-negotiable.** All of the above disables under
`prefers-reduced-motion: reduce`, and motion-only elements (ping rings,
traveling dots, wire flows) are **hidden at rest** rather than frozen — a parked
glowing dot reads as a rendering bug. Entrance animations must also never be the
only thing making content visible.

### Interaction states
**Hover:** links warm from ink to clay; buttons drop to 0.9 opacity (or lighten
the glass fill); cards lift `translateY(-2px)` and gain a soft shadow and a
darker border over 0.3s. **Press:** `scale(0.97)` — the one universal press
signal. **Focus:** always a 2px violet outline with 2px offset. **Selection:**
a tan wash `rgba(234,228,209,0.55)`, never a colored outline. **Disabled:**
0.45 opacity, no color change.

### Cards
Two kinds, and the difference is semantic. A **document** (figure plate, code
card, terminal) is square-cornered, hairline-bordered, and flat, with an
uppercase mono header carrying a right-aligned status word. A **floating object**
(figure node, benefit card, workspace panel) is 10–12px rounded with a warm soft
shadow. Choose by what the thing *is*, not by how it looks.

### Imagery
Warm risograph / halftone landscape prints — mountains, quarries, valleys,
ripples, dusk skies. Earthy: cream, sand, terracotta, sage, slate-blue, with
visible grain. Decorative images take `alt=""`. Illustration is **hand-drawn
line art in the architect language**, never 3D renders, never stock, never
gradient blobs.

---

## ICONOGRAPHY

Bobi has its own hand-drawn icon set, ported verbatim into
`components/icons/Icon.jsx` from the product source (`bobi-icons.tsx`). The rules:

- **24×24 viewBox, 1.5 ink stroke, round caps and joins.** No fills except where
  a glyph is deliberately solid.
- **At most ONE violet detail per glyph** — a dot, a highlighted path, a rotated
  square. This echoes the probe mark's ink-body-plus-violet-dot signature and is
  the reason the set reads as Bobi's.
- **Do not mix in another icon pack.** No Lucide, Heroicons, Feather, Material.
  The hand-drawn stroke personality *is* the brand. If a glyph is missing, draw
  it in this language and add it to `Icon.jsx`.
- **No emoji as iconography**, anywhere.
- **Unicode glyphs are used deliberately and sparingly** as figure notation:
  `◇` event · `▢` agent · `▤` workflow · `◆` gate · `✓` outcome · `☾` nightly
  cycle · `↳` outcome line · `●` live · `↗` external link.
- **The probe mark doubles as the director's icon** — the one place branding and
  iconography intentionally overlap.
- **GitHub is the single third-party mark** and must stay the official filled
  silhouette (`GithubGlyph`), not redrawn as line art.
- On dark surfaces pass `color="currentColor"` — the default ink stroke
  disappears on the void sidebar.

Semantic pairings the product already relies on (reuse, don't re-map):
`ticket`→Linear · `bell`→PagerDuty · `mail`→email · `chat`→Slack ·
`queue`→the event queue · `code`→engineer · `pulse`→SRE/oncall ·
`headset`→support · `checklist`→a workflow · `checkCircle`→shipped ·
`diamond`→awaiting approval · `moon`→the nightly sleep cycle ·
`human`→a human gate · `coin`→cost · `chip`→model choice.

---

## THE ENTERPRISE APP LAYER

The marketing language is deliberately **flat**: one cream canvas, hairlines, no
elevation, sentence-case editorial headings. That reads beautifully at
full-bleed scale and badly on a dense operational screen, where an operator
needs to know instantly what is a surface, what is an object, and what needs
them.

So product UI adds a second layer, scoped to `.bobi-app` so it can never leak
into a marketing page:

**A surface ramp instead of one flat canvas.** The page goes *sunken*
(`#F1ECDE`), cards go warm-white (`#FFFDF8`), and popovers get the one true
white. Cards now lift off the page instead of being outlined on it. Hairlines
lighten (10–11% ink) because elevation is doing the separating.

**A real elevation scale.** `--elev-1` through `--elev-4`, each a 1px warm
contact shadow plus a wide soft ambient. Elevation encodes altitude, not
importance: tiles sit at 1, cards at 2, popovers at 3, modals at 4.

**Chrome in the same family as content.** `SideNav` defaults to
`tone="light"` in product UI — a dense app reads as one calm surface when the
chrome doesn't fight it. `tone="void"` remains for login, demo, and
marketing-adjacent shells where the brand's paper/void pairing should show.

**Lowercase page titles.** `PageHeader` sets its title lowercase in Geist
semibold at 21px. Lowercase matches the wordmark and every other name the system
owns (agents, roles, files, commands), so the chrome sounds like the product;
marketing headings stay sentence case with a clay highlight clause. The two
languages are now told apart by *voice*, not by shouting.

**Group labels outside cards.** `SectionLabel` puts a sentence-case 13.5px
label above a card, outside its border, instead of a tinted header bar inside
it. Cards stay clean; a screen can carry four groups without visual noise.

**Badges as objects.** Every badge and status now carries a soft tint *and* a
matching border, so it reads as a discrete object in a dense table rather than
loose colored text. `pill` + `caps={false}` gives the soft product label
("on the clock", "3 pending"); the default squared caps form is for status.

**Density and alignment.** Table rows tighten to 10px padding, titles to
13.5px, and every column of numbers, ids, and timestamps gets
`.bobi-tnum` (tabular figures). Misaligned digits are the fastest way to make
an enterprise table look unfinished.

**Operator affordances.** `Toolbar` (search grows, icon buttons, one primary
action) with a `KeyHint` chip in the field; breadcrumbs and tab counts in the
header; an agent switcher at the top of the nav.

### The two disciplines that separate this from "AI dev-tool UI"

Both were violated in the first pass and are now enforced in the components.

**1. Mono earns its place on DATA, not chrome.**
The temptation with a CLI-adjacent product is to set the whole app in monospace.
That reads as terminal cosplay and is the single clearest tell of a generated
interface. The line:

| Mono | Sans |
|---|---|
| File paths, filenames | Nav labels |
| Event ids (`ENG-142`), run ids | Page titles, headings |
| Cron expressions, commands, YAML | Buttons, tabs, form labels |
| Source lines (`linear · ENG-142`) | Status words, badges |
| Figure/plate labels, corner marks | Table column headers |
| Version strings, key hints | Metric labels, card copy |

Numbers stay sans but get `.bobi-tnum` (tabular figures). A status is a *label*,
not data — so `StatusBadge` reads "Live", not `LIVE`.

**2. No uppercase in product chrome — titles are lowercase.**
The first pass had five competing caps treatments (page title, column headers,
metric labels, badges, card headers); when everything is emphasized, nothing is.
Product chrome now has none. `PageHeader` sets its title **lowercase**, which is
not a stylistic tic: Bobi's entire vocabulary is lowercase — the wordmark
`bobi`, agent names (`director`, `engineer`), roles, filenames, CLI verbs — so
lowercase chrome speaks in the product's own voice. Tabs match. Column headers,
labels, and badges are sentence case.

Two deliberate uppercase exceptions remain, both inherited from the source rather
than invented: the `CodeCard` / figure-plate header
(`WORKFLOWS/SUPPORT-TRIAGE.YAML`) and the corner plate mark
(`MODA LABS · BOBI`). Those objects are *documents*, not chrome — that uppercase
mono is the architect-plate language from `bobi-setup-walkthrough.tsx`, and it
stays. The *marketing* language likewise keeps its uppercase mono eyebrows; that
is a different surface with a different job.

### Interaction states are not optional

Components style layout inline, but `:hover`, `:focus-visible`, and `:active`
**cannot** be expressed inline — so the first pass had none. Rows didn't respond
to the cursor, keyboard focus was invisible, and buttons faked a press with JS
mouse handlers. A dense app without these reads broken no matter how good the
tokens are.

`tokens/app.css` now owns them: `.bobi-row` (hover, selected rail, a chevron
that fades in), `.bobi-nav-item` (hover + `aria-current`), `.bobi-btn`
(hover/active/disabled), `.bobi-tab` (`aria-selected`), `.bobi-field`
(focus-within lights the whole field, not the bare input), `.bobi-file-row`,
`.bobi-switch`, plus one `:focus-visible` ring for everything. Rows and file
rows are real keyboard targets (tabbable, Enter/Space). **Never reintroduce JS
mouse handlers to fake a state.**

### Terminal states are quiet

`done` and `idle` badges drop their fill entirely — only *active* states carry
color. A table of 40 finished runs should read as calm gray, not a wall of
confetti. Color in a list means "look here".

### Table columns have a content model

Only the event title truncates. Agent and workflow are sized to their real
content and truncate with a `title` tooltip as a fallback. Three ellipses in one
row means the column model is wrong, not the content — fix the widths, don't clip
harder.

*Method note:* this layer was informed by studying a screenshot of Vercel's
dashboard for general UX principles — light chrome, labels outside cards, small
tinted pill badges, a search-first toolbar, keyboard hints. None of Vercel's
distinctive visual design was reproduced; every pattern above is expressed in
Bobi's own palette, type, radii, and shadow language.

---

## The four rules that matter most

1. **Violet is state, not decoration.** Live, enforced, gated, focused. If it
   isn't one of those, it isn't violet — it's clay, ink, or muted.
2. **Gates are sacred.** A human approval step always renders with the violet
   left rail + 4% wash + rotated-square glyph, always names the workflow and
   step that stopped, and is never downgraded to a toast or an auto-dismissing
   dialog.
3. **Config is the interface.** An agent *is* its files. Show real filenames in
   `FileChip`s, real YAML in `CodeCard`s, real shell in `Terminal`s. Don't
   abstract the config behind invented UI metaphors.
4. **The Moda Labs byline travels with the lockup.** Always.

---

## Intentional additions

The source is a marketing site plus a CLI framework — there was **no product UI
to recreate**. These components had no counterpart in the source and were added
so a product can actually be built. Treat them as proposals open for review, not
as settled brand:

| Added | Why |
|---|---|
| `Icon` | A wrapper over the source's ~34 individual icon exports, so callers select by name and get the stroke rules for free. Geometry is unchanged. |
| `Button` | The source styles buttons inline per instance. This consolidates the five real variants that already exist (violet, inverse, tan, glass, bordered). |
| `Input`, `Select`, `Switch` | No form controls exist in the source. Drawn from the system's border, radius, mono, and focus rules. |
| `SideNav` | Product chrome. Extends the marketing site's paper/void relationship to an app shell. |
| `StatusBadge` | The source encodes only live/enforced/waiting. `failed` was needed and borrows Moda's `brick #9E3A28` rather than inventing a red. |
| `EventRow` | The queue list. Column order mirrors the source's own loop: signal → triage → dispatch → report. |
| `GateApproval` | The gate exists in the source as a *concept* (a YAML node, a figure node) but never as an interactive control. This is the highest-risk invention here and the most important one to review. |
| `PageHeader`, `Toolbar`, `SectionLabel`, `MetricTile` | App chrome with no marketing counterpart. See "The enterprise app layer" above for the reasoning behind each. |
| The `.bobi-app` surface ramp + elevation scale | The marketing language has no elevation at all. Dense operational screens need surface hierarchy, so this is additive and scoped. |

## Known gaps

- **Fonts are linked from Google Fonts**, not shipped as binaries — the source
  repo loads them via `next/font/google`, so no local files exist. Substitute
  real binaries if you need offline or licensed hosting.
- **No dark mode for the product surface.** Bobi's dark values are currently
  scoped to chrome (sidebar, footer, terminal, hero). A full dark workspace
  would need a second surface ramp that does not exist yet — the `.bobi-app`
  ramp is light-only.
- **No motion spec for product UI**, only for marketing figures. The transitions
  in the control-plane kit follow the shared easing but were not derived from a
  source implementation.
- **Docs URL is still the repo README** (`bobi-links.ts` flags this as a TODO).
