import argparse
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

from bobi.brain.base import BrainInvocation, BrainUsage, TurnResult
from bobi.metrics.collector import MetricsCollectorService
from bobi.metrics.providers import claude_usage, codex_usage
from bobi.metrics.runtime import MetricsRuntime
from bobi.metrics.store import connect, migrate
import scripts.live_metrics_smoke as live_smoke
from scripts.live_metrics_smoke import (
    SMOKE_MARKER,
    _assert_common_parity,
    _assert_exact_live_usage,
    _brain_options,
    _codex_sessions_root,
    _codex_parity_expected,
    _configure_installed_smoke_package,
    _experiment_config,
    _find_claude_transcript,
    _isolated_brain_defaults,
    _reset_disposable_transport_state,
    _sanitized_reconciliation_result,
    _smoke_env,
    _start_disposable_supervisor,
    _start_or_join_restarted_manager,
    _stop_disposable_supervisor,
    _verify_experiment_database,
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
    monkeypatch.setenv("FLEET_OPERATOR_TOKEN", "must-not-reach-provider")

    env = _smoke_env(tmp_path)

    assert env["BOBI_METRICS_FAULT_INJECTION"] == "1"
    assert "FLEET_OPERATOR_TOKEN" not in env


def test_arm_fault_requires_provisioned_disposable_home(tmp_path):
    with pytest.raises(SystemExit, match="run provision first"):
        arm_fault(argparse.Namespace(
            bobi_home=tmp_path,
            agent="smoke",
            fault="drop-next-online-usage",
            hold_seconds=0,
        ))


def test_transport_reset_removes_only_broker_bound_disposable_state(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / SMOKE_MARKER).write_text(
        json.dumps({"created_by": "live_metrics_smoke.py"}) + "\n"
    )
    run_root = home / "agents" / "smoke" / "run"
    state = run_root / "state"
    (state / "deployments").mkdir(parents=True)
    (state / "cursors").mkdir()
    stale = [
        state / "bubble.json",
        state / "bubble.lock",
        state / "admin-cursor.json",
        state / "deployments" / "manager.json",
        state / "cursors" / "manager.json",
    ]
    for path in stale:
        path.write_text("stale\n")
    preserved = [
        state / "metrics" / "metrics.db",
        state / "events-retained.jsonl",
        run_root / "workspace" / "LIVE_SMOKE.txt",
    ]
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preserved\n")

    removed = _reset_disposable_transport_state(home, run_root)

    assert set(removed) == set(stale)
    assert all(not path.exists() for path in stale)
    assert all(path.read_text() == "preserved\n" for path in preserved)


@pytest.mark.parametrize("marker", [None, {}, {"created_by": "someone-else"}])
def test_transport_reset_refuses_unmarked_home(tmp_path, marker):
    home = tmp_path / "home"
    home.mkdir()
    if marker is not None:
        (home / SMOKE_MARKER).write_text(json.dumps(marker) + "\n")
    run_root = home / "agents" / "smoke" / "run"
    state = run_root / "state"
    state.mkdir(parents=True)
    bubble = state / "bubble.json"
    bubble.write_text("must survive\n")

    with pytest.raises(RuntimeError, match="provisioned smoke home"):
        _reset_disposable_transport_state(home, run_root)

    assert bubble.read_text() == "must survive\n"


def test_transport_reset_refuses_run_root_outside_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / SMOKE_MARKER).write_text(
        json.dumps({"created_by": "live_metrics_smoke.py"}) + "\n"
    )
    outside = tmp_path / "outside" / "run"
    state = outside / "state"
    state.mkdir(parents=True)
    bubble = state / "bubble.json"
    bubble.write_text("must survive\n")

    with pytest.raises(RuntimeError, match="outside disposable home"):
        _reset_disposable_transport_state(home, outside)

    assert bubble.read_text() == "must survive\n"


def test_transport_supervisor_uses_foreground_sidecar_without_second_reset(
    monkeypatch, tmp_path
):
    captured = {}

    class Process:
        pass

    process = Process()

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(live_smoke.subprocess, "Popen", fake_popen)
    run_root = tmp_path / "run"

    actual, log_file = _start_disposable_supervisor(
        {"BOBI_EVENT_SERVER": "http://localhost:1234"}, "smoke", run_root
    )
    log_file.close()

    assert actual is process
    assert captured["argv"][1:] == [
        "agent", "smoke", "supervise", "--", "--foreground",
    ]
    assert "--fresh" not in captured["argv"]
    assert captured["kwargs"]["env"]["BOBI_EVENT_SERVER"].endswith(":1234")
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["stderr"] is live_smoke.subprocess.STDOUT


