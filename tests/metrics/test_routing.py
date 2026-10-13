"""Routing lifecycle, policy, privacy, and fallback regressions."""

import asyncio
import httpx
import json
import pytest

from bobi.brain.base import ERROR_KIND_AUTHENTICATION, ERROR_KIND_MAX_TURNS
import threading
import time
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
    call_policy,
    guard_result,
)
from bobi.metrics.router import (
    LONG_FORM_ERROR,
    ExperimentConfig,
    load_experiment,
    projection_fields,
    provider_subprocess_env,
)
from bobi.metrics.routing import RoutingContext, resolve_route
from bobi.metrics.runtime import MetricsRuntime
from bobi.metrics.store import connect
from bobi.redact import REDACTION_PLACEHOLDER, env_secret_values, redact_secrets
from bobi.sdk import load_session_route, save_session_id, save_session_route
from bobi.session import Session
from bobi.setup.actions import redact_secrets as setup_redact
from bobi.subagent import _run_agent_supervised
from bobi.workflow.orchestrator import run_workflow
from bobi.workflow.schema import StepDef, Workflow
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tests.metrics.helpers import configured, context, decision, fixed_policy

def concise_config():
    return {"experiment_id": "exp_claude_v1", "mode": "enforce",
            "control_model": "haiku",
            "candidate_models": ["haiku", "sonnet"],
            "roles": ["director", "engineer"],
            "instructions": "Route coding & complex logic to Sonnet; trivial queries to Haiku."}

def test_a_policy_cannot_name_a_bare_session_start_entry_point():
    # A Session decides with the entry point its caller names; none names a
    # bare session start, so a policy listing one would never route anything.
    with pytest.raises(ValueError, match="entry_points"):
        ExperimentConfig.from_mapping({**concise_config(), "entry_points": ["session_start"]})


def test_minimal_jev_config_has_stable_defaults_and_fingerprint():
    raw = {key: concise_config()[key] for key in ("control_model", "candidate_models", "instructions")}
    config = ExperimentConfig.from_mapping(raw)
    assert config.experiment_id == "jev-routing-v1"
    assert config.policy.roles == ("director", "engineer")
    assert config.policy.mode == "enforce" and config.policy.prompt_egress == "none"
    assert config == ExperimentConfig.from_mapping(dict(reversed(list(raw.items()))))
    assert ExperimentConfig.from_mapping(config.to_mapping()) == config
    assert config.fingerprint() == ExperimentConfig.from_mapping(raw).fingerprint()
    for field, value in (("experiment_id", "explicit"), ("router_name", "custom"),
                         ("router_version", "v7"), ("feature_schema_version", "features-v2")):
        assert getattr(ExperimentConfig.from_mapping({**raw, field: value}), field) == value


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_concise_jev_config_compiles_to_the_typesafe_policy(mode):
    raw = {**concise_config(), "mode": mode}
    config = ExperimentConfig.from_mapping(raw)
    assert (config.router_name, config.router_version, config.feature_schema_version) == (
        "jev-router", "v1", "jev-features-v1")
    assert config.policy.mode == mode
    assert config.policy.version == config.policy_version == "jev-1.13.0"
    assert config.policy.prompt_egress == "none"
    assert config.policy.brain == "auto"
    assert config.policy.name == "typesafe-jev" and config.policy.credential_env == "TYPESAFE_API_KEY"
    assert config.policy.entry_points == ("subagent_phase", "subagent_persistent",
                                          "subagent_supervised", "workflow_start")
    assert json.loads(config.policy.options_json)["instructions"] == raw["instructions"]
    assert ExperimentConfig.from_mapping(config.to_mapping()) == config
    TypeSafePolicy(config.policy)
    assert load_experiment({"BOBI_METRICS_EXPERIMENT_JSON": json.dumps(raw)}) == config
    assert load_experiment({}) is None


@pytest.mark.parametrize("field,value", [
    ("mode", "typo"), ("candidate_models", []), ("candidate_models", "haiku"),
    ("candidate_models", [False]), ("candidate_models", ["haiku", "haiku"]),
    ("control_model", "unknown"), ("roles", []), ("instructions", ""),
    ("criteria", {"haiku": "only one"}), ("endpoint", "http://localhost"),
    ("policy_version", "jev-latest"), ("prompt_egress", "raw"), ("options_json", "{}"),
    ("experiment_id", None), ("experiment_id", ""), ("roles", None),
    ("router_name", None), ("router_version", ""), ("feature_schema_version", False),
    ("brain", "unknown"), ("min_confidence", float("nan")), ("min_confidence", True),
    ("deadline_ms", 49), ("deadline_ms", 3001), ("deadline_ms", True),
    ("entry_points", []), ("entry_points", ["unknown"]), ("max_prompt_bytes", 0),
    ("cohort", ""), ("credential_env", "CUSTOM_KEY"), ("unexpected", "field"),
])
def test_concise_jev_config_rejects_invalid_input(field, value):
    with pytest.raises(ValueError):
        ExperimentConfig.from_mapping({**concise_config(), field: value})


@pytest.mark.parametrize("legacy", [{"variants": []}, {"policy": {}}])
def test_long_form_config_is_rejected_with_a_rewrite_hint(legacy):
    with pytest.raises(ValueError) as error:
        ExperimentConfig.from_mapping({**concise_config(), **legacy})
    assert str(error.value) == LONG_FORM_ERROR


def test_fingerprint_covers_decision_settings_but_not_the_endpoint():
    config = ExperimentConfig.from_mapping(concise_config())
    moved = ExperimentConfig.from_mapping({**concise_config(), "endpoint": "https://other.example.com/systemone"})
    assert moved.fingerprint() == config.fingerprint()
    for changes in ({"mode": "shadow"}, {"instructions": "Other"}, {"min_confidence": 0.5},
                    {"experiment_id": "other"}, {"prompt_egress": "redacted"}):
        assert ExperimentConfig.from_mapping({**concise_config(), **changes}).fingerprint() != config.fingerprint()


def test_shared_redaction_preserves_setup_import():
    assert setup_redact is redact_secrets
    assert redact_secrets("API_KEY=private-value")[0] == "API_KEY=[redacted]"
    assert redact_secrets("sk-abcdefghijklmnopqrstu-other-private")[0] == "[redacted]"


def test_redaction_leaves_words_that_merely_contain_sk_dash():
    text = "check out branch task-implement-fine-grained-metrics and ask-for-review"
    assert redact_secrets(text) == (text, 0)


