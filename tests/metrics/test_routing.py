import asyncio
from dataclasses import asdict, replace
import json
import pytest

from bobi.metrics.router import ExperimentConfig, assign_variant
from bobi.metrics.routing import RoutingContext, resolve_route
from bobi.metrics.runtime import MetricsRuntime


@pytest.mark.parametrize("egress", ["none", "redacted"])
def test_routing_does_not_persist_prompt_or_private_memory(monkeypatch, tmp_path, caplog, egress):
    from bobi.metrics.collector import MetricsCollectorService

    raw = configured()
    raw["policy"]["egress"]["prompt"] = egress
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    ctx = replace(context(raw, "treatment"), prompt=(
        "unique-task-canary-74821 API_KEY=unique-secret-canary-74821 "
        "/private/customer-canary-74821/data.txt\n"
        "## Long-Term Memory\nunique-memory-canary-74821"))
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    outcome = asyncio.run(resolve_route(ctx, runtime=runtime))
    assert outcome.decision is not None
    turn = runtime.begin_turn("agent", provider="openai", model_requested=outcome.model)
    turn.finish(status="completed")
    assert runtime.close(timeout=2)
    MetricsCollectorService(tmp_path).collect_once()
    canaries = [b"unique-task-canary-74821", b"unique-secret-canary-74821",
                b"customer-canary-74821", b"unique-memory-canary-74821"]
    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert any(path.name == "metrics.db" for path in files)
    for path in files:
        contents = path.read_bytes()
        assert all(canary not in contents for canary in canaries), path
    assert all(canary.decode() not in caplog.text for canary in canaries)

def configured():
    return {"experiment_id": "routing-test", "router_name": "bobi-arm", "router_version": "1",
        "policy_version": "p1", "feature_schema_version": "jev-features-v1", "control_model": "control",
        "variants": [{"variant_id": "control", "weight": 0.5, "model": "control"},
                     {"variant_id": "treatment", "weight": 0.5, "policy": "static"}],
        "policy": {"name": "static", "version": "v1", "brain": "codex", "mode": "shadow",
            "candidate_models": ["control", "cheap"], "scope": {"roles": ["engineer"],
            "entry_points": ["session_start"]}, "egress": {"prompt": "none"},
            "credential_env": "ROUTING_TEST_KEY", "options": {"model": "cheap"}}}


@pytest.mark.parametrize("fault,reason", [
    ("secret", "routing_assignment_secret_missing"),
    ("json", "routing_config_invalid"),
    ("schema", "routing_config_invalid"),
    ("adapter", "routing_policy_unavailable"),
    ("missing_config", "routing_config_missing"),
    ("metrics", "routing_metrics_unavailable"),
])
def test_routing_misconfiguration_records_safe_fallback(monkeypatch, tmp_path, caplog, fault, reason):
    from bobi.metrics.collector import MetricsCollectorService
    from bobi.metrics.store import connect

    raw = configured()
    ctx = context(raw, "treatment")
    if fault == "schema":
        raw["policy"]["mode"] = "secret-schema-canary"
    if fault == "adapter":
        monkeypatch.setattr("bobi.metrics.routing.load_policy", lambda config: (
            _ for _ in ()).throw(ValueError("secret-adapter-canary")))
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", "" if fault == "missing_config" else "secret-json-canary" if fault == "json" else json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "" if fault == "secret" else "test-secret")
    runtime = MetricsRuntime(tmp_path, mode="disabled" if fault == "metrics" else "enabled")
    try:
        outcome = asyncio.run(resolve_route(ctx, runtime=runtime))
        assert outcome.model == "configured" and outcome.decision is None
        assert outcome.reason == reason
        if fault == "metrics":
            assert any(getattr(record, "fallback_reason", None) == reason for record in caplog.records)
            return
        turn = runtime.begin_turn(ctx.session_name, provider="openai")
        turn.finish(status="completed")
    finally:
        assert runtime.close(timeout=2)
    collector = MetricsCollectorService(tmp_path)
    collector.collect_once()
    with connect(collector.db_path, readonly=True) as connection:
        metadata = json.loads(connection.execute("SELECT metadata_json FROM sessions").fetchone()[0])
        assert metadata["router_fallback"]["fallback_reason"] == reason
        assert connection.execute("SELECT COUNT(*) FROM router_decisions").fetchone()[0] == 0
    warning = next(record for record in caplog.records if getattr(record, "fallback_reason", None) == reason)
    assert json.loads(warning.message.split(" ", 1)[1])["fallback_reason"] == reason
    assert "secret-schema-canary" not in caplog.text
    assert "secret-json-canary" not in caplog.text
    assert "secret-adapter-canary" not in caplog.text

