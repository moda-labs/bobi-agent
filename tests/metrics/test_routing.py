"""Routing lifecycle, policy, privacy, and fallback regressions."""

import asyncio
import copy
import httpx
import json
import multiprocessing
import pytest
import threading
import time
import yaml
from bobi.brain.base import BrainInvocation, BrainUsage, TurnResult
from bobi.brain.stub import StubBrain
from bobi.inbox import Message
from bobi.metrics.collector import MetricsCollectorService
from bobi.metrics.features import build_features, prepare_task
from bobi.metrics.policies.typesafe import TypeSafePolicy
from bobi.metrics.policy import (
    CircuitBreaker,
    PolicyConfig,
    PolicyError,
    PolicyRequest,
    PolicyResult,
    StaticPolicy,
    call_policy,
    guard_result,
    load_policy,
)
from bobi.metrics.router import (
    ExperimentConfig,
    RouterDecision,
    assign_variant,
    choose_assignment_key,
    load_experiment,
    projection_fields,
    provider_subprocess_env,
    public_config,
)
from bobi.metrics.routing import RoutingContext, resolve_route
from bobi.metrics.runtime import MetricsRuntime
from bobi.metrics.store import connect
from bobi.redact import redact_secrets
from bobi.sdk import load_session_route, save_session_id, save_session_route
from bobi.session import Session
from bobi.setup.actions import redact_secrets as setup_redact
from bobi.subagent import _run_agent_supervised
from bobi.workflow.orchestrator import run_workflow
from bobi.workflow.schema import StepDef, Workflow
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tests.metrics.helpers import assignment_vectors, configured, context, decision


def test_shared_redaction_preserves_setup_import():
    assert setup_redact is redact_secrets
    assert redact_secrets("API_KEY=private-value")[0] == "API_KEY=[redacted]"
    assert redact_secrets("sk-abcdefghijklmnopqrstu-other-private")[0] == "[redacted]"


def test_task_strips_memory_and_redacts_paths_and_credentials():
    prompt = ("Fix /repo/src/main.py using API_KEY=private-value. "
              "Ignore /other/private.txt and C:\\private\\secret.txt\n"
              "## Long-Term Memory\n## Facts\nprivate memory\n## Decisions\nprivate decision")
    task, count = prepare_task(prompt, repo_path="/repo", max_bytes=8192)
    assert "src/main.py" in task
    assert "private-value" not in task and "private memory" not in task
    assert "/other" not in task and "C:\\private" not in task
    assert count == 3


@pytest.mark.parametrize("limit", [1, 2, 4, 5, 6, 11, 20])
def test_utf8_head_tail_respects_byte_cap(limit):
    task, _ = prepare_task("你好" * 30, repo_path="/repo", max_bytes=limit)
    assert len(task.encode("utf-8")) <= limit
    assert "�" not in task


def test_features_none_never_contains_prompt_and_invalid_identifiers_fail():
    args = dict(prompt="private prompt", repo_path="/repo", entry_point="session_start",
                role="engineer", brain="codex", prompt_egress="none")
    features, count = build_features(**args)
    assert "task" not in features and count == 0
    assert features["prompt_bytes"] == 14
    with pytest.raises(ValueError):
        build_features(**{**args, "role": "/private/path"})
    with pytest.raises(ValueError):
        build_features(**{**args, "prompt_egress": "default"})


@pytest.mark.parametrize("root,prompt,expected", [
    ("/repo", 'Read "/repo/source files/main.py"', 'Read "source files/main.py"'),
    ("/repo", "Read '/private/customer files/key.txt'", "Read '[path]'"),
    ("/repo", r"Read \\private-server\customer\key.txt", "Read [path]"),
    (r"C:\repo", r'Read "C:\repo\source files\main.py"', 'Read "source files/main.py"'),
    (r"\\server\repo", r"Read \\server\repo\main.py", "Read main.py"),
    ("/repo", 'Read "/repo/../private/customer files/key.txt"', 'Read "[path]"'),
])
def test_quoted_and_windows_paths_do_not_disclose_external_locations(root, prompt, expected):
    task, _ = prepare_task(prompt, repo_path=root, max_bytes=8192)
    assert task == expected


def test_static_policy_selects_only_an_allowed_model():
    request = PolicyRequest("jev-features-v1", {}, ("control", "cheap"), "control", "v1")
    result = asyncio.run(StaticPolicy("cheap").decide(request, timeout_s=1))
    assert result.model == "cheap"
    assert result.model_version == "v1"
    assert result.confidence == 1.0
    with pytest.raises(ValueError, match="candidate"):
        asyncio.run(StaticPolicy("unknown").decide(request, timeout_s=1))
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(StaticPolicy("cheap").decide(request, timeout_s=0))
    with pytest.raises(ValueError, match="requires a model"):
        StaticPolicy("")


def policy_config():
    raw = assignment_vectors()["config"]
    raw["variants"][1] = {"variant_id": "treatment_jev", "weight": 0.5, "policy": "static"}
    raw["policy"] = {
        "name": "static", "version": "v1", "brain": "codex", "mode": "shadow",
        "candidate_models": ["model-control", "cheap"],
        "scope": {"entry_points": ["session_start"], "roles": ["engineer"]},
        "egress": {"prompt": "none"}, "credential_env": "POLICY_API_KEY",
        "options": {"model": "cheap", "endpoint": "https://example.com"},
    }
    return raw


def test_policy_config_preserves_arm_and_public_fingerprint_contract():
    raw = policy_config()
    parsed = ExperimentConfig.from_mapping(raw)
    assert parsed.variants[1].policy == "static"
    assert parsed.variants[1].model == ""
    assert parsed.policy.deadline_ms == 1000
    public = public_config(parsed)
    assert "endpoint" not in public["policy"]["options"]
    changed = copy.deepcopy(raw)
    changed["policy"]["options"]["endpoint"] = "https://other.example.com"
    assert public_config(ExperimentConfig.from_mapping(changed)) == public
    changed["policy"]["mode"] = "enforce"
    assert public_config(ExperimentConfig.from_mapping(changed)) != public


