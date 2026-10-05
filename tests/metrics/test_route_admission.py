import json
from pathlib import Path

import pytest

from bobi.brain.base import BrainInvocation, BrainUsage, TurnResult
from bobi.metrics.collector import MetricsCollectorService
from bobi.metrics.router import RouterDecision
from bobi.metrics.runtime import MetricsRuntime
from bobi.metrics.store import connect
from bobi.sdk import load_session_route, save_session_id, save_session_route

def decision():
    fields = json.loads(Path("tests/fixtures/metrics/historical-router-assignment.json").read_text())
    metadata = json.loads(fields.pop("metadata_json"))
    fields["candidate_models"] = tuple(json.loads(fields.pop("candidate_models_json")))
    return RouterDecision(**fields, config_fingerprint=metadata["config_fingerprint"],
        expected_weight=metadata["expected_weight"],
        expected_weights=tuple(metadata["expected_weights"].items()),
        entry_point="session_start", policy_metadata_json='{"status":"not_called"}')

def test_admission_materializes_policy_decisions_before_invocations(tmp_path):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    assert runtime.admit_route("agent", decision(), brain="codex", role="engineer")
    observation = runtime.begin_turn("agent", provider="openai", model_requested="model-control")
    assert observation.router_decision_id
    usage = BrainUsage(model="model-control", input_tokens=1, output_tokens=1)
    observation.record_result(TurnResult(session_id="provider", usage=[usage],
        invocations=[BrainInvocation(model="model-control", usage=usage)]))
    observation.finish(status="completed")
    assert not runtime.admit_route("agent", decision(), brain="codex")
    assert runtime.close(timeout=2)
    MetricsCollectorService(tmp_path).collect_once()
    database = tmp_path / "state/metrics/metrics.db"
    conn = connect(database, readonly=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM experiment_outcomes").fetchone()[0] == 0
        row = conn.execute("SELECT * FROM router_decisions").fetchone()
        assert json.loads(row["metadata_json"])["policy"]["status"] == "not_called"
        invocation = conn.execute("SELECT router_decision_id FROM llm_invocations").fetchone()
        assert invocation[0] == row["router_decision_id"]
        events = conn.execute("SELECT event_type FROM raw_events ORDER BY producer_sequence").fetchall()
        names = [event[0] for event in events]
        assert names.index("router_decision.recorded") < names.index("invocation.recorded")
    finally:
        conn.close()

def test_rejected_admission_does_not_enroll(tmp_path, monkeypatch):
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    monkeypatch.setattr(runtime, "emit", lambda *args, **kwargs: False)
    assert not runtime.admit_route("agent", decision(), brain="codex")
    observation = runtime.begin_turn("agent", provider="openai")
    assert observation.router_decision_id is None
    assert runtime.close(timeout=2)

def test_route_storage_is_root_scoped_and_cleared_with_session(tmp_path, monkeypatch):
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex")
    first = tmp_path / "first"
    second = tmp_path / "second"
    record = {"experiment_id": "exp", "config_fingerprint": "hash", "model_selected": "cheap"}
    save_session_route("agent", record, root=first)
    assert load_session_route("agent", root=first) == record
    assert load_session_route("agent", root=second) is None
    save_session_id("agent", "provider-session", model="cheap", root=first)
    assert load_session_route("agent", root=first) == record
    save_session_id("agent", "", root=first)
    assert load_session_route("agent", root=first) is None

def test_invalid_and_corrupt_route_records_are_not_reused(tmp_path):
    from bobi.sdk import _sessions_dir

    with pytest.raises(ValueError):
        save_session_route("../escape", {}, root=tmp_path)
    assert load_session_route("../escape", root=tmp_path) is None
    path = _sessions_dir(tmp_path) / "agent.route.json"
    for encoded in ("not-json", "[]", "x" * 65537):
        path.write_text(encoded)
        assert load_session_route("agent", root=tmp_path) is None

def test_failed_atomic_write_preserves_previous_route(tmp_path, monkeypatch):
    save_session_route("agent", {"model_selected": "control"}, root=tmp_path)
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr("bobi.sdk.atomic_write_json", fail)
    with pytest.raises(OSError):
        save_session_route("agent", {"model_selected": "cheap"}, root=tmp_path)
    assert load_session_route("agent", root=tmp_path) == {"model_selected": "control"}


def test_recovery_keeps_route_but_explicit_clear_removes_it(tmp_path, monkeypatch):
    from bobi.sdk import load_session_id

    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex")
    record = {"model_selected": "cheap"}
    save_session_route("agent", record, root=tmp_path)
    save_session_id("agent", "stale-id", model="cheap", root=tmp_path)
    save_session_id("agent", "", root=tmp_path, preserve_route=True)
    assert load_session_id("agent", root=tmp_path) == ""
    assert load_session_route("agent", root=tmp_path) == record
    save_session_id("agent", "recovered-id", model="cheap", root=tmp_path)
    assert load_session_route("agent", root=tmp_path) == record
    save_session_id("agent", "", root=tmp_path)
    assert load_session_route("agent", root=tmp_path) is None