def test_routing_fallback_survives_telemetry_failure(monkeypatch, tmp_path, caplog):
    raw = configured()
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.delenv("BOBI_METRICS_ASSIGNMENT_SECRET", raising=False)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    def fail_recording(*args, **kwargs):
        raise OSError("secret-recording-canary")
    monkeypatch.setattr(runtime, "admit_route", fail_recording)
    try:
        outcome = asyncio.run(resolve_route(context(raw, "treatment"), runtime=runtime))
        assert outcome.model == "configured"
        assert outcome.reason == "routing_assignment_secret_missing"
        assert "fallback recording failed (OSError)" in caplog.text
        assert "secret-recording-canary" not in caplog.text
    finally:
        assert runtime.close(timeout=2)

def context(raw, arm):
    config = ExperimentConfig.from_mapping(raw)
    subject = next(str(index) for index in range(100) if assign_variant(config, b"test-secret",
        assignment_unit="experiment_subject", assignment_key=str(index))[0].variant_id == arm)
    return RoutingContext("agent", "session_start", "codex", "configured", False,
                          "Do a task", "engineer", True, experiment_subject=subject)


def test_capacity_timeout_does_not_create_a_policy_call(monkeypatch, tmp_path):
    from bobi.metrics.collector import MetricsCollectorService
    from bobi.metrics.policy import CircuitBreaker

    raw = configured()
    raw["policy"].update(mode="enforce", max_in_flight=1, deadline_ms=50)
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    breaker = CircuitBreaker()
    assert breaker.acquire(1)[0] == "acquired"
    monkeypatch.setattr("bobi.metrics.policy.policy_breaker", lambda config: breaker)
    monkeypatch.setattr("bobi.metrics.routing.policy_breaker", lambda config: breaker)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        outcome = asyncio.run(resolve_route(context(raw, "treatment"), runtime=runtime))
        assert outcome.model == "control"
        assert outcome.decision.fallback_reason == "policy_timeout"
        metadata = json.loads(outcome.decision.policy_metadata_json)
        assert metadata["status"] == "skipped" and metadata["call_id"] is None
        runtime.begin_turn("agent", provider="openai").finish(status="completed")
    finally:
        breaker.finish(None, probe=False)
        assert runtime.close(timeout=2)
    MetricsCollectorService(tmp_path).collect_once()
    from bobi.metrics.store import connect

    with connect(tmp_path / "state" / "metrics" / "metrics.db", readonly=True) as conn:
        policy = json.loads(conn.execute(
            "SELECT metadata_json FROM router_decisions"
        ).fetchone()[0])["policy"]
    assert policy["status"] == "skipped"
    assert policy["call_id"] is None


def test_workflow_uses_only_its_checkpoint_for_sticky_routes(monkeypatch, tmp_path):
    from bobi.sdk import load_session_route, save_session_id, save_session_route

    raw = configured()
    raw["policy"]["mode"] = "enforce"
    raw["policy"]["scope"]["entry_points"] = ["workflow_start"]
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex")
    ctx = replace(context(raw, "treatment"), entry_point="workflow_start")
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    outcome = asyncio.run(resolve_route(ctx, runtime=runtime))
    assert outcome.decision and outcome.model == "cheap"
    record = {"brain": "codex", "decision": asdict(outcome.decision)}
    try:
        assert load_session_route("agent", root=tmp_path) is None
    finally:
        assert runtime.close(timeout=2)
    save_session_route("agent", record, root=tmp_path)
    save_session_id("agent", "provider-id", model="cheap", root=tmp_path)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        resumed = asyncio.run(resolve_route(replace(ctx, fresh=False), runtime=runtime))
        assert resumed.model == "configured" and resumed.decision is None
        resumed = asyncio.run(resolve_route(replace(ctx, fresh=False), runtime=runtime, sticky_record=record))
        assert resumed.model == "cheap" and resumed.decision
    finally:
        assert runtime.close(timeout=2)


@pytest.mark.parametrize("fault,reason", [("timeout", "policy_timeout"),
    ("transport", "policy_unavailable"), ("model", "policy_invalid_response"),
    ("version", "policy_version_drift")])