@pytest.mark.parametrize("field,value", [
    ("mode", "full"), ("brain", "unknown"), ("min_confidence", float("nan")),
    ("min_confidence", True), ("deadline_ms", 49), ("deadline_ms", 3001),
    ("deadline_ms", True), ("max_in_flight", 0), ("store_reason_text", "false"),
    ("credential_env", "bad-name"), ("candidate_models", ["cheap"]),
    ("candidate_models", ["model-control", "model-control"]),
    ("scope", {"entry_points": [], "roles": ["engineer"]}),
    ("egress", {}), ("egress", {"prompt": "redacted", "max_prompt_bytes": 0}),
    ("options", {"nested": [{"api_key": "never-store"}]}),
    ("options", {"rate": float("inf")}), ("unexpected", "field"),
])
def test_policy_config_rejects_invalid_input(field, value):
    raw = policy_config()
    raw["policy"][field] = value
    with pytest.raises(ValueError):
        ExperimentConfig.from_mapping(raw)


def test_policy_and_variant_contract_is_strict():
    for change in ("missing", "mismatch", "both", "unused", "duplicate-control"):
        raw = policy_config()
        if change == "missing":
            del raw["policy"]
        elif change == "mismatch":
            raw["variants"][1]["policy"] = "other"
        elif change == "both":
            raw["variants"][1]["model"] = "cheap"
        elif change == "unused":
            raw["variants"][1] = {"variant_id": "other", "weight": 0.5, "model": "cheap"}
        else:
            raw["variants"][0]["weight"] = 0.25
            raw["variants"].append({"variant_id": "second-control", "weight": 0.25, "model": "model-control"})
        with pytest.raises(ValueError):
            ExperimentConfig.from_mapping(raw)


def test_policy_metadata_preserves_assignment_and_executed_model():
    decision = RouterDecision(
        experiment_id="policy-experiment", variant_id="treatment_jev",
        assignment_unit="run_key", assignment_status="assigned",
        assignment_key_hash="hashed", assignment_algorithm="hmac-sha256-u64-v1",
        cohort=None, router_name="bobi-arm", router_version="1", policy_version="p1",
        feature_schema_version="jev-features-v1", candidate_models=("control", "cheap"),
        model_selected="control", control_model="control", router_score=0.7,
        router_reason="policy_shadow", router_latency_ms=10, fallback_reason=None,
        decided_at_us=100, expected_weight=0.5,
        expected_weights=(("control", 0.5), ("treatment_jev", 0.5)),
        config_fingerprint="fingerprint", entry_point="session_start",
        policy_metadata_json=json.dumps({"mode": "shadow", "recommended_model": "cheap",
                                         "status": "decided", "call_id": "policy-call"}),
    )
    fields = projection_fields(decision)
    metadata = json.loads(fields["metadata_json"])
    assert fields["model_selected"] == "control"
    assert fields["router_score"] == 0.7
    assert fields["fallback_reason"] is None
    assert metadata["policy"]["recommended_model"] == "cheap"
    assert metadata["policy"]["call_id"] == "policy-call"
    assert metadata["config_fingerprint"] == "fingerprint"
    assert metadata["expected_weights"] == {"control": 0.5, "treatment_jev": 0.5}


@pytest.mark.parametrize("loader", ["cli", "child"])
@pytest.mark.parametrize("override", [None, "", "operator-value"])
def test_sdk_blanked_routing_environment_restores_at_runtime_boundary(tmp_path, monkeypatch, loader, override):
    import os

    from bobi.config import load_dotenv
    from bobi.env import _load_dotenv_into
    from bobi.metrics.router import load_experiment
    from tests.metrics.helpers import configured

    values = {"BOBI_METRICS_EXPERIMENT_JSON": json.dumps(configured()),
              "BOBI_METRICS_ASSIGNMENT_SECRET": "synthetic-assignment-secret",
              "ROUTING_TEST_KEY": "synthetic-policy-secret"}
    (tmp_path / "package").mkdir()
    (tmp_path / ".env").write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    environment = provider_subprocess_env(values, blank_inherited=True)
    assert all(not environment.get(key) for key in values)
    if override is not None:
        environment["BOBI_METRICS_ASSIGNMENT_SECRET"] = override
        if not override:
            environment.pop("BOBI_INTERNAL_PROVIDER_CLEARED_ENV", None)
    if loader == "cli":
        for key in values:
            monkeypatch.delenv(key, raising=False)
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        load_dotenv(tmp_path)
        environment = dict(os.environ)
    else:
        _load_dotenv_into(environment, tmp_path)
    expected_secret = values["BOBI_METRICS_ASSIGNMENT_SECRET"] if override is None else override
    assert environment["BOBI_METRICS_ASSIGNMENT_SECRET"] == expected_secret
    assert "BOBI_INTERNAL_PROVIDER_CLEARED_ENV" not in environment
    if override != "":
        assert environment["ROUTING_TEST_KEY"] == values["ROUTING_TEST_KEY"]
        assert load_experiment(environment) is not None


