# Metrics and routing web view

Status: implemented and verified locally; not deployed or published.

The agent page keeps its state, identity, one runs table, and recovery actions.
A linked `#/agents/<name>/metrics` view adds bounded usage and routing inspection;
the existing run slab gains read-only usage and routing sections. This is new UI
scope, not an unfinished requirement of the September metrics plan.

- [x] Read-only snapshot queries, canonical token selection, cursor pagination,
  policy metadata allowlisting, and explicit unavailable/busy responses.
- [x] Native ES modules, shipped design tokens and fonts, aggregate view and
  run drilldown. No React, CDN, frontend build, or retired phosphor styling.
- [x] Offline token/routing, privacy, WAL concurrency, and browser regression
  tests. Keep spend/runs interfaces and live volumes unchanged.

Routing selects once per fresh session. A policy call can be represented on
multiple turns; count distinct recorded call IDs, not decision rows. Missing
usage is unknown, not zero. Reported and estimated costs remain separate.
The web view does not invoke models, change routing, migrate the database, or
claim measured savings. Hosted runtime support is explicitly unavailable until
its bounded Admin query transport is extended separately.

Verification: 5,806 unit tests passed (11 skipped); 59 webapp Playwright tests
passed with system Chrome and `TZ=UTC`, matching the existing transcript timestamp
expectations. Coverage includes canonical provider aliases, unknown versus zero,
policy confidence/privacy, sticky call counts, WAL snapshots, auth/Host guards,
busy/unavailable responses, filter changes during reads, navigation cancellation,
and preserved transcript/composer behavior. Screenshots were reviewed locally;
QA assets are untracked. Live Slack, providers, and volumes were not accessed.

### 2026-10-13 amendment

Later work on the same branch changed three statements above; the current
contract is `docs/FINE_GRAINED_METRICS.md`.

- The view now invokes models on request: `POST .../metrics/test-run` runs one
  sandbox prompt on a candidate model, and the JEV modal's Test Routing calls
  the saved TypeSafe policy with bounded, redacted text.
- The view changes routing: `POST .../metrics/jev-config` writes the short JEV
  policy to the agent's `run/.env` (mode `0600`), and disabling keeps it as
  `BOBI_METRICS_EXPERIMENT_DISABLED_JSON`.
- The runs table falls back to metrics usage for runs without a ledger total, so
  the runs interface is no longer unchanged.
- The verification counts above are from 2026-10-05; the pull request records
  the current suite results.
