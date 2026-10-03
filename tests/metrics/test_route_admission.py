import json
from pathlib import Path

from bobi.brain.base import BrainInvocation, BrainUsage, TurnResult
from bobi.metrics.collector import MetricsCollectorService
from bobi.metrics.router import RouterDecision
from bobi.metrics.runtime import MetricsRuntime
from bobi.metrics.store import connect

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