def test_restored_claude_route_projects_decision_and_invocation(tmp_path, monkeypatch):
    import os

    from bobi.brain.base import BrainInvocation, TurnResult
    from bobi.config import load_dotenv
    from bobi.metrics.collector import MetricsCollectorService
    from bobi.metrics.routing import resolve_route
    from bobi.metrics.runtime import MetricsRuntime
    from bobi.metrics.store import connect
    from tests.metrics.helpers import configured, context

    raw = configured()
    raw["control_model"] = "provider/control"
    raw["variants"][0]["model"] = "provider/control"
    raw["policy"].update(brain="claude", mode="enforce",
        candidate_models=["provider/control", "provider/reasoning"],
        options={"model": "provider/reasoning"},
        scope={"roles": ["engineer"], "entry_points": ["workflow_start"]})
    values = {"BOBI_METRICS_EXPERIMENT_JSON": json.dumps(raw),
              "BOBI_METRICS_ASSIGNMENT_SECRET": "test-secret"}
    (tmp_path / "package").mkdir()
    (tmp_path / ".env").write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    for key, value in provider_subprocess_env(values, blank_inherited=True).items():
        monkeypatch.setenv(key, value)
    load_dotenv(tmp_path)
    assert os.environ["BOBI_METRICS_EXPERIMENT_JSON"] == values["BOBI_METRICS_EXPERIMENT_JSON"]
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        for arm, expected in [("control", "provider/control"), ("treatment", "provider/reasoning")]:
            ctx = replace(context(raw, arm), session_name=f"agent-{arm}",
                brain="claude", entry_point="workflow_start")
            outcome = asyncio.run(resolve_route(ctx, runtime=runtime))
            assert outcome.decision is not None and outcome.model == expected
            turn = runtime.begin_turn(ctx.session_name, provider="claude", model_requested=outcome.model)
            turn.record_result(TurnResult(invocations=[BrainInvocation(model=expected.removeprefix("provider/"))]))
            turn.finish(status="completed")
    finally:
        assert runtime.close(timeout=2)
    collector = MetricsCollectorService(tmp_path)
    collector.collect_once()
    with connect(collector.db_path, readonly=True) as connection:
        rows = connection.execute("SELECT d.model_selected, d.candidate_models_json, "
            "i.model_selected AS provider_model FROM router_decisions d "
            "JOIN llm_invocations i USING (router_decision_id) ORDER BY d.decided_at_us").fetchall()
        assert len(rows) == 2
        assert [row["model_selected"] for row in rows] == raw["policy"]["candidate_models"]
        assert [row["provider_model"] for row in rows] == ["control", "reasoning"]
        assert all(json.loads(row["candidate_models_json"]) == raw["policy"]["candidate_models"] for row in rows)
        assert connection.execute("SELECT COUNT(*) FROM raw_events "
            "WHERE projection_state != 'projected'").fetchone()[0] == 0


@pytest.mark.parametrize("assignment_secret", [True, False])
def test_cold_provider_environment_scrubs_configured_policy_credential(monkeypatch, assignment_secret):
    from tests.metrics.helpers import configured

    monkeypatch.setattr("bobi.metrics.policy._secret_names", set())
    raw = configured()
    environment = {"BOBI_METRICS_EXPERIMENT_JSON": json.dumps(raw),
                   "ROUTING_TEST_KEY": "synthetic-policy-secret", "SAFE": "yes"}
    if assignment_secret:
        environment["BOBI_METRICS_ASSIGNMENT_SECRET"] = "synthetic-assignment-secret"
    assert provider_subprocess_env(environment) == {"SAFE": "yes"}


@pytest.mark.parametrize("encoded", ["invalid-json", "[]", "null", '{"policy": []}'])
def test_invalid_experiment_does_not_expose_default_typesafe_credential(monkeypatch, encoded):
    monkeypatch.setattr("bobi.metrics.policy._secret_names", set())
    assert provider_subprocess_env({"BOBI_METRICS_EXPERIMENT_JSON": encoded,
        "TYPESAFE_API_KEY": "synthetic-policy-secret", "SAFE": "yes"}) == {"SAFE": "yes"}


def policy_runtime_config():
    return PolicyConfig("static", "v1", "codex", "shadow", ("control", "cheap"),
                        ("session_start",), ("engineer",), "none", "TEST_POLICY_KEY",
                        '{"model":"cheap"}')


def request():
    return PolicyRequest("jev-features-v1", {}, ("control", "cheap"), "control", "v1")


def test_registry_and_guard():
    policy = load_policy(policy_runtime_config())
    result, reason = asyncio.run(call_policy(policy, request(), policy_runtime_config(), breaker=CircuitBreaker()))
    assert result.model == "cheap" and reason is None
    assert result.cost_usd == 0.0
    assert provider_subprocess_env({"TEST_POLICY_KEY": "private", "SAFE": "yes"}) == {"SAFE": "yes"}
    for confidence in (None, True, float("nan"), float("inf"), -1, 2):
        assert guard_result(replace(result, confidence=confidence), policy_runtime_config()) == "policy_invalid_response"
    assert guard_result(replace(result, model="bad", model_version="bad"), policy_runtime_config()) == "policy_invalid_response"
    assert guard_result(replace(result, model_version="bad", confidence=0), policy_runtime_config()) == "policy_version_drift"
    for changes in ({"confidence": float("nan")}, {"cost_usd": float("inf")},
                    {"cost_usd": "private-value"}, {"confidence": True}):
        assert guard_result(replace(result, model_version="bad", **changes), policy_runtime_config()) == "policy_invalid_response"
    assert guard_result(replace(result, confidence=0.1), policy_runtime_config()) == "policy_low_confidence"
    for changes in ({"tier": {}}, {"reason_text": []}, {"model_version": None}, {"model": []}):
        assert guard_result(replace(result, **changes), policy_runtime_config()) == "policy_invalid_response"
    with pytest.raises(ValueError):
        load_policy(replace(policy_runtime_config(), options_json='{"unexpected":1}'))


@pytest.mark.parametrize("reason", ["API_KEY=private-value", [], "policy_low_confidence"])
def test_plugin_error_reason_is_restricted_to_safe_failure_codes(reason):
    class BrokenPolicy:
        async def decide(self, request, *, timeout_s):
            raise PolicyError(reason)
    breaker = CircuitBreaker()
    result, failure = asyncio.run(call_policy(BrokenPolicy(), request(), policy_runtime_config(), breaker=breaker))
    assert result is None and failure == "policy_unavailable"
    assert breaker.health()["last_failure_kind"] == "policy_unavailable"