@pytest.mark.parametrize("text,leaked", [
    ("DB_PASSWORD=hunter2", "hunter2"),
    ('{"password": "hunter2"}', "hunter2"),
    ("{'client_secret': 'hunter2'}", "hunter2"),
    ("GH_TOKEN=gho_short123", "gho_short123"),
    ("export SLACK_TOKEN=abc123", "abc123"),
    ("postgres://admin:pw@db/prod", "admin:pw"),
    ("SECRET_KEY=django-local", "django-local"),
    ("DB_PASS=hunter2", "hunter2"),
    ("AWS_SECRET_ACCESS_KEY=abc/def", "abc/def"),
    ('{"password": "it\'s a secret"}', "s a secret"),
    ("_PASSWORD=hunter2", "hunter2"),
    ("__API_KEY=abcdef", "abcdef"),
    ("secret_key = 'django-local'", "django-local"),
    ("PGPASSWORD=hunter2 psql", "hunter2"),
    ("Authorization: Basic dXNlcjpodW50ZXIy", "dXNlcjpodW50ZXIy"),
    ("mysql --password hunter2 db", "hunter2"),
    ("mysql --password=hunter2 db", "hunter2"),
])
def test_redaction_catches_common_secret_shapes(text, leaked):
    redacted, count = redact_secrets(text)
    assert leaked not in redacted and count >= 1
    assert REDACTION_PLACEHOLDER in redacted


def test_redaction_keeps_ordinary_token_prose():
    for text in ("max token: 500", "the token count is 12", "postgres://db/prod", "key: 5",
                 "the keyboard and compass are in the passport case", "id = Column(primary_key=True)",
                 "rows.sort(sort_key=lambda row: row.id)", "next_page_token: str", "first_pass: done",
                 "the password is strong", "reset your password --help", "a basic understanding of auth"):
        assert redact_secrets(text) == (text, 0)


@pytest.mark.parametrize("text", ["a." * 100_000, "a_" * 50_000 + "=x", "x://" + "a:" * 50_000],
                         ids=["dotted", "underscored", "userinfo"])
def test_redaction_stays_linear_on_adversarial_text(text):
    started = time.monotonic()
    redact_secrets(text)
    assert time.monotonic() - started < 1.0


def test_env_scrubbing_skips_paths_numbers_and_process_bookkeeping():
    env = {"PWD": "/repo", "OLDPWD": "/home/dev", "SSH_AUTH_SOCK": "/tmp/agent.sock",
           "XAUTHORITY": "/home/dev/.Xauthority", "MAX_TOKENS": "4096", "USE_AUTH": "true",
           "KEYBOARD_LAYOUT": "dvorak-programmer", "STRIPE_KEY": "rk_local_123456",
           "AUTH_TIMEOUT": "300000", "DB_PASSWORD": "12345678", "API_TOKEN": "~tilde-local"}
    assert set(env_secret_values(env)) == {"rk_local_123456", "12345678", "~tilde-local"}
    task, _ = prepare_task("edit /repo/app/main.py in /application", repo_path="/repo", max_bytes=8192,
                           secrets=env_secret_values(env))
    assert task == "edit app/main.py in [path]"


def test_task_scrubs_exact_secret_values_from_the_environment():
    env = {"DB_PASS": "hunter2", "STRIPE_WEBHOOK_SECRET": "whsec-local", "BOBI_BRAIN": "claude",
           "EMPTY_TOKEN": ""}
    task, count = prepare_task("run the deploy with hunter2 then whsec-local on claude",
                               repo_path="/repo", max_bytes=8192, secrets=env_secret_values(env))
    assert task == "run the deploy with [redacted] then [redacted] on claude" and count == 2


def test_keyword_valued_credential_setting_does_not_disable_shape_redaction():
    env = {"BOBI_AUTH": "api_key"}
    task, _ = prepare_task('set api_key=fakefake1234 and {"api_key": "fakefake5678"}',
                           repo_path="/repo", max_bytes=8192, secrets=env_secret_values(env))
    assert "fakefake1234" not in task and "fakefake5678" not in task


@pytest.mark.parametrize("text,expected", [
    ('PASSWORD="hun\\"ter2"', 'PASSWORD="[redacted]"'),
    ('password = """hunter2"""', 'password = """[redacted]"""'),
    ("password = '''hunter2'''", "password = '''[redacted]'''"),
    ('{"password": null}', '{"password": null}'),
    ('{"api_key": true, "n": 1}', '{"api_key": true, "n": 1}'),
    ('{"password": hunter2, "n": 1}', '{"password": [redacted], "n": 1}'),
    ("DB_PASSWORD=abc,def", "DB_PASSWORD=[redacted]"),
    ('{"password": ' + "x" * 60, '{"password": [redacted]'),
])
def test_redaction_handles_escaped_quotes_triple_quotes_and_json_delimiters(text, expected):
    assert redact_secrets(text)[0] == expected


