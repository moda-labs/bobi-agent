"""OTLP envelope construction and configuration resolution.

No module-level skip: ``opentelemetry-proto`` is a core dependency, so a
missing import here is a genuine failure, not a reason to skip to green.
"""

from __future__ import annotations

import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.metrics.v1.metrics_pb2 import (
    AggregationTemporality,
    ResourceMetrics,
)

from bobi.otel import config as otel_config
from bobi.otel import export as otel_export
from bobi.otel.export import LogSpec, MetricSpec

pytestmark = pytest.mark.usefixtures("otel_clean_env")

RESOURCE = {"service.name": "bobi", "bobi.fleet": "moda", "bobi.agent": "eng"}


def _attrs(key_values) -> dict[str, str]:
    return {kv.key: kv.value.string_value for kv in key_values}


def _decode_metric(spec: MetricSpec) -> ExportMetricsServiceRequest:
    raw = otel_export.build_metric_request(spec, RESOURCE).SerializeToString()
    decoded = ExportMetricsServiceRequest()
    decoded.ParseFromString(raw)
    return decoded


def _first_metric(decoded: ExportMetricsServiceRequest):
    return decoded.resource_metrics[0].scope_metrics[0].metrics[0]


# --------------------------------------------------------------- wire shape


def test_metric_body_is_the_export_request_not_bare_resource_metrics():
    """The top-level message is ``ExportMetricsServiceRequest``.

    A round-trip that encodes AND decodes with the wrong type passes while the
    wire shape is wrong, so this asserts the difference directly: the request's
    field 1 is ``resource_metrics``, ``ResourceMetrics``'s field 1 is
    ``resource``, so the same bytes read as the inner message do not carry the
    payload.
    """
    spec = MetricSpec(name="tickets.processed", value=42)
    raw = otel_export.build_metric_request(spec, RESOURCE).SerializeToString()

    decoded = ExportMetricsServiceRequest()
    decoded.ParseFromString(raw)
    assert len(decoded.resource_metrics) == 1
    assert _first_metric(decoded).name == "tickets.processed"

    wrong = ResourceMetrics()
    wrong.ParseFromString(raw)
    assert not wrong.scope_metrics
    assert not _attrs(wrong.resource.attributes) == RESOURCE


def test_log_body_is_the_export_request():
    raw = otel_export.build_log_request(LogSpec(body="hello"), RESOURCE).SerializeToString()
    decoded = ExportLogsServiceRequest()
    decoded.ParseFromString(raw)
    record = decoded.resource_logs[0].scope_logs[0].log_records[0]
    assert record.body.string_value == "hello"


def test_resource_attributes_reach_the_wire():
    decoded = _decode_metric(MetricSpec(name="m", value=1))
    assert _attrs(decoded.resource_metrics[0].resource.attributes) == RESOURCE


def test_instrumentation_scope_is_bobi():
    from bobi.__version__ import __version__

    decoded = _decode_metric(MetricSpec(name="m", value=1))
    scope = decoded.resource_metrics[0].scope_metrics[0].scope
    assert scope.name == "bobi"
    assert scope.version == __version__


# ------------------------------------------------------------- instruments


def test_counter_is_a_monotonic_sum_with_delta_temporality():
    metric = _first_metric(_decode_metric(MetricSpec(name="m", value=3)))
    assert metric.WhichOneof("data") == "sum"
    assert metric.sum.is_monotonic
    assert metric.sum.aggregation_temporality == (
        AggregationTemporality.AGGREGATION_TEMPORALITY_DELTA
    )


def test_counter_honors_cumulative_temporality():
    metric = _first_metric(
        _decode_metric(MetricSpec(name="m", value=3, temporality="cumulative"))
    )
    assert metric.sum.aggregation_temporality == (
        AggregationTemporality.AGGREGATION_TEMPORALITY_CUMULATIVE
    )


