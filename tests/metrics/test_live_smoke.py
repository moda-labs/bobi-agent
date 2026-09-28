import os

import pytest
import yaml

from bobi.metrics.providers import claude_usage, codex_usage
from bobi.metrics.store import connect
from scripts.live_metrics_smoke import (
    _assert_common_parity,
    _brain_options,
    _codex_sessions_root,
    _codex_parity_expected,
    _configure_installed_smoke_package,
    _find_claude_transcript,
    _isolated_brain_defaults,
    _write_database,
)


def test_codex_parity_allows_only_unresolved_stream_model():
    rollout = codex_usage(
        {"input_tokens": 10, "output_tokens": 2}, model="cx/gpt-live"
    )
    unresolved = codex_usage(
        {"input_tokens": 10, "output_tokens": 2}, model="codex"
    )

    assert _codex_parity_expected(unresolved, rollout).model == "codex"

    resolved = codex_usage(
        {"input_tokens": 10, "output_tokens": 2}, model="gpt-other"
    )
    with pytest.raises(RuntimeError, match="model mismatch"):
        _codex_parity_expected(resolved, rollout)


def test_provision_pins_disposable_claude_to_known_live_model(tmp_path):
    package = tmp_path / "agent.yaml"
    package.write_text("agent: smoke\n")

    _configure_installed_smoke_package(package, "claude")

    assert yaml.safe_load(package.read_text())["brain"] == {
        "kind": "claude",
        "model": "opus",
    }


def test_provision_preserves_codex_model_if_fixture_declares_one(tmp_path):
    package = tmp_path / "agent.yaml"
    package.write_text("agent: smoke\nbrain:\n  model: gpt-live\n")

    _configure_installed_smoke_package(package, "codex")

    assert yaml.safe_load(package.read_text())["brain"] == {
        "kind": "codex",
        "model": "gpt-live",
    }


def test_live_probe_forwards_only_explicit_model():
    assert _brain_options(None) is None
    assert _brain_options("") is None
    assert _brain_options("opus") == {"model": "opus"}


def test_live_probe_temporarily_removes_ambient_bobi_brain_pins(monkeypatch):
    pins = {
        "BOBI_BRAIN": "codex",
        "BOBI_BRAIN_MODEL": "cc/claude-sonnet-5",
        "BOBI_BRAIN_EFFORT": "max",
    }
    for name, value in pins.items():
        monkeypatch.setenv(name, value)

    with _isolated_brain_defaults():
        assert all(name not in os.environ for name in pins)

    assert {name: os.environ[name] for name in pins} == pins


def test_live_probe_respects_codex_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert _codex_sessions_root() == tmp_path / "sessions"


def test_live_probe_respects_claude_config_dir(monkeypatch, tmp_path):
    config = tmp_path / "claude"
    transcript = config / "projects" / "project" / "session-id.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

    assert _find_claude_transcript("session-id", 0) == transcript


def test_claude_parity_allows_exact_result_only_internal_models():
    main = claude_usage(
        {"input_tokens": 2, "output_tokens": 3}, model="claude-opus"
    )
    internal = claude_usage(
        {"input_tokens": 1, "output_tokens": 1}, model="claude-haiku"
    )

    _assert_common_parity([main, internal], [main], allow_left_extras=True)


def test_claude_parity_still_rejects_transcript_mismatch():
    result = claude_usage(
        {"input_tokens": 2, "output_tokens": 3}, model="claude-opus"
    )
    transcript = claude_usage(
        {"input_tokens": 2, "output_tokens": 4}, model="claude-opus"
    )

    with pytest.raises(RuntimeError, match="output_tokens"):
        _assert_common_parity([result], [transcript], allow_left_extras=True)


def test_claude_parity_rejects_missing_adapter_dimension():
    result = claude_usage(
        {"input_tokens": 2, "output_tokens": 3}, model="claude-opus"
    )
    transcript = claude_usage(
        {
            "input_tokens": 2,
            "cache_creation_input_tokens": 4,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 4,
                "ephemeral_1h_input_tokens": 0,
            },
            "output_tokens": 3,
        },
        model="claude-opus",
    )

    with pytest.raises(RuntimeError, match="cache_write"):
        _assert_common_parity([result], [transcript], allow_left_extras=True)


def test_live_probe_can_append_repeated_runs_to_one_database(tmp_path):
    db = tmp_path / "metrics.db"
    usage = [
        claude_usage(
            {"input_tokens": 2, "output_tokens": 3},
            model="claude-opus",
            scope="turn",
        )
    ]

    _write_database(db, "claude", "provider-session-1", usage, 1.0)
    _write_database(db, "claude", "provider-session-2", usage, 1.0)

    conn = connect(db, readonly=True)
    assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM usage_measurements").fetchone()[0] == 2
    conn.close()
    assert len(list(tmp_path.glob("metrics-live-smoke-*.telemetry"))) == 2
