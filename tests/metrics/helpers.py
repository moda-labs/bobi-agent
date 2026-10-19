"""Shared synthetic payloads and routing builders for metrics tests."""

import json

import pytest

from bobi.metrics.policies.typesafe import TypeSafePolicy
from bobi.metrics.policy import PolicyResult
from bobi.metrics.router import RouterDecision
from bobi.metrics.routing import RoutingContext


def rekey(conn):
    """Key every metrics row from its current model name, after a fixture
    inserted or renamed rows outside projection."""
    from bobi.metrics.schema import MODEL_KEY_SOURCES
    from bobi.metrics.store import fill_model_keys

    for table in MODEL_KEY_SOURCES:
        conn.execute(f"UPDATE {table} SET model_key=NULL")
    fill_model_keys(conn)


def seed_dashboard(root, started_at_us):
    from bobi.metrics.store import connect, fill_model_keys, migrate

    conn = connect(root / "state/metrics/metrics.db")
    migrate(conn)
    conn.executemany(
        "INSERT INTO sessions(session_id,session_name,brain,provider,started_at_us,status,metadata_json) "
        "VALUES(?, 'worker-a','claude','gateway',?,'completed',?)",
        [("s1", started_at_us, "{}"), ("s2", started_at_us, json.dumps({
            "router_fallback": {"fallback_reason": "invalid_config"}, "secret": "private-task",
        }))],
    )
    for index, turn_id in enumerate(("t-routine", "t-pro", "t-reused", "t-unknown")):
        started = started_at_us + index * 1_000_000
        alias, model = ("haiku", "claude-haiku-4-5") if index in (0, 3) else ("sonnet", "claude-sonnet-5-5")
        conn.execute(
            "INSERT INTO turns(turn_id,session_id,turn_index,started_at_us,status,trigger_kind,is_user_initiated) VALUES(?,?,?,?,?,'user',1)",
            (turn_id, "s2" if index == 3 else "s1", index, started, "completed"),
        )
        conn.execute(
            "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,"
            "model_requested,model_selected,started_at_us,status) VALUES(?,?,0,'gateway',?,?,?,'completed')",
            (f"i{index}", turn_id, alias, model, started),
        )
        if index < 3:
            policy = {"mode": "enforce", "status": "ok", "recommended_model": alias,
                      "confidence": 0.72 if index == 0 else 0.96, "latency_ms": 12,
                      "call_id": "call-haiku" if index == 0 else "call-sonnet", "explanation": "private-task"}
            conn.execute(
                "INSERT INTO router_decisions(router_decision_id,turn_id,variant_id,model_selected,"
                "router_score,router_latency_ms,decided_at_us,router_reason,metadata_json,"
                "router_name,router_version,candidate_models_json) "
                "VALUES(?,?,'treatment_jev',?,0.12,15,?,'private-task',?,'jev','1','[\"haiku\",\"sonnet\"]')",
                (f"r{index}", turn_id, alias, started, json.dumps({"policy": policy})),
            )
            conn.execute(
                "INSERT INTO usage_measurements(measurement_id,scope,turn_id,invocation_id,provider,"
                "model,measurement_source,is_estimated,input_tokens,output_tokens,cache_read_input_tokens,"
                "cache_write_input_tokens,observed_at_us,token_semantics_version) VALUES(?,'invocation',?,?,'gateway',?,?,?,?,?,?,?,?,1)",
                (f"u{index}", turn_id, f"i{index}", model, "local_tokenizer" if index == 2 else "provider_stream",
                 int(index == 2), (100, 300, 999)[index], (0, 60, 999)[index],
                 (0, 50, 0)[index], 0 if index == 0 else None, started),
            )
    conn.execute(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,provider,model,measurement_source,"
        "is_estimated,input_tokens,output_tokens,cache_read_input_tokens,observed_at_us,token_semantics_version) "
        "VALUES('terminal','turn','t-reused','gateway','sonnet','provider_stream',0,320,80,50,?,1)",
        (started_at_us + 2_000_000,),
    )
    fill_model_keys(conn)
    conn.commit()
    conn.close()


PROVIDER_VERSIONS = {"captured_at":"2026-09-28","claude_agent_sdk":"0.2.128","claude_code":"2.1.274","codex_cli":"0.157.1","notes":"Synthetic IDs and empty content; token shapes preserve live provider contracts."}

