"""OTLP/HTTP request construction and export.

The request body is an ``ExportMetricsServiceRequest`` /
``ExportLogsServiceRequest`` - **not** a bare ``ResourceMetrics`` /
``ResourceLogs``. The distinction is invisible to a round-trip test that
encodes and decodes with the same wrong type (``ExportMetricsServiceRequest``
field 1 is ``resource_metrics``; ``ResourceMetrics`` field 1 is ``resource``),
which is why the acceptance bar for this module is a real collector.

No existing module can absorb this: ``bobi/events/publish.py`` speaks bobi's
own signed-envelope protocol to bobi's own bus.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from bobi.otel.config import SignalConfig

CONTENT_TYPE = "application/x-protobuf"

# A one-shot CLI an LLM may call several times per turn, with loud failures and
# no backoff: a model that observes a failure retries, so the cap is what stops
# a transient 429 from becoming a hot loop, and `otel metric "m$(uuidgen)" 1`
# in a loop from becoming an active-series explosion.
MAX_EMISSIONS_PER_PROCESS = 100

# Remote bytes reach the agent's context through stderr, so they are a
# second-order prompt-injection channel and a context-exhaustion lever.
MAX_REMOTE_EXCERPT_BYTES = 200
REMOTE_EXCERPT_PREFIX = "remote response (untrusted):"

SEVERITY_NUMBERS = {
    "debug": 5,
    "info": 9,
    "warn": 13,
    "error": 17,
    "fatal": 21,
}

_CONTROL_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]")

_emitted = 0


class OtelExportError(Exception):
    """An export did not satisfy the success contract."""


class OtelEmissionCapExceeded(OtelExportError):
    """This process already emitted its allowance."""


@dataclass(frozen=True)
class MetricSpec:
    """One agent-authored measurement, already validated by the CLI."""

    name: str
    value: int | float
    kind: str = "counter"
    temporality: str = "delta"
    unit: str = ""
    description: str = ""
    attributes: dict[str, str] | None = None


@dataclass(frozen=True)
class LogSpec:
    """One agent-authored log record, already validated by the CLI."""

    body: str
    severity: str = "info"
    attributes: dict[str, str] | None = None


def sanitize_remote(text: str | bytes) -> str:
    """Bound and defang remote-controlled bytes before they reach stderr.

    Truncation is on BYTES (the resource being bounded), decoded leniently so a
    cut multi-byte sequence cannot raise.
    """
    raw = text.encode("utf-8", "replace") if isinstance(text, str) else bytes(text)
    clipped = raw[:MAX_REMOTE_EXCERPT_BYTES]
    decoded = clipped.decode("utf-8", "replace")
    cleaned = _CONTROL_RE.sub(" ", decoded).strip()
    suffix = "" if len(raw) <= MAX_REMOTE_EXCERPT_BYTES else " ..."
    return f"{REMOTE_EXCERPT_PREFIX} {cleaned}{suffix}".strip()


def _key_values(attrs: dict[str, str]):
    """Attributes as OTLP ``KeyValue``s. Every value is a ``string_value``."""
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

    return [
        KeyValue(key=key, value=AnyValue(string_value=value))
        for key, value in attrs.items()
    ]


def _temporality(name: str):
    from opentelemetry.proto.metrics.v1.metrics_pb2 import AggregationTemporality

    if name == "cumulative":
        return AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE
    return AggregationTemporality.AGGREGATION_TEMPORALITY_DELTA


def _scope():
    from opentelemetry.proto.common.v1.common_pb2 import InstrumentationScope

    from bobi.__version__ import __version__

    return InstrumentationScope(name="bobi", version=__version__)


def _set_number(point, value: int | float) -> None:
    """``as_int`` vs ``as_double`` is a real oneof, so the choice is on the wire."""
    if isinstance(value, int):
        point.as_int = value
    else:
        point.as_double = value


def build_metric_request(spec: MetricSpec, resource_attrs: dict[str, str]):
    """Build the ``ExportMetricsServiceRequest`` for one measurement."""
    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
        ExportMetricsServiceRequest,
    )
    from opentelemetry.proto.metrics.v1.metrics_pb2 import (
        HistogramDataPoint,
        Metric,
        NumberDataPoint,
        ResourceMetrics,
        ScopeMetrics,
    )
    from opentelemetry.proto.resource.v1.resource_pb2 import Resource

    attrs = _key_values(spec.attributes or {})
    now = time.time_ns()

    metric = Metric(name=spec.name, unit=spec.unit, description=spec.description)
    if spec.kind == "gauge":
        point = NumberDataPoint(attributes=attrs, time_unix_nano=now)
        _set_number(point, spec.value)
        metric.gauge.data_points.append(point)
    elif spec.kind == "histogram":
        # Deliberately degenerate: one observation, one unbounded bucket. It
        # exists so a backend that pre-aggregates duration histograms receives
        # the right INSTRUMENT TYPE, not to convey a distribution.
        value = float(spec.value)
        point = HistogramDataPoint(
            attributes=attrs,
            # A one-shot process has no interval, so start == end.
            start_time_unix_nano=now,
            time_unix_nano=now,
            count=1,
            sum=value,
            min=value,
            max=value,
            bucket_counts=[1],
        )
        metric.histogram.data_points.append(point)
        metric.histogram.aggregation_temporality = _temporality(spec.temporality)
    else:
        point = NumberDataPoint(
            attributes=attrs,
            start_time_unix_nano=now,
            time_unix_nano=now,
        )
        _set_number(point, spec.value)
        metric.sum.data_points.append(point)
        metric.sum.is_monotonic = True
        metric.sum.aggregation_temporality = _temporality(spec.temporality)

    return ExportMetricsServiceRequest(
        resource_metrics=[
            ResourceMetrics(
                resource=Resource(attributes=_key_values(resource_attrs)),
                scope_metrics=[ScopeMetrics(scope=_scope(), metrics=[metric])],
            )
        ]
    )


def build_log_request(spec: LogSpec, resource_attrs: dict[str, str]):
    """Build the ``ExportLogsServiceRequest`` for one record."""
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
        ExportLogsServiceRequest,
    )
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue
    from opentelemetry.proto.logs.v1.logs_pb2 import (
        LogRecord,
        ResourceLogs,
        ScopeLogs,
    )
    from opentelemetry.proto.resource.v1.resource_pb2 import Resource

    now = time.time_ns()
    record = LogRecord(
        time_unix_nano=now,
        observed_time_unix_nano=now,
        severity_number=SEVERITY_NUMBERS[spec.severity],
        severity_text=spec.severity.upper(),
        # Verbatim, never JSON-sniffed: the agent chose these bytes.
        body=AnyValue(string_value=spec.body),
        attributes=_key_values(spec.attributes or {}),
    )
    return ExportLogsServiceRequest(
        resource_logs=[
            ResourceLogs(
                resource=Resource(attributes=_key_values(resource_attrs)),
                scope_logs=[ScopeLogs(scope=_scope(), log_records=[record])],
            )
        ]
    )


def _check_partial_success(signal: str, body: bytes) -> None:
    """A collector can REJECT at HTTP 200. Status alone is not the contract."""
    if signal == "metrics":
        from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
            ExportMetricsServiceResponse as Response,
        )
        rejected_field = "rejected_data_points"
    else:
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
            ExportLogsServiceResponse as Response,
        )
        rejected_field = "rejected_log_records"

    response = Response()
    if body:
        try:
            response.ParseFromString(body)
        except Exception:
            # An unparseable 2xx body is not evidence of rejection; the
            # collector accepted. Treat the response as empty.
            return

    partial = response.partial_success
    rejected = getattr(partial, rejected_field)
    if not rejected and not partial.error_message:
        return

    detail = (
        sanitize_remote(partial.error_message)
        if partial.error_message
        else "no error message"
    )
    raise OtelExportError(
        f"Collector rejected the export at HTTP 200: "
        f"{rejected_field}={rejected}. {detail}"
    )


def _post(cfg: SignalConfig, payload: bytes) -> None:
    """POST one OTLP request and enforce the full success contract."""
    import httpx

    from bobi import http as bobi_http

    headers = dict(cfg.headers)
    headers["Content-Type"] = CONTENT_TYPE

    try:
        response = bobi_http.post(
            cfg.url,
            content=payload,
            headers=headers,
            timeout=cfg.timeout_s,
            # Never follow a redirect on an export. httpx strips only
            # `Authorization` and `Cookie` cross-origin, so a 307 replays a
            # vendor token (`x-honeycomb-team`, `api-key`, `signoz-ingestion-key`)
            # verbatim to the redirect target with no agent compromise at all.
            # It would also rewrite the POST to a GET and drop the body, making
            # a misrouted export look successful.
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise OtelExportError(f"OTLP export to {cfg.url} failed: {exc}") from exc

    if 300 <= response.status_code < 400:
        location = response.headers.get("location", "")
        target = _origin_of(location) or "an undisclosed host"
        raise OtelExportError(
            f"OTLP export to {cfg.url} was redirected ({response.status_code}) "
            f"to {target}; refusing to forward credentials. Point "
            f"OTEL_EXPORTER_OTLP_ENDPOINT at the final URL."
        )

    if not (200 <= response.status_code < 300):
        raise OtelExportError(
            f"OTLP export to {cfg.url} failed with HTTP "
            f"{response.status_code}. {sanitize_remote(response.content)}"
        )

    _check_partial_success(cfg.signal, response.content)


def _origin_of(url: str) -> str:
    import urllib.parse

    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    if not parts.hostname:
        return ""
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme else parts.netloc


def _count_emission() -> None:
    global _emitted
    if _emitted >= MAX_EMISSIONS_PER_PROCESS:
        raise OtelEmissionCapExceeded(
            f"This process already emitted {MAX_EMISSIONS_PER_PROCESS} OTLP "
            "signals, its per-process cap. Emit fewer, coarser measurements."
        )
    _emitted += 1


def export_metric(cfg: SignalConfig, spec: MetricSpec, resource_attrs: dict[str, str]) -> None:
    """Build and POST one metric. Raises :class:`OtelExportError` on failure."""
    _count_emission()
    _post(cfg, build_metric_request(spec, resource_attrs).SerializeToString())


def export_log(cfg: SignalConfig, spec: LogSpec, resource_attrs: dict[str, str]) -> None:
    """Build and POST one log record. Raises :class:`OtelExportError` on failure."""
    _count_emission()
    _post(cfg, build_log_request(spec, resource_attrs).SerializeToString())
