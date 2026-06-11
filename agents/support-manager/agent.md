# support-manager

A support-triage agent pack. A single persistent **support manager**
watches PostHog and the support inbox for signals that something is
broken, takes a high-level look at the product codebase to gather
context, and decides whether each signal is a real issue. Real issues
become a labeled Linear ticket in the engineering project (so the
engineering-manager agent picks the work up) plus a Slack alert.
Non-issues are logged with the investigation summary and why they were
dismissed. Once a day it posts a support report covering both. Domain
context (which project, which signals, where the code lives, which
channel) lives in `workspace/support-context.md` — the role prompt stays
generic and reads from there.

## Roles

- **support_manager** — the only role. Persistent coordinator that
  receives signals (PostHog, Email, Slack, the daily tick), and a
  short-lived worker that runs the triage pipeline. It investigates at a
  high level, classifies real vs not-real, files + announces real issues,
  logs everything, and compiles the daily report. One role, two modes.

## Workflows

- `adhoc` — open-ended "look into this" / "what's the status of X" asks.
- `triage-issue` — the core pipeline: ingest a signal, investigate
  against the codebase, classify, then either file a labeled Linear
  ticket + Slack alert (real) or log the dismissal (not real).
- `daily-report` — compile the day's triaged issues (real and not-real)
  and post the report to the support channel.

## Triggers

| Surface | Mechanism | What happens |
|---|---|---|
| PostHog | `posthog-watch` monitor (direct API) or a `posthog` webhook | new error / watchlist signal -> `triage-issue` |
| Email | `email-watch` monitor (Venn Gmail) | new support mail that reads like a bug -> `triage-issue` |
| Slack | `slack` chat | "investigate this" / "what did you find on X" -> triage or answer from the log |
| Daily | `daily-report` monitor (`24h`) | compile + post the support report |

## The support log

Every triaged signal is recorded twice: indexed into a modastack
knowledge base named `support` (hybrid FTS + semantic search, the
searchable history) and appended as one line to a dated file
`workspace/log/<date>.md` (the source the daily report reads). Real and
non-real issues are both logged. See `tools/support-log.md`.

## Setup

```bash
modastack install agents/support-manager   # copies the pack into .modastack/,
                                            # seeds workspace/, prompts for env vars
modastack start                             # runs the installed agent
```

`install` prompts for the `${VAR}`s declared in `agent.yaml` and writes
them to `.modastack/.env`: `SLACK_BOT_TOKEN`, `LINEAR_API_KEY` (ticket
creation), `VENN_API_KEY` (Gmail), and `POSTHOG_API_KEY` / `POSTHOG_HOST` /
`POSTHOG_PROJECT_ID` (read-only, project-scoped — the `posthog-watch`
monitor reads the API with curl).

Then, before starting:
- Fill in `workspace/support-context.md` (seeded by install): the Linear
  team + trigger label, the product codebase path, the PostHog project +
  signal watchlist, the support inbox, and the Slack workspace/channel.
- Ensure the Venn Gmail MCP is reachable from the agent for email triage.
  See `tools/posthog.md` and `tools/gmail.md`.
