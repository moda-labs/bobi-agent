---
name: bobi-design
description: Use this skill to generate well-branded interfaces and assets for Bobi, Moda Labs' open-source framework for agent teams — either for production or throwaway prototypes/mocks. Contains essential design guidelines, colors, type, fonts, assets, and UI kit components for prototyping.
user-invocable: true
---

Read the README.md file within this skill, and explore the other available files.

If creating visual artifacts (slides, mocks, throwaway prototypes, etc), copy assets out and create static HTML files for the user to view. If working on production code, you can copy assets and read the rules here to become an expert in designing with this brand.

If the user invokes this skill without any other guidance, ask them what they want to build or design, ask some questions, and act as an expert designer who outputs HTML artifacts _or_ production code, depending on the need.

## Fast orientation

- `readme.md` — the design guide: sources, content fundamentals, visual foundations, iconography, and the four rules that matter most. **Read this first.**
- `styles.css` — link this single file to inherit every token.
- `components/*/` — each has `<Name>.jsx`, `<Name>.d.ts` (props + adherence rules), `<Name>.prompt.md` (what/when + example).
- `ui_kits/control-plane/index.html` — open this to see the product surface assembled.
- `guidelines/*.card.html` — foundation specimens.

## Non-negotiables

1. Violet (`--bobi-acc`) means **state**: live, enforced, gated, focused. Never decorative.
2. Human approval gates always render with the violet left rail + 4% wash + rotated-square glyph, and always name the workflow and step. Never a toast.
3. Show real config — filenames in `FileChip`, YAML in `CodeCard`, shell in `Terminal`.
4. The Bobi lockup always travels with the "BY MODA LABS ↗" byline.
5. Moda Labs' red `#FD4235` never appears on a Bobi surface.
6. Product chrome is lowercase (page titles, tabs, nav, agent names, files); no uppercase except figure-plate headers and corner marks. Mono is for data (paths, ids, crons, code) — sans for chrome.
7. One easing curve: `cubic-bezier(0.16, 1, 0.3, 1)`. All motion disables under `prefers-reduced-motion`.
8. Bobi's own hand-drawn icon set only — never Lucide, Heroicons, or emoji.

## Where this lives, and how to invoke it

In the `bobi-agent` repo this system lives at `docs/design-system/` — it is a
product artifact, so it sits with the rest of the reference material rather
than inside anyone's tool config. A thin entry point at
`.claude/skills/bobi-design/SKILL.md` points here, so `/bobi-design` still
works and Claude Code loads it when you ask for Bobi UI.

To use it in another project, copy this folder wherever you like and add a
skill entry pointing at it, or drop the whole folder into
`~/.claude/skills/bobi-design/` for every-project availability.

## Using it in a real Next.js app (the modalabswebsite repo)

The components here are plain JSX authored for a bundler-less browser. In the app:

1. **Tokens** — copy `tokens/*.css` in and `@import` them from your global CSS,
   or lift the values into `tailwind.config.ts` (the repo already maps
   `bobi` / `bobiBright` / `void`; `.bobi-app` and `tokens/app.css` are new).
2. **`tokens/app.css` is required for product UI** — it carries every
   `:hover` / `:focus-visible` / `:active` state. Components style layout
   inline and cannot express pseudo-states themselves.
3. **Components** — port each `.jsx` to a typed `.tsx` in
   `src/components/bobi/`. The `.d.ts` beside each one is the props contract;
   the `.prompt.md` explains when to use it and the rules that must hold.
4. **Wrap product routes in `className="bobi-app"`** to pick up the surface
   ramp, elevation scale, and interaction states. Marketing routes keep
   `.bobi-page` and must NOT get `.bobi-app`.
5. **Do not copy `ui_kits/control-plane/component-shim.jsx`** — it is a
   generated bundler-less shim. Import the real components instead.

## Regenerating the kit shim

If you edit a component and want the static kit to reflect it, the shim is
assembled from `components/**/<Name>.jsx` with `import`/`export` stripped and
each file wrapped in an IIFE. Bump the `?v=` query params in
`ui_kits/control-plane/index.html` afterwards or the browser will serve the old
scripts.
