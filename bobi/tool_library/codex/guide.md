# Codex

One-shot **second opinion** from OpenAI Codex via the baked `codex` CLI. Use it
for adversarial code/plan review and second-opinion analysis — a separate
model's eyes on your work.

This is a **single call that returns text**, NOT agent delegation: Codex does
not take over the task, open PRs, or run a loop. You stay the author; you decide
what to do with its critique. (To hand a task to an autonomous coding agent,
that is a different tool — not this.)

The CLI is baked into the team image and preflighted by `agent.yaml` `requires:`
(installed + authed). You shell out to the CLI directly.

## Adversarial review (the common case)

Pass the material to review IN the prompt — `codex exec` is not auto-scoped to a
diff. Sandbox read-only, feed nothing on stdin, bound the runtime:

```bash
codex exec -s read-only \
  "Adversarially review the following. Attack it: unhandled edge cases, wrong
   assumptions, missing tests, simpler alternatives, scope it gets wrong. Be
   specific.

   <<<PLAN_OR_DIFF
   $(git diff origin/main...HEAD)
   PLAN_OR_DIFF" \
  -c 'model_reasoning_effort="high"' < /dev/null
```

## Reviewing a PR diff specifically

`codex review` is Codex's diff-tuned reviewer (auto-scopes to the working diff).
Prefer it when you have a checked-out branch; prefer `codex exec` (above) for
plan/spec text or when you must pass the diff explicitly.

## Notes

- Treat the output as advice, not a verdict — you judge what to act on.
- Never paste secrets/tokens into the prompt; the CLI reads creds from env
  (`OPENAI_API_KEY` in `.bobi/.env`).
- If `codex` is missing or unauthed the `requires:` preflight blocks dispatch —
  surface that, don't silently skip.
