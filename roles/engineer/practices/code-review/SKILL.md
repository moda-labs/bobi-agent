# Code Review — Quality Gates

This documents the quality gates required before shipping code.
These are mandatory — do not skip them.

## Mandatory: /review before every PR

Invoke `/review` on your changes before creating a PR. `/review` checks
the diff against the base branch for:
- SQL safety
- LLM trust boundary violations
- Conditional side effects
- Security issues
- Structural problems

Fix everything `/review` finds. This is not optional.

## For bugs: /investigate before fixing

When working on a bug (classified by `/triage`), invoke `/investigate`
before writing any fix. `/investigate` follows the Iron Law: no fixes
without root cause analysis. It will:
- Investigate the issue systematically
- Identify the root cause
- Propose a fix

## For web frontends: /qa

If the project has a web frontend (check for index.html, App.tsx, etc.),
invoke `/qa` to do browser-based QA testing after implementation.

## For specs: triple review

Non-trivial specs should be reviewed by:
1. `/plan-eng-review` — architecture, edge cases, test coverage
2. `/plan-design-review` — UX, design dimensions scored 0-10
3. `/plan-ceo-review` — scope: too narrow? too wide?

Incorporate review feedback into the spec before shipping.

## Tests

- Write tests BEFORE implementation (TDD)
- Run the project's test command before every PR
- The test command is auto-detected from package.json / pyproject.toml / Makefile