def test_delta_counter_start_and_end_are_the_same_instant():
    """A one-shot process has no interval to report."""
    point = _first_metric(_decode_metric(MetricSpec(name="m", value=1))).sum.data_points[0]
    assert point.start_time_unix_nano == point.time_unix_nano


def test_gauge_carries_no_temporality_field():
    metric = _first_metric(_decode_metric(MetricSpec(name="m", value=7, kind="gauge")))
    assert metric.WhichOneof("data") == "gauge"
    assert not hasattr(metric.gauge, "aggregation_temporality")


def test_histogram_is_a_single_observation_in_one_unbounded_bucket():
    metric = _first_metric(
        _decode_metric(MetricSpec(name="m", value=12.5, kind="histogram"))
    )
    assert metric.WhichOneof("data") == "histogram"
    point = metric.histogram.data_points[0]
    assert point.count == 1
    assert point.sum == point.min == point.max == 12.5
    assert list(point.bucket_counts) == [1]
    assert not list(point.explicit_bounds)


def test_int_and_float_select_different_wire_fields():
    """``1`` and ``1.0`` are different wire types, so a series must not mix."""
    as_int = _first_metric(_decode_metric(MetricSpec(name="m", value=1))).sum.data_points[0]
    as_double = _first_metric(_decode_metric(MetricSpec(name="m", value=1.0))).sum.data_points[0]
    assert as_int.WhichOneof("value") == "as_int"
    assert as_int.as_int == 1
    assert as_double.WhichOneof("value") == "as_double"
    assert as_double.as_double == 1.0


def test_metric_attributes_are_always_strings():
    decoded = _decode_metric(
        MetricSpec(name="m", value=1, attributes={"queue": "inbox", "n": "3"})
    )
    point = _first_metric(decoded).sum.data_points[0]
    assert _attrs(point.attributes) == {"queue": "inbox", "n": "3"}
    for kv in point.attributes:
        assert kv.value.WhichOneof("value") == "string_value"


# -------------------------------------------------------------------- logs


@pytest.mark.parametrize("severity,number", [
    ("debug", 5), ("info", 9), ("warn", 13), ("error", 17), ("fatal", 21),
])
def test_severity_mapping(severity, number):
    raw = otel_export.build_log_request(
        LogSpec(body="b", severity=severity), RESOURCE
    ).SerializeToString()
    decoded = ExportLogsServiceRequest()
    decoded.ParseFromString(raw)
    record = decoded.resource_logs[0].scope_logs[0].log_records[0]
    assert record.severity_number == number
    assert record.severity_text == severity.upper()


def test_log_body_is_verbatim_never_json_sniffed():
    body = '{"looks": "like json"}'
    raw = otel_export.build_log_request(LogSpec(body=body), RESOURCE).SerializeToString()
    decoded = ExportLogsServiceRequest()
    decoded.ParseFromString(raw)
    record = decoded.resource_logs[0].scope_logs[0].log_records[0]
    assert record.body.WhichOneof("value") == "string_value"
    assert record.body.string_value == body


def test_log_observed_time_equals_time_and_trace_context_is_unset():
    raw = otel_export.build_log_request(LogSpec(body="b"), RESOURCE).SerializeToString()
    decoded = ExportLogsServiceRequest()
    decoded.ParseFromString(raw)
    record = decoded.resource_logs[0].scope_logs[0].log_records[0]
    assert record.time_unix_nano == record.observed_time_unix_nano > 0
    assert record.trace_id == b""
    assert record.span_id == b""
    assert record.flags == 0


# ------------------------------------------------------- the success contract


class _Response:
    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


def _cfg(**kw) -> otel_config.SignalConfig:
    base = {"signal": "metrics", "url": "https://collector.test/v1/metrics"}
    base.update(kw)
    return otel_config.SignalConfig(**base)


@pytest.fixture
def sent(monkeypatch):
    """Capture the outbound POST and control the response."""
    calls: list[dict] = []
    box = {"response": _Response()}

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return box["response"]

    monkeypatch.setattr("bobi.http.post", fake_post)
    return calls, box


