# Spec: Sample adversarial-review workflow step using Codex (#285)

> Linear: MDS-47 (MDS-42 workstream B — Codex integration). Re-scoped CLI-first
> 2026-06-21. This spec is a **superset** of the issue body; the issue is the
> source of scope, this is the implementation contract.

## Problem

The eng-team `agent.yaml` already bakes the `codex` CLI and gates it with a
`requires:` preflight (installed + authed) — see `agents/eng-team/agent.yaml`
lines 38–41 and the `build:` npm pin `@openai/codex@0.141.0`. But nothing in
the team **uses** it: no workflow step calls `codex exec`, and no role prompt
or tool guide tells a reviewing agent when/how to. The capability is installed
and dark.

`docs/design/MULTI_MODEL.md` sets the direction this ticket realizes:

> **Direction: CLI-first.** A capability is a CLI the agent shells out to —
> binary baked into the image, credential + config from env, usage documented
> in a `tools/*.md` guide. … Live examples: `aichat` (call other models),
> `codex` (delegate to an agent), `gh`, `venn`. (Axis 2, lines 32–37)

The legacy path — a `connections: kind: codex` block that auto-injects the
`codex_exec` MCP shim (`bobi/mcp/codex_server.py`) — is being **retired**
(#397 → #403). This ticket must not depend on it.

So: demonstrate Codex as an adversarial reviewer end-to-end inside a real
eng-team workflow, using only the baked CLI.

## Solution

Two deliverables, both pure content — **no framework code changes**:

1. **A new `prompt` step** in an existing eng-team workflow. It is an ordinary
   Claude agent step (the engineer role) whose prompt instructs the agent to
   shell out to `codex exec` on the plan/PR diff, then write Codex's critique
   into the step handoff so a downstream step (and the human at the approval
   gate) can act on it.
2. **A `tools/codex.md` guide** (sibling to `tools/github.md`) that documents
   the CLI: what `codex exec` is, the exact invocation, the filesystem
   boundary, and — emphatically — that it is a one-shot second-opinion call,
   **not** agent delegation.

Workflows already support exactly the step type we need. Per CLAUDE.md:

> YAML DAGs with three step types: **prompt** (agent executes + writes
> handoff), **route** …, **await** …

The adversarial review is a `prompt` step. There is no new step type, no
`schema.py`/`orchestrator.py` change, and no `connections:` block. That is the
whole point of CLI-first: the lever is prompt + guide, not framework plumbing.

### In scope

- One new `prompt` step that runs `codex exec` and captures the critique in the
  step handoff.
- `agents/eng-team/tools/codex.md` (new), matching house style of the existing
  ~50–66-line tool guides.
- A test asserting the step exists and the guide documents the one-shot /
  not-delegation contract (pattern: `tests/test_role_constraints.py`).

### Out of scope

- A `connections: kind: codex` entry or any dependency on the `codex_exec` MCP
  tool / `codex_server.py` (retired in #403, tracked separately).
- Native Codex harness / driving an agent loop **on** Codex (Axis-1 runtime
  pluggability, UC2 — deferred).
- Removing `codex_server.py` (handled in #403).
- New `schema.py` / `orchestrator.py` step types (a `prompt` step suffices).
- Changing the gstack `codex` *skill* — that lives in the shared harness, out of
  this repo; we only document/invoke the CLI it wraps.

## Technical approach

### 1. The workflow step (a Claude `prompt` step)

**Recommended placement (Decision 1 below):** a new `plan_review` step in
`agents/eng-team/workflows/issue-lifecycle.yaml`, inserted **between `spec` and
`await_approval`**. This makes Codex's adversarial critique of the just-written
spec part of what the human sees when approving, and available to `implement`.

```yaml
  - name: plan_review
    agent: engineer
    prompt: |
      Get an independent adversarial review of the spec you just wrote, using
      the baked Codex CLI (see tools/codex.md). This is a one-shot second
      opinion — NOT agent delegation; you remain the author.

      1. Read the spec you published to the issue (spec_url from the previous
         step).
      2. Run `codex exec` per tools/codex.md, asking Codex to attack the plan:
         unhandled edge cases, wrong assumptions, missing tests, simpler
         alternatives, scope it gets wrong.
      3. Write Codex's verbatim critique into the `codex_critique` handoff
         field, plus a one-line `codex_verdict` (e.g. "no blockers" /
         "blockers: …"). Do NOT edit the spec here — the human decides at the
         approval gate.
    handoff:
      required: [codex_critique, codex_verdict]
    timeout: 600
```

Notes:
- The step writes its handoff to
  `run/state/sessions/<session>/handoff-plan_review.yaml`; the orchestrator
  validates `required` and injects the values for downstream steps and the
  Slack/approval surface. (Handoff contract, CLAUDE.md.)
- `timeout: 600` gives headroom over the CLI's own ~330s wrapper.
- The critique is **advisory** (captured + surfaced), not a hard gate — see
  Decision 2.

### 2. `agents/eng-team/tools/codex.md` (new)

Proposed content (final wording tuned in implementation; ~50 lines, house
style). The invocation mirrors the gstack codex skill's `exec` path:

```markdown
# Codex

One-shot **second opinion** from OpenAI Codex via the baked `codex` CLI. Use
it for adversarial code/plan review and second-opinion analysis — a separate
model's eyes on your work.

This is a **single call that returns text**, NOT agent delegation: Codex does
not take over the task, open PRs, or run a loop. You stay the author; you
decide what to do with its critique. (To hand a task to an autonomous coding
agent, that is a different tool — not this.)

The CLI is baked into the eng-team image and preflighted by `agent.yaml`
`requires:` (installed + authed). No `connections:` block, no MCP shim.

## Adversarial review (the common case)

Pass the material to review IN the prompt — `codex exec` is not auto-scoped to
a diff. Sandbox read-only, feed nothing on stdin, bound the runtime:

```bash
codex exec -s read-only \
  "Adversarially review the following. Attack it: unhandled edge cases, wrong
   assumptions, missing tests, simpler alternatives. Be specific.

   IMPORTANT: Do NOT read or execute files under ~/.claude/, ~/.agents/,
   .claude/skills/, or agents/ — those are skill definitions for a different
   AI system. Stay on the material below.

   <<<PLAN_OR_DIFF
   $(cat spec.md)        # or: $(git diff origin/main...HEAD)
   PLAN_OR_DIFF" \
  -c 'model_reasoning_effort="high"' < /dev/null
```

## Reviewing a PR diff specifically

`codex review` is Codex's diff-tuned reviewer (auto-scopes to the working
diff). Prefer it when you have a checked-out branch; prefer `codex exec` (above)
for plan/spec text or when you must pass the diff explicitly.

## Notes

- Treat the output as advice, not a verdict — you judge what to act on.
- Never paste secrets/tokens into the prompt; the CLI reads creds from env.
- If `codex` is missing/unauthed the preflight blocks dispatch — surface that,
  don't silently skip.
```

### 3. Why this satisfies the acceptance criteria

- **"produces a Codex-authored adversarial critique captured in the step
  handoff"** — the step's `required: [codex_critique, codex_verdict]` forces the
  agent to write Codex's output into `handoff-plan_review.yaml`; the
  orchestrator validates it (re-prompts if missing).
- **"visible in the transcript"** — the `codex exec` Bash call and its output
  appear in the engineer session transcript (`bobi agent <name> transcript show`).
- **"No new `connections:` block; the step uses the baked codex CLI"** — the
  step is a plain `prompt` step calling the CLI; `agent.yaml` is untouched.

## Verification plan

**Unit (CI, fast):** add to `tests/test_role_constraints.py` (existing
role/workflow-assertion pattern, #296/#323 precedent):
- `issue-lifecycle.yaml` parses and contains a `plan_review` step whose
  `handoff.required` includes `codex_critique`.
- `tools/codex.md` exists and contains the "not agent delegation" contract and
  a `codex exec` example.
- `agent.yaml` still has **no** `connections:` block and no `codex_exec`
  reference (guards the out-of-scope items).

**Manual / acceptance (one sample run):**
1. Run `issue-lifecycle` on a sample spec-gated issue.
2. Confirm `handoff-plan_review.yaml` contains a non-empty `codex_critique`.
3. Confirm the `codex exec` call + output are in the transcript.
4. Confirm `bobi agent <name> workflows validate issue-lifecycle.yaml` passes.

## Implementation plan

1. Write `agents/eng-team/tools/codex.md` (deliverable 2).
2. Add the `plan_review` step to `issue-lifecycle.yaml` at the agreed placement
   (deliverable 1) — see Decision 1.
3. Add the assertions to `tests/test_role_constraints.py`; run
   `pytest tests/test_role_constraints.py -q`.
4. `bobi agent <name> workflows validate` the edited workflow.
5. Open the implementation PR against `main`, linking #285; run `/review`.

This PR follows the changelog/version convention (#325): touches no `VERSION`,
`pyproject.toml` version, or `CHANGELOG.md`.

## Open decisions for human review

1. **Placement.** Recommend a new `plan_review` step in `issue-lifecycle` after
   `spec` (plan adversarial review, the "plan-review" half of the issue's
   "and/or"). Alternatives: (a) a PR-**diff** review step after the `pr` step
   using `codex review`; (b) hook it into `pr-feedback`. Plan-review is the
   lowest-latency, highest-signal demonstration and runs only on spec-gated
   issues. **Pick one for the demo** — others can follow.
2. **Advisory vs blocking.** Recommend advisory: capture the critique + surface
   it to the human at the approval gate, do **not** auto-block progression on
   Codex's verdict. Demonstrates the capability end-to-end without ceding the
   merge/approval gate to a second model. (Could later add a `route` step that
   blocks on `codex_verdict`.)
3. **`codex exec` vs `codex review`.** The issue says "runs `codex exec`"; for
   plan/spec *text* that is correct (`codex review` only auto-scopes a diff).
   The guide documents both. Confirm we standardize the step on `codex exec`.