def test_breaker_threshold_probe_cooldown_and_auth():
    now = [0.0]
    breaker = CircuitBreaker(clock=lambda: now[0])
    for _ in range(5):
        assert breaker.acquire(8) == ("acquired", False)
        breaker.finish("policy_timeout", probe=False)
    assert breaker.acquire(8)[0] == "open"
    now[0] = 30
    assert breaker.acquire(8) == ("acquired", True)
    assert breaker.acquire(8)[0] == "open"
    breaker.finish("policy_unavailable", probe=True)
    now[0] = 89
    assert breaker.acquire(8)[0] == "open"
    now[0] = 90
    assert breaker.acquire(8) == ("acquired", True)
    breaker.finish(None, probe=True)
    assert breaker.health()["state"] == "closed"
    assert breaker.acquire(8) == ("acquired", False)
    breaker.finish("policy_unavailable", probe=False, authentication=True)
    now[0] += 599
    assert breaker.acquire(8)[0] == "open"
    now[0] += 1
    assert breaker.acquire(8) == ("acquired", True)
    breaker.finish(None, probe=True, cancelled=True)
    assert breaker.acquire(8) == ("acquired", True)
    breaker.finish(None, probe=True)


def test_failure_window_and_valid_low_confidence():
    breaker = CircuitBreaker()
    for index in range(10):
        assert breaker.acquire(1)[0] == "acquired"
        breaker.finish("policy_invalid_response" if index % 2 else None, probe=False)
    assert breaker.health()["state"] == "open"


def test_concurrent_authentication_failures_log_once_until_recovery(caplog):
    now = [0.0]
    breaker = CircuitBreaker(clock=lambda: now[0])
    for _ in range(2):
        assert breaker.acquire(2) == ("acquired", False)
    for _ in range(2):
        breaker.finish("policy_unavailable", probe=False, authentication=True)
    assert len(caplog.records) == 1
    assert "600 seconds" in caplog.records[0].message
    now[0] = 600
    assert breaker.acquire(2) == ("acquired", True)
    breaker.finish(None, probe=True)
    assert breaker.acquire(2) == ("acquired", False)
    breaker.finish("policy_unavailable", probe=False, authentication=True)
    assert len(caplog.records) == 2


def test_late_authentication_failure_does_not_restart_maximum_cooldown():
    now = [0.0]
    breaker = CircuitBreaker(clock=lambda: now[0])
    assert breaker.acquire(2) == ("acquired", False)
    assert breaker.acquire(2) == ("acquired", False)
    breaker.finish("policy_unavailable", probe=False, authentication=True)
    now[0] = 100
    breaker.finish("policy_unavailable", probe=False, authentication=True)
    now[0] = 599
    assert breaker.acquire(2) == ("open", False)
    now[0] = 600
    assert breaker.acquire(2) == ("acquired", True)
    breaker.finish(None, probe=True)
    assert breaker.health()["state"] == "closed"


def test_deadline_includes_capacity_wait_and_releases_cancelled_call():
    async def exercise():
        breaker = CircuitBreaker()
        started = asyncio.Event()
        class SlowPolicy:
            async def decide(self, request, *, timeout_s):
                started.set()
                await asyncio.sleep(10)
        first = asyncio.create_task(call_policy(SlowPolicy(), request(), policy_runtime_config(), breaker=breaker))
        await started.wait()
        result, reason = await call_policy(load_policy(policy_runtime_config()), request(),
                                          replace(policy_runtime_config(), max_in_flight=1, deadline_ms=50), breaker=breaker)
        assert result is None and reason == "policy_timeout"
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert breaker.acquire(1)[0] == "acquired"
        breaker.finish(None, probe=False)
    asyncio.run(exercise())


def test_capacity_is_shared_across_thread_owned_event_loops():
    breaker = CircuitBreaker()
    lock = threading.Lock()
    counters = {"active": 0, "peak": 0}
    class Policy:
        async def decide(self, request, *, timeout_s):
            with lock:
                counters["active"] += 1
                counters["peak"] = max(counters["peak"], counters["active"])
            try:
                await asyncio.sleep(0.02)
                return PolicyResult("cheap", 1, None, "v1", None, None)
            finally:
                with lock:
                    counters["active"] -= 1
    def run(_):
        return asyncio.run(call_policy(Policy(), request(), replace(policy_runtime_config(), max_in_flight=1), breaker=breaker))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(4)))
    assert counters["peak"] == 1
    assert all(reason is None for _, reason in results)


def test_auth_errors_open_breaker_without_exposing_error_text():
    class Policy:
        async def decide(self, request, *, timeout_s):
            raise PolicyError("policy_unavailable", authentication=True)
    breaker = CircuitBreaker()
    assert asyncio.run(call_policy(Policy(), request(), policy_runtime_config(), breaker=breaker)) == (None, "policy_unavailable")
    assert breaker.health()["state"] == "open"


def test_health_preserves_multiple_endpoints_without_exposing_urls(monkeypatch):
    from bobi.metrics import policy

    first = CircuitBreaker()
    second = CircuitBreaker()
    second.acquire(1)
    second.finish("policy_unavailable", probe=False, authentication=True)
    monkeypatch.setattr(policy, "_breakers", {
        ("static", "https://private-first.example"): first,
        ("static", "https://private-second.example"): second,
    })
    health = policy.policy_health()
    assert [item["state"] for item in health["static"]] == ["closed", "open"]
    assert "private" not in str(health)


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


def _assign_in_process(payload):
    config, secret, unit, key = payload
    result = assign_variant(config, secret, assignment_unit=unit, assignment_key=key)
    return result[0].variant_id, result[1], result[2]


def test_golden_assignment_vectors_are_stable_across_processes():
    fixture = assignment_vectors()
    config = ExperimentConfig.from_mapping(fixture["config"])
    secret = fixture["secret"].encode()
    inputs = []
    expected = []
    for vector in fixture["vectors"]:
        inputs.append((config, secret, fixture["assignment_unit"], vector["assignment_key"]))
        expected.append((vector["variant_id"], vector["bucket"], vector["assignment_key_hash"]))
    assert [_assign_in_process(item) for item in inputs] == expected
    with multiprocessing.get_context("spawn").Pool(2) as pool:
        assert pool.map(_assign_in_process, inputs) == expected


