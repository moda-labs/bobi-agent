from bobi.brain.base import BrainInvocation, BrainUsage, TurnResult
from bobi.metrics.collector import MetricsCollectorService
import pytest

from bobi.metrics.faults import FaultInjectionRefused, arm_fault, read_fault
from bobi.metrics.runtime import MetricsRuntime
from bobi.metrics.store import connect


def _smoke_root(tmp_path):
    home = tmp_path / "home"
    (home / ".bobi-metrics-smoke.json").parent.mkdir(parents=True)
    (home / ".bobi-metrics-smoke.json").write_text("{}\n")
    root = home / "agents" / "smoke" / "run"
    root.mkdir(parents=True)
    return root


def test_fault_injection_is_refused_outside_disposable_home(monkeypatch, tmp_path):
    monkeypatch.setenv("BOBI_METRICS_FAULT_INJECTION", "1")

    runtime = MetricsRuntime(tmp_path / "normal", mode="enabled")

    assert runtime.enabled is False
    assert runtime.health()["init_error"] == "FaultInjectionRefused"


def test_fault_cannot_be_armed_outside_disposable_home(tmp_path):
    with pytest.raises(FaultInjectionRefused, match="disposable metrics smoke home"):
        arm_fault(tmp_path / "normal", "drop-next-online-usage")


def test_drop_online_usage_is_one_shot_and_preserves_reconciliation_metadata(
    monkeypatch, tmp_path
):
    root = _smoke_root(tmp_path)
    monkeypatch.setenv("BOBI_METRICS_FAULT_INJECTION", "1")
    arm_fault(root, "drop-next-online-usage", hold_seconds=0)
    runtime = MetricsRuntime(root, mode="enabled")
    assert runtime._fault_injection_enabled is True
    observation = runtime.begin_turn(
        "agent",
        provider="anthropic",
        brain="claude",
        prompt_bytes=10,
        is_user_initiated=True,
    )
    usage = BrainUsage(
        model="claude-test",
        provider_event_id="message-1",
        input_tokens=10,
        output_tokens=2,
    )
    observation.record_result(TurnResult(
        session_id="provider-session",
        usage=[usage],
        invocations=[BrainInvocation(
            provider_event_id="message-1",
            model="claude-test",
            usage=usage,
        )],
    ))
    observation.finish(status="completed")
    runtime.close()
    service = MetricsCollectorService(root, reconcile_interval=3600)
    service.collect_once()

    state = read_fault(root)
    assert state["status"] == "consumed"
    assert state["turn_id"] == observation.turn_id
    conn = connect(service.db_path, readonly=True)
    try:
        assert conn.execute(
            "SELECT provider_session_id FROM sessions"
        ).fetchone()[0] == "provider-session"
        assert conn.execute(
            "SELECT status FROM turns WHERE turn_id=?", (observation.turn_id,)
        ).fetchone()[0] == "completed"
        assert conn.execute(
            "SELECT COUNT(*) FROM usage_measurements"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM llm_invocations"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_background_turn_cannot_consume_recovery_fault(monkeypatch, tmp_path):
    root = _smoke_root(tmp_path)
    monkeypatch.setenv("BOBI_METRICS_FAULT_INJECTION", "1")
    arm_fault(root, "drop-next-online-usage", hold_seconds=0)
    runtime = MetricsRuntime(root, mode="enabled")
    usage = BrainUsage(
        model="claude-test",
        provider_event_id="message-1",
        input_tokens=10,
        output_tokens=2,
    )
    background = runtime.begin_turn(
        "curator",
        provider="anthropic",
        brain="claude",
        is_user_initiated=False,
    )
    background.record_result(TurnResult(
        session_id="provider-session",
        usage=[usage],
        invocations=[BrainInvocation(
            provider_event_id="message-1",
            model="claude-test",
            usage=usage,
        )],
    ))
    background.finish(status="completed")

    assert read_fault(root)["status"] == "armed"

    foreground = runtime.begin_turn(
        "manager",
        provider="anthropic",
        brain="claude",
        is_user_initiated=True,
    )
    foreground.record_result(TurnResult(
        session_id="provider-session",
        usage=[usage],
        invocations=[BrainInvocation(
            provider_event_id="message-1",
            model="claude-test",
            usage=usage,
        )],
    ))
    foreground.finish(status="completed")
    runtime.close()

    state = read_fault(root)
    assert state["status"] == "consumed"
    assert state["turn_id"] == foreground.turn_id