def test_routed_task_never_carries_a_runtime_credential_value(monkeypatch, tmp_path):
    from bobi import paths

    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured(mode="enforce", prompt_egress="redacted")))
    paths.env_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    paths.env_path(tmp_path).write_text("DB_PASS=hunter2-local\nBOBI_BRAIN=codex\n")
    calls = fixed_policy(monkeypatch)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        asyncio.run(resolve_route(replace(context(), prompt="connect with hunter2-local on codex"), runtime=runtime))
    finally:
        assert runtime.close(timeout=2)
    [request] = calls
    assert request.features["task"] == "connect with [redacted] on codex"


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
    args = dict(prompt="private prompt", repo_path="/repo", entry_point="subagent_phase",
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


def test_projection_fields_keep_policy_metadata_and_executed_model():
    routed = replace(decision(), model_selected="model-control", candidate_models=("model-control", "cheap"),
        policy_metadata_json=json.dumps({"mode": "shadow", "recommended_model": "cheap",
                                         "status": "decided", "call_id": "policy-call"}))
    fields = projection_fields(routed)
    metadata = json.loads(fields["metadata_json"])
    assert fields["model_selected"] == "model-control"
    assert fields["variant_id"] == "treatment_jev" and fields["assignment_status"] == "assigned"
    assert fields["fallback_reason"] is None
    assert metadata["policy"]["recommended_model"] == "cheap"
    assert metadata["policy"]["call_id"] == "policy-call"
    assert metadata["config_fingerprint"] == "fingerprint"
    assert "assignment_key_hash" not in fields and "expected_weights" not in metadata


@pytest.mark.parametrize("loader", ["cli", "child"])
@pytest.mark.parametrize("override", [None, "", "operator-value"])
def test_sdk_blanked_routing_environment_restores_at_runtime_boundary(tmp_path, monkeypatch, loader, override):
    import os

    from bobi.config import load_dotenv
    from bobi.env import _load_dotenv_into

    values = {"BOBI_METRICS_EXPERIMENT_JSON": json.dumps(configured()),
              "TYPESAFE_API_KEY": "synthetic-policy-secret"}
    (tmp_path / "package").mkdir()
    (tmp_path / ".env").write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    environment = provider_subprocess_env(values, blank_inherited=True)
    assert all(not environment.get(key) for key in values)
    if override is not None:
        environment["TYPESAFE_API_KEY"] = override
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
    expected_key = values["TYPESAFE_API_KEY"] if override is None else override
    assert environment["TYPESAFE_API_KEY"] == expected_key
    assert "BOBI_INTERNAL_PROVIDER_CLEARED_ENV" not in environment
    if override != "":
        assert environment["BOBI_METRICS_EXPERIMENT_JSON"] == values["BOBI_METRICS_EXPERIMENT_JSON"]
        assert load_experiment(environment) is not None


def test_restored_claude_route_projects_decision_and_invocation(tmp_path, monkeypatch):
    import os

    from bobi.config import load_dotenv

    candidates = ["provider/control", "provider/reasoning"]
    raw = configured(control_model="provider/control", candidate_models=candidates, brain="claude",
                     mode="enforce", entry_points=["workflow_start"])
    values = {"BOBI_METRICS_EXPERIMENT_JSON": json.dumps(raw)}
    (tmp_path / "package").mkdir()
    (tmp_path / ".env").write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    for key, value in provider_subprocess_env(values, blank_inherited=True).items():
        monkeypatch.setenv(key, value)
    load_dotenv(tmp_path)
    assert os.environ["BOBI_METRICS_EXPERIMENT_JSON"] == values["BOBI_METRICS_EXPERIMENT_JSON"]
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        for index, expected in enumerate(candidates):
            fixed_policy(monkeypatch, expected)
            ctx = replace(context(), session_name=f"agent-{index}", brain="claude", entry_point="workflow_start")
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
        assert [row["model_selected"] for row in rows] == candidates
        assert [row["provider_model"] for row in rows] == ["control", "reasoning"]
        assert all(json.loads(row["candidate_models_json"]) == candidates for row in rows)
        assert connection.execute("SELECT COUNT(*) FROM raw_events "
            "WHERE projection_state != 'projected'").fetchone()[0] == 0


@pytest.mark.parametrize("encoded", [json.dumps(configured()), "invalid-json", "[]", "null", '{"policy": []}'])
def test_provider_environment_never_carries_the_routing_config_or_policy_key(encoded):
    assert provider_subprocess_env({"BOBI_METRICS_EXPERIMENT_JSON": encoded,
        "BOBI_METRICS_EXPERIMENT_DISABLED_JSON": encoded,
        "TYPESAFE_API_KEY": "synthetic-policy-secret", "SAFE": "yes"}) == {"SAFE": "yes"}


def policy_runtime_config():
    return PolicyConfig("v1", "codex", "shadow", ("control", "cheap"),
                        ("subagent_phase",), ("engineer",), "none", "{}")


def test_confidence_at_the_threshold_is_accepted():
    import math

    config = policy_runtime_config()
    result = PolicyResult("cheap", config.min_confidence, None, config.version, None, None)
    assert guard_result(result, config) is None
    below = replace(result, confidence=math.nextafter(config.min_confidence, 0))
    assert guard_result(below, config) == "policy_low_confidence"


class _FixedPolicy:
    async def decide(self, request, *, timeout_s):
        return PolicyResult("cheap", 1.0, None, request.pinned_version, None, 0.0)


def request():
    return PolicyRequest("jev-features-v1", {}, ("control", "cheap"), "control", "v1")


def test_guard_result_rejects_invalid_answers():
    result, reason = asyncio.run(call_policy(_FixedPolicy(), request(), policy_runtime_config(), breaker=CircuitBreaker()))
    assert result.model == "cheap" and reason is None
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


def test_successful_probe_closes_a_rate_tripped_circuit():
    now = [0.0]
    breaker = CircuitBreaker(clock=lambda: now[0])
    for failure in [None] * 10 + ["policy_timeout"] * 5:
        assert breaker.acquire(8) == ("acquired", False)
        breaker.finish(failure, probe=False)
    # An outage that outlasts five probes leaves half of the 20-call window failed.
    for _ in range(5):
        now[0] += 600
        assert breaker.acquire(8) == ("acquired", True)
        breaker.finish("policy_unavailable", probe=True)
    now[0] += 600
    assert breaker.acquire(8) == ("acquired", True)
    breaker.finish(None, probe=True)
    assert breaker.health() == {"state": "closed", "consecutive_failures": 0,
                                "opened_at_us": None, "last_failure_kind": None}
    # The cleared window means one later failure does not re-trip on the outage's history.
    assert breaker.acquire(8) == ("acquired", False)
    breaker.finish("policy_timeout", probe=False)
    assert breaker.health()["state"] == "closed"


def test_open_circuit_refuses_calls_without_asking_the_policy():
    breaker = CircuitBreaker()
    for _ in range(5):
        breaker.acquire(8)
        breaker.finish("policy_timeout", probe=False)
    calls = []
    class Policy:
        async def decide(self, request, *, timeout_s):
            calls.append(request)
            return PolicyResult("cheap", 1.0, None, request.pinned_version, None, 0.0)
    result, reason = asyncio.run(call_policy(Policy(), request(), policy_runtime_config(), breaker=breaker))
    assert (result, reason, calls) == (None, "policy_circuit_open", [])


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
        result, reason = await call_policy(_FixedPolicy(), request(),
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


@pytest.mark.parametrize("egress", ["none", "redacted"])
def test_routing_does_not_persist_prompt_or_private_memory(monkeypatch, tmp_path, caplog, egress):
    raw = configured(prompt_egress=egress)
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    fixed_policy(monkeypatch)
    ctx = replace(context(), prompt=(
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
    ("json", "routing_config_invalid"),
    ("schema", "routing_config_invalid"),
    ("long_form", "routing_config_invalid"),
    ("adapter", "routing_policy_unavailable"),
    ("metrics", "routing_metrics_unavailable"),
])
def test_routing_misconfiguration_records_safe_fallback(monkeypatch, tmp_path, caplog, fault, reason):
    raw = configured()
    if fault == "schema":
        raw["mode"] = "secret-schema-canary"
    if fault == "long_form":
        raw = {"variants": [{"variant_id": "secret-long-form-canary"}], "policy": {}}
    if fault == "adapter":
        monkeypatch.setattr("bobi.metrics.routing.TypeSafePolicy", lambda config: (
            _ for _ in ()).throw(ValueError("secret-adapter-canary")))
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", "secret-json-canary" if fault == "json" else json.dumps(raw))
    ctx = context()
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
    for canary in ("secret-schema-canary", "secret-json-canary", "secret-adapter-canary", "secret-long-form-canary"):
        assert canary not in caplog.text


def test_routing_fallback_survives_telemetry_failure(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", "invalid-json")
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    def fail_recording(*args, **kwargs):
        raise OSError("secret-recording-canary")
    monkeypatch.setattr(runtime, "admit_route", fail_recording)
    try:
        outcome = asyncio.run(resolve_route(context(), runtime=runtime))
        assert outcome.model == "configured"
        assert outcome.reason == "routing_config_invalid"
        assert "fallback recording failed (OSError)" in caplog.text
        assert "secret-recording-canary" not in caplog.text
    finally:
        assert runtime.close(timeout=2)


def test_capacity_timeout_does_not_create_a_policy_call(monkeypatch, tmp_path):
    raw = configured(mode="enforce", deadline_ms=50)
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    calls = fixed_policy(monkeypatch)
    breaker = CircuitBreaker()
    capacity = ExperimentConfig.from_mapping(raw).policy.max_in_flight
    for _ in range(capacity):
        assert breaker.acquire(capacity)[0] == "acquired"
    monkeypatch.setattr("bobi.metrics.policy.policy_breaker", lambda config: breaker)
    monkeypatch.setattr("bobi.metrics.routing.policy_breaker", lambda config: breaker)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        outcome = asyncio.run(resolve_route(context(), runtime=runtime))
        assert outcome.model == "control"
        assert outcome.decision.fallback_reason == "policy_timeout"
        metadata = json.loads(outcome.decision.policy_metadata_json)
        assert metadata["status"] == "skipped" and metadata["call_id"] is None
        assert calls == []
        runtime.begin_turn("agent", provider="openai").finish(status="completed")
    finally:
        for _ in range(capacity):
            breaker.finish(None, probe=False)
        assert runtime.close(timeout=2)
    MetricsCollectorService(tmp_path).collect_once()
    with connect(tmp_path / "state" / "metrics" / "metrics.db", readonly=True) as conn:
        policy = json.loads(conn.execute(
            "SELECT metadata_json FROM router_decisions"
        ).fetchone()[0])["policy"]
    assert policy["status"] == "skipped"
    assert policy["call_id"] is None


@pytest.mark.parametrize("fault,reason", [("timeout", "policy_timeout"),
    ("transport", "policy_unavailable"), ("model", "policy_invalid_response"),
    ("version", "policy_version_drift")])
def test_policy_fault_projects_control_execution(monkeypatch, tmp_path, fault, reason):
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured(mode="enforce")))
    async def decide(self, request, *, timeout_s):
        if fault == "timeout":
            raise TimeoutError
        if fault == "transport":
            raise PolicyError("policy_unavailable")
        return PolicyResult("unknown" if fault == "model" else "cheap", 1,
            None, "changed" if fault == "version" else request.pinned_version, None, None)
    monkeypatch.setattr(TypeSafePolicy, "decide", decide)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    outcome = asyncio.run(resolve_route(context(), runtime=runtime))
    assert outcome.model == "control"
    turn = runtime.begin_turn("agent", provider="openai", model_requested=outcome.model)
    turn.finish(status="completed")
    assert runtime.close(timeout=2)
    collector = MetricsCollectorService(tmp_path)
    collector.collect_once()
    with connect(collector.db_path, readonly=True) as connection:
        row = connection.execute("SELECT * FROM router_decisions").fetchone()
        assert row["model_selected"] == "control" and row["variant_id"] == "treatment_jev"
        assert row["fallback_reason"] == reason
        assert json.loads(row["metadata_json"])["policy"]["status"] == "failed"


def test_shadow_records_and_enforce_executes_the_recommendation(monkeypatch, tmp_path):
    fixed_policy(monkeypatch)
    for mode, expected in [("shadow", "control"), ("enforce", "cheap")]:
        monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured(mode=mode)))
        runtime = MetricsRuntime(tmp_path / mode, mode="enabled")
        outcome = asyncio.run(resolve_route(context(), runtime=runtime))
        assert outcome.model == expected
        policy = json.loads(outcome.decision.policy_metadata_json)
        assert policy["status"] == "decided" and policy["recommended_model"] == "cheap"
        assert outcome.decision.fallback_reason is None
        assert runtime.close(timeout=2)