JSONL = {
    "claude-transcript.jsonl": r"""{"isSidechain":false,"message":{"content":[],"id":"msg-fixture-1","model":"claude-opus-4-8","role":"assistant","type":"message","usage":{"cache_creation":{"ephemeral_1h_input_tokens":8192,"ephemeral_5m_input_tokens":16384},"cache_creation_input_tokens":24576,"cache_read_input_tokens":15282,"input_tokens":2,"output_tokens":10,"output_tokens_details":null}},"requestId":"req-fixture-1","sessionId":"fixture-claude-session","timestamp":"2026-09-25T00:00:00.000Z","type":"assistant","uuid":"entry-fixture-1"}
{"isSidechain":false,"message":{"content":[],"id":"msg-fixture-1","model":"claude-opus-4-8","role":"assistant","type":"message","usage":{"cache_creation":{"ephemeral_1h_input_tokens":8192,"ephemeral_5m_input_tokens":16384},"cache_creation_input_tokens":24576,"cache_read_input_tokens":15282,"input_tokens":2,"output_tokens":10,"output_tokens_details":null}},"requestId":"req-fixture-duplicate","sessionId":"fixture-claude-session","timestamp":"2026-09-25T00:00:00.001Z","type":"assistant","uuid":"entry-fixture-duplicate"}
{"isSidechain":true,"message":{"content":[],"id":"msg-fixture-sidechain","model":"claude-haiku-4-5","role":"assistant","type":"message","usage":{"cache_creation_input_tokens":0,"cache_read_input_tokens":128,"input_tokens":3,"output_tokens":4,"output_tokens_details":{"thinking_tokens":1}}},"requestId":"req-fixture-shared","sessionId":"fixture-claude-session","timestamp":"2026-09-25T00:00:00.002Z","type":"assistant","uuid":"entry-fixture-sidechain"}
""",
    "codex-rollout.jsonl": r"""{"payload":{"cli_version":"0.157.1","id":"fixture-codex-thread","model_provider":"openai"},"timestamp":"2026-09-25T00:00:00.000Z","type":"session_meta"}
{"payload":{"model":"gpt-test","turn_id":"fixture-codex-turn"},"timestamp":"2026-09-25T00:00:00.000Z","type":"turn_context"}
{"ordinal":1,"payload":{"response_id":"resp-fixture-old","root_turn_id":"fixture-codex-turn","session_id":"fixture-codex-thread","turn_id":"fixture-codex-turn","turn_token_usage":{"cache_write_input_tokens":64,"cached_input_tokens":512,"input_tokens":2048,"output_tokens":20,"reasoning_output_tokens":8,"total_tokens":2068}},"timestamp":"2026-09-25T00:00:00.000Z","type":"token_usage_record"}
{"ordinal":2,"payload":{"response_id":"resp-fixture-final","root_turn_id":"fixture-codex-turn","session_id":"fixture-codex-thread","turn_id":"fixture-codex-turn","turn_token_usage":{"cache_write_input_tokens":128,"cached_input_tokens":1024,"input_tokens":4096,"output_tokens":30,"reasoning_output_tokens":12,"total_tokens":4126}},"timestamp":"2026-09-25T00:00:01.000Z","type":"token_usage_record"}
""",
    "codex-rollout-v0159.jsonl": r"""{"timestamp":"2026-09-25T00:00:00.000Z","ordinal":0,"type":"session_meta","payload":{"cli_version":"0.159.0","id":"fixture-codex-v0159","model_provider":"openai"}}
{"timestamp":"2026-09-25T00:00:00.100Z","ordinal":1,"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-v0159-1"}}
{"timestamp":"2026-09-25T00:00:00.200Z","ordinal":2,"type":"turn_context","payload":{"turn_id":"turn-v0159-1","model":"gpt-test"}}
{"timestamp":"2026-09-25T00:00:00.300Z","ordinal":3,"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":100,"cached_input_tokens":40,"cache_write_input_tokens":0,"output_tokens":10,"reasoning_output_tokens":3,"total_tokens":110}}}}
{"timestamp":"2026-09-25T00:00:01.000Z","ordinal":4,"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":150,"cached_input_tokens":60,"cache_write_input_tokens":0,"output_tokens":15,"reasoning_output_tokens":4,"total_tokens":165}}}}
{"timestamp":"2026-09-25T00:00:02.000Z","ordinal":5,"type":"event_msg","payload":{"type":"task_started","turn_id":"turn-v0159-2"}}
{"timestamp":"2026-09-25T00:00:02.100Z","ordinal":6,"type":"turn_context","payload":{"turn_id":"turn-v0159-2","model":"gpt-test"}}
{"timestamp":"2026-09-25T00:00:02.200Z","ordinal":7,"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":230,"cached_input_tokens":90,"cache_write_input_tokens":0,"output_tokens":22,"reasoning_output_tokens":6,"total_tokens":252}}}}
""",
    # Captured from codex-cli 0.144.5, the version the image pins, then trimmed
    # to the fields the usage contract reads (no instructions, paths or account data).
    "codex-rollout-v0144.jsonl": r"""{"timestamp":"2026-10-12T09:30:32.050Z","type":"session_meta","payload":{"id":"fixture-codex-v0144","cli_version":"0.144.5","originator":"codex_exec","source":"exec","model_provider":"openai","timestamp":"2026-10-12T09:30:31.945Z"}}
{"timestamp":"2026-10-12T09:30:32.051Z","type":"event_msg","payload":{"type":"task_started","turn_id":"turn-v0144-1","started_at":1791797432,"model_context_window":258400,"collaboration_mode_kind":"default"}}
{"timestamp":"2026-10-12T09:30:34.336Z","type":"turn_context","payload":{"turn_id":"turn-v0144-1","model":"gpt-5.6-luna","effort":"low"}}
{"timestamp":"2026-10-12T09:30:34.344Z","type":"event_msg","payload":{"type":"user_message","message":"Reply with the single word OK.\n"}}
{"timestamp":"2026-10-12T09:30:36.191Z","type":"event_msg","payload":{"type":"agent_message","message":"OK","phase":"final_answer"}}
{"timestamp":"2026-10-12T09:30:36.255Z","type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":13364,"cached_input_tokens":8960,"output_tokens":5,"reasoning_output_tokens":0,"total_tokens":13369},"last_token_usage":{"input_tokens":13364,"cached_input_tokens":8960,"output_tokens":5,"reasoning_output_tokens":0,"total_tokens":13369},"model_context_window":258400}}}
{"timestamp":"2026-10-12T09:30:36.261Z","type":"event_msg","payload":{"type":"task_complete","turn_id":"turn-v0144-1","last_agent_message":"OK","completed_at":1791797436,"duration_ms":4214}}
""",
    "codex-stdout.jsonl": r"""{"thread_id":"fixture-codex-thread","type":"thread.started"}
{"type":"turn.started"}
{"type":"token_count","usage":{"cached_input_tokens":1024,"input_tokens":2048,"output_tokens":20}}
{"turn_id":"fixture-codex-turn","type":"turn.completed","usage":{"cache_write_input_tokens":128,"cached_input_tokens":1024,"input_tokens":4096,"output_tokens":30,"reasoning_output_tokens":12,"total_tokens":4126}}
""",
}