def test_policy_fault_keeps_arm_and_projects_control_execution(monkeypatch, tmp_path, fault, reason):
    from bobi.metrics.collector import MetricsCollectorService
    from bobi.metrics.policy import PolicyError, PolicyResult
    from bobi.metrics.store import connect

    raw = configured()
    raw["policy"]["mode"] = "enforce"
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    class FaultPolicy:
        async def decide(self, request, *, timeout_s):
            if fault == "timeout":
                raise TimeoutError
            if fault == "transport":
                raise PolicyError("policy_unavailable")
            return PolicyResult("unknown" if fault == "model" else "cheap", 1,
                None, "changed" if fault == "version" else "v1", None, None)
    monkeypatch.setattr("bobi.metrics.routing.load_policy", lambda config: FaultPolicy())
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    outcome = asyncio.run(resolve_route(context(raw, "treatment"), runtime=runtime))
    assert outcome.model == "control" and outcome.decision.variant_id == "treatment"
    turn = runtime.begin_turn("agent", provider="openai", model_requested=outcome.model)
    turn.finish(status="completed")
    assert runtime.close(timeout=2)
    collector = MetricsCollectorService(tmp_path)
    collector.collect_once()
    with connect(collector.db_path, readonly=True) as connection:
        row = connection.execute("SELECT * FROM router_decisions").fetchone()
        assert row["model_selected"] == "control" and row["variant_id"] == "treatment"
        assert row["fallback_reason"] == reason
        assert json.loads(row["metadata_json"])["policy"]["status"] == "failed"

def test_shadow_enforce_and_control(monkeypatch, tmp_path):
    raw = configured()
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    for arm, mode, expected in [("treatment", "shadow", "control"),
                                ("treatment", "enforce", "cheap"), ("control", "enforce", "control")]:
        raw["policy"]["mode"] = mode
        monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
        runtime = MetricsRuntime(tmp_path / f"{arm}-{mode}", mode="enabled")
        outcome = asyncio.run(resolve_route(context(raw, arm), runtime=runtime))
        assert outcome.model == expected
        assert outcome.decision.variant_id == arm
        policy = json.loads(outcome.decision.policy_metadata_json)
        assert policy["status"] == ("not_called" if arm == "control" else "decided")
        assert outcome.decision.fallback_reason is None
        assert runtime.close(timeout=2)

def test_ineligible_and_rejected_admission(monkeypatch, tmp_path):
    raw = configured()
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    ctx = context(raw, "treatment")
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    for changes in ({"explicit_model": True}, {"brain": "claude"}, {"role": "manager"},
                    {"prompt": " "}, {"fresh": False, "existing_transcript": True}):
        outcome = asyncio.run(resolve_route(replace(ctx, **changes), runtime=runtime))
        assert outcome.model == "configured" and outcome.decision is None
    monkeypatch.setattr(runtime, "admit_route", lambda *args, **kwargs: False)
    outcome = asyncio.run(resolve_route(ctx, runtime=runtime))
    assert outcome.model == "control" and outcome.reason == "admission_rejected"
    assert runtime.close(timeout=2)

def test_sticky_route_reuses_model_without_policy(monkeypatch, tmp_path):
    from bobi.sdk import save_session_id

    raw = configured()
    raw["policy"]["mode"] = "enforce"
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex-test")
    ctx = context(raw, "treatment")
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    first = asyncio.run(resolve_route(ctx, runtime=runtime))
    assert first.model == "cheap"
    save_session_id("agent", "provider-session", model="cheap", root=tmp_path)
    assert runtime.close(timeout=2)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    def no_policy(*args):
        raise AssertionError("sticky reuse must not load policy")
    monkeypatch.setattr("bobi.metrics.routing.load_policy", no_policy)
    reused = asyncio.run(resolve_route(replace(ctx, fresh=False, prompt=""), runtime=runtime))
    assert reused.model == "cheap"
    assert json.loads(reused.decision.policy_metadata_json)["status"] == "reused"
    raw["policy"]["version"] = "changed"
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    mismatch = asyncio.run(resolve_route(replace(ctx, fresh=False), runtime=runtime))
    assert mismatch.model == "configured" and mismatch.decision is None
    assert runtime.close(timeout=2)