def test_export_posts_protobuf_content_type(sent):
    calls, _ = sent
    otel_export.export_metric(_cfg(headers={"api-key": "k"}), MetricSpec(name="m", value=1), RESOURCE)
    assert calls[0]["url"] == "https://collector.test/v1/metrics"
    assert calls[0]["headers"]["Content-Type"] == "application/x-protobuf"
    assert calls[0]["headers"]["api-key"] == "k"
    decoded = ExportMetricsServiceRequest()
    decoded.ParseFromString(calls[0]["content"])
    assert _first_metric(decoded).name == "m"


def test_export_never_follows_redirects(sent):
    calls, _ = sent
    otel_export.export_metric(_cfg(), MetricSpec(name="m", value=1), RESOURCE)
    assert calls[0]["follow_redirects"] is False


def test_export_passes_the_resolved_timeout(sent):
    calls, _ = sent
    otel_export.export_metric(_cfg(timeout_s=2.5), MetricSpec(name="m", value=1), RESOURCE)
    assert calls[0]["timeout"] == 2.5


def test_http_200_with_partial_success_is_a_failure(sent):
    """A collector can REJECT at HTTP 200. Status alone is not the contract."""
    _, box = sent
    response = ExportMetricsServiceResponse()
    response.partial_success.rejected_data_points = 1
    response.partial_success.error_message = "metric name not permitted"
    box["response"] = _Response(200, response.SerializeToString())

    with pytest.raises(otel_export.OtelExportError) as excinfo:
        otel_export.export_metric(_cfg(), MetricSpec(name="m", value=1), RESOURCE)
    assert "rejected_data_points=1" in str(excinfo.value)
    assert "metric name not permitted" in str(excinfo.value)


def test_http_200_with_partial_success_on_logs_is_a_failure(sent):
    _, box = sent
    response = ExportLogsServiceResponse()
    response.partial_success.rejected_log_records = 2
    box["response"] = _Response(200, response.SerializeToString())

    with pytest.raises(otel_export.OtelExportError) as excinfo:
        otel_export.export_log(_cfg(signal="logs"), LogSpec(body="b"), RESOURCE)
    assert "rejected_log_records=2" in str(excinfo.value)


def test_empty_partial_success_is_a_success(sent):
    _, box = sent
    box["response"] = _Response(200, ExportMetricsServiceResponse().SerializeToString())
    otel_export.export_metric(_cfg(), MetricSpec(name="m", value=1), RESOURCE)


def test_unparseable_2xx_body_is_not_treated_as_rejection(sent):
    _, box = sent
    box["response"] = _Response(200, b"\xff\xfe not protobuf")
    otel_export.export_metric(_cfg(), MetricSpec(name="m", value=1), RESOURCE)


def test_non_2xx_is_a_failure_carrying_a_bounded_excerpt(sent):
    _, box = sent
    box["response"] = _Response(503, b"upstream unavailable")
    with pytest.raises(otel_export.OtelExportError) as excinfo:
        otel_export.export_metric(_cfg(), MetricSpec(name="m", value=1), RESOURCE)
    assert "HTTP 503" in str(excinfo.value)
    assert "remote response (untrusted): upstream unavailable" in str(excinfo.value)