def test_assignment_key_precedence_and_unassigned_state():
    assert choose_assignment_key(
        experiment_subject="subject", run_key="run", session_id="session"
    ) == ("experiment_subject", "subject")
    assert choose_assignment_key(run_key="run", session_id="session") == ("run_key", "run")
    assert choose_assignment_key(session_id="session") == ("session_id", "session")
    assert choose_assignment_key() == (None, None)


@pytest.mark.parametrize("selected_index", [0, 1])
def test_full_allocation_never_assigns_zero_weight_arm(selected_index, monkeypatch):
    fixture = assignment_vectors()
    raw = fixture["config"]
    for index, variant in enumerate(raw["variants"]):
        variant["weight"] = int(index == selected_index)
    config = ExperimentConfig.from_mapping(raw)
    for index in range(1000):
        selected, _, _ = assign_variant(config, fixture["secret"].encode(),
            assignment_unit="run_key", assignment_key=f"synthetic-{index}")
        assert selected.variant_id == config.variants[selected_index].variant_id

    class BoundaryDigest:
        def digest(self):
            return b"\xff" * 32

        def hexdigest(self):
            return self.digest().hex()

    monkeypatch.setattr("bobi.metrics.router.hmac.new", lambda *args, **kwargs: BoundaryDigest())
    selected, _, _ = assign_variant(config, b"synthetic-secret",
        assignment_unit="run_key", assignment_key="boundary")
    assert selected.variant_id == config.variants[selected_index].variant_id


@pytest.mark.parametrize("weights", [(-0.1, 1.1), (0, 0), (1, 1)])
def test_invalid_allocation_weights_are_rejected(weights):
    raw = assignment_vectors()["config"]
    for variant, weight in zip(raw["variants"], weights):
        variant["weight"] = weight
    with pytest.raises(ValueError, match="weight"):
        ExperimentConfig.from_mapping(raw)


def test_config_is_strict_and_requires_runtime_secret():
    fixture = assignment_vectors()
    encoded = json.dumps(fixture["config"])
    config, secret = load_experiment({
        "BOBI_METRICS_EXPERIMENT_JSON": encoded,
        "BOBI_METRICS_ASSIGNMENT_SECRET": fixture["secret"],
    })
    assert config.experiment_id == "jev-routing-v1"
    assert secret == fixture["secret"].encode()
    with pytest.raises(ValueError, match="ASSIGNMENT_SECRET"):
        load_experiment({"BOBI_METRICS_EXPERIMENT_JSON": encoded})
    with pytest.raises(ValueError, match="unsupported experiment config fields"):
        ExperimentConfig.from_mapping({**fixture["config"], "typo": True})
    invalid_control = {**fixture["config"], "control_model": "not-a-variant"}
    with pytest.raises(ValueError, match="control_model"):
        ExperimentConfig.from_mapping(invalid_control)


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


