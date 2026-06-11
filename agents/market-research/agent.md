# market-research

A market-intelligence agent pack. A persistent **research manager**
coordinates a team of short-lived research workers that monitor the
market, run deep research on demand, pressure-test product ideas, and
maintain a living corpus of findings. Domain context (who you research
for, which topics you watch, which voices matter) lives in a project
file and a knowledge base — the pack itself is generic and retargetable.

## Roles

- **research_manager** — persistent coordinator. Talks to humans on
  Slack, receives research-request tickets from Linear, runs the weekly
  cadence, and owns the research corpus. Dispatches workers and delivers
  results. Never does long-running research itself.

- **topic_researcher** — worker. Deep research on any topic: demand and
  pain signal, key voices, key companies, use cases and wedges, and a
  3/6/12-month forecast. Produces a cited brief and indexes it.

- **landscape_scanner** — worker. Runs the recurring content-landscape
  scan across the standing coverage areas, maps findings onto stable
  themes, tags momentum, and names whitespace. Maintains the landscape
  map in the corpus.

- **pmf_navigator** — worker. Pressure-tests an early product hypothesis
  across four investigations (signal, real-user access, demand tests,
  distribution) and issues a verdict before anything is built.

## Workflows

- `adhoc` — open-ended "look into this" requests with no fixed lifecycle.
- `topic-research` — full deep-research pipeline on a single topic.
- `weekly-landscape` — scheduled content-landscape scan + Slack digest.
- `pmf-check` — the four-function PMF investigation + verdict report.
- `linear-research` — research orchestrated from a Linear ticket, with
  results posted back to the ticket.

## Triggers

| Surface | Mechanism | What happens |
|---|---|---|
| Slack chat | `slack` event source | "look into X" / "validate this idea" → manager dispatches a worker, replies in-thread |
| Weekly cron | `weekly-landscape` monitor (`7d`) | landscape scan → digest posted to Slack |
| Linear ticket | `linear` event source | a research-request ticket → `linear-research` workflow → results commented back |
| RSS | `rss-watch` monitor (poll) | configured feeds polled; relevant new items surfaced to the manager |

## The research corpus

Findings persist in a modastack knowledge base named `research`
(hybrid FTS + semantic search). Domain context — positioning, ICP
topics, the standing themes, and the named-voices hit list — lives in
`workspace/moda-context.md` (project-relative) and is seeded into the KB
on first run. See `tools/research-corpus.md`.

## Setup

```bash
# from the project root
modastack install agents/market-research   # or: modastack install market-research
modastack start
```

Install seeds `workspace/` at the project root from the pack's
templates (existing files are never overwritten).

Prerequisites:
- Fill in `workspace/moda-context.md` with who you research for, the
  topics to watch, and the source hit list.
- Fill in `workspace/feeds.txt` with RSS/source URLs for `rss-watch`.
- (Optional) Configure Playwright / Firecrawl / BrowserMCP restricted to
  the allowed source domains — see `tools/web-research.md`.
