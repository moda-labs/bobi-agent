# Changelog

## 0.2.0 — 2026-05-15

Hardened `/brand-identity` from its first real dogfood (sleeperboard). The skill
produced a strong brand but only because the founder repeatedly *demanded* to
see visuals — the workflow itself was not directive enough. Made the
show→react→iterate loop load-bearing instead of incidental.

**Changed — `/brand-identity` v1.0.0 → v1.1.0**
- New philosophy #7: the founder converges by *seeing*, not reading — forced.
- Phase 3 rebuilt as a loop: `Converge → Comp → React → Iterate → GATE`.
  - **Comp (mandatory, every round):** every territory rendered as a
    self-contained static HTML file with the product's real content (identical
    across comps) and **opened automatically** — never wait to be asked, never
    substitute prose.
  - **React (mandatory):** structured AskUserQuestion forcing like *and*
    dislike on color / type / motif / density, captured verbatim to the
    Decisions Log as the next round's input.
  - **Iterate (hard floor):** no lock GATE offered until ≥2 full
    show→react→re-spin cycles; actively pushes for a 3rd unless keep/kill has
    converged; decisive-founder escape hatch.
- Loop engine is **always dependency-free static HTML**. `/design-shotgun`
  demoted from "the visual exploration" to **optional post-lock enrichment**
  (Phase 4), key-gated and skippable.
- Generator-readiness preflight reframed from a blocking gate to a one-line
  informational note — a missing OpenAI key no longer defers the founder
  seeing the brand or pauses the interview.

**Notes**
- Driven by the sleeperboard dogfood: 8 hand-built comps over 3 organic
  re-spin rounds were what actually produced convergence; this release
  codifies that path so it happens by default, not by founder insistence.

## 0.1.0 — 2026-05-15

First assembly. Compiled four scattered authored skills into one gstack-sibling
expansion pack with a documented chaining map.

**Added**
- `/frontdoor` v2.0.0 — task intake & routing (the airlock) — from branch `gstack-front-door-skill`
- `/brand-identity` v1.0.0 — brand discovery & visual identity — from branch `luke/brand-identity-skill`
- `/build` v1.0.0 — staff-engineer implementer (frontdoor-integrated superset) — from branch `gstack-front-door-skill`
- `/office-helper` v0.1.0 — adversarial design-doc reviewer — from `~/.claude/skills` (global)
- `setup` — gstack-parallel symlink installer (re-runnable, backs up real dirs)
- `pack.json` manifest, `README.md` with the chaining map + gstack hand-off contracts

**Fixed (same-day, pre-adoption)**
- `setup` backed up existing real dirs *in-place* as `<dst>.pre-modastack.bak`
  inside `~/.claude/skills/` — which Claude Code then scanned and loaded as a
  duplicate skill. Backups now go to `~/.claude/skills-backups/` (outside the
  scanned dir). The one backup created on first run (`office-helper`) was
  relocated there; no data lost.

**Notes**
- `office-helper` is the lineage outlier (gstack-template, not lean-custom) —
  flagged as a rewrite-to-house-style candidate.
- Source branches/locations left untouched; modastack is canonical going forward.
- Excluded by intent: `frontend-design`, `stripe-*` (external/managed, not authored).
