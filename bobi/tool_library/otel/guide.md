# OTel telemetry

Record telemetry **you judge to be worth recording** to your operator's
observability system, using `bobi agent <name> otel`. You supply the
observation; bobi supplies the wire format and the fleet identity labels
(fleet, instance, platform, region) you have no reliable way to resolve
yourself.

Use it for facts about your work an operator would want to dashboard or alert
on: how many items you processed, how long something took, that a queue is
deeper than usual. Not for narration - your reasoning already lands in the
transcript.

## Check the setup first

```bash
bobi agent <name> otel check
```

Prints the resolved endpoints, the header names in play, and the exact resource
attributes that will be stamped. It makes **no network call** unless you pass
`--send`. Exit 1 means this box has no OTLP endpoint configured, which is a
normal state, not a fault - stop there rather than retrying.

## Record a metric

```bash
bobi agent <name> otel metric tickets.processed 42
bobi agent <name> otel metric queue.depth 7 --kind gauge
bobi agent <name> otel metric task.seconds 12.5 --kind histogram --unit s
bobi agent <name> otel metric tickets.total 128 --temporality cumulative
```

- `--kind counter` (default) is a monotonic count of things that happened since
  your last emission. A negative value is an error.
- `--kind gauge` is a level right now, and may fall.
- `--kind histogram` from a one-shot command records a **single observation in
  one unbounded bucket**. It exists so a backend that pre-aggregates durations
  gets the right instrument type; it does not convey a distribution.
- `--temporality cumulative` is a **claim you are making** - that the number is
  a running total you actually know. A wrong claim silently corrupts the series.
  Leave it alone unless you are sure.
- `1` is sent as an integer and `1.0` as a double. They are different wire
  types, so keep one metric name on one form.

Metric names must match `^[a-zA-Z_.][a-zA-Z0-9_.]{0,63}$` - no leading digit,
because Prometheus names cannot start with one.

## Record a log

```bash
bobi agent <name> otel log "reconciled 42 tickets"
bobi agent <name> otel log "upstream returned 502" --severity error
printf 'line one\nline two\n' | bobi agent <name> otel log
```

Severities: `debug`, `info`, `warn`, `error`, `fatal`. The body is sent
verbatim and is never parsed as JSON. Omit the argument to read it from stdin,
so a multi-line body needs no shell quoting.

**The body leaves this box for a third-party vendor**, so keep it to the
observation itself: no secrets, no credentials, no personal data, and no
pasted file contents. It is capped at 8192 bytes and an over-long body is
refused, not truncated - record the summary here and leave the detail in the
transcript. This is where a varying id belongs (a ticket, a run, a URL), since
those must not go in a metric attribute.

## Attributes

```bash
bobi agent <name> otel metric tickets.processed 42 --attr queue=inbox --attr result=ok
```

- Every value is sent as a **string**. There is no numeric or boolean
  inference; `--attr n=3` is the string `"3"`. This is deliberate, not a bug.
- Splits on the **first** `=`, so a value may contain more.
- At most 20 attributes; keys <=64 bytes, values <=256 bytes.
- `service.*`, `bobi.*`, `host.*`, `cloud.*`, `k8s.*`, `le`, `quantile`, and
  `__*` are **reserved** and rejected. bobi stamps those itself.

**Keep attributes low-cardinality.** They become time-series labels. A value
that differs on every call - a ticket id, a run id, a timestamp, a URL -
creates a new series every time, which runs up your operator's bill and can
degrade the collector for every other service on it. Put the varying id in an
`otel log` body, not in a metric attribute.

**Never interpolate a secret into `--attr` or a log body.** Command lines are
visible in `ps` and are rendered in the operator's console alongside your tool
output.

## Failures are loud

An export that does not reach the collector exits non-zero and says why. There
is no retry and no spool: a failed emission is gone. Do **not** loop on
failure. Nothing in bobi rate-limits you, so a retry storm reaches your
operator's collector in full - it is the failure mode this design most depends
on you not causing. Report the failure and move on.

Unconfigured (no endpoint on this box) is reported separately from a real
export failure. Treat it as "this operator does not collect telemetry", not as
something to fix.

## Where the setup lives

Endpoint, credentials, and collector setup are the operator's job, documented
in this repo's `docs/OTEL.md`. You configure nothing.