@pytest.fixture
def fault_server():
    state = {"fault": None, "calls": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["calls"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if state["fault"] == "timeout":
                time.sleep(0.3)
                return
            status = {"5xx": 503, "401": 401, "403": 403, "429": 429}.get(state["fault"], 200)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if state["fault"] == "malformed":
                self.wfile.write(b"invalid JSON")
                return
            self.wfile.write(json.dumps({
                "model": "changed" if state["fault"] == "version" else "jev-1.13.0",
                "answers": {"route": {"type": "choice",
                    "choice": "unknown" if state["fault"] == "invalid_model" else "cheap",
                    "confidence": 0.84 if state["fault"] == "low_confidence" else 1,
                    "probabilities": {"cheap": 1, "control": 0}}},
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("entry_point,mode,fault,reason", [
    ("session_start", "shadow", None, None),
    ("session_start", "enforce", None, None),
    ("subagent_phase", "shadow", None, None),
    ("subagent_phase", "enforce", None, None),
    ("subagent_persistent", "shadow", None, None),
    ("subagent_persistent", "enforce", None, None),
    ("subagent_supervised", "shadow", None, None),
    ("subagent_supervised", "enforce", None, None),
    ("workflow_start", "shadow", None, None),
    ("workflow_start", "enforce", None, None),
    ("session_start", "shadow", "typesafe_ok", None),
    ("session_start", "enforce", "typesafe_ok", None),
    ("session_start", "enforce", "timeout", "policy_timeout"),
    ("session_start", "enforce", "5xx", "policy_unavailable"),
    ("session_start", "enforce", "invalid_model", "policy_invalid_response"),
    ("session_start", "enforce", "version", "policy_version_drift"),
    ("session_start", "enforce", "401", "policy_unavailable"),
    ("session_start", "enforce", "403", "policy_unavailable"),
    ("session_start", "enforce", "429", "policy_unavailable"),
    ("session_start", "enforce", "malformed", "policy_invalid_response"),
    ("session_start", "enforce", "low_confidence", "policy_low_confidence"),
    ("workflow_start", "shadow", "timeout", "policy_timeout"),
    ("subagent_supervised", "enforce", "low_confidence", "policy_low_confidence"),
    ("subagent_persistent", "shadow", "malformed", "policy_invalid_response"),
])
def test_route_executes_and_projects_a_scripted_turn(bobi_install, monkeypatch, entry_point,
                                                    mode, fault, reason, fault_server):
    monkeypatch.setattr("bobi.metrics.policy._breakers", {})
    raw = configured()
    raw["policy"].update(mode=mode, scope={
        "roles": ["engineer"], "entry_points": [entry_point]})
    failed_policy = reason is not None
    executed_model = "control" if failed_policy or mode == "shadow" else "cheap"
    endpoint, server_state = fault_server
    if fault:
        raw["policy"].update(name="typesafe-jev", version="jev-1.13.0", deadline_ms=200,
            options={"instructions": "route", "criteria": {"control": "full", "cheap": "small"}})
        raw["variants"][1]["policy"] = "typesafe-jev"
        server_state["fault"] = fault
        monkeypatch.setenv("ROUTING_TEST_KEY", "test-only-key")

        def local_policy(config):
            policy = TypeSafePolicy(config)
            policy.endpoint = endpoint
            return policy

        monkeypatch.setattr("bobi.metrics.routing.load_policy", local_policy)
    ctx = replace(context(raw, "treatment"), entry_point=entry_point)
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_SUBJECT", ctx.experiment_subject)
    monkeypatch.setenv("BOBI_STUB_BRAIN", "1")
    monkeypatch.setenv("BOBI_BRAIN", "codex")
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex")
    monkeypatch.setattr("bobi.subagent.session_brain_label", lambda: "codex")
    brain = StubBrain()
    monkeypatch.setattr("bobi.brain.get_brain", lambda *args: brain)
    monkeypatch.setattr("bobi.session.get_brain", lambda *args: brain)
    monkeypatch.setattr("bobi.events.publish.post_event", lambda *args, **kwargs: True)
    root = bobi_install.repo_path
    runtime = MetricsRuntime(root, mode="enabled")
    monkeypatch.setattr("bobi.metrics.runtime._runtime", runtime)
    session = None
    try:
        if entry_point == "workflow_start":
            output = {}
            assert run_workflow(Workflow(name="routed", steps=[
                StepDef(name="work", prompt="__stub__:options", agent="engineer")]),
                task="test", repo="test", cwd=str(root), run_key="routing", collect=output)
            text = output["final_text"]
        elif entry_point == "subagent_supervised":
            result = asyncio.run(_run_agent_supervised("__stub__:options", str(root),
                "routing", "work", 5, role="engineer", fresh=True))
            assert not result.error
            text = result.final_text
        else:
            session = Session("agent", str(root), fresh=True, role="engineer",
                extra_options={"model": "configured"}, routing=ctx)
            assert session.start("__stub__:options", timeout=5)
            text = session._last_response
        assert json.loads(text)["model"] == executed_model
        if session is not None and not failed_policy:
            session.stop()
            assert runtime.close(timeout=2)
            runtime = MetricsRuntime(root, mode="enabled")
            monkeypatch.setattr("bobi.metrics.runtime._runtime", runtime)

            def no_policy(config):
                raise AssertionError("resume must not load a policy")

            monkeypatch.setattr("bobi.metrics.routing.load_policy", no_policy)
            original_make_session = brain.make_session
            resume_attempts = []

            def recovering_session(**kwargs):
                client = original_make_session(**kwargs)
                resume_attempts.append(kwargs.get("resume"))
                if kwargs.get("resume"):
                    async def stale_connect():
                        raise RuntimeError("test-only stale provider token")
                    client.connect = stale_connect
                else:
                    assert load_session_route("agent", root=root) is not None
                return client

            monkeypatch.setattr(brain, "make_session", recovering_session)
            session = Session("agent", str(root), fresh=False, role="engineer",
                extra_options={"model": "configured"}, routing=ctx)
            assert session.start("__stub__:options", timeout=5)
            assert json.loads(session._last_response)["model"] == executed_model
            assert resume_attempts == ["stub-session", None]
            candidate = original_make_session(options={"model": executed_model})
            asyncio.run_coroutine_threadsafe(candidate.connect(), session._loop).result(timeout=5)
            asyncio.run_coroutine_threadsafe(session._commit_rotation(candidate, "test"),
                                            session._loop).result(timeout=5)
            assert load_session_route("agent", root=root) is not None
            asyncio.run_coroutine_threadsafe(session._process_message(
                Message(id="rotation", sender="test", text="__stub__:options")),
                session._loop).result(timeout=5)
            assert json.loads(session._last_response)["model"] == executed_model
    finally:
        if session is not None:
            session.stop()
        assert runtime.close(timeout=2)
    MetricsCollectorService(root).collect_once()
    with connect(root / "state/metrics/metrics.db", readonly=True) as connection:
        row = connection.execute("SELECT * FROM router_decisions").fetchone()
        assert row["model_selected"] == executed_model
        assert row["variant_id"] == "treatment" and row["fallback_reason"] == reason
        metadata = json.loads(row["metadata_json"])
        assert metadata["entry_point"] == entry_point
        assert metadata["policy"]["status"] == ("failed" if failed_policy else "decided")
        assert metadata["policy"]["call_id"]
        if fault is None:
            assert metadata["policy"]["cost_usd"] == 0.0
        assert connection.execute("SELECT status FROM turns").fetchone()[0] == "completed"
        if session is not None and not failed_policy:
            decisions = connection.execute("SELECT metadata_json FROM router_decisions ORDER BY decided_at_us").fetchall()
            assert [json.loads(row[0])["policy"]["status"] for row in decisions] == ["decided", "reused", "reused"]
    if entry_point != "workflow_start":
        name = session.name if session is not None else "engineer-routing-work"
        assert load_session_route(name, root=root)["decision"]["model_selected"] == executed_model
    assert len(server_state["calls"]) == (1 if fault else 0)
    assert all("task" not in call["state"] for call in server_state["calls"])


def test_documented_operator_configuration_validates_offline():
    from bobi.metrics.router import ExperimentConfig

    text = Path("docs/JEV_ROUTER_DATA_FLOW.md").read_text().split("## 9. Configuration", 1)[1]
    raw = json.loads(text.split("```json", 1)[1].split("```", 1)[0])
    documented = ExperimentConfig.from_mapping(raw)
    TypeSafePolicy(documented.policy)
    assert documented.policy.mode == "shadow"
    assert documented.policy.prompt_egress == "none"


def typesafe_policy_config():
    return PolicyConfig("typesafe-jev", "jev-1.13.0", "codex", "shadow",
        ("control", "cheap"), ("session_start",), ("engineer",), "none",
        "TEST_TYPESAFE_KEY", json.dumps({"instructions": "Choose a model",
        "criteria": {"control": "Complex task", "cheap": "Simple task"}}))


def body():
    return {"model": "jev-1.13.0", "answers": {"route": {"type": "choice",
        "choice": "cheap", "confidence": 0.94,
        "probabilities": {"cheap": 0.97, "control": 0.03}}},
        "usage": {"input_tokens": 100, "output_tokens": 20}}


def run(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setenv("TEST_TYPESAFE_KEY", "test-only-secret")
    monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
                        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))
    request = PolicyRequest("jev-features-v1", {"prompt_bytes": 10},
                            ("control", "cheap"), "control", "jev-1.13.0")
    return asyncio.run(TypeSafePolicy(typesafe_policy_config()).decide(request, timeout_s=1))


def test_real_wire_contract_is_one_bounded_call(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        raw = json.loads(request.content)
        assert raw["model"] == "jev-1.13.0"
        assert raw["state"] == {"prompt_bytes": 10}
        assert raw["questions"]["route"]["type"] == "choice"
        assert request.headers["authorization"] == "Bearer test-only-secret"
        return httpx.Response(200, json=body())
    result = run(monkeypatch, handler)
    assert result.model == "cheap" and result.model_version == "jev-1.13.0"
    assert result.cost_usd is None and result.reason_text is None
    assert len(calls) == 1


@pytest.mark.parametrize("status,reason,auth", [
    (401, "policy_unavailable", True), (403, "policy_unavailable", True),
    (429, "policy_unavailable", False), (529, "policy_unavailable", False),
    (422, "policy_invalid_response", False), (302, "policy_invalid_response", False),
])
def test_http_errors_are_redacted_and_never_retried(monkeypatch, status, reason, auth):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private vendor response", headers={"location": "https://other.example"})
    with pytest.raises(PolicyError) as caught:
        run(monkeypatch, handler)
    assert caught.value.reason == reason and caught.value.authentication == auth
    assert "private" not in str(caught.value) and len(calls) == 1


@pytest.mark.parametrize("mutation", ["unknown", "nan", "sum", "usage", "oversized"])
def test_malformed_vendor_response_fails_closed(monkeypatch, mutation):
    raw = body()
    if mutation == "unknown":
        raw["answers"]["route"]["choice"] = "not-a-candidate"
    elif mutation == "nan":
        raw["answers"]["route"]["confidence"] = float("nan")
    elif mutation == "sum":
        raw["answers"]["route"]["probabilities"]["control"] = 0.5
    elif mutation == "usage":
        raw["usage"]["input_tokens"] = True
    else:
        raw["extra"] = "x" * 65536
    with pytest.raises(PolicyError, match="policy_invalid_response"):
        run(monkeypatch, lambda request: httpx.Response(200, content=json.dumps(raw)))


def test_adapter_validates_endpoint_pin_and_criteria():
    for options in (
        {"endpoint": "http://example.com", "instructions": "route", "criteria": {"control": "a", "cheap": "b"}},
        {"instructions": "route", "criteria": {"cheap": "b"}},
        {"instructions": "route", "criteria": {"control": "a", "cheap": "b"}, "unknown": 1},
    ):
        with pytest.raises(ValueError):
            TypeSafePolicy(replace(typesafe_policy_config(), options_json=json.dumps(options)))
    with pytest.raises(ValueError, match="pinned"):
        TypeSafePolicy(replace(typesafe_policy_config(), version="jev-latest"))


@pytest.mark.parametrize("features", [{"task": "界" * 50000}, {"confidence": float("nan")}])
def test_request_bounds_reject_before_network(monkeypatch, features):
    monkeypatch.setenv("TEST_TYPESAFE_KEY", "test-only-secret")
    def no_client(**kwargs):
        raise AssertionError("invalid request must not create an HTTP client")
    monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient", no_client)
    request = PolicyRequest("jev-features-v1", features,
                            ("control", "cheap"), "control", "jev-1.13.0")
    with pytest.raises(PolicyError, match="policy_invalid_response"):
        asyncio.run(TypeSafePolicy(typesafe_policy_config()).decide(request, timeout_s=1))


@pytest.mark.parametrize("error,reason", [
    (httpx.ConnectTimeout, "policy_timeout"),
    (httpx.ReadTimeout, "policy_timeout"),
    (httpx.ConnectError, "policy_unavailable"),
    (httpx.RemoteProtocolError, "policy_unavailable"),
])
def test_transport_errors_are_sanitized_and_not_retried(monkeypatch, error, reason):
    calls = []
    def handler(request):
        calls.append(request)
        raise error("API_KEY=private-vendor-error", request=request)
    with pytest.raises(PolicyError) as caught:
        run(monkeypatch, handler)
    assert caught.value.reason == reason
    assert "private" not in str(caught.value)
    assert len(calls) == 1


def test_oversized_stream_is_closed_without_consuming_remaining_chunks(monkeypatch):
    state = {"chunks": 0, "closed": False}
    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(3):
                state["chunks"] += 1
                yield b"x" * 40000
        async def aclose(self):
            state["closed"] = True
    with pytest.raises(PolicyError, match="policy_invalid_response"):
        run(monkeypatch, lambda request: httpx.Response(200, stream=OversizedStream()))
    assert state == {"chunks": 2, "closed": True}


def test_adapter_guard_with_loopback_timeout(monkeypatch):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            time.sleep(1)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("TEST_TYPESAFE_KEY", "test-only-secret")
    policy = TypeSafePolicy(typesafe_policy_config())
    policy.endpoint = f"http://127.0.0.1:{server.server_port}/"
    request = PolicyRequest("jev-features-v1", {"prompt_bytes": 10},
                            ("control", "cheap"), "control", "jev-1.13.0")
    try:
        started = time.monotonic()
        result, failure = asyncio.run(call_policy(policy, request,
            replace(typesafe_policy_config(), deadline_ms=200), breaker=CircuitBreaker()))
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert failure == "policy_timeout"
    assert len(calls) == 1
    assert result is None and elapsed < 0.8


def test_slack_socket_mode_token_is_optional():
    config = yaml.safe_load((Path(__file__).resolve().parents[2] / "agents/eng-team/agent.yaml").read_text())
    slack = next(service for service in config["services"] if service["name"] == "slack")
    assert slack["credentials"]["app_token"] == "${SLACK_APP_TOKEN:-}"
    assert slack["channels"] == "${SLACK_CHANNELS}"


def test_session_construction_preserves_configured_model(
    bobi_install, monkeypatch
):
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", "invalid-legacy-config")
    routed = Session(
        name="experiment-session",
        cwd=str(bobi_install.repo_path),
        extra_options={"model": "control-model"},
        run_key="run-1",
        experiment_subject="subject-1",
    )

    assert routed._extra_options["model"] == "control-model"


@pytest.mark.asyncio
async def test_session_routes_before_resume_and_client_construction(bobi_install, monkeypatch):
    from bobi.metrics.routing import RouteOutcome, RoutingContext

    calls = []
    context = RoutingContext("agent", "session_start", "codex", "control", False,
                             "", "engineer", True)
    session = Session("agent", str(bobi_install.repo_path), extra_options={"model": "control"},
                      fresh=True, routing=context)
    async def route(ctx):
        assert ctx.prompt == "initial task" and ctx.fresh
        calls.append("route")
        return RouteOutcome("cheap")
    class StopConstruction(Exception):
        pass
    def construct(**kwargs):
        calls.append("construct")
        assert session._extra_options["model"] == "cheap"
        raise StopConstruction
    monkeypatch.setattr("bobi.metrics.routing.resolve_route", route)
    monkeypatch.setattr(session, "_make_brain_session", construct)
    with pytest.raises(StopConstruction):
        await session._run("initial task")
    assert calls == ["route", "construct"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_point", ["session_start", "subagent_phase", "subagent_persistent"])
async def test_session_static_policy_reaches_construction_and_projection(bobi_install, monkeypatch, entry_point):
    from dataclasses import replace
    import json
    from bobi.metrics.collector import MetricsCollectorService
    from bobi.metrics.runtime import MetricsRuntime
    from bobi.metrics.store import connect
    from bobi.sdk import save_session_id
    from tests.metrics.helpers import configured, context

    raw = configured()
    raw["policy"]["mode"] = "enforce"
    raw["policy"]["scope"]["entry_points"] = [entry_point]
    routing_context = replace(context(raw, "treatment"), entry_point=entry_point)
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex-test")
    runtime = MetricsRuntime(bobi_install.state_dir.parent, mode="enabled")
    monkeypatch.setattr("bobi.metrics.routing.get_runtime", lambda: runtime)
    session = Session("agent", str(bobi_install.repo_path), fresh=True,
        extra_options={"model": "configured"}, routing=routing_context)
    class StopConstruction(Exception):
        pass
    def construct(**kwargs):
        assert session._extra_options["model"] == "cheap"
        turn = runtime.begin_turn("agent", provider="openai", model_requested="cheap")
        assert turn.router_decision_id
        turn.finish(status="completed")
        raise StopConstruction
    monkeypatch.setattr(session, "_make_brain_session", construct)
    with pytest.raises(StopConstruction):
        await session._run("initial task")
    assert runtime.close(timeout=2)
    collector = MetricsCollectorService(bobi_install.state_dir.parent)
    collector.collect_once()
    with connect(collector.db_path, readonly=True) as connection:
        row = connection.execute("SELECT model_selected,metadata_json FROM router_decisions").fetchone()
        assert row["model_selected"] == "cheap"
        assert json.loads(row["metadata_json"])["policy"]["status"] == "decided"
    save_session_id("agent", "provider-session", model="cheap", root=runtime.root)
    runtime = MetricsRuntime(bobi_install.state_dir.parent, mode="enabled")
    def no_policy(*args):
        raise AssertionError("resumed Session must not load policy")
    monkeypatch.setattr("bobi.metrics.routing.load_policy", no_policy)
    def resumable(name, model):
        assert model == "cheap"
        return "provider-session"
    monkeypatch.setattr("bobi.session.load_resumable_session_id", resumable)
    session = Session("agent", str(bobi_install.repo_path), fresh=False,
        extra_options={"model": "configured"}, routing=routing_context)
    def resumed_construct(**kwargs):
        assert kwargs["resume"] == "provider-session"
        return construct(**kwargs)
    monkeypatch.setattr(session, "_make_brain_session", resumed_construct)
    with pytest.raises(StopConstruction):
        await session._run("follow-up task")
    assert runtime.close(timeout=2)
    collector.collect_once()
    with connect(collector.db_path, readonly=True) as connection:
        statuses = {json.loads(row[0])["policy"]["status"] for row in connection.execute(
            "SELECT metadata_json FROM router_decisions")}
        assert statuses == {"decided", "reused"}


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_point", ["session_start", "subagent_phase", "subagent_persistent"])
async def test_session_explicit_model_bypasses_policy(bobi_install, monkeypatch, entry_point):
    from dataclasses import replace
    import json
    from bobi.metrics.runtime import MetricsRuntime
    from tests.metrics.helpers import configured, context

    raw = configured()
    raw["policy"]["scope"]["entry_points"] = [entry_point]
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    runtime = MetricsRuntime(bobi_install.state_dir.parent, mode="enabled")
    monkeypatch.setattr("bobi.metrics.routing.get_runtime", lambda: runtime)
    def no_policy(*args):
        raise AssertionError("explicit model must not load policy")
    monkeypatch.setattr("bobi.metrics.routing.load_policy", no_policy)
    routing_context = replace(context(raw, "treatment"), entry_point=entry_point,
        explicit_model=True, configured_model="operator-model")
    session = Session("agent", str(bobi_install.repo_path), fresh=True,
        extra_options={"model": "operator-model"}, routing=routing_context)
    class StopConstruction(Exception):
        pass
    def construct(**kwargs):
        assert session._extra_options["model"] == "operator-model"
        raise StopConstruction
    monkeypatch.setattr(session, "_make_brain_session", construct)
    with pytest.raises(StopConstruction):
        await session._run("initial task")
    assert runtime.close(timeout=2)
