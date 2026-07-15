"""Tests for resource-aware launch admission."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from bobi import paths
from bobi.launch_admission import (
    LaunchAdmissionPolicy,
    LaunchAdmissionSnapshot,
    build_snapshot,
    classify_init_failure,
    evaluate_launch_admission,
    load_init_health,
    record_init_health,
)
from bobi.sdk import SessionEntry
from bobi.service import manager_session_name


def _snapshot(**overrides):
    data = {
        "active_agents": 0,
        "starting_agents": 0,
        "load_1m": 0.5,
        "cpu_count": 2,
        "mem_available_mb": 2048.0,
        "mem_total_mb": 4096.0,
        "recent_init_failures": 0,
        "recent_init_successes": 0,
        "metrics_readable": True,
    }
    data.update(overrides)
    return LaunchAdmissionSnapshot(**data)


def test_allows_under_cap_low_load_healthy_init_history():
    decision = evaluate_launch_admission(
        _snapshot(active_agents=1, load_1m=1.0, recent_init_successes=1),
        LaunchAdmissionPolicy(max_concurrent_agents=4),
    )

    assert decision.allowed is True
    assert decision.binding_signal == "none"


def test_blocks_at_static_cap():
    decision = evaluate_launch_admission(
        _snapshot(active_agents=4),
        LaunchAdmissionPolicy(max_concurrent_agents=4),
    )

    assert decision.allowed is False
    assert decision.binding_signal == "static_cap"
    assert decision.effective_cap == 4


def test_blocks_when_another_launch_is_starting():
    decision = evaluate_launch_admission(
        _snapshot(starting_agents=1),
        LaunchAdmissionPolicy(max_concurrent_agents=4, max_starting_agents=1),
    )

    assert decision.allowed is False
    assert decision.binding_signal == "starting_agents"


def test_blocks_on_hard_load():
    decision = evaluate_launch_admission(
        _snapshot(load_1m=4.0, cpu_count=2),
        LaunchAdmissionPolicy(
            max_concurrent_agents=4,
            load_per_cpu_soft_limit=1.5,
            load_per_cpu_hard_limit=2.0,
        ),
    )

    assert decision.allowed is False
    assert decision.binding_signal == "load"
    assert decision.load_per_cpu == 2.0


def test_queues_on_soft_load_without_recent_success_signal():
    decision = evaluate_launch_admission(
        _snapshot(load_1m=3.0, cpu_count=2, recent_init_successes=0),
        LaunchAdmissionPolicy(
            max_concurrent_agents=4,
            load_per_cpu_soft_limit=1.5,
            load_per_cpu_hard_limit=2.0,
        ),
    )

    assert decision.allowed is False
    assert decision.binding_signal == "load"


def test_allows_soft_load_with_recent_success_signal():
    decision = evaluate_launch_admission(
        _snapshot(load_1m=3.0, cpu_count=2, recent_init_successes=1),
        LaunchAdmissionPolicy(
            max_concurrent_agents=4,
            load_per_cpu_soft_limit=1.5,
            load_per_cpu_hard_limit=2.0,
        ),
    )

    assert decision.allowed is True


def test_blocks_when_recent_init_failures_exceed_threshold():
    decision = evaluate_launch_admission(
        _snapshot(recent_init_failures=2),
        LaunchAdmissionPolicy(
            max_concurrent_agents=4,
            init_failure_backoff_threshold=2,
        ),
    )

    assert decision.allowed is False
    assert decision.binding_signal == "init_health"


def test_blocks_on_memory_guardrail_only_when_memory_low():
    policy = LaunchAdmissionPolicy(
        max_concurrent_agents=4,
        min_memory_available_mb=512,
    )

    low = evaluate_launch_admission(_snapshot(mem_available_mb=511.0), policy)
    high = evaluate_launch_admission(_snapshot(mem_available_mb=512.0), policy)

    assert low.allowed is False
    assert low.binding_signal == "memory"
    assert high.allowed is True


def test_fails_open_to_static_and_starting_guards_when_metrics_unreadable():
    policy = LaunchAdmissionPolicy(max_concurrent_agents=4)

    allowed = evaluate_launch_admission(
        _snapshot(
            load_1m=100.0,
            cpu_count=1,
            mem_available_mb=1.0,
            metrics_readable=False,
        ),
        policy,
    )
    capped = evaluate_launch_admission(
        _snapshot(active_agents=4, metrics_readable=False),
        policy,
    )

    assert allowed.allowed is True
    assert allowed.binding_signal == "metrics_unreadable"
    assert capped.allowed is False
    assert capped.binding_signal == "static_cap"


def test_build_snapshot_excludes_only_entry_manager_session(tmp_path):
    paths.package_dir(tmp_path).mkdir(parents=True)
    paths.agent_yaml_path(tmp_path).write_text("entry_point: gtm-director\n")
    registry = MagicMock()
    registry.list_active.return_value = [
        SessionEntry(name=manager_session_name(tmp_path), role="gtm-director",
                     status="idle"),
        SessionEntry(name="wf-adhoc-repo-1", role="gtm-director",
                     status="running"),
        SessionEntry(name="wf-adhoc-repo-2", role="engineer",
                     status="starting"),
        SessionEntry(name="wf-adhoc-repo-3", role="manager",
                     status="running"),
        SessionEntry(name="check-1", role="monitor", status="running"),
    ]

    with patch("bobi.launch_admission.get_registry", return_value=registry):
        with patch("bobi.launch_admission.read_host_metrics",
                   return_value=(0.5, 2, 2048.0, 4096.0, True)):
            snapshot = build_snapshot(
                tmp_path, LaunchAdmissionPolicy(max_concurrent_agents=4)
            )

    assert snapshot.active_agents == 3
    assert snapshot.starting_agents == 1


def test_metrics_unreadable_still_blocks_on_init_health():
    decision = evaluate_launch_admission(
        _snapshot(metrics_readable=False, recent_init_failures=2),
        LaunchAdmissionPolicy(
            max_concurrent_agents=4,
            init_failure_backoff_threshold=2,
        ),
    )

    assert decision.allowed is False
    assert decision.binding_signal == "init_health"


def test_init_health_recording_counts_recent_events_and_expires_old(tmp_path, monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr("bobi.launch_admission.time.time", lambda: now[0])

    record_init_health(tmp_path, "init_failure")
    record_init_health(tmp_path, "init_success")
    now[0] = 1_700.0
    record_init_health(tmp_path, "init_failure")

    counts = load_init_health(tmp_path, window_seconds=600)

    assert counts.recent_init_failures == 1
    assert counts.recent_init_successes == 0
    data = json.loads((Path(tmp_path) / "state" / "launch-init-health.json").read_text())
    assert len(data["events"]) == 1


def test_init_health_ignores_malformed_events(tmp_path, monkeypatch):
    monkeypatch.setattr("bobi.launch_admission.time.time", lambda: 1_100.0)
    path = Path(tmp_path) / "state" / "launch-init-health.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "events": [
            "bad",
            {"kind": "init_failure", "ts": "bad"},
            {"kind": "unknown", "ts": 1_000},
            {"kind": "init_failure", "ts": 1_000},
        ]
    }))

    counts = load_init_health(tmp_path, window_seconds=10_000)

    assert counts.recent_init_failures == 1
    assert counts.recent_init_successes == 0


def test_classifies_only_known_initialize_timeout_signature():
    assert classify_init_failure(
        '_send_control_request("initialize") exceeded deadline; '
        "total_cost_usd: 0; model_usage: {}"
    ) is True
    assert classify_init_failure("tool call failed after model_usage was recorded") is False