def test_ineligible_and_rejected_admission(monkeypatch, tmp_path):
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured()))
    fixed_policy(monkeypatch)
    ctx = context()
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
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured(
        mode="enforce", entry_points=["subagent_phase", "workflow_start"])))
    fixed_policy(monkeypatch)
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex-test")
    ctx = context()
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    first = asyncio.run(resolve_route(ctx, runtime=runtime))
    assert first.model == "cheap"
    save_session_id("agent", "provider-session", model="cheap", root=tmp_path)
    workflow = replace(ctx, session_name="workflow-step", entry_point="workflow_start")
    routed = asyncio.run(resolve_route(workflow, runtime=runtime))
    # A workflow keeps its decision in its own checkpoint, not the session route file.
    assert routed.model == "cheap" and load_session_route("workflow-step", root=tmp_path) is None
    checkpoint = {"brain": "codex-test", "decision": asdict(routed.decision)}
    assert runtime.close(timeout=2)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    def no_policy(*args):
        raise AssertionError("sticky reuse must not load policy")
    monkeypatch.setattr("bobi.metrics.routing.TypeSafePolicy", no_policy)
    try:
        reused = asyncio.run(resolve_route(replace(ctx, fresh=False, prompt=""), runtime=runtime))
        assert reused.model == "cheap"
        assert json.loads(reused.decision.policy_metadata_json)["status"] == "reused"
        resumed = replace(workflow, fresh=False, prompt="")
        assert asyncio.run(resolve_route(resumed, runtime=runtime)).decision is None
        reused = asyncio.run(resolve_route(resumed, runtime=runtime, sticky_record=checkpoint))
        assert reused.model == "cheap"
        assert json.loads(reused.decision.policy_metadata_json)["status"] == "reused"
    finally:
        assert runtime.close(timeout=2)


def test_sticky_route_does_not_replace_a_later_explicit_model(monkeypatch, tmp_path):
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured(mode="enforce")))
    fixed_policy(monkeypatch)
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex")
    ctx = context()
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    assert asyncio.run(resolve_route(ctx, runtime=runtime)).model == "cheap"
    assert runtime.close(timeout=2)
    save_session_id("agent", "operator-transcript", model="operator-model", root=tmp_path)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        outcome = asyncio.run(resolve_route(replace(ctx, fresh=False), runtime=runtime))
        # The saved transcript ran the operator's model: resuming keeps it.
        assert outcome.model == "operator-model" and outcome.decision is None
        assert outcome.reason == "routing_sticky_stale"
        assert runtime.begin_turn("agent", provider="openai").router_decision_id is None
        explicit = asyncio.run(resolve_route(replace(ctx, fresh=False, explicit_model=True,
                                                     configured_model="operator-model"), runtime=runtime))
        assert explicit.model == "operator-model" and explicit.decision is None and explicit.reason is None
    finally:
        assert runtime.close(timeout=2)