def test_sticky_route_does_not_replace_a_later_explicit_model(monkeypatch, tmp_path):
    from bobi.sdk import save_session_id

    raw = configured()
    raw["policy"]["mode"] = "enforce"
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex")
    ctx = context(raw, "treatment")
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    assert asyncio.run(resolve_route(ctx, runtime=runtime)).model == "cheap"
    assert runtime.close(timeout=2)
    save_session_id("agent", "operator-transcript", model="operator-model", root=tmp_path)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        outcome = asyncio.run(resolve_route(replace(ctx, fresh=False), runtime=runtime))
        assert outcome.model == "configured" and outcome.decision is None
        assert runtime.begin_turn("agent", provider="openai").router_decision_id is None
    finally:
        assert runtime.close(timeout=2)

@pytest.mark.parametrize("arm,mode", [("control", "enforce"), ("treatment", "shadow")])
def test_sticky_record_cannot_change_control_execution_model(monkeypatch, tmp_path, arm, mode):
    from bobi.sdk import load_session_route, save_session_id, save_session_route

    raw = configured()
    raw["policy"]["mode"] = mode
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex-test")
    ctx = context(raw, arm)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    assert asyncio.run(resolve_route(ctx, runtime=runtime)).model == "control"
    save_session_id("agent", "provider-session", model="control", root=tmp_path)
    record = load_session_route("agent", root=tmp_path)
    record["decision"]["model_selected"] = "cheap"
    save_session_route("agent", record, root=tmp_path)
    assert runtime.close(timeout=2)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    outcome = asyncio.run(resolve_route(replace(ctx, fresh=False), runtime=runtime))
    assert outcome.model == "configured" and outcome.decision is None
    assert runtime.close(timeout=2)


@pytest.mark.parametrize("mutation", [None, "assignment_algorithm", "router_version", "expected_weight",
                                     "router_latency_ms", "decided_at_us", "confidence", "cost_usd",
                                     "mode", "version", "recommended_model", "tier", "breaker_state", "redactions",
                                     "call_id", "assignment_unit", "assignment_key_hash",
                                     "candidate_models", "router_reason", "fallback_reason"])
def test_sticky_reuse_discards_unknown_metadata_and_unapproved_reason(monkeypatch, tmp_path, mutation):
    from bobi.sdk import load_session_route

    raw = configured()
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex-test")
    ctx = context(raw, "treatment")
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    assert asyncio.run(resolve_route(ctx, runtime=runtime)).decision
    record = load_session_route("agent", root=tmp_path)
    metadata = json.loads(record["decision"]["policy_metadata_json"])
    metadata.update(raw_prompt="private-task", reason_text="API_KEY=private-value")
    record["decision"]["policy_metadata_json"] = json.dumps(metadata)
    if mutation:
        if mutation in {"confidence", "cost_usd", "mode", "version", "recommended_model", "tier", "breaker_state", "redactions", "call_id"}:
            metadata[mutation] = -1 if mutation in {"confidence", "cost_usd", "redactions"} else "API_KEY=private-value"
            record["decision"]["policy_metadata_json"] = json.dumps(metadata)
        else:
            record["decision"][mutation] = "tampered"
    assert runtime.close(timeout=2)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    outcome = asyncio.run(resolve_route(replace(ctx, fresh=False), runtime=runtime, sticky_record=record))
    if mutation:
        assert outcome.decision is None and outcome.model == "configured"
        assert runtime.close(timeout=2)
        return
    assert outcome.decision
    reused = json.loads(outcome.decision.policy_metadata_json)
    assert "raw_prompt" not in reused and "reason_text" not in reused
    assert runtime.close(timeout=2)

