---
name: frontdoor
version: 2.0.0
description: |
  Task intake & routing. Classifies the ask (update / inquiry / bug),
  captures problem + scope + UX decision into a single intake doc at
  `.context/intake.md`, then hands off to the right downstream skill
  (/investigate, /office-hours, /autoplan, or /build). Use on the first
  task-shaped message of a session before any other planning or build
  skill. Proactively suggest it if no skill has been invoked yet.
allowed-tools:
  - Read
  - Write
  - Grep
  - Glob
  - AskUserQuestion
  - Skill
---

# /frontdoor: Task Intake

The airlock between "Luke had a thought" and "an agent is doing real work." You classify, scope, and route. You do not plan, design, or build. Your output is a single intake doc that downstream skills read.

## Step 0 — Fast-path

If the task is obviously trivial (typo, copy tweak, single-file change under ~30 lines, dependency bump, narrow refactor in well-known code) AND has no UX impact, no schema change, no billing touch — say so in one sentence and route directly to `/build`. Skip the rest. Examples: "Fix typo on /pricing", "Bump openai to latest", "Rename helper X".

## Step 1 — Classify

Pick one. AskUserQuestion only if genuinely unclear:

- **Bug** — broken, regressing, or failing in prod. Past-tense.
- **Inquiry** — question or exploration, no code change implied.
- **Update** — new or changed capability. Future-tense.

## Step 2 — Route by classification

- **Bug** → invoke `/investigate`. Remind Luke: start from the failing run ID, log line, or stack trace, not from a DB snapshot (memory `feedback_root_cause_logs_first`). Skip intake doc.
- **Inquiry** — split it:
  - "How does X work / where does Y live" → answer directly in the conversation.
  - "Should we build X / is this worth doing / what should this look like" → invoke `/office-hours`.
  - Skip intake doc either way.
- **Update** → continue.

## Step 3 — Problem & solution read-back

State back, in plain prose:

- The problem this solves, and the user/moment it solves it for
- Your one-sentence read of the proposed solution
- What you think is explicitly OUT of scope

One AskUserQuestion:

- A) Locked — proceed
- B) Refine — Luke edits, you re-state until A
- C) Solution is fuzzy → invoke `/office-hours` and stop

## Step 4 — Scope guards

Run the three HARD STOPs from CLAUDE.md ("Scope guards") against the locked scope: billing, multi-screen flow, schema change. For each that fires, get Luke's plain-prose answer. Capture in the intake doc. /autoplan does not substitute.

## Step 5 — Size verdict (carve before mocking)

Make a judgment from areas touched, screens involved, prod-data risk, integration count. State it:

- **Small** — one cohesive PR, single domain. Continue.
- **Medium** — one PR, multi-domain. Continue.
- **Large** — propose a carve into 2–4 tickets with one-line scopes and suggested order. AskUserQuestion:
  - A) Carve as proposed; proceed with ticket 1
  - B) Different carve-up — Luke describes
  - C) Keep as one ticket (Luke takes the risk)

  Proceed only with the active ticket. Luke creates Linear tickets himself.

## Step 6 — UX decision (on the active ticket)

Does this scope change something the user sees or interacts with?

- No → skip.
- Tiny (small copy / element on an existing page) → note "no mock needed" and skip.
- Yes → AskUserQuestion:
  - A) `/design-shotgun` — explore variants
  - B) `/design-html` — single prototype
  - C) Skip mock; build directly

  If A or B: invoke the chosen skill. After Luke approves a variant or mock, link the path in the intake doc.

## Step 7 — Write intake & hand off

Write `.context/intake.md` (overwrite if present):

```md
# Intake — YYYY-MM-DD — <branch>
Classification: update
Problem: <one sentence>
User & moment: <who, where, when>
In scope: <one sentence>
Out of scope:
  - <bullet>
Size: small | medium | large (carved → ticket 1 of N: "<scope>")
UX decision: none | mock skipped | /design-shotgun → <path> | /design-html → <path>
Scope-guard answers (resolve CLAUDE.md HARD STOPs — downstream skills do NOT need to re-ask):
  Billing primitive: <A one-time / B recurring / N/A>
  User journey: <plain prose or N/A>
  Schema change: <additive/destructive + rollback or N/A>
Next: /autoplan | /build
```

Then route:

- **Small** → tell Luke the intake doc is the plan; suggest `/build` directly. Skip /autoplan.
- **Medium / Large** → tell Luke to draft `.claude/plans/<slug>.md` using the intake doc as the contract, then run `/autoplan`. /autoplan needs a plan file as input; /frontdoor does not produce one — drafting the plan is the next step.

## Contract for downstream skills

When `.context/intake.md` exists, `/investigate`, `/autoplan`, and `/build` should read it first. Scope-guard answers recorded there satisfy the CLAUDE.md HARD STOPs — do not re-ask. `/build`'s Step 0 is wired for this; gstack-owned skills will follow once they grow an integration hook.