def test_resume_after_a_control_rerun_stays_on_the_control_model(monkeypatch, tmp_path):
    from bobi.metrics.routing import candidate_failed

    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured(mode="enforce")))
    fixed_policy(monkeypatch)
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex")
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        routed = asyncio.run(resolve_route(context(), runtime=runtime))
        assert routed.model == "cheap"
        # The policy's model failed its first turn and the session reran on control.
        assert candidate_failed(routed.decision, session_name="agent", brain="codex", model="cheap",
                                error="model unavailable", root=tmp_path) == "control"
        save_session_id("agent", "provider-session", model="control", root=tmp_path)
    finally:
        assert runtime.close(timeout=2)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        resumed = asyncio.run(resolve_route(replace(context(), fresh=False, prompt=""), runtime=runtime))
        assert resumed.model == "control" and resumed.reason is None
        assert resumed.decision.fallback_reason == "candidate_failed"
        assert json.loads(resumed.decision.policy_metadata_json)["status"] == "reused"
    finally:
        assert runtime.close(timeout=2)


def test_turns_after_a_control_rerun_record_the_fallback(monkeypatch, tmp_path):
    from bobi.metrics.routing import candidate_failed

    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured(mode="enforce")))
    fixed_policy(monkeypatch)
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex")
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        routed = asyncio.run(resolve_route(context(), runtime=runtime))
        failed = runtime.begin_turn("agent", provider="openai", model_requested="cheap")
        failed.finish(status="failed", error_kind="provider_error")
        assert candidate_failed(routed.decision, session_name="agent", brain="codex", model="cheap",
                                error="model unavailable", root=tmp_path, runtime=runtime) == "control"
        rerun = runtime.begin_turn("agent", provider="openai", model_requested="control")
        rerun.finish(status="completed")
    finally:
        assert runtime.close(timeout=2)
    MetricsCollectorService(tmp_path).collect_once()
    conn = connect(tmp_path / "state/metrics/metrics.db", readonly=True)
    try:
        rows = {row["turn_id"]: row for row in conn.execute("SELECT * FROM router_decisions")}
    finally:
        conn.close()
    # The rerun ran the control model: it is a fallback, never a routed pick.
    assert (rows[failed.turn_id]["model_selected"], rows[failed.turn_id]["fallback_reason"]) == ("cheap", None)
    assert (rows[rerun.turn_id]["model_selected"], rows[rerun.turn_id]["fallback_reason"]) == (
        "control", "candidate_failed")


def test_workflow_checkpoint_keeps_the_control_model_after_a_rerun(bobi_install, monkeypatch):
    from bobi.metrics.routing import RouteOutcome
    from bobi.workflow import orchestrator
    from bobi.workflow.orchestrator import DrainResult
    from bobi.workflow.state import WorkflowRun

    brain = StubBrain()
    monkeypatch.setenv("BOBI_STUB_BRAIN", "1")
    monkeypatch.setattr("bobi.brain.get_brain", lambda *args: brain)
    monkeypatch.setattr("bobi.events.publish.post_event", lambda *args, **kwargs: True)
    routed = replace(decision(), model_selected="cheap", control_model="control",
                     candidate_models=("control", "cheap"), entry_point="workflow_start")

    async def route(ctx, **kwargs):
        return RouteOutcome("cheap", routed)

    monkeypatch.setattr("bobi.metrics.routing.resolve_route", route)
    drains = iter([DrainResult(None, "model unavailable", "", tools_ran=False),
                   DrainResult("done", ""), DrainResult("done", "")])

    async def scripted_drain(*args, **kwargs):
        return next(drains)

    monkeypatch.setattr(orchestrator, "_drain_response", scripted_drain)
    assert run_workflow(Workflow(name="routed", steps=[
        StepDef(name="first", prompt="do the work", agent="engineer"),
        StepDef(name="second", prompt="finish", agent="engineer")]),
        task="test", repo="test", cwd=str(bobi_install.repo_path), run_key="rerouted")
    [run] = WorkflowRun.list_runs()
    runtime_scope = run.variable_scopes["_runtime"]
    # A retry of this run resumes from the checkpoint: it must find the control model's route.
    assert runtime_scope["model"] == "control"
    assert runtime_scope["route"]["decision"]["model_selected"] == "control"
    assert runtime_scope["route"]["decision"]["fallback_reason"] == "candidate_failed"


def test_workflow_routing_prompt_carries_the_launch_input(bobi_install, monkeypatch):
    from bobi.metrics.routing import RouteOutcome
    from bobi.workflow import orchestrator
    from bobi.workflow.orchestrator import DrainResult

    brain = StubBrain()
    monkeypatch.setenv("BOBI_STUB_BRAIN", "1")
    monkeypatch.setattr("bobi.brain.get_brain", lambda *args: brain)
    monkeypatch.setattr("bobi.events.publish.post_event", lambda *args, **kwargs: True)
    prompts = []

    async def route(ctx, **kwargs):
        prompts.append(ctx.prompt)
        return RouteOutcome("configured")

    async def drain(*args, **kwargs):
        return DrainResult("done", "")

    monkeypatch.setattr("bobi.metrics.routing.resolve_route", route)
    monkeypatch.setattr(orchestrator, "_drain_response", drain)
    assert run_workflow(Workflow(name="routed", steps=[
        StepDef(name="setup", prompt="Create a git worktree for this issue.", agent="engineer"),
        StepDef(name="implement", prompt="Implement the change.", agent="engineer")]),
        task="Rewrite the billing reconciler in Rust", repo="test",
        cwd=str(bobi_install.repo_path), run_key="launch-input")
    [prompt] = prompts
    assert "Rewrite the billing reconciler in Rust" in prompt
    assert "Create a git worktree for this issue." in prompt