def test_transport_supervisor_cleanup_terminates_and_closes_log(tmp_path):
    class Process:
        def __init__(self):
            self.terminated = False
            self.waited = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            self.waited = True
            return 0

    process = Process()
    log_file = (tmp_path / "supervisor.log").open("w")

    _stop_disposable_supervisor(process, log_file)

    assert process.terminated is True
    assert process.waited is True
    assert log_file.closed is True


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


def test_process_kill_recovery_accepts_supervisor_restart_winner(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(
        live_smoke,
        "_bobi",
        lambda env, *argv, **kwargs: calls.append(argv) or (
            "Already running (pid 222). Use restart."
        ),
    )
    monkeypatch.setattr(
        live_smoke,
        "_wait_manager_restarted",
        lambda run_root, previous_pid, timeout: {
            "pid": 222,
            "manager": {"status": "idle"},
        },
    )

    health = _start_or_join_restarted_manager(
        {"BOBI_HOME": str(tmp_path)}, "agent", tmp_path, 111
    )

    assert calls == [("agent", "agent", "start")]
    assert health["pid"] == 222


def test_reconciliation_accepts_idempotent_winner_and_removes_source_path():
    sanitized = _sanitized_reconciliation_result({
        "status": "done",
        "errors": 0,
        "turns_repaired": 0,
        "duplicates_skipped": 3,
        "source_path": "/private/provider/transcript.jsonl",
    })

    assert sanitized["turns_repaired"] == 0
    assert sanitized["duplicates_skipped"] == 3
    assert "source_path" not in sanitized


def test_reconciliation_rejects_errors():
    with pytest.raises(RuntimeError, match="reconciliation failed"):
        _sanitized_reconciliation_result({"status": "done", "errors": 1})


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


def test_phase4_parser_and_vectors_expose_experiment_matrix(tmp_path):
    vectors = Path("tests/fixtures/metrics/jev-live-smoke-vectors.json")
    parsed = parser().parse_args([
        "experiment-matrix",
        "--bobi-home", str(tmp_path),
        "--experiment-id", "metrics-live-smoke-v1",
        "--assignment-vectors", str(vectors),
        "--artifact-dir", str(tmp_path / "artifacts"),
    ])

    fixture = json.loads(vectors.read_text())
    claude, assignments = _experiment_config(fixture, "claude", "")
    codex, _ = _experiment_config(fixture, "codex", "")

    assert parsed.command == "experiment-matrix"
    assert parsed.checks == "single-turn,tool-loop,process-kill,admin,mcp"
    assert set(assignments) == {"control", "treatment"}
    assert [item["model"] for item in claude["variants"]] == ["opus", "sonnet"]
    assert [item["model"] for item in codex["variants"]] == [
        "cx/gpt-5.6-sol(high)", "cx/gpt-6-luna"
    ]


def test_phase4_database_verifier_proves_assignment_and_event_order(
    monkeypatch, tmp_path
):
    fixture = json.loads(
        Path("tests/fixtures/metrics/jev-assignment-vectors.json").read_text()
    )
    monkeypatch.setenv(
        "BOBI_METRICS_EXPERIMENT_JSON", json.dumps(fixture["config"])
    )
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", fixture["secret"])
    runtime = MetricsRuntime(tmp_path, mode="shadow")
    observation = runtime.begin_turn(
        "agent",
        provider="anthropic",
        model_requested="model-control",
        experiment_subject="subject-001",
    )
    usage = BrainUsage(model="model-control", input_tokens=3, output_tokens=1)
    observation.record_result(TurnResult(
        session_id="provider-session",
        usage=[usage],
        invocations=[BrainInvocation(model="model-control", usage=usage)],
    ))
    observation.finish(status="completed")
    assert runtime.close(timeout=2)
    MetricsCollectorService(tmp_path).collect_once()

    result = _verify_experiment_database(
        tmp_path / "state" / "metrics" / "metrics.db",
        experiment_id=fixture["config"]["experiment_id"],
        variant_id="control",
        assignment_key_hash=fixture["vectors"][0]["assignment_key_hash"],
        decisions_before=0,
    )

    assert result["stable"] is True
    assert result["router_before_invocation"] is True
    assert result["report"]["variants"][0]["coverage"][
        "exact_measurement_turns"
    ] == 1


def test_phase3_transport_checks_fail_closed_without_inputs(monkeypatch, tmp_path):
    monkeypatch.delenv("BOBI_ADMIN_URL", raising=False)
    monkeypatch.delenv("FLEET_OPERATOR_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="require tool-loop"):
        live_smoke.installed_matrix(parser().parse_args([
            "installed-matrix", "--bobi-home", str(tmp_path),
            "--checks", "admin,mcp", "--providers", "claude",
        ]))

    with pytest.raises(SystemExit, match="require --admin-url"):
        live_smoke.installed_matrix(parser().parse_args([
            "installed-matrix", "--bobi-home", str(tmp_path),
            "--checks", "tool-loop,admin", "--providers", "claude",
        ]))

    with pytest.raises(SystemExit, match="require FLEET_OPERATOR_TOKEN"):
        live_smoke.installed_matrix(parser().parse_args([
            "installed-matrix", "--bobi-home", str(tmp_path),
            "--checks", "tool-loop,admin", "--providers", "claude",
            "--admin-url", "https://events.example.com",
        ]))

    monkeypatch.setenv("FLEET_OPERATOR_TOKEN", "operator-secret")
    parsed = parser().parse_args([
        "installed-matrix", "--bobi-home", str(tmp_path),
        "--checks", "tool-loop,admin", "--providers", "claude",
        "--admin-url", "https://events.example.com",
    ])
    assert parsed.operator_token == "operator-secret"
    assert "--operator-token" not in parser().format_help()


def test_wait_admin_target_accepts_future_compatible_supervisor(monkeypatch):
    monkeypatch.setattr(
        live_smoke,
        "_http_json",
        lambda *args, **kwargs: (
            200,
            json.dumps({"supervisor": {"version": "1.2.3"}}),
        ),
    )

    live_smoke._wait_admin_target(
        "https://events.example.com",
        token="operator-token",
        fleet="fleet",
        instance="agent",
        timeout=0.1,
    )


def test_admin_transport_keeps_operator_token_off_argv(monkeypatch):
    captured = {}

    def fake_bobi(env, *argv, **kwargs):
        captured["env"] = env
        captured["argv"] = argv
        return json.dumps({"status": "done", "result": {"usage_turn": {}}})

    monkeypatch.setattr(live_smoke, "_bobi", fake_bobi)
    result = live_smoke._admin_metrics_call(
        {"FLEET_OPERATOR_TOKEN": "operator-secret"},
        base_url="https://events.example.com",
        fleet="fleet",
        instance="agent",
        alias="metrics_turn",
        query_args={"turn_id": "turn-1"},
        timeout=30,
    )

    assert result["status"] == "done"
    assert "operator-secret" not in captured["argv"]
    assert "--token" not in captured["argv"]
    assert captured["env"]["FLEET_OPERATOR_TOKEN"] == "operator-secret"


def test_mcp_transport_decodes_matching_sse_json_rpc(monkeypatch):
    payload = {"status": "done", "result": {"usage_turn": {"turn": {}}}}
    response = {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}],
        },
    }
    captured = {}

    def fake_http(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return 200, f"event: message\ndata: {json.dumps(response)}\n\n"

    monkeypatch.setattr(live_smoke, "_http_json", fake_http)

    assert live_smoke._mcp_metrics_call(
        base_url="https://events.example.com/",
        token="operator-secret",
        request_id=7,
        tool="bobi_usage_turn",
        arguments={"fleet": "fleet", "instance": "agent", "turn_id": "turn-1"},
        timeout=30,
    ) == payload
    assert captured["url"] == "https://events.example.com/mcp"
    assert captured["token"] == "operator-secret"
    assert captured["payload"]["id"] == 7


@pytest.mark.parametrize(
    ("granularity", "scope"),
    [("invocation", "invocation"), ("turn", "turn"), ("mixed", "turn")],
)
def test_exact_live_usage_accepts_supported_exact_granularity(granularity, scope):
    _assert_exact_live_usage(
        {
            "totals": {"turns": 1, "input_tokens": 10, "output_tokens": 2},
            "coverage": {"usage_granularity": granularity},
        },
        {"usage_measurements": [{"scope": scope, "is_estimated": 0}]},
    )


@pytest.mark.parametrize(
    ("summary", "turn"),
    [
        (
            {
                "totals": {"turns": 1, "input_tokens": 10, "output_tokens": 2},
                "coverage": {"usage_granularity": "turn"},
            },
            {"usage_measurements": [{"scope": "turn", "is_estimated": 1}]},
        ),
        (
            {
                "totals": {"turns": 1, "input_tokens": 0, "output_tokens": 0},
                "coverage": {"usage_granularity": "turn"},
            },
            {"usage_measurements": [{"scope": "turn", "is_estimated": 0}]},
        ),
    ],
)
def test_exact_live_usage_rejects_estimated_or_empty_usage(summary, turn):
    with pytest.raises(RuntimeError, match="no exact live-provider usage"):
        _assert_exact_live_usage(summary, turn)


def test_phase3_transport_runs_all_admin_and_mcp_queries_with_parity(
        monkeypatch, tmp_path):
    db = tmp_path / "metrics.db"
    conn = connect(db)
    migrate(conn)
    conn.execute(
        "INSERT INTO sessions(session_id,session_name,brain,provider,"
        "started_at_us,status) VALUES(?,?,?,?,?,?)",
        ("session-1", "manager", "claude", "anthropic", 1, "running"),
    )
    conn.execute(
        "INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,"
        "is_user_initiated,started_at_us,status) VALUES(?,?,?,?,?,?,?)",
        ("turn-1", "session-1", 1, "message", 1, 2, "completed"),
    )
    conn.commit()
    conn.close()

    totals = {"turns": 1, "input_tokens": 10}
    coverage = {
        "exact_invocations": 1,
        "estimated_invocations": 0,
        "usage_granularity": "invocation",
    }
    payloads = {
        "summary": {"totals": totals, "coverage": coverage},
        "session": {
            "session": {"session_id": "session-1"},
            "turns": [{"turn_id": "turn-1"}],
        },
        "turn": {
            "turn": {"turn_id": "turn-1"},
            "tool_executions": [{"scope": "tool"}] * 3,
            "usage_measurements": [{"scope": "invocation", "is_estimated": 0}],
        },
        "hotspots": {"hotspots": [{"scope": "tool", "id": "tool-1"}]},
        "experiment": {
            "experiment_id": "metrics-smoke-unassigned",
            "variants": [],
            "outcomes": [],
        },
    }
    alias_to_name = {
        "metrics_summary": "summary",
        "metrics_session": "session",
        "metrics_turn": "turn",
        "metrics_hotspots": "hotspots",
        "metrics_experiment": "experiment",
    }
    tool_to_name = {
        "bobi_usage_summary": "summary",
        "bobi_usage_session": "session",
        "bobi_usage_turn": "turn",
        "bobi_usage_hotspots": "hotspots",
        "bobi_usage_experiment": "experiment",
    }
    admin_calls = []
    mcp_calls = []
    admin_envs = []
    monkeypatch.setattr(live_smoke, "_wait_admin_target", lambda *a, **k: None)

    def admin_call(_env, **kwargs):
        admin_envs.append(_env)
        name = alias_to_name[kwargs["alias"]]
        admin_calls.append((name, kwargs["query_args"]))
        wire = "usage" if name == "summary" else f"usage_{name}"
        return {"status": "done", "result": {wire: payloads[name]}}

    def mcp_call(**kwargs):
        name = tool_to_name[kwargs["tool"]]
        mcp_calls.append((name, kwargs["arguments"]))
        if name == "summary":
            return {
                "complete": True,
                "fleets": [{
                    "fleet": "fleet",
                    "instances": [{
                        "instance": "agent",
                        "fine_grained": {
                            "totals": totals,
                            "coverage": coverage,
                        },
                    }],
                }],
            }
        return {
            "status": "done",
            "result": {f"usage_{name}": payloads[name]},
        }

    monkeypatch.setattr(live_smoke, "_admin_metrics_call", admin_call)
    monkeypatch.setattr(live_smoke, "_mcp_metrics_call", mcp_call)

    live_smoke._run_s3_transport(
        env={"BOBI_HOME": str(tmp_path)},
        base_url="https://events.example.com",
        token="operator-token",
        fleet="fleet",
        instance="agent",
        db=db,
        turn_id="turn-1",
        artifacts=tmp_path,
        run_admin=True,
        run_mcp=True,
        timeout=30,
    )

    assert [name for name, _args in admin_calls] == [
        "summary", "session", "turn", "hotspots", "experiment",
    ]
    assert [name for name, _args in mcp_calls] == [
        "summary", "session", "turn", "hotspots", "experiment",
    ]
    assert mcp_calls[0][1]["session_id"] == "session-1"
    assert all(env["FLEET_OPERATOR_TOKEN"] == "operator-token" for env in admin_envs)
    assert json.loads(
        (tmp_path / "agent-admin-mcp-parity.json").read_text()
    ) == {
        "status": "passed",
        "checks": {
            "summary": True,
            "session": True,
            "turn": True,
            "hotspots": True,
            "experiment": True,
        },
    }


def _run_phase3_experiment_transport(
    monkeypatch,
    tmp_path,
    *,
    admin_experiment,
    mcp_experiment,
):
    db = tmp_path / "metrics.db"
    conn = connect(db)
    migrate(conn)
    conn.execute(
        "INSERT INTO sessions(session_id,session_name,brain,provider,"
        "started_at_us,status) VALUES(?,?,?,?,?,?)",
        ("session-1", "manager", "claude", "anthropic", 1, "running"),
    )
    conn.execute(
        "INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,"
        "is_user_initiated,started_at_us,status) VALUES(?,?,?,?,?,?,?)",
        ("turn-1", "session-1", 1, "message", 1, 2, "completed"),
    )
    conn.commit()
    conn.close()

    totals = {"turns": 1, "input_tokens": 10}
    coverage = {
        "exact_invocations": 1,
        "estimated_invocations": 0,
        "usage_granularity": "invocation",
    }
    payloads = {
        "summary": {"totals": totals, "coverage": coverage},
        "session": {
            "session": {"session_id": "session-1"},
            "turns": [{"turn_id": "turn-1"}],
        },
        "turn": {
            "turn": {"turn_id": "turn-1"},
            "tool_executions": [{"scope": "tool"}] * 3,
            "usage_measurements": [{"scope": "invocation", "is_estimated": 0}],
        },
        "hotspots": {"hotspots": [{"scope": "tool", "id": "tool-1"}]},
    }
    alias_to_name = {
        "metrics_summary": "summary",
        "metrics_session": "session",
        "metrics_turn": "turn",
        "metrics_hotspots": "hotspots",
        "metrics_experiment": "experiment",
    }
    tool_to_name = {
        "bobi_usage_summary": "summary",
        "bobi_usage_session": "session",
        "bobi_usage_turn": "turn",
        "bobi_usage_hotspots": "hotspots",
        "bobi_usage_experiment": "experiment",
    }
    monkeypatch.setattr(live_smoke, "_wait_admin_target", lambda *a, **k: None)

    def admin_call(_env, **kwargs):
        name = alias_to_name[kwargs["alias"]]
        if name == "experiment":
            return admin_experiment
        wire = "usage" if name == "summary" else f"usage_{name}"
        return {"status": "done", "result": {wire: payloads[name]}}

    def mcp_call(**kwargs):
        name = tool_to_name[kwargs["tool"]]
        if name == "experiment":
            return mcp_experiment
        if name == "summary":
            return {
                "complete": True,
                "fleets": [{
                    "fleet": "fleet",
                    "instances": [{
                        "instance": "agent",
                        "fine_grained": {
                            "totals": totals,
                            "coverage": coverage,
                        },
                    }],
                }],
            }
        return {
            "status": "done",
            "result": {f"usage_{name}": payloads[name]},
        }

    monkeypatch.setattr(live_smoke, "_admin_metrics_call", admin_call)
    monkeypatch.setattr(live_smoke, "_mcp_metrics_call", mcp_call)
    live_smoke._run_s3_transport(
        env={"BOBI_HOME": str(tmp_path)},
        base_url="https://events.example.com",
        token="operator-token",
        fleet="fleet",
        instance="agent",
        db=db,
        turn_id="turn-1",
        artifacts=tmp_path,
        run_admin=True,
        run_mcp=True,
        timeout=30,
    )


def test_phase3_transport_accepts_matching_unknown_experiment(monkeypatch, tmp_path):
    terminal = {
        "status": "error",
        "result": {
            "code": "unknown_experiment",
            "experiment_id": "metrics-smoke-unassigned",
        },
    }

    _run_phase3_experiment_transport(
        monkeypatch,
        tmp_path,
        admin_experiment=terminal,
        mcp_experiment=terminal,
    )

    assert json.loads(
        (tmp_path / "agent-admin-mcp-parity.json").read_text()
    )["checks"]["experiment"] is True


@pytest.mark.parametrize("failing_transport", ["admin", "mcp"])
def test_phase3_transport_rejects_other_experiment_errors(
    monkeypatch, tmp_path, failing_transport
):
    unknown = {
        "status": "error",
        "result": {
            "code": "unknown_experiment",
            "experiment_id": "metrics-smoke-unassigned",
        },
    }
    unexpected = {
        "status": "error",
        "result": {"code": "metrics_busy"},
    }

    with pytest.raises(RuntimeError, match="experiment query failed unexpectedly"):
        _run_phase3_experiment_transport(
            monkeypatch,
            tmp_path,
            admin_experiment=(
                unexpected if failing_transport == "admin" else unknown
            ),
            mcp_experiment=(
                unexpected if failing_transport == "mcp" else unknown
            ),
        )
