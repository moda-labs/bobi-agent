---
name: bobi-design
description: Use this skill to generate well-branded interfaces and assets for Bobi, Moda Labs' open-source framework for agent teams — either for production or throwaway prototypes/mocks. Contains essential design guidelines, colors, type, fonts, assets, and UI kit components for prototyping.
user-invocable: true
---

# Bobi design system

**The system itself lives at `docs/design-system/` in this repo, not here.**

This file is only the entry point that makes it invocable as `/bobi-design`.
The assets are a product artifact — the source of truth for anything visual on
any Bobi surface — so they live under `docs/` where anyone can find them,
whether or not they use Claude Code. `.claude/` is tooling config; a design
system is not.

## Start here

1. Read `docs/design-system/README.md` — the design guide: sources, content
   fundamentals, visual foundations, iconography, the enterprise app layer, and
   the four rules that matter most. **Read this first.**
2. Read `docs/design-system/SKILL.md` for the fast orientation and the
   non-negotiables.
3. Then explore what you need:
   - `docs/design-system/styles.css` — link this one file to inherit every token.
   - `docs/design-system/tokens/` — colors, typography, spacing, surfaces, motion.
   - `docs/design-system/components/*/` — each has `<Name>.jsx`, `<Name>.d.ts`
     (props + adherence rules), `<Name>.prompt.md` (what/when + example).
   - `docs/design-system/ui_kits/control-plane/index.html` — open this to see
     the product surface assembled.
   - `docs/design-system/guidelines/*.card.html` — foundation specimens.

## The four rules that matter most

1. **Violet is state, not decoration.** Live, enforced, gated, focused. If it
   isn't one of those, it's clay, ink, or muted.
2. **Gates are sacred.** A human approval step always renders with the violet
   left rail + 4% wash + rotated-square glyph, and always names the workflow
   and step that stopped. Never a toast.
3. **Config is the interface.** Show real filenames, real YAML, real shell.
4. **The Moda Labs byline travels with the lockup.** Always.

Also load-bearing: mono is for **data** (paths, ids, crons, code) and sans for
chrome; product chrome is **lowercase**, with uppercase only on document plate
labels and corner marks; Bobi's own hand-drawn icon set only — never Lucide,
Heroicons, or emoji; Moda Labs' red `#FD4235` never appears on a Bobi surface.

## Working in this repo

Bobi's own web UIs (`bobi setup`, `bobi app`) already consume a vanilla-CSS
port of this system at `bobi/webui_common/static/tokens.css`, with the brand
faces vendored under `bobi/webui_common/static/fonts/`. Those UIs are offline
and build-step-free, so use the ported tokens there rather than the JSX
components. `tests/test_webui_tokens.py` fails if the port drifts from
`docs/design-system/tokens/colors.css`.

For UX (not visual) decisions on `bobi setup`, read `DESIGN.md`.