def test_config_edit_never_switches_a_routed_session_model(monkeypatch, tmp_path):
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured(mode="enforce")))
    fixed_policy(monkeypatch)
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex")
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        assert asyncio.run(resolve_route(context(), runtime=runtime)).model == "cheap"
        save_session_id("agent", "provider-session", model="cheap", root=tmp_path)
    finally:
        assert runtime.close(timeout=2)
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(
        configured(mode="enforce", instructions="Edited routing criteria.")))
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        resumed = asyncio.run(resolve_route(replace(context(), fresh=False, prompt=""), runtime=runtime))
        assert resumed.model == "cheap" and resumed.decision is None
        assert resumed.reason == "routing_sticky_stale"
        # A session saved under another brain cannot resume, so it gets the configured model.
        monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex-gateway")
        other = asyncio.run(resolve_route(replace(context(), fresh=False, prompt=""), runtime=runtime))
        assert other.model == "configured" and other.reason == "routing_sticky_stale"
    finally:
        assert runtime.close(timeout=2)


def test_persistence_failure_never_executes_the_routed_model(monkeypatch, tmp_path):
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured(mode="enforce")))
    fixed_policy(monkeypatch)
    def fail_write(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr("bobi.metrics.routing.save_session_route", fail_write)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    outcome = asyncio.run(resolve_route(context(), runtime=runtime))
    assert outcome.model == "control" and outcome.reason == "route_persistence_failed"
    observation = runtime.begin_turn("agent", provider="openai")
    assignment = json.loads(runtime._session_contexts[observation.session_id]["metadata_json"])["router_assignment"]
    assert assignment["model_selected"] == "control"
    assert assignment["fallback_reason"] == "route_persistence_failed"
    observation.finish(status="completed")
    assert runtime.close(timeout=2)


def test_failed_corrective_admission_leaves_no_routed_projection(monkeypatch, tmp_path):
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured(mode="enforce")))
    fixed_policy(monkeypatch)
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
    outcome = asyncio.run(resolve_route(context(), runtime=runtime))
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


@pytest.mark.parametrize("failure", ["policy_low_confidence", "policy_version_drift"])
def test_valid_fallback_answer_keeps_reported_policy_cost(monkeypatch, tmp_path, failure):
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured(mode="enforce")))
    async def answer(*args, **kwargs):
        kwargs["on_call"]()
        return PolicyResult("cheap", 0.1, None, "v1", None, 0.02), failure
    monkeypatch.setattr("bobi.metrics.routing.call_policy", answer)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    outcome = asyncio.run(resolve_route(context(), runtime=runtime))
    assert outcome.model == "control"
    assert outcome.decision.fallback_reason == failure
    metadata = json.loads(outcome.decision.policy_metadata_json)
    assert metadata["status"] == "failed" and metadata["call_id"]
    assert metadata["cost_usd"] == 0.02
    assert metadata["recommended_model"] == "cheap"
    assert runtime.close(timeout=2)


