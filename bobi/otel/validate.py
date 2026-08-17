"""Input bounds for agent-authored telemetry.

These live in the library rather than in the CLI handler so the abuse suite can
prove them directly, without a CLI layer in the way. The CLI catches
:class:`OtelUsageError` and re-raises it as a ``click.UsageError``.

**These bound SHAPE, not RATE**, and that is deliberate - see ``docs/OTEL.md``.
Each invocation is a fresh process emitting one data point, so a per-process
emission cap would bound nothing, and a cross-invocation limiter needs exactly
the durable state the disk-spool rejection rules out.
"""

from __future__ import annotations

import math
import re

# A leading digit is the one shape no downstream normalization repairs:
# Prometheus names are `[a-zA-Z_:][a-zA-Z0-9_:]*`, and while the exporter maps
# `.` to `_`, it cannot invent a first character - so `5xx_count` is refused.
METRIC_NAME_RE = re.compile(r"^[a-zA-Z_.][a-zA-Z0-9_.]{0,63}$")

# Framework resource attributes are the labels this feature exists to make
# trustworthy, and `le` / `quantile` / `__name__` corrupt Prometheus series on
# ingest.
RESERVED_ATTR_PREFIXES = ("service.", "bobi.", "host.", "cloud.", "k8s.", "__")
RESERVED_ATTR_KEYS = frozenset({"le", "quantile"})

MAX_ATTRS = 20
MAX_ATTR_KEY_BYTES = 64
MAX_ATTR_VALUE_BYTES = 256

# A log body reads from stdin by design, so without a bound it is the one
# unbounded field in a list that caps everything else - an ingest-cost and
# availability gap on the BENIGN path, before any exfiltration argument.
# Generous enough for a multi-line observation, small enough to bound a
# runaway.
MAX_BODY_BYTES = 8192


class OtelUsageError(Exception):
    """Agent-supplied input that must not reach the wire."""


def validate_metric_name(name: str) -> str:
    """Reject a metric name that is not a bounded, label-safe identifier.

    Note this constrains SHAPE, not distinctness: `uuidgen | tr -d -` passes.
    Distinctness is bounded operator-side (see ``docs/OTEL.md``).
    """
    if not METRIC_NAME_RE.match(name):
        raise OtelUsageError(
            "Metric name must match ^[a-zA-Z_.][a-zA-Z0-9_.]{0,63}$ - an "
            "unbounded name is an active-series explosion, not a label."
        )
    return name


def validate_value(raw: str) -> int | float:
    """Parse a measurement. ``1`` is an int and ``1.0`` a double, by design.

    They select different wire fields (a real ``oneof``), so a series must not
    mix the two forms.
    """
    text = raw.strip()
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    try:
        value = float(text)
    except ValueError:
        raise OtelUsageError(f"<value> must be a number, got {raw!r}.") from None
    if not math.isfinite(value):
        raise OtelUsageError(f"<value> must be finite, got {raw!r}.")
    return value


def validate_body(body: str) -> str:
    """Bound a log body. Empty is a usage error; oversized is refused, not cut.

    Truncating would hand the collector a body the agent did not write, which
    is worse than refusing: a silently clipped observation reads as complete.
    """
    if not body.strip():
        raise OtelUsageError("Provide the log body as an argument or on stdin.")
    size = len(body.encode("utf-8"))
    if size > MAX_BODY_BYTES:
        raise OtelUsageError(
            f"Log body is {size} bytes, over the {MAX_BODY_BYTES}-byte bound. "
            "Record the summary here and leave the detail in the transcript."
        )
    return body


def validate_attrs(pairs: tuple[str, ...] | list[str]) -> dict[str, str]:
    """Parse repeated ``key=value`` into a bounded, non-forging attribute set.

    Splits on the FIRST ``=`` so a value may contain more. Every value stays a
    string; no numeric or boolean inference happens here or anywhere else.
    Duplicate keys resolve last-wins.
    """
    attrs: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise OtelUsageError(f"--attr must be key=value, got {pair!r}.")
        key = key.strip()
        if not key:
            raise OtelUsageError(f"--attr key must not be empty, got {pair!r}.")
        if key in RESERVED_ATTR_KEYS or key.startswith(RESERVED_ATTR_PREFIXES):
            raise OtelUsageError(
                f"--attr key {key!r} is reserved. Framework identity "
                "(service.*, bobi.*, host.*, cloud.*, k8s.*) is stamped by bobi, "
                "and le/quantile/__* corrupt Prometheus series."
            )
        if len(key.encode("utf-8")) > MAX_ATTR_KEY_BYTES:
            raise OtelUsageError(
                f"--attr key {key!r} exceeds {MAX_ATTR_KEY_BYTES} bytes."
            )
        if len(value.encode("utf-8")) > MAX_ATTR_VALUE_BYTES:
            raise OtelUsageError(
                f"--attr value for {key!r} exceeds {MAX_ATTR_VALUE_BYTES} bytes."
            )
        attrs[key] = value
    if len(attrs) > MAX_ATTRS:
        raise OtelUsageError(
            f"At most {MAX_ATTRS} --attr pairs; got {len(attrs)}. Attributes "
            "become time-series labels: high cardinality is a billing and "
            "availability hazard."
        )
    return attrs
