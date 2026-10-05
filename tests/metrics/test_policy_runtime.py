import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import threading

import pytest

from bobi.metrics.policy import (
    CircuitBreaker, PolicyConfig, PolicyError, PolicyRequest, PolicyResult,
    call_policy, guard_result, load_policy,
)
from bobi.metrics.router import provider_subprocess_env

@pytest.mark.parametrize("loader", ["cli", "child"])
@pytest.mark.parametrize("override", [None, "", "operator-value"])
def test_sdk_blanked_routing_environment_restores_at_runtime_boundary(tmp_path, monkeypatch, loader, override):
    import os

    from bobi.config import load_dotenv
    from bobi.env import _load_dotenv_into
    from bobi.metrics.router import load_experiment
    from tests.metrics.test_routing import configured

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
    from tests.metrics.test_routing import configured, context

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
    from tests.metrics.test_routing import configured

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

def config():
    return PolicyConfig("static", "v1", "codex", "shadow", ("control", "cheap"),
                        ("session_start",), ("engineer",), "none", "TEST_POLICY_KEY",
                        '{"model":"cheap"}')

def request():
    return PolicyRequest("jev-features-v1", {}, ("control", "cheap"), "control", "v1")

def test_registry_and_guard():
    policy = load_policy(config())
    result, reason = asyncio.run(call_policy(policy, request(), config(), breaker=CircuitBreaker()))
    assert result.model == "cheap" and reason is None
    assert result.cost_usd == 0.0
    assert provider_subprocess_env({"TEST_POLICY_KEY": "private", "SAFE": "yes"}) == {"SAFE": "yes"}
    for confidence in (None, True, float("nan"), float("inf"), -1, 2):
        assert guard_result(replace(result, confidence=confidence), config()) == "policy_invalid_response"
    assert guard_result(replace(result, model="bad", model_version="bad"), config()) == "policy_invalid_response"
    assert guard_result(replace(result, model_version="bad", confidence=0), config()) == "policy_version_drift"
    for changes in ({"confidence": float("nan")}, {"cost_usd": float("inf")},
                    {"cost_usd": "private-value"}, {"confidence": True}):
        assert guard_result(replace(result, model_version="bad", **changes), config()) == "policy_invalid_response"
    assert guard_result(replace(result, confidence=0.1), config()) == "policy_low_confidence"
    for changes in ({"tier": {}}, {"reason_text": []}, {"model_version": None}, {"model": []}):
        assert guard_result(replace(result, **changes), config()) == "policy_invalid_response"
    with pytest.raises(ValueError):
        load_policy(replace(config(), options_json='{"unexpected":1}'))


@pytest.mark.parametrize("reason", ["API_KEY=private-value", [], "policy_low_confidence"])
def test_plugin_error_reason_is_restricted_to_safe_failure_codes(reason):
    class BrokenPolicy:
        async def decide(self, request, *, timeout_s):
            raise PolicyError(reason)
    breaker = CircuitBreaker()
    result, failure = asyncio.run(call_policy(BrokenPolicy(), request(), config(), breaker=breaker))
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
        first = asyncio.create_task(call_policy(SlowPolicy(), request(), config(), breaker=breaker))
        await started.wait()
        result, reason = await call_policy(load_policy(config()), request(),
                                          replace(config(), max_in_flight=1, deadline_ms=50), breaker=breaker)
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
        return asyncio.run(call_policy(Policy(), request(), replace(config(), max_in_flight=1), breaker=breaker))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(4)))
    assert counters["peak"] == 1
    assert all(reason is None for _, reason in results)

def test_auth_errors_open_breaker_without_exposing_error_text():
    class Policy:
        async def decide(self, request, *, timeout_s):
            raise PolicyError("policy_unavailable", authentication=True)
    breaker = CircuitBreaker()
    assert asyncio.run(call_policy(Policy(), request(), config(), breaker=breaker)) == (None, "policy_unavailable")
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
