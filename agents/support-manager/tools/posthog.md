# PostHog

PostHog is where most issue signals start: error tracking (exceptions) and
a watchlist of events/insights defined in `workspace/support-context.md`.
PostHog is not a native modastack event source, so signals reach you via
the **PostHog API** (default, below) or, once the event server is public, a
webhook. Both end in the same place — a `triage-issue` run.

## Path B — polling via the PostHog API (default, works today)

The `posthog-watch` monitor fires on an interval. Its check agent queries
PostHog's REST + Query API directly with `curl` (the agent has Bash) and
emits `monitor/support.posthog` only for genuinely new, in-scope signals.

> Why the API and not the PostHog MCP? The REST/Query API needs no MCP
> connection — it is headless/cron-safe and works from a spawned check
> agent with just `curl` and the key. The agent uses it directly.

Credentials are environment variables, declared in `agent.yaml` and loaded
from `.modastack/.env`: `$POSTHOG_API_KEY` (read-only, project-scoped),
`$POSTHOG_HOST` (e.g. `https://us.posthog.com`), `$POSTHOG_PROJECT_ID`.
They are already in the agent's environment:

```bash
PH_KEY="$POSTHOG_API_KEY"; PH_HOST="$POSTHOG_HOST"; PID="$POSTHOG_PROJECT_ID"
```

**Error tracking** (the primary signal) — list issues, then pull each one's
name, status, first/last seen, and occurrence count:

```bash
curl -s "$PH_HOST/api/projects/$PID/error_tracking/issues/?limit=25" \
  -H "Authorization: Bearer $PH_KEY"
```
Each result has `id` (UUID), `name`, `status` (`active`/`resolved`), and
first/last-seen + counts. Construct the issue link for the ticket/log as
`$PH_HOST/project/$PID/error_tracking/<id>`.

**Watchlist / spike detection via HogQL** — the Query API:

```bash
curl -s "$PH_HOST/api/projects/$PID/query/" \
  -H "Authorization: Bearer $PH_KEY" -H "Content-Type: application/json" \
  -d '{"query":{"kind":"HogQLQuery","query":"SELECT properties.$exception_type, count() AS c, max(timestamp) AS last_seen FROM events WHERE event = '\''$exception'\'' AND timestamp > now() - INTERVAL 1 DAY GROUP BY 1 ORDER BY c DESC LIMIT 20"}}'
```
Rows come back under `.results`. Swap the query for any watchlist event in
`workspace/support-context.md` to flag a spike that crosses its threshold.

## Path A — webhook (preferred once the event server is public)

If a PostHog realtime destination is configured to POST to the event
server's generic ingest route (`POST <event-server>/events/posthog`), the
signal arrives as an event on the `posthog` topic. This needs the event
server reachable from PostHog Cloud (deploy the Cloudflare Worker or tunnel
the local one) and the agent subscribed to the `posthog` topic. When live,
pause the polling monitor: `modastack monitors pause posthog-watch`.

## What to pass into triage

For each new signal, hand `triage-issue` the error name/type, the message,
the count and trend, first/last seen, the affected URL or user segment,
and the PostHog issue URL (for the ticket and the log).

## Key rules

- **Respect the ignore list.** `workspace/support-context.md` lists benign
  errors, test/staging/bot traffic, and known noise — do not emit signals
  or file tickets for those.
- **New and material only.** Emit a signal for a genuinely new error or a
  meaningful spike, not for every poll of a long-standing low-rate error
  already in the log.
- **Always include the count and link.** Severity classification depends
  on blast radius; the ticket and log need the source URL.
- A `401`/`403` from the API means the key is missing, expired, or lacks
  `error_tracking:read` / `query:read` (or is scoped to a different
  project). On any auth/transport error, emit nothing (not a false signal)
  and note the gap — do not guess.
