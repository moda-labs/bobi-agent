import argparse
import json
import os
import sys

import pytest
import yaml

from bobi.metrics.providers import claude_usage, codex_usage
from bobi.metrics.store import connect, migrate
import scripts.live_metrics_smoke as live_smoke
from scripts.live_metrics_smoke import (
    SMOKE_MARKER,
    _assert_common_parity,
    _brain_options,
    _codex_sessions_root,
    _codex_parity_expected,
    _configure_installed_smoke_package,
    _find_claude_transcript,
    _isolated_brain_defaults,
    _smoke_env,
    _wait_claude_invocation_usage,
    _wait_manager_restarted,
    _write_database,
    arm_fault,
    parser,
    verify_token_parity,
    wait_crash_window,
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


def test_claude_parity_waits_for_transcript_append(monkeypatch, tmp_path):
    usage = claude_usage(
        {"input_tokens": 2, "output_tokens": 3},
        model="claude-opus",
        provider_event_id="message-1",
    )
    attempts = iter(([], [usage]))
    monkeypatch.setattr(
        live_smoke, "parse_claude_transcript", lambda path: next(attempts)
    )

    assert _wait_claude_invocation_usage(
        tmp_path / "transcript.jsonl", {"message-1"}, timeout=0.5
    ) == [usage]


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


def test_smoke_env_forces_fault_injection_on(monkeypatch, tmp_path):
    monkeypatch.setenv("BOBI_METRICS_FAULT_INJECTION", "0")

    assert _smoke_env(tmp_path)["BOBI_METRICS_FAULT_INJECTION"] == "1"


def test_arm_fault_requires_provisioned_disposable_home(tmp_path):
    with pytest.raises(SystemExit, match="run provision first"):
        arm_fault(argparse.Namespace(
            bobi_home=tmp_path,
            agent="smoke",
            fault="drop-next-online-usage",
            hold_seconds=0,
        ))


def test_wait_crash_window_requires_running_turn_tool_and_provider_usage(
    monkeypatch, tmp_path
):
    home = tmp_path / "home"
    (home / SMOKE_MARKER).parent.mkdir(parents=True)
    (home / SMOKE_MARKER).write_text("{}\n")
    root = home / "agents" / "smoke" / "run"
    metrics_root = root / "state" / "metrics"
    metrics_root.mkdir(parents=True)
    (root / "state" / "manager.pid").write_text(str(os.getpid()))
    (metrics_root / "fault.json").write_text(json.dumps({
        "fault": "drop-next-online-usage",
        "status": "consumed",
        "turn_id": "turn-1",
    }))
    db = metrics_root / "metrics.db"
    conn = connect(db)
    migrate(conn)
    conn.execute(
        "INSERT INTO sessions(session_id,session_name,provider_session_id,brain,"
        "provider,started_at_us,status) VALUES(?,?,?,?,?,?,?)",
        ("session-1", "manager", "provider-session", "claude", "anthropic", 1, "running"),
    )
    conn.execute(
        "INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,"
        "is_user_initiated,started_at_us,status) VALUES(?,?,?,?,?,?,?)",
        ("turn-1", "session-1", 1, "message", 1, 1, "running"),
    )
    conn.execute(
        "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,"
        "provider,model_selected,started_at_us,status) VALUES(?,?,?,?,?,?,?)",
        ("inv-1", "turn-1", 1, "anthropic", "claude-test", 1, "completed"),
    )
    conn.execute(
        "INSERT INTO tool_executions(tool_execution_id,turn_id,"
        "triggering_invocation_id,tool_name,tool_kind,started_at_us,status) "
        "VALUES(?,?,?,?,?,?,?)",
        ("tool-1", "turn-1", "inv-1", "Read", "file_read", 1, "completed"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        live_smoke,
        "_provider_records_for_running_turn",
        lambda provider, session_id, started_at_us: (tmp_path / "provider.jsonl", [object()]),
    )

    turn_id = wait_crash_window(argparse.Namespace(
        bobi_home=home,
        agent="smoke",
        provider="claude",
        db=db,
        require_transcript_usage=True,
        require_online_measurement_absent=True,
        timeout=0.5,
    ))

    assert turn_id == "turn-1"


def test_reconciled_parity_accepts_transcript_source(monkeypatch, tmp_path):
    usage = claude_usage(
        {"input_tokens": 10, "output_tokens": 2},
        model="claude-test",
        provider_event_id="message-1",
        scope="turn",
    )
    db = tmp_path / "metrics.db"
    turn_id = _write_database(db, "claude", "provider-session", [usage], 1.0)
    conn = connect(db)
    conn.execute(
        "UPDATE usage_measurements SET measurement_source='claude_transcript'"
    )
    conn.execute(
        "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,"
        "provider,model_selected,provider_event_id,started_at_us,status) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("inv-1", turn_id, 1, "anthropic", "claude-test", "message-1", 1, "completed"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        live_smoke, "_find_claude_transcript", lambda session_id, started_ns: tmp_path / "transcript.jsonl"
    )
    monkeypatch.setattr(live_smoke, "parse_claude_transcript", lambda path: [usage])

    artifact = verify_token_parity(argparse.Namespace(
        agent="smoke",
        provider="claude",
        turn_id=turn_id,
        db=db,
        json_out=None,
        require_reconciled=True,
    ))

    assert artifact["duplicates"] == 0


def test_reconciled_codex_parity_accepts_interrupted_turn(monkeypatch, tmp_path):
    usage = codex_usage(
        {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2},
        model="codex",
        provider_event_id="response-1",
        scope="turn",
    )
    db = tmp_path / "metrics.db"
    turn_id = _write_database(db, "codex", "provider-session", [usage], 1.0)
    conn = connect(db)
    observed_at_us = int(conn.execute(
        "SELECT observed_at_us FROM usage_measurements WHERE turn_id=?",
        (turn_id,),
    ).fetchone()[0])
    conn.execute(
        "UPDATE usage_measurements SET measurement_source='codex_rollout' "
        "WHERE turn_id=?",
        (turn_id,),
    )
    conn.execute(
        "UPDATE turns SET ended_at_us=NULL,status='running' WHERE turn_id=?",
        (turn_id,),
    )
    conn.commit()
    conn.close()
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("")
    monkeypatch.setattr(
        live_smoke, "_find_named_jsonl",
        lambda root, session_id, started_ns: rollout,
    )
    captured = {}

    def fake_turn_usage(path, *, started_at_us, ended_at_us, model):
        captured["ended_at_us"] = ended_at_us
        return usage

    monkeypatch.setattr(live_smoke, "_codex_turn_usage", fake_turn_usage)

    artifact = verify_token_parity(argparse.Namespace(
        agent="smoke",
        provider="codex",
        turn_id=turn_id,
        db=db,
        json_out=None,
        require_reconciled=True,
    ))

    assert captured["ended_at_us"] == observed_at_us
    assert artifact["duplicates"] == 0


def test_wait_manager_restarted_requires_new_live_health_pid(monkeypatch, tmp_path):
    from bobi import manager_health

    run_root = tmp_path / "run"
    state = run_root / "state"
    state.mkdir(parents=True)
    (state / "manager.pid").write_text(str(os.getpid()))
    (state / "manager-health.port").write_text("12345")
    monkeypatch.setattr(
        manager_health,
        "health",
        lambda url, timeout: {
            "status": "ok",
            "pid": os.getpid(),
            "manager": {"status": "idle"},
        },
    )

    health = _wait_manager_restarted(run_root, os.getpid() + 1, timeout=0.5)

    assert health["pid"] == os.getpid()


def test_parser_and_dispatch_expose_phase2_recovery_commands(monkeypatch, tmp_path):
    parsed = parser().parse_args([
        "verify-token-parity", "--agent", "smoke", "--provider", "claude",
        "--turn-id", "turn-1", "--db", str(tmp_path / "metrics.db"),
        "--require-reconciled",
    ])
    assert parsed.require_reconciled is True
    matrix = parser().parse_args([
        "installed-matrix", "--bobi-home", str(tmp_path),
        "--checks", "process-kill", "--providers", "claude",
    ])
    assert matrix.checks == "process-kill"

    called = []
    monkeypatch.setattr(live_smoke, "wait_reconciliation", lambda args: called.append(args.turn_id))
    monkeypatch.setattr(sys, "argv", [
        "live_metrics_smoke.py", "wait-reconciliation",
        "--db", str(tmp_path / "metrics.db"), "--turn-id", "turn-2",
    ])
    live_smoke.main()

    assert called == ["turn-2"]