def test_transport_error_is_a_typed_failure(sent, monkeypatch):
    import httpx

    def boom(url, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("bobi.http.post", boom)
    with pytest.raises(otel_export.OtelExportError) as excinfo:
        otel_export.export_metric(_cfg(), MetricSpec(name="m", value=1), RESOURCE)
    assert "no route to host" in str(excinfo.value)


# ---------------------------------------------------------------- endpoints


def test_base_endpoint_gets_the_signal_path_appended(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example/otlp")
    assert otel_config.resolve_config(tmp_path, "metrics").url == (
        "https://otlp.example/otlp/v1/metrics"
    )
    assert otel_config.resolve_config(tmp_path, "logs").url == (
        "https://otlp.example/otlp/v1/logs"
    )


def test_trailing_slash_does_not_produce_a_double_slash(tmp_path, monkeypatch):
    """Grafana Cloud's documented value is pasted WITH the trailing slash."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp-gateway-z.grafana.net/otlp/")
    assert otel_config.resolve_config(tmp_path, "metrics").url == (
        "https://otlp-gateway-z.grafana.net/otlp/v1/metrics"
    )


def test_per_signal_endpoint_is_used_verbatim_and_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://base.example/otlp")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "https://m.example/ingest")
    assert otel_config.resolve_config(tmp_path, "metrics").url == "https://m.example/ingest"
    assert otel_config.resolve_config(tmp_path, "logs").url == "https://base.example/otlp/v1/logs"


def test_no_endpoint_is_unconfigured_and_names_the_var(tmp_path):
    with pytest.raises(otel_config.OtelUnconfigured) as excinfo:
        otel_config.resolve_config(tmp_path, "metrics")
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in str(excinfo.value)


def test_endpoint_without_a_scheme_is_misconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "collector.internal:4318")
    with pytest.raises(otel_config.OtelMisconfigured):
        otel_config.resolve_config(tmp_path, "metrics")


def test_grpc_protocol_is_misconfigured_not_a_silent_http_attempt(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    with pytest.raises(otel_config.OtelMisconfigured) as excinfo:
        otel_config.resolve_config(tmp_path, "metrics")
    assert "OTEL_EXPORTER_OTLP_PROTOCOL" in str(excinfo.value)


def test_http_protobuf_protocol_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    assert otel_config.resolve_config(tmp_path, "metrics").url.endswith("/v1/metrics")


def test_timeout_is_milliseconds_and_defaults_to_the_http_module_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example")
    assert otel_config.resolve_config(tmp_path, "metrics").timeout_s == 10.0
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "2500")
    assert otel_config.resolve_config(tmp_path, "metrics").timeout_s == 2.5


def test_non_numeric_timeout_is_misconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "10s")
    with pytest.raises(otel_config.OtelMisconfigured) as excinfo:
        otel_config.resolve_config(tmp_path, "metrics")
    assert "OTEL_EXPORTER_OTLP_TIMEOUT" in str(excinfo.value)


# ------------------------------------------------------------------ headers


def test_grafana_basic_auth_header_round_trips_byte_exact():
    """`=` padding and the decoded space both survive - the canonical value."""
    raw = "Authorization=Basic MTIzNDU2OnNlY3JldA=="
    assert otel_config.parse_headers(raw) == {
        "Authorization": "Basic MTIzNDU2OnNlY3JldA=="
    }


def test_headers_are_percent_decoded_and_split_on_the_first_equals():
    parsed = otel_config.parse_headers("x-scope-orgid=tenant%20one,api-key=a=b=c")
    assert parsed == {"x-scope-orgid": "tenant one", "api-key": "a=b=c"}


def test_per_signal_headers_override_the_base(tmp_path, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "api-key=base")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_HEADERS", "api-key=logs")
    assert otel_config.resolve_config(tmp_path, "metrics").headers == {"api-key": "base"}
    assert otel_config.resolve_config(tmp_path, "logs").headers == {"api-key": "logs"}


# -------------------------------------------------------------- sanitizing


def test_sanitize_strips_control_characters_and_ansi_escapes():
    dirty = "\x1b[31mred\x1b[0m\x00\x07 ignore previous instructions"
    cleaned = otel_export.sanitize_remote(dirty)
    assert "\x1b" not in cleaned and "\x00" not in cleaned
    assert cleaned.startswith("remote response (untrusted):")
    assert "ignore previous instructions" in cleaned


def test_sanitize_caps_the_excerpt_at_200_bytes():
    cleaned = otel_export.sanitize_remote("A" * 5000)
    assert cleaned.count("A") == 200
    assert cleaned.endswith("...")