def test_circuit_open_has_no_policy_call_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured()))
    async def unavailable(*args, **kwargs):
        return None, "policy_circuit_open"
    monkeypatch.setattr("bobi.metrics.routing.call_policy", unavailable)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    outcome = asyncio.run(resolve_route(context(), runtime=runtime))
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
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "model": "jev-1.13.0",
                "answers": {"route": {"type": "choice", "choice": "cheap",
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


# One scripted turn per entry point, alternating shadow and enforce, plus one
# policy failure. Each failure class is pinned at the adapter level below
# (HTTP status, malformed body, timeout) and by `guard_result` above.
@pytest.mark.parametrize("entry_point,mode,fault,reason", [
    ("subagent_phase", "shadow", None, None),
    ("subagent_persistent", "enforce", None, None),
    ("subagent_supervised", "shadow", None, None),
    ("workflow_start", "enforce", None, None),
    ("subagent_phase", "enforce", "low_confidence", "policy_low_confidence"),
])
def test_route_executes_and_projects_a_scripted_turn(bobi_install, monkeypatch, entry_point,
                                                    mode, fault, reason, fault_server):
    monkeypatch.setattr("bobi.metrics.policy._breakers", {})
    raw = configured(mode=mode, entry_points=[entry_point])
    failed_policy = reason is not None
    executed_model = "control" if failed_policy or mode == "shadow" else "cheap"
    endpoint, server_state = fault_server
    server_state["fault"] = fault
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-key")

    def local_policy(config):
        policy = TypeSafePolicy(config)
        policy.endpoint = endpoint
        return policy

    monkeypatch.setattr("bobi.metrics.routing.TypeSafePolicy", local_policy)
    ctx = replace(context(), entry_point=entry_point)
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
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

            monkeypatch.setattr("bobi.metrics.routing.TypeSafePolicy", no_policy)
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
        assert row["variant_id"] == "treatment_jev" and row["fallback_reason"] == reason
        metadata = json.loads(row["metadata_json"])
        assert metadata["entry_point"] == entry_point
        assert metadata["policy"]["status"] == ("failed" if failed_policy else "decided")
        assert metadata["policy"]["call_id"]
        assert connection.execute("SELECT status FROM turns").fetchone()[0] == "completed"
        if session is not None and not failed_policy:
            decisions = connection.execute("SELECT metadata_json FROM router_decisions ORDER BY decided_at_us").fetchall()
            assert [json.loads(row[0])["policy"]["status"] for row in decisions] == ["decided", "reused", "reused"]
    if entry_point != "workflow_start":
        name = session.name if session is not None else "engineer-routing-work"
        assert load_session_route(name, root=root)["decision"]["model_selected"] == executed_model
    assert len(server_state["calls"]) == 1
    assert all("task" not in call["state"] for call in server_state["calls"])


def test_documented_operator_configuration_validates_offline():
    text = Path("docs/JEV_ROUTER_DATA_FLOW.md").read_text().split("## 2. Configuration", 1)[1]
    raw = json.loads(text.split("```json", 1)[1].split("```", 1)[0])
    documented = ExperimentConfig.from_mapping(raw)
    assert documented.policy.mode == "shadow"
    assert documented.policy.prompt_egress == "none"


def typesafe_policy_config():
    return PolicyConfig("jev-1.13.0", "codex", "shadow",
        ("control", "cheap"), ("subagent_phase",), ("engineer",), "none",
        json.dumps({"instructions": "Choose a model",
        "criteria": {"control": "Complex task", "cheap": "Simple task"}}))


def body():
    return {"model": "jev-1.13.0", "answers": {"route": {"type": "choice",
        "choice": "cheap", "confidence": 0.94,
        "probabilities": {"cheap": 0.97, "control": 0.03}}},
        "usage": {"input_tokens": 100, "output_tokens": 20}}


def run(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-secret")
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
    (401, "policy_unauthenticated", True), (403, "policy_unauthenticated", True),
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


@pytest.mark.parametrize("mutation", ["unknown", "nan", "sum", "usage", "oversized", "nested", "surrogate"])
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
    elif mutation == "surrogate":
        raw["extra"] = "\ud800"
    elif mutation == "nested":
        with pytest.raises(PolicyError, match="policy_invalid_response"):
            run(monkeypatch, lambda request: httpx.Response(200, content=b"[" * 2000 + b"0" + b"]" * 2000))
        return
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
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-secret")
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
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-secret")
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


def test_session_construction_preserves_configured_model(
    bobi_install, monkeypatch
):
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", "invalid-legacy-config")
    routed = Session(
        name="experiment-session",
        cwd=str(bobi_install.repo_path),
        extra_options={"model": "control-model"},
        run_key="run-1",
    )

    assert routed._extra_options["model"] == "control-model"


@pytest.mark.asyncio
async def test_session_routes_before_resume_and_client_construction(bobi_install, monkeypatch):
    from bobi.metrics.routing import RouteOutcome, RoutingContext

    calls = []
    context = RoutingContext("agent", "subagent_phase", "codex", "control", False,
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
@pytest.mark.parametrize("entry_point", ["subagent_phase", "subagent_persistent"])
async def test_session_policy_reaches_construction_and_projection(bobi_install, monkeypatch, entry_point):
    raw = configured(mode="enforce", entry_points=[entry_point])
    routing_context = replace(context(), entry_point=entry_point)
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    fixed_policy(monkeypatch)
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
    monkeypatch.setattr("bobi.metrics.routing.TypeSafePolicy", no_policy)
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
@pytest.mark.parametrize("entry_point", ["subagent_phase", "subagent_persistent"])
async def test_session_explicit_model_bypasses_policy(bobi_install, monkeypatch, entry_point):
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured(entry_points=[entry_point])))
    runtime = MetricsRuntime(bobi_install.state_dir.parent, mode="enabled")
    monkeypatch.setattr("bobi.metrics.routing.get_runtime", lambda: runtime)
    def no_policy(*args):
        raise AssertionError("explicit model must not load policy")
    monkeypatch.setattr("bobi.metrics.routing.TypeSafePolicy", no_policy)
    routing_context = replace(context(), entry_point=entry_point,
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


@pytest.mark.parametrize("returned,matches", [
    ("jev-1.13.0", True), ("jev-1.13.4", True), ("jev-1.13.0+build7", True), ("jev-1.13", True),
    ("jev-1.14.0", False), ("jev-2.13.0", False), ("other-1.13.0", False), ("jev-latest", False),
])
def test_policy_version_tolerates_patch_bumps_only(returned, matches):
    from bobi.metrics.policy import version_matches

    assert version_matches(returned, "jev-1.13.0") is matches


def _recorded_fallbacks(runtime, monkeypatch):
    recorded = []
    admit = runtime.admit_route
    def spy(session_name, decision, **kwargs):
        if decision is None:
            recorded.append(kwargs["fallback_reason"])
        return admit(session_name, decision, **kwargs)
    monkeypatch.setattr(runtime, "admit_route", spy)
    return recorded


def test_routing_withheld_from_a_child_and_not_restored_is_recorded(monkeypatch, tmp_path):
    import os

    from bobi import config as config_module
    from bobi.config import load_dotenv

    (tmp_path / "package").mkdir()
    (tmp_path / ".env").write_text("UNRELATED=1\n")
    # A provider launch blanked the routing config the parent had as real process env.
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", "")
    monkeypatch.setenv("BOBI_INTERNAL_PROVIDER_CLEARED_ENV", "BOBI_METRICS_EXPERIMENT_JSON")
    monkeypatch.setattr(config_module, "_UNRESTORED_PROVIDER_ENV", set())
    load_dotenv(tmp_path)
    assert "BOBI_METRICS_EXPERIMENT_JSON" not in os.environ
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    recorded = _recorded_fallbacks(runtime, monkeypatch)
    try:
        outcome = asyncio.run(resolve_route(context(), runtime=runtime))
        assert outcome.model == "configured" and outcome.reason == "routing_config_unrestored"
        assert recorded == ["routing_config_unrestored"]
    finally:
        assert runtime.close(timeout=2)


@pytest.mark.parametrize("policy_brain,agent_brain,routed", [
    ("auto", "codex", True), ("auto", "claude", True), ("codex", "codex", True), ("codex", "claude", False),
])
def test_policy_brain_auto_follows_the_agent(monkeypatch, tmp_path, policy_brain, agent_brain, routed):
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured(brain=policy_brain, mode="enforce")))
    fixed_policy(monkeypatch)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    recorded = _recorded_fallbacks(runtime, monkeypatch)
    try:
        outcome = asyncio.run(resolve_route(replace(context(), brain=agent_brain), runtime=runtime))
        if routed:
            assert outcome.model == "cheap" and recorded == []
        else:
            # A policy pinned to another brain is recorded, not silently skipped.
            assert outcome.model == "configured" and outcome.reason == "routing_brain_mismatch"
            assert recorded == ["routing_brain_mismatch"]
    finally:
        assert runtime.close(timeout=2)


@pytest.mark.parametrize("change", [{"policy_version": "jev-1.14.0"}, {"instructions": "Changed"},
                                    {"candidate_models": ["control", "other"]}])
def test_stale_sticky_route_records_why_the_session_kept_its_model(monkeypatch, tmp_path, caplog, change):
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured(mode="enforce")))
    fixed_policy(monkeypatch)
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex-test")
    ctx = context()
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    assert asyncio.run(resolve_route(ctx, runtime=runtime)).model == "cheap"
    save_session_id("agent", "provider-session", model="cheap", root=tmp_path)
    assert runtime.close(timeout=2)
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured(mode="enforce", **change)))
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    recorded = _recorded_fallbacks(runtime, monkeypatch)
    try:
        outcome = asyncio.run(resolve_route(replace(ctx, fresh=False), runtime=runtime))
        # The saved transcript ran cheap; a policy edit never switches it mid-session.
        assert outcome.model == "cheap" and outcome.reason == "routing_sticky_stale"
        assert recorded == ["routing_sticky_stale"]
        assert any(getattr(record, "fallback_reason", None) == "routing_sticky_stale" for record in caplog.records)
    finally:
        assert runtime.close(timeout=2)


def test_sticky_route_read_failure_is_not_called_a_config_error(monkeypatch, tmp_path):
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured()))
    save_session_id("agent", "provider-session", model="cheap", root=tmp_path)
    def unreadable(*args, **kwargs):
        raise ValueError("corrupt route record")
    monkeypatch.setattr("bobi.metrics.routing.load_session_route", unreadable)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    try:
        outcome = asyncio.run(resolve_route(replace(context(), fresh=False), runtime=runtime))
        assert outcome.model == "cheap" and outcome.reason == "routing_sticky_invalid"
    finally:
        assert runtime.close(timeout=2)


