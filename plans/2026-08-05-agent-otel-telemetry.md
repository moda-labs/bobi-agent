# Agent-authored OTel telemetry (`bobi agent <name> otel`)

> **Status:** Draft
> **Tracking issue:** moda-labs/bobi-agent#976 · **Created:** 2026-08-05 · **Last amended:** — (see Amendments)
>
> Markers: `[ ]` idle · `[wip]` in progress · `[x]` done · `[f]` failed/blocked (always with a note)

## Purpose

Give an agent a CLI affordance for recording **arbitrary telemetry of its own
choosing** — a count, a duration, a structured observation — to any OTLP
endpoint (Grafana Cloud, an OpenTelemetry Collector, Honeycomb), so operators
can dashboard and alert on what their agents actually do. The agent decides
what is worth recording; bobi supplies the wire format and, critically, the
identity labels the agent could not supply for itself.

The 10x version of this — auto-instrumenting bobi's whole runtime (every
session, tool call, and workflow step) as OTel traces — is explicitly **out of
scope and rejected here**. That is a different feature with a different risk
profile (it changes hot paths rather than adding a leaf command), and it does
not subsume this one: auto-instrumentation can only emit what the framework
knows, never what the agent judges to be significant. This plan is the
agent-judgement path, and it is deliberately a leaf.

## Problem

Bobi has **no path from an agent's own judgement to an external observability
system**, and no telemetry export of any kind.

- Observability is greenfield. A grep for
  `otel|opentelemetry|prometheus|statsd|grafana|otlp|datadog` across `*.py`,
  `*.ts`, `*.toml`, `*.md`, and `*.json` (excluding `node_modules`, `.venv`)
  returns **zero** matches. There is no exporter, no metrics registry, and no
  collector configuration to hook into.
- The one module named for telemetry is unrelated.
  `bobi/supervisor/telemetry.py:32-33` publishes to `fleet/heartbeat` and
  `fleet/lifecycle` — bubble-signed POSTs onto bobi's **own** event bus, for
  fleet supervision. It is neither metrics-shaped nor OTLP-shaped, and nothing
  external can consume it.
- An agent that wants to record "I processed 42 tickets" today has two bad
  options: hand-roll a `curl` with correct OTLP encoding inside a prompt, or
  drop the observation into a transcript nobody queries. The first is where
  the OTLP/JSON encoding traps bite (64-bit integers must be JSON **strings**;
  trace/span ids are **hex**, a deliberate deviation from proto3 JSON's
  base64) — silently rejected payloads, in a shell string, authored by a
  language model.
- Even if an agent got the encoding right, it **cannot label the data**. The
  fleet identity that makes telemetry sliceable — fleet, instance, machine,
  region, node, platform — is resolved by
  `bobi/supervisor/identity.py:60-91`, from env vars the agent has no reliable
  way to read and normalize itself.

That last point is why this belongs in bobi rather than in a prompt snippet: a
generic `otel-cli` would emit unlabelled numbers.

## Solution

A new runtime-scoped click group, `bobi agent <name> otel`, with three
commands, backed by a small new `bobi/otel/` package that builds OTLP protobuf
payloads and POSTs them through the existing pooled HTTP client.

```
bobi agent <n> otel metric <name> <value> [--kind counter|gauge|histogram]
                                          [--temporality delta|cumulative]
                                          [--attr k=v]... [--unit s] [--desc "..."]
bobi agent <n> otel log <body> [--severity debug|info|warn|error|fatal] [--attr k=v]...
bobi agent <n> otel check [--send]
```

**Shape of the change.** Additive throughout: one new package, one new command
group registered through the existing re-parent lists, one new optional extra.
The only edits to existing files are registration lines and documentation —
no hot path, no load-bearing logic, nothing refactored.

**Integration seams touched** (four, all stable extension points):