def test_persistence_failure_never_executes_treatment(monkeypatch, tmp_path):
    raw = configured()
    raw["policy"]["mode"] = "enforce"
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    def fail_write(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr("bobi.metrics.routing.save_session_route", fail_write)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    outcome = asyncio.run(resolve_route(context(raw, "treatment"), runtime=runtime))
    assert outcome.model == "control" and outcome.reason == "route_persistence_failed"
    observation = runtime.begin_turn("agent", provider="openai")
    assignment = json.loads(runtime._session_contexts[observation.session_id]["metadata_json"])["router_assignment"]
    assert assignment["model_selected"] == "control"
    assert assignment["fallback_reason"] == "route_persistence_failed"
    observation.finish(status="completed")
    assert runtime.close(timeout=2)

def test_failed_corrective_admission_leaves_no_treatment_projection(monkeypatch, tmp_path):
    from bobi.metrics.collector import MetricsCollectorService
    from bobi.metrics.store import connect

    raw = configured()
    raw["policy"]["mode"] = "enforce"
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    def fail_write(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr("bobi.metrics.routing.save_session_route", fail_write)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    admit = runtime.admit_route
    attempts = []
    def reject_second(*args, **kwargs):
        attempts.append(1)
        return admit(*args, **kwargs) if len(attempts) == 1 else False
    monkeypatch.setattr(runtime, "admit_route", reject_second)
    outcome = asyncio.run(resolve_route(context(raw, "treatment"), runtime=runtime))
    assert outcome.model == "control"
    observation = runtime.begin_turn("agent", provider="openai", model_requested="control")
    assert observation.router_decision_id is None
    observation.finish(status="completed")
    assert runtime.close(timeout=2)
    MetricsCollectorService(tmp_path).collect_once()
    conn = connect(tmp_path / "state/metrics/metrics.db", readonly=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM router_decisions").fetchone()[0] == 0
    finally:
        conn.close()

def test_reason_text_is_opt_in_redacted_and_bounded(monkeypatch, tmp_path):
    from bobi.metrics.policy import PolicyResult

    raw = configured()
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    async def answer(*args, **kwargs):
        kwargs["on_call"]()
        return PolicyResult("cheap", 1, "valid_tier", "v1",
                            "API_KEY=private-value " + "text " * 1000, None), None
    monkeypatch.setattr("bobi.metrics.routing.call_policy", answer)
    for enabled in (False, True):
        raw["policy"]["store_reason_text"] = enabled
        monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
        runtime = MetricsRuntime(tmp_path / str(enabled), mode="enabled")
        outcome = asyncio.run(resolve_route(context(raw, "treatment"), runtime=runtime))
        metadata = json.loads(outcome.decision.policy_metadata_json)
        assert ("reason_text" in metadata) == enabled
        if enabled:
            assert "private-value" not in metadata["reason_text"]
            assert len(metadata["reason_text"].encode()) <= 1024
        assert runtime.close(timeout=2)


@pytest.mark.parametrize("failure", ["policy_low_confidence", "policy_version_drift"])
def test_valid_fallback_answer_keeps_reported_policy_cost(monkeypatch, tmp_path, failure):
    from bobi.metrics.policy import PolicyResult

    raw = configured()
    raw["policy"]["mode"] = "enforce"
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    async def answer(*args, **kwargs):
        kwargs["on_call"]()
        return PolicyResult("cheap", 0.1, None, "v1", None, 0.02), failure
    monkeypatch.setattr("bobi.metrics.routing.call_policy", answer)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    outcome = asyncio.run(resolve_route(context(raw, "treatment"), runtime=runtime))
    assert outcome.model == "control"
    assert outcome.decision.fallback_reason == failure
    metadata = json.loads(outcome.decision.policy_metadata_json)
    assert metadata["status"] == "failed" and metadata["call_id"]
    assert metadata["cost_usd"] == 0.02
    assert metadata["recommended_model"] == "cheap"
    assert runtime.close(timeout=2)

def test_workflow_record_reuses_before_provider_id_exists(monkeypatch, tmp_path):
    from dataclasses import asdict

    raw = configured()
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex-test")
    ctx = context(raw, "treatment")
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    first = asyncio.run(resolve_route(ctx, runtime=runtime))
    record = {"brain": "codex-test", "decision": asdict(first.decision)}
    assert runtime.close(timeout=2)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    def no_policy(*args):
        raise AssertionError("workflow retry must reuse decision")
    monkeypatch.setattr("bobi.metrics.routing.load_policy", no_policy)
    reused = asyncio.run(resolve_route(replace(ctx, fresh=False, prompt=""),
                                      runtime=runtime, sticky_record=record))
    assert reused.decision is not None
    assert json.loads(reused.decision.policy_metadata_json)["status"] == "reused"
    assert runtime.close(timeout=2)

def test_circuit_open_has_no_policy_call_identity(monkeypatch, tmp_path):
    raw = configured()
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    async def unavailable(*args, **kwargs):
        return None, "policy_circuit_open"
    monkeypatch.setattr("bobi.metrics.routing.call_policy", unavailable)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    outcome = asyncio.run(resolve_route(context(raw, "treatment"), runtime=runtime))
    metadata = json.loads(outcome.decision.policy_metadata_json)
    assert metadata["status"] == "skipped" and metadata["call_id"] is None
    assert outcome.model == "control"
    assert runtime.close(timeout=2)