def test_rejected_admission_is_logged_with_its_reason(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(configured()))
    fixed_policy(monkeypatch)
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    monkeypatch.setattr(runtime, "admit_route", lambda *args, **kwargs: False)
    try:
        outcome = asyncio.run(resolve_route(context(), runtime=runtime))
        assert outcome.model == "control" and outcome.reason == "admission_rejected"
        assert any(getattr(record, "fallback_reason", None) == "admission_rejected" for record in caplog.records)
    finally:
        assert runtime.close(timeout=2)


def _fallback_log(root):
    from bobi import paths

    path = paths.state_path(root) / "jev_routing_fallback.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.asyncio
async def test_session_retries_on_control_when_the_routed_model_cannot_start(bobi_install, monkeypatch):
    from bobi.metrics.routing import RouteOutcome

    routed = replace(decision(), model_selected="broken-model", control_model="control")
    context = RoutingContext("agent", "subagent_phase", "claude", "configured", False, "", "engineer", True)
    session = Session("agent", str(bobi_install.repo_path), fresh=True, routing=context)
    async def route(ctx):
        return RouteOutcome("broken-model", routed)
    connects = []
    class Done(Exception):
        pass
    class Client:
        def __init__(self, model):
            self.model = model
        async def connect(self):
            connects.append(self.model)
            if self.model == "broken-model":
                raise RuntimeError("model broken-model not found API_KEY=private-value")
            raise Done
    monkeypatch.setattr("bobi.metrics.routing.resolve_route", route)
    monkeypatch.setattr(session, "_make_brain_session", lambda resume=None: Client(session._extra_options["model"]))
    with pytest.raises(Done):
        await session._run(None)
    assert connects == ["broken-model", "control"]
    [incident] = _fallback_log(bobi_install.repo_path)
    assert incident["event"] == "jev_candidate_failed" and incident["attempted_model"] == "broken-model"
    assert datetime.fromisoformat(incident["logged_at"]).tzinfo is not None
    assert incident["fallback_model"] == "control" and incident["brain"] == "claude"
    assert "not found" in incident["error"] and "private-value" not in incident["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("routed_model,error_kind,expected", [
    ("broken-model", "", ["broken-model", "control"]),
    ("control", "", ["control"]),
    ("broken-model", "max_turns_reached", ["broken-model"]),
])
async def test_session_reruns_a_failed_first_turn_on_control(bobi_install, monkeypatch, routed_model, error_kind, expected):
    from bobi.inbox import Message

    session = Session("agent", str(bobi_install.repo_path), fresh=True,
                      routing=RoutingContext("agent", "subagent_phase", "claude", "configured", False, "", "engineer", True))
    session._extra_options["model"] = routed_model
    session._route_decision = replace(decision(), model_selected=routed_model, control_model="control")
    session._set_state("waiting_input")
    turns = []
    class Client:
        def __init__(self, model):
            self.model = model
        async def connect(self):
            pass
        async def query(self, text):
            turns.append(self.model)
    async def drain():
        session._last_is_error = session._client.model == "broken-model" or (error_kind and routed_model == "control")
        session._last_error_kind = error_kind if session._last_is_error else ""
        session._last_error_message = "model not available" if session._last_is_error else ""
        return "done"
    async def disconnect(client):
        pass
    session._client = Client(routed_model)
    monkeypatch.setattr(session, "_make_brain_session", lambda resume=None: Client(session._extra_options["model"]))
    monkeypatch.setattr(session, "_drain_turn", drain)
    monkeypatch.setattr(session, "_safe_disconnect", disconnect)
    monkeypatch.setattr(session, "_is_transient_turn_error", lambda: False)
    await session._process_message(Message(id="m1", sender="launch", text="do the task"))
    assert turns == expected
    assert session._route_decision is None
    logged = [item["attempted_model"] for item in _fallback_log(bobi_install.repo_path)]
    assert logged == (["broken-model"] if expected == ["broken-model", "control"] else [])


@pytest.mark.asyncio
async def test_session_rerun_on_control_is_its_own_turn(bobi_install, monkeypatch):
    from bobi.inbox import Message

    observed = []

    class Observation:
        results = []

        def __init__(self, model):
            self.model, self.status = model, None
            observed.append(self)

        def finish(self, *, status, error_kind=""):
            self.status = status

    monkeypatch.setattr("bobi.session.observe_turn",
                        lambda _name, **kwargs: Observation(kwargs["model_requested"]))
    session = Session("agent", str(bobi_install.repo_path), fresh=True,
                      routing=RoutingContext("agent", "subagent_phase", "claude", "configured", False, "", "engineer", True))
    session._extra_options["model"] = "broken-model"
    session._route_decision = replace(decision(), model_selected="broken-model", control_model="control")
    session._set_state("waiting_input")

    class Client:
        def __init__(self, model):
            self.model = model
        async def connect(self):
            pass
        async def query(self, text):
            pass

    async def drain():
        session._last_is_error = session._client.model == "broken-model"
        session._last_error_kind = ""
        session._last_error_message = "model not available" if session._last_is_error else ""
        return "done"

    async def disconnect(client):
        pass

    session._client = Client("broken-model")
    monkeypatch.setattr(session, "_make_brain_session", lambda resume=None: Client(session._extra_options["model"]))
    monkeypatch.setattr(session, "_drain_turn", drain)
    monkeypatch.setattr(session, "_safe_disconnect", disconnect)
    monkeypatch.setattr(session, "_is_transient_turn_error", lambda: False)
    await session._process_message(Message(id="m1", sender="launch", text="do the task"))
    # The failed pick and the control rerun are two turns, so the rerun records the fallback.
    assert [(item.model, item.status) for item in observed] == [
        ("broken-model", "failed"), ("control", "failed")]


@pytest.mark.parametrize("status,error_kind,tools_ran,allowed", [
    ("assigned", "", False, True),
    ("assigned", "", True, False),  # a rerun would repeat the tool's side effects
    ("assigned", ERROR_KIND_AUTHENTICATION, False, False),  # the control model fails the same way
    ("assigned", ERROR_KIND_MAX_TURNS, False, False),
    ("reused", "", False, False),  # a saved route belongs to a transcript
])
def test_rerun_on_control_only_when_nothing_would_repeat(status, error_kind, tools_ran, allowed):
    from types import SimpleNamespace

    from bobi.metrics.routing import may_rerun_on_control

    decision = SimpleNamespace(policy_metadata_json=json.dumps({"status": status}))
    assert may_rerun_on_control(decision, error_kind=error_kind, tools_ran=tools_ran) is allowed
    assert may_rerun_on_control(None, error_kind="", tools_ran=False) is False
