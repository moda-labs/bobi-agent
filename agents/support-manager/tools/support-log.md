# Support log

Every triaged signal — real and not-real — is recorded in two places:

1. **The `support` knowledge base** (hybrid FTS + semantic search): the
   searchable history. Used for dedup ("have we seen this?") and for
   answering "what did you find on X" asks.
2. **A dated file `workspace/log/<YYYY-MM-DD>.md`**: an append-only daily
   log. This is the source the daily report reads, so the report is just a
   formatting pass over the day's lines.

Both, always. The KB is for recall across time; the dated file is for the
report.

## KB commands

```bash
modastack kb create support                  # one-time; safe to re-run
modastack kb add support --text "..."        # index an entry
modastack kb search support "your query"     # hybrid FTS + semantic search
modastack kb info support                     # entry count, stats
```

## Search before you triage

Before investigating, `modastack kb search support "<error or subject>"`.
If we already have a verdict, do not start over — handle the recurrence
per the role prompt's Intake phase.

## Entry conventions

Prefix each KB entry and always include a date (`YYYY-MM-DD`).

| Prefix | Holds |
|---|---|
| `context::` | Product, codebase, PostHog, Linear, Slack config (seeded from `workspace/support-context.md`) |
| `issue::` | A triaged signal: source, summary, investigation, verdict, reason, ticket URL (if real) |
| `report::` | A daily report's date and counts |

`issue::` entry shape (one per triaged signal):

```bash
modastack kb add support --text "issue:: 2026-06-11 — verdict:real — source:posthog
TypeError in checkout.submit() — ~40 users, 120 events since 09:00.
Investigation: app/checkout/submit.ts:88; likely from commit a1b2c3d (added coupon path); medium effort.
Ticket: https://linear.app/.../BAO-142
Source: https://us.posthog.com/.../issues/..."
```

```bash
modastack kb add support --text "issue:: 2026-06-11 — verdict:not_real — source:email
User reported 'can't log in' — was a forgotten password, not a defect.
Investigation: auth path healthy, no error spike in PostHog.
Reason: user error / account question, not a bug."
```

## The dated log file

Append one line per triaged signal to `workspace/log/<today>.md` (create the
file with a `# Support log — <date>` header if it does not exist). Keep
each line greppable and self-contained:

```
- [real] posthog — TypeError in checkout.submit (~40 users) — BAO-142 https://linear.app/.../BAO-142
- [not_real] email — "can't log in" was a forgotten password — user error, not a bug
```

The daily report groups these by `[real]` / `[not_real]`, so the verdict
tag must be the first token.