# A router decision as an older build recorded it in session metadata, with
# the assignment columns the current router no longer fills. Projection must
# still turn it into a router_decisions row.
_HISTORICAL_ASSIGNMENT = r"""{
  "experiment_id": "jev-routing-v1",
  "variant_id": "control",
  "assignment_unit": "experiment_subject",
  "assignment_status": "assigned",
  "assignment_key_hash": "dd653ee82de87012aa58849d303a3707025bcc568251bed2b50f6c98af3216d0",
  "assignment_algorithm": "hmac-sha256-u64-v1",
  "cohort": null,
  "router_name": "jev",
  "router_version": "1.0.0",
  "policy_version": "policy-2026-09-29",
  "feature_schema_version": "features-v1",
  "candidate_models_json": "[\"model-control\",\"model-treatment\"]",
  "model_selected": "model-control",
  "control_model": "model-control",
  "router_score": 0.13753355569280226,
  "router_reason": "deterministic_weighted_assignment",
  "router_latency_ms": 0.033791,
  "fallback_reason": null,
  "decided_at_us": 100,
  "metadata_json": "{\"config_fingerprint\":\"702c8e6467a885eb546d8524b56983cd1b32bbcbed0ff74baecee8b949c36673\",\"expected_weight\":0.5,\"expected_weights\":{\"control\":0.5,\"treatment\":0.5}}"
}
"""


def historical_assignment():
    return json.loads(_HISTORICAL_ASSIGNMENT)


@pytest.fixture
def metrics_fixtures(tmp_path):
    directory = tmp_path / "provider-fixtures"
    directory.mkdir()
    for name, payload in JSONL.items():
        (directory / name).write_bytes(payload.encode("utf-8"))
    return directory


def configured(**changes):
    """The short JEV config production writes, routing between control and cheap."""
    return {"experiment_id": "routing-test", "mode": "shadow", "control_model": "control",
            "candidate_models": ["control", "cheap"], "roles": ["engineer"],
            "entry_points": ["subagent_phase"], "brain": "codex",
            "instructions": "Route simple tasks to cheap.", **changes}


def fixed_policy(monkeypatch, model="cheap", *, confidence=1.0, version=None):
    """Answer every TypeSafe policy call with ``model`` instead of calling the vendor."""
    calls = []

    async def decide(self, request, *, timeout_s):
        calls.append(request)
        return PolicyResult(model, confidence, None, version or request.pinned_version, None, None)

    monkeypatch.setattr(TypeSafePolicy, "decide", decide)
    return calls


def context():
    return RoutingContext("agent", "subagent_phase", "codex", "configured", False,
                          "Do a task", "engineer", True)


def decision():
    return RouterDecision(
        experiment_id="jev-routing-v1", cohort=None, router_name="jev-router", router_version="v1",
        policy_version="jev-1.13.0", feature_schema_version="jev-features-v1",
        candidate_models=("model-control", "model-treatment"), model_selected="model-control",
        control_model="model-control", router_reason="policy_shadow", router_latency_ms=0.03,
        fallback_reason=None, decided_at_us=100, config_fingerprint="fingerprint",
        entry_point="subagent_phase", policy_metadata_json='{"status":"not_called"}')