1. `bobi/cli.py` — the group registers via the existing `_group_name`
   re-parent list at `bobi/cli.py:3579` plus the pop list at `cli.py:3586-3590`
   (`otel` is a *group*, not a plain command, so it uses the group list).
2. `bobi/http.py` — outbound POST through the shared pooled `httpx.Client`, as
   all framework HTTP is required to be.
3. `bobi/supervisor/identity.py` — `resolve_deployment_identity()` is **reused
   as-is** to populate resource attributes. Not copied, not reimplemented.
4. `bobi/tool_library/` — a new catalog entry distributes the guide and pins
   the extra, exactly as `venn`/`codex`/`gstack` do.

**Resource attributes**, auto-stamped on every emission, mapped onto OTel
semantic conventions where one exists:

| Attribute | Source |
|---|---|
| `service.name` | `OTEL_SERVICE_NAME`, default `bobi` |
| `service.version` | `bobi.__version__.__version__` |
| `service.instance.id` | `resolve_deployment_identity()["instance"]` |
| `bobi.fleet` | `…["fleet"]` |
| `bobi.agent` | the `<name>` argument of the `agent` group |
| `bobi.team` | `agent` key from the installed `package/agent.yaml` |
| `host.id` / `cloud.region` / `k8s.node.name` | `…["machine"]` / `…["region"]` / `…["node"]` — omitted when null |

There is deliberately **no session or run id**: verified this session, no such
value exists in the environment. `bobi/env.py:125-165` `child_agent_env()` sets
only `BOBI_ROOT` and `BOBI_BRAIN` (and strips `BOBI_LAUNCH_LINEAGE`), and a
grep for `BOBI_SESSION|BOBI_RUN` returns nothing. Agents that want run
correlation pass it explicitly with `--attr`.

**Configuration** is spec-standard env vars only — no `Config` dataclass
change, no `agent.yaml` schema change:

```
OTEL_EXPORTER_OTLP_ENDPOINT   # base, e.g. https://otlp-gateway-<zone>.grafana.net/otlp
OTEL_EXPORTER_OTLP_HEADERS    # e.g. Authorization=Basic <base64>
OTEL_SERVICE_NAME             # optional, defaults to "bobi"
```

Read from `run/.env` with process env winning, which is the precedence already
implemented at `bobi/config.py:98-111` and propagated to every spawned agent by
`child_agent_env()`. These are the variable names every OTel toolchain and
Grafana Cloud's own documentation already use, so an operator who has
configured OTel anywhere configures this with no bobi-specific knowledge.

**Failure behavior**: one POST, short timeout, no retry, no disk spool. On
transport or non-2xx failure, write a diagnostic to stderr and `SystemExit(1)`
so the *agent* observes the failure and can react — the same contract as
`bobi agent <n> events publish` (`bobi/cli.py:2040-2049`). An **unconfigured**
endpoint is a distinct, clean error naming `OTEL_EXPORTER_OTLP_ENDPOINT`, so a
box without observability configured does not look broken.

**Distribution is opt-in per team**, via a `bobi/tool_library/otel/` catalog
entry (guide + `install:` pinning the extra) plus a section in
`skills/bobi.md`. It is deliberately **not** added to `bobi/prompts/base.md`:
that file is injected into every agent on every box, and most boxes have no
OTLP endpoint, so advertising there would put a reliably-failing tool call in
front of the whole fleet.

### Alternatives considered

- **`opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-http`** — lost on
  cost. Measured this session: ~150ms wall clock to import against a ~20ms
  bare-interpreter floor, and 15 packages. `bobi --version` is ~120ms today,
  so this roughly doubles cold start on a command an agent may call several
  times per turn. Its provider/batching model also assumes a long-lived
  process, which a one-shot CLI is not.
