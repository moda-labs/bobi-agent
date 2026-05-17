# modastack

**Luke's authored skill layer on top of [gstack](https://github.com/garrytan/gstack).**

gstack is the instrumented execution engine. modastack is the **control plane
around it** — the front of the funnel, the back, and two specialist bookends.
Together they're one workflow; apart, modastack is a pile of skills. This README
is the spine that makes it a pack.

## Requires

gstack must be installed (`~/.claude/skills/gstack`, team mode). modastack skills
hand off into gstack skills by name and depend on contracts documented below.

## The chaining map

```
first task-shaped message
   └─► /frontdoor ........................ modastack  (the airlock — classify & route)
         ├─ trivial ──────────────► /build ............... modastack
         ├─ bug ──────────────────► /investigate ......... gstack
         ├─ inquiry ──────────────► answer · /office-hours  gstack
         │                              └─► /office-helper  modastack (doc adversary)
         └─ update ───────────────► /office-hours .......... gstack
                  └─► /brand-identity ... modastack ─► /design-shotgun · /design-html  gstack
                  └─► /autoplan · /plan-* gstack ─► /build  modastack ─► /review · /qa · /ship  gstack
```

The four authored skills are the **entry (frontdoor)**, the **exit (build)**,
and two **specialist bookends (brand-identity, office-helper)**. gstack does the
heavy middle.

## Skills

| Skill | v | Role | Lineage |
|---|---|---|---|
| **`/frontdoor`** | 2.0.0 | Task intake & routing — the airlock. Classifies update/inquiry/bug, writes `.context/intake.md`, routes to `/investigate`, `/office-hours`, `/autoplan`, or `/build`. Use on the first task-shaped message of a session. | lean-custom |
| **`/brand-identity`** | 1.0.0 | Brand discovery & visual identity. Founder interview → cross-domain research → 2-3 named visual territories → `/design-shotgun` (locked via `DESIGN.md`) → native type/color/logo sharpen → `BRAND.md` + `DESIGN.md` tokens → `/design-html`. | lean-custom |
| **`/build`** | 1.0.0 | Staff-engineer implementer. Reads the reviewed plan, ships production code. Domain scope-guards (billing primitive, user-flow, prod-schema). Frontdoor-integrated. | lean-custom |
| **`/office-helper`** | 0.1.0 | Adversarial reviewer for `/office-hours` design docs. Attacks weak problem statements, invented demand, magic-step MVPs. | gstack-template ⚠️ *rewrite candidate — see below* |

> ⚠️ **`office-helper` is the lineage outlier.** It uses the gstack template
> shape (`preamble-tier`, gbrain, v0.1.0), unlike the other three (clean
> lean-custom, no gstack scaffolding). It's in the pack by intent but is a
> candidate to be rewritten to house style for consistency.

## gstack hand-off contracts (the auditable seams)

- **`/brand-identity` → `/design-shotgun`** — slug via shared `gstack-slug`
  binary (paths must agree); lock channel is `DESIGN.md` (shotgun's default
  constraint); `$_DESIGN_BRIEF` is the skip-gathering signal. brand-identity
  runtime-detects this contract (`CONTRACT_OK`/`CONTRACT_DRIFTED`) and degrades
  loudly if gstack changes upstream — it does not auto-track gstack.
- **`/frontdoor` → `/build` · `/investigate` · `/office-hours` · `/autoplan`** —
  routes via classification; intake doc at `.context/intake.md`.
- **`/build` → `/review` · `/qa` · `/ship`** (gstack) — suggested next steps.

## Install (gstack-parallel)

```sh
git clone <repo-url> ~/.claude/skills/modastack
cd ~/.claude/skills/modastack && ./setup
```

`./setup` symlinks each skill dir into `~/.claude/skills/<skill>` so Claude Code
discovers them. Re-runnable. Existing real dirs are moved to
`~/.claude/skills-backups/<name>.pre-modastack.bak` — **outside** the scanned
skills dir, so a backup is never itself loaded as a duplicate skill.

## Provenance (first assembly, 2026-05-15)

Skills were gathered from four scattered homes; modastack is now canonical going
forward. Source branches/locations are left untouched (Luke's to clean up):

- `frontdoor` ← branch `gstack-front-door-skill` (worktree `khartoum`)
- `build` ← branch `gstack-front-door-skill` (main's build **+** frontdoor wiring; the superset)
- `brand-identity` ← branch `luke/brand-identity-skill` (commit `5716cb5`)
- `office-helper` ← `~/.claude/skills/office-helper` (global)

Excluded by intent: `frontend-design` (Anthropic, hash-locked), `stripe-*`
(docs.stripe.com, managed) — those stay external, not authored.
