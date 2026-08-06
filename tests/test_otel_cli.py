"""Command surface for ``bobi agent <name> otel``."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from bobi.cli import main
from tests.conftest import TEST_AGENT_NAME

pytestmark = pytest.mark.usefixtures("otel_clean_env")


@pytest.fixture
def sent(monkeypatch):
    """Capture what the exporter would have shipped, without a network call.

    Patches the function in its DEFINING module: ``cli`` imports it with a
    ``from bobi.otel.export import ...`` form, so patching ``bobi.otel.export``
    as a whole would be silently bypassed through ``sys.modules``.
    """
    calls: list[dict] = []

    def fake_metric(cfg, spec, resource):
        calls.append({"kind": "metric", "cfg": cfg, "spec": spec, "resource": resource})

    def fake_log(cfg, spec, resource):
        calls.append({"kind": "log", "cfg": cfg, "spec": spec, "resource": resource})

    monkeypatch.setattr("bobi.otel.export.export_metric", fake_metric)
    monkeypatch.setattr("bobi.otel.export.export_log", fake_log)
    return calls


@pytest.fixture
def configured(bobi_install, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example/otlp")
    return bobi_install


def _run(*args) -> object:
    return CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "otel", *args])


# ------------------------------------------------------------- registration


def test_agent_help_lists_otel(bobi_install):
    result = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "--help"])
    assert result.exit_code == 0, result.output
    assert "otel" in result.output


def test_otel_help_lists_all_three_commands(bobi_install):
    result = _run("--help")
    assert result.exit_code == 0, result.output
    for command in ["metric", "log", "check"]:
        assert command in result.output


def test_otel_is_not_a_top_level_command():
    result = CliRunner().invoke(main, ["otel", "--help"])
    assert result.exit_code != 0


# ------------------------------------------------------------------ metric


def test_metric_builds_a_counter_spec(configured, sent):
    result = _run("metric", "tickets.processed", "42")
    assert result.exit_code == 0, result.output
    spec = sent[0]["spec"]
    assert (spec.name, spec.value, spec.kind, spec.temporality) == (
        "tickets.processed", 42, "counter", "delta"
    )
    assert sent[0]["cfg"].url == "https://otlp.example/otlp/v1/metrics"


def test_metric_value_int_vs_float_is_preserved(configured, sent):
    _run("metric", "m", "1")
    _run("metric", "m", "1.0")
    assert isinstance(sent[0]["spec"].value, int)
    assert isinstance(sent[1]["spec"].value, float)


def test_metric_rejects_a_non_numeric_value(configured, sent):
    result = _run("metric", "m", "lots")
    assert result.exit_code != 0
    assert "must be a number" in result.output
    assert not sent


def test_metric_rejects_a_non_finite_value(configured, sent):
    result = _run("metric", "m", "inf")
    assert result.exit_code != 0
    assert "finite" in result.output
    assert not sent


def test_negative_counter_is_a_usage_error(configured, sent):
    result = _run("metric", "m", "-1")
    assert result.exit_code != 0
    assert "monotonic" in result.output
    assert not sent


def test_negative_gauge_is_allowed(configured, sent):
    result = _run("metric", "m", "-1", "--kind", "gauge")
    assert result.exit_code == 0, result.output
    assert sent[0]["spec"].value == -1


def test_a_mistyped_option_still_fails_loudly(configured, sent):
    """`ignore_unknown_options` lets `-1` through; it must not swallow typos."""
    result = _run("metric", "m", "1", "--kidn", "gauge")
    assert result.exit_code != 0
    assert not sent


def test_a_log_body_may_start_with_a_dash(configured, sent):
    result = _run("log", "-> reconciled the backlog")
    assert result.exit_code == 0, result.output
    assert sent[0]["spec"].body == "-> reconciled the backlog"


def test_temporality_with_gauge_is_a_usage_error_not_a_silent_ignore(configured, sent):
    result = _run("metric", "m", "1", "--kind", "gauge", "--temporality", "delta")
    assert result.exit_code != 0
    assert "Gauge carries no aggregation temporality" in result.output
    assert not sent


def test_cumulative_temporality_reaches_the_spec(configured, sent):
    result = _run("metric", "m", "128", "--temporality", "cumulative")
    assert result.exit_code == 0, result.output
    assert sent[0]["spec"].temporality == "cumulative"


def test_unit_and_description_reach_the_spec(configured, sent):
    _run("metric", "task.seconds", "1.5", "--kind", "histogram", "--unit", "s",
         "--desc", "wall clock")
    assert sent[0]["spec"].unit == "s"
    assert sent[0]["spec"].description == "wall clock"


# -------------------------------------------------------------------- attrs


def test_attr_splits_on_the_first_equals_only(configured, sent):
    _run("metric", "m", "1", "--attr", "note=a=b")
    assert sent[0]["spec"].attributes == {"note": "a=b"}


def test_attr_without_an_equals_is_a_usage_error(configured, sent):
    result = _run("metric", "m", "1", "--attr", "novalue")
    assert result.exit_code != 0
    assert "key=value" in result.output


def test_attr_with_an_empty_key_is_a_usage_error(configured, sent):
    result = _run("metric", "m", "1", "--attr", "=v")
    assert result.exit_code != 0
    assert "must not be empty" in result.output


def test_attr_with_an_empty_value_is_allowed(configured, sent):
    result = _run("metric", "m", "1", "--attr", "queue=")
    assert result.exit_code == 0, result.output
    assert sent[0]["spec"].attributes == {"queue": ""}


def test_duplicate_attr_key_keeps_the_last(configured, sent):
    _run("metric", "m", "1", "--attr", "queue=a", "--attr", "queue=b")
    assert sent[0]["spec"].attributes == {"queue": "b"}


# ---------------------------------------------------------------------- log


def test_log_builds_a_record_spec(configured, sent):
    result = _run("log", "reconciled the backlog", "--severity", "error")
    assert result.exit_code == 0, result.output
    spec = sent[0]["spec"]
    assert (spec.body, spec.severity) == ("reconciled the backlog", "error")
    assert sent[0]["cfg"].url == "https://otlp.example/otlp/v1/logs"


def test_log_body_comes_from_stdin_when_omitted(configured, sent):
    result = CliRunner().invoke(
        main, ["agent", TEST_AGENT_NAME, "otel", "log"], input="line one\nline two\n"
    )
    assert result.exit_code == 0, result.output
    assert sent[0]["spec"].body == "line one\nline two\n"


def test_empty_log_body_is_a_usage_error(configured, sent):
    result = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "otel", "log"], input="")
    assert result.exit_code != 0
    assert not sent


# -------------------------------------------------------- resource attributes


def test_resource_attributes_carry_the_fleet_identity(configured, sent, monkeypatch):
    monkeypatch.setenv("BOBI_FLEET", "moda")
    monkeypatch.setenv("BOBI_INSTANCE", "eng-team")
    _run("metric", "m", "1")
    resource = sent[0]["resource"]
    assert resource["bobi.fleet"] == "moda"
    assert resource["service.instance.id"] == "eng-team"
    # `bobi.agent` is the <name> argument - the first consumer of ctx.obj in
    # the tree, so assert it explicitly rather than assuming it binds.
    assert resource["bobi.agent"] == TEST_AGENT_NAME
    assert resource["bobi.team"] == TEST_AGENT_NAME
    assert resource["service.name"] == "bobi"


def test_null_enrichment_attributes_are_omitted(configured, sent):
    _run("metric", "m", "1")
    resource = sent[0]["resource"]
    for key in ["host.id", "cloud.region", "k8s.node.name"]:
        assert key not in resource


def test_fly_enrichment_is_mapped_onto_semconv_keys(configured, sent, monkeypatch):
    monkeypatch.setenv("FLY_APP_NAME", "bobi-fleet")
    monkeypatch.setenv("FLY_MACHINE_ID", "148e12")
    monkeypatch.setenv("FLY_REGION", "iad")
    _run("metric", "m", "1")
    resource = sent[0]["resource"]
    assert resource["bobi.platform"] == "fly"
    assert resource["host.id"] == "148e12"
    assert resource["cloud.region"] == "iad"


def test_service_name_is_overridable(configured, sent, monkeypatch):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "agents")
    _run("metric", "m", "1")
    assert sent[0]["resource"]["service.name"] == "agents"


def test_otel_resource_attributes_merge_but_never_win(configured, sent, monkeypatch):
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES", "deployment.environment=prod,bobi.fleet=forged"
    )
    monkeypatch.setenv("BOBI_FLEET", "moda")
    _run("metric", "m", "1")
    resource = sent[0]["resource"]
    assert resource["deployment.environment"] == "prod"
    assert resource["bobi.fleet"] == "moda"


# ------------------------------------------------------------- unconfigured


def test_unconfigured_exits_non_zero_naming_the_env_var(bobi_install, sent):
    result = _run("metric", "m", "1")
    assert result.exit_code == 1
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in result.output
    assert not sent


def test_misconfigured_protocol_exits_non_zero(bobi_install, sent, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://otlp.example")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    result = _run("metric", "m", "1")
    assert result.exit_code == 1
    assert "OTEL_EXPORTER_OTLP_PROTOCOL" in result.output
    assert not sent


def test_export_failure_exits_non_zero(configured, monkeypatch):
    from bobi.otel.export import OtelExportError

    def boom(cfg, spec, resource):
        raise OtelExportError("OTLP export failed with HTTP 503.")

    monkeypatch.setattr("bobi.otel.export.export_metric", boom)
    result = _run("metric", "m", "1")
    assert result.exit_code == 1
    assert "HTTP 503" in result.output


# -------------------------------------------------------------------- check


def test_check_makes_no_network_call_without_send(configured, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("check must not make a network call without --send")

    monkeypatch.setattr("bobi.http.post", boom)
    result = _run("check")
    assert result.exit_code == 0, result.output
    assert "no request sent" in result.output
    assert "https://otlp.example/otlp/v1/metrics" in result.output
    assert "https://otlp.example/otlp/v1/logs" in result.output


def test_check_reports_the_wire_format_and_resource_attributes(configured):
    result = _run("check")
    assert "opentelemetry.proto importable" in result.output
    assert "service.instance.id=" in result.output
    assert f"bobi.agent={TEST_AGENT_NAME}" in result.output


def test_check_exits_1_with_a_diagnosis_when_unconfigured(bobi_install):
    result = _run("check")
    assert result.exit_code == 1
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_check_send_exports_a_throwaway_gauge(configured, sent):
    result = _run("check", "--send")
    assert result.exit_code == 0, result.output
    spec = sent[0]["spec"]
    assert (spec.name, spec.value, spec.kind, spec.unit) == ("bobi.otel.check", 1, "gauge", "1")
    assert "sent bobi.otel.check" in result.output


def test_check_send_exits_1_when_the_export_fails(configured, monkeypatch):
    from bobi.otel.export import OtelExportError

    def boom(cfg, spec, resource):
        raise OtelExportError("collector said no")

    monkeypatch.setattr("bobi.otel.export.export_metric", boom)
    result = _run("check", "--send")
    assert result.exit_code == 1
    assert "collector said no" in result.output