- **Hand-rolled OTLP/JSON, zero dependencies** — lost on both axes it was
  supposed to win. The OTLP spec makes binary protobuf **MUST**-support for
  OTLP/HTTP and JSON only **MAY**-support, so JSON is the optional half and
  risks silent incompatibility with a collector we do not control. And it is
  not faster: bobi already imports `httpx` (~90ms, core dep) on any networked
  command, against which protobuf encoding measured ~10ms marginal with one
  transitive dependency (`protobuf`).
- **Piping bobi's internal event bus to OTel** — a different feature. Rejected
  in scoping discussion 2026-08-05: the request is for agent-authored
  arbitrary information, not for exporting framework events.
- **Advertising in `bobi/prompts/base.md`** — rejected as above; failing tool
  calls fleet-wide on unconfigured boxes.
- **Spooling failed exports to disk with retry** — rejected 2026-08-05.
  Would introduce durable state (`fsutil.atomic_write_json` + `file_lock` per
  the repo's durability rules), a drain path, spool growth bounds, and a
  corrupt-spool failure mode, to protect best-effort telemetry. Loud failure
  puts the decision in the agent's hands instead.

## Relevant files

### Existing (verified 2026-08-05)

- `bobi/cli.py` — click command tree. `otel` group registers in the
  `_group_name` re-parent list (`:3579`) and the pop list (`:3586-3590`).
  `events publish` (`:2030-2049`) is the shape to copy: validate, lazy-import,
  POST, `SystemExit(1)`; helpers `_detect_project_root()` (`:73-88`) and the
  stdin-or-flag payload idiom (`:1976-1993`).
- `bobi/supervisor/identity.py:60-91` — `resolve_deployment_identity()`,
  returns `{fleet, instance, platform, machine, region, node}`,
  orchestrator-neutral, enrichment fields null when unknown. Reused as-is.
- `bobi/http.py` — the pooled `httpx.Client` all framework HTTP must use.
- `bobi/config.py:98-111` — `project_env()`, the process-env-over-`.env`
  precedence this feature relies on.
- `bobi/env.py:125-165` — `child_agent_env()`; why `OTEL_*` in `run/.env`
  reaches spawned agents with no plumbing.
- `bobi/__version__.py` — `service.version` source.
- `pyproject.toml:42-46` — the `kb` extra, the pattern the `[otel]` extra
  copies.
- `bobi/kb/store.py:208-219` — the ImportError-naming-the-pip-command idiom
  for a missing extra.
- `bobi/tool_library/venn/{tool.yaml,guide.md}` — catalog entry shape
  (`success:` / `why:` / `fix:` / `install:`).
- `tests/test_cli.py:59-64` — `test_agent_help_lists_runtime_commands`; needs
  `otel` added to its asserted list.
- `tests/test_kb_cli.py:1-38` — the extra-gated CLI test template
  (`pytestmark` skipif on `importlib.util.find_spec`, isolated `BOBI_HOME`,
  `paths.bind_root(None)` bracket).
- `skills/bobi.md` — CLI reference an agent can pull with `bobi skill`.
- `CLAUDE.md` — Reference Docs list; gains a `docs/OTEL.md` line.

### New

- `bobi/otel/__init__.py` — public surface (`export_metric`, `export_log`,
  `resolve_config`). Exists so the CLI layer imports one stable name and the
  encoding stays swappable.
- `bobi/otel/config.py` — resolve endpoint/headers/service name from env;
  distinguish *unconfigured* from *misconfigured*. Separate from `export.py`
  because `otel check` needs it without sending anything.
- `bobi/otel/resource.py` — build the OTel `Resource` attribute set from
  `resolve_deployment_identity()` + agent/team. Separate so it is unit-testable
  without a network or a protobuf round-trip.
- `bobi/otel/export.py` — build `ResourceMetrics` / `ResourceLogs` protobuf and
  POST via `bobi/http.py`. No existing module can absorb this: `events/publish`
  speaks bobi's signed-envelope protocol to bobi's own bus, a different wire
  format to a different destination.
- `docs/OTEL.md` — what the command emits, how to point it at Grafana Cloud or
  a collector, the resource-attribute table, and the delta-temporality
  consequence.
- `bobi/tool_library/otel/{tool.yaml,guide.md}` — opt-in distribution.
- `tests/test_otel_export.py`, `tests/test_otel_cli.py` — see Proof of work.

## Questionables

- **Q1:** What metric temporality does a stateless CLI emit, and what does
  that cost downstream? A one-shot process cannot maintain a running total,
  and the disk spool that could was rejected — so **delta** is forced for the
  self-contained case. But Prometheus-native backends (Mimir, and therefore
  Grafana Cloud's metrics tier) are cumulative-native, and delta ingestion
  may require a collector running `deltatocumulative` in the path.
  Options: (a) delta only, and document that a Prometheus-native backend may
  need a collector hop; (b) `--temporality delta|cumulative`, default delta,
  letting an agent that genuinely knows its running total ("42 tickets today")
  declare it cumulative and hit Mimir directly; (c) reopen the no-spool
  decision so bobi can accumulate. Recommendation: **(b)** — it is a few lines
  over (a), and it is the difference between "works against Grafana Cloud
  directly" and "requires a collector", which is exactly the deployment most
  users will try first. (c) reverses a decision made deliberately.

- **Q2:** What is the acceptance bar for wire-format correctness? Options:
  (a) unit tests that decode the emitted bytes back with `opentelemetry-proto`
  and assert structure — hermetic, fast, no new CI infrastructure, but proves
  only self-consistency (a payload we encode and decode with the same library
  can still be one a real collector rejects); (b) additionally an integration
  test that POSTs to a real `otel/opentelemetry-collector` container with a
  debug exporter and asserts the collector accepted and rendered it — proves
  the actual claim, at the cost of a container in the test path;
  (c) additionally a live-lane canary against real Grafana Cloud, following
  the `ci:live` pattern. Recommendation: **(b)** — the entire risk of this
  feature is "does a real collector accept our bytes", which (a) structurally
  cannot answer. (c) adds a credential and a spend lane for a format that does
  not drift once correct.

- **Q3:** Where should the identity helper live, given `bobi/otel/` would
  import from `bobi/supervisor/`? Options: (a) import
  `bobi.supervisor.identity` in place — zero edits to existing code, but a
  leaf CLI feature now depends on the sidecar package for a function that is
  not sidecar-specific; (b) promote it to `bobi/identity.py` and re-export
  from `bobi/supervisor/identity.py` for compatibility — cleaner layering,
  but edits a module the supervisor's heartbeat depends on. Recommendation:
  **(a)** — the module is a pure env resolver with no supervisor imports, the
  dependency is one function, and (b) touches load-bearing sidecar code to
  buy tidiness. Revisit if a third consumer appears.

## Phases

### Phase 1 — Exporter core

- [ ] Add the `otel` extra to `pyproject.toml` (`opentelemetry-proto`),
      following the `kb` block at `:42-46`.
- [ ] `bobi/otel/config.py`: resolve endpoint / headers / service name from
      env via the `project_env()` precedence; parse `OTEL_EXPORTER_OTLP_HEADERS`
      (`k=v,k=v`); return a typed result distinguishing unconfigured from
      malformed.
- [ ] `bobi/otel/resource.py`: build resource attributes from
      `resolve_deployment_identity()` plus agent name and team; omit null
      enrichment fields rather than emitting empty strings.
- [ ] `bobi/otel/export.py`: build `ResourceMetrics` (counter / gauge /
      histogram, temporality per Q1) and `ResourceLogs` protobuf; POST via
      `bobi/http.py` to `<endpoint>/v1/metrics` and `<endpoint>/v1/logs` with
      `Content-Type: application/x-protobuf`; short timeout; raise a typed
      error on non-2xx carrying status and body excerpt.
- [ ] Guard the missing extra with the `bobi/kb/store.py:208-219` idiom,
      naming `pip install 'bobi[otel]'`.

**Validation gate** — do not exit this phase until every line passes; if a
command fails, fix the cause and re-run.

- [ ] `pytest tests/test_otel_export.py -q` green
- [ ] Emitted bytes decode back with `opentelemetry-proto` and carry every
      expected resource attribute, with null enrichment fields absent
- [ ] With the extra uninstalled, the import guard raises the message naming
      `pip install 'bobi[otel]'` (not an opaque `ModuleNotFoundError`)
- [ ] `pip install -e ".[dev,kb]"` (no `otel`) still imports `bobi.cli` cleanly

### Phase 2 — CLI surface

- [ ] `otel` click group + `metric`, `log`, `check` commands in `bobi/cli.py`,
      following the `events publish` shape: validate args, lazy-import
      `bobi.otel`, act, `SystemExit(1)` on failure.
- [ ] Register in the `_group_name` re-parent list (`:3579`) and the pop list
      (`:3586-3590`).
- [ ] `--attr k=v` repeatable, parsed into string attributes; reject malformed
      pairs with `click.UsageError`.
- [ ] Unconfigured endpoint produces a distinct, actionable error naming
      `OTEL_EXPORTER_OTLP_ENDPOINT` — not a stack trace, not a generic failure.
- [ ] `otel check`: validate config, resolve and print the resource attribute
      set, report reachability; `--send` emits one test datapoint.
- [ ] Docstrings end with the `Usage:` block convention every other command
      uses — this is what the agent reads via `--help`.
- [ ] Add `otel` to the asserted list in `tests/test_cli.py:59-64`.

**Validation gate**

- [ ] `pytest tests/test_otel_cli.py tests/test_cli.py -q` green
- [ ] `bobi agent <n> otel --help` lists all three commands with `Usage:` blocks
- [ ] `bobi agent <n> otel metric x 1` with no endpoint configured exits
      non-zero naming the env var
- [ ] `bobi agent <n> otel metric x 1` against a stub HTTP server that returns
      500 exits non-zero and writes the status to stderr

### Phase 3 — Distribution and docs

- [ ] `bobi/tool_library/otel/tool.yaml` — `success:` probes the extra
      (`python -c "import opentelemetry.proto"`), `fix:`/`install:` pin
      `bobi[otel]`, `why:` points at `tools/otel.md`.
- [ ] `bobi/tool_library/otel/guide.md` — the agent-facing guide: when to emit
      a metric vs a log, the `--attr` convention, and that failures are loud.
- [ ] `docs/OTEL.md` — operator-facing: env vars, Grafana Cloud and collector
      setup, resource-attribute table, the Q1 temporality consequence.
- [ ] `skills/bobi.md` — add the command group to the CLI reference.
- [ ] `CLAUDE.md` — add the `docs/OTEL.md` line to Reference Docs.
- [ ] Confirm no `bobi/prompts/base.md` change (opt-in by design; stated so
      the next reader knows it was a decision, not an oversight).

**Validation gate**

- [ ] `pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q` green
- [ ] `bobi skill bobi` output contains the `otel` section
- [ ] A team declaring `tool_library: [otel]` expands to a `tools/otel.md` in
      its installed package image

### Phase 4 — End-to-end verification

- [ ] Verify against a real collector per the Q2 decision.
- [ ] Manual run in an isolated `BOBI_HOME` against a real installed agent:
      emit a counter and a log, confirm both arrive with correct identity
      attributes.
- [ ] Confirm the marginal cold-start cost against the ~120ms `bobi --version`
      baseline measured 2026-08-05; record the number in the PR.

**Validation gate**

- [ ] Full suite green: `pytest tests/ --ignore=tests/e2e/ -q`
- [ ] A real collector accepts and renders both signals with the expected
      resource attributes (per Q2)
- [ ] Cold-start delta recorded and within ~20ms of baseline

## Proof of work

This is a **new-behavior** unit, so the proof idiom is plain assertion of the
new contract, not a failing-test-first reproduction (nothing is broken today —
verified: zero telemetry code exists).

- `tests/test_otel_export.py` — envelope construction and encoding.
  Round-trips emitted bytes back through `opentelemetry-proto` and asserts
  structure, resource attributes, temporality, and the omit-nulls rule.
  Transport is stubbed; no network.
- `tests/test_otel_cli.py` — command surface, following `tests/test_kb_cli.py`:
  `pytestmark` skipif on `importlib.util.find_spec("opentelemetry")`, isolated
  `BOBI_HOME`, `paths.bind_root(None)` bracket, `CliRunner`. Covers arg
  validation, the unconfigured-endpoint error, the export-failure exit code,
  and `--attr` parsing. Mocks at `bobi.otel.export` — which works precisely
  because the CLI imports it lazily inside the handler.
- `tests/test_cli.py` — must stay green with `otel` added to
  `test_agent_help_lists_runtime_commands`, and `otel` must stay **absent**
  from `test_top_level_help_is_machine_scoped`.
- Suites that must stay green: `pytest tests/ --ignore=tests/integration/
  --ignore=tests/e2e/ --timeout=30 -q`, plus
  `tests/test_import_boundaries.py` (a new `bobi/` package must land on the
  correct side of the public/private allowlists).
- Integration proof per **Q2**.

**Real-Claude e2e: not required.** Per `CLAUDE.md`'s judgement call, a
brain-agnostic change is proven by the deterministic path and does not need a
Claude leg. Nothing here touches session orchestration, turn handling, or
event delivery — the command is a leaf that never involves a brain. The e2e
risk is the *collector*, not the model, which is what Q2 addresses.

## Lane map

| Lane | Dispatch issue | Phases | One-line scope | Marker mode | Status |
|---|---|---|---|---|---|
| A | #TBD | 1-4 | The whole feature: exporter, CLI, distribution, verification | solo | open |

**Lanes:** One lane — the null topology and the default. This is a single
coherent deliverable in a single repo whose phases are strictly sequential
(the CLI cannot be written before the exporter it calls; distribution cannot
document a surface that does not exist; verification needs all three). There
is no wall-clock justification for a same-repo parallel cut: the sequential
estimate is a single agent session, not calendar scale. One lane therefore
means one dispatch issue, one PR, one review, and no fuse or convergence gate.

## Amendments

- **2026-08-05** (create): plan drafted.

## Notes

- **Measurements** (this repo, 2026-08-05, macOS, warm FS cache): bare
  interpreter ~20ms; `import httpx` ~90ms; `bobi --version` ~120ms;
  `opentelemetry-proto` pb2 modules ~10ms marginal (1 transitive dep,
  `protobuf`); full `opentelemetry-sdk` + OTLP HTTP exporter ~150ms
  (15 packages). These drove the exporter decision and are the baseline for
  the Phase 4 gate.
- `protobuf` is already present transitively in a `.[dev,kb]` install
  (`onnxruntime` ← `fastembed`) at 7.35.1, so the two coexist today. Keeping
  `opentelemetry-proto` in an extra rather than core means protobuf's
  historically disruptive ABI breaks cannot affect a default `pip install bobi`.
- **Unverified, and deliberately not load-bearing:** whether Grafana Cloud's
  OTLP gateway accepts JSON payloads (medium confidence that it does). It does
  not matter under the protobuf decision; recorded only so a future reader does
  not treat the JSON path as known-good.
- OTLP/JSON encoding traps, for whoever revisits the encoding decision:
  64-bit integers must be JSON strings, and trace/span ids are hex rather than
  proto3 JSON's base64.
- Deferred follow-up: spans. A one-shot CLI has no context to hold open, so a
  span would only ever be an already-completed duration reported after the
  fact. If trace correlation is wanted later, it needs a different mechanism
  than this command.
