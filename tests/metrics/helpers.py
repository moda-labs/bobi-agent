"""Shared synthetic payloads and routing builders for metrics tests."""

import json

import pytest

from bobi.metrics.router import ExperimentConfig, RouterDecision, assign_variant
from bobi.metrics.routing import RoutingContext


def seed_dashboard(root, started_at_us):
    from bobi.metrics.store import connect, migrate

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
        model = "deepseek-flash" if index in (0, 3) else "deepseek-v4-pro"
        conn.execute(
            "INSERT INTO turns(turn_id,session_id,turn_index,started_at_us,status,trigger_kind,is_user_initiated) VALUES(?,?,?,?,?,'user',1)",
            (turn_id, "s2" if index == 3 else "s1", index, started, "completed"),
        )
        conn.execute(
            "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,"
            "model_requested,model_selected,started_at_us,status) VALUES(?,?,0,'gateway',?,?,?,'completed')",
            (f"i{index}", turn_id, f"ds/{model}", model, started),
        )
        if index < 3:
            policy = {"mode": "enforce", "status": "ok", "recommended_model": f"ds/{model}",
                      "confidence": 0.72 if index == 0 else 0.96, "latency_ms": 12,
                      "call_id": "call-flash" if index == 0 else "call-pro", "explanation": "private-task"}
            conn.execute(
                "INSERT INTO router_decisions(router_decision_id,turn_id,variant_id,model_selected,"
                "router_score,router_latency_ms,decided_at_us,router_reason,metadata_json,"
                "router_name,router_version,candidate_models_json) "
                "VALUES(?,?,'treatment_jev',?,0.12,15,?,'private-task',?,'jev','1','[\"ds/deepseek-flash\",\"ds/deepseek-v4-pro\"]')",
                (f"r{index}", turn_id, f"ds/{model}", started, json.dumps({"policy": policy})),
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
        "VALUES('terminal','turn','t-reused','gateway','ds/deepseek-v4-pro','provider_stream',0,320,80,50,?,1)",
        (started_at_us + 2_000_000,),
    )
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
    "codex-stdout.jsonl": r"""{"thread_id":"fixture-codex-thread","type":"thread.started"}
{"type":"turn.started"}
{"type":"token_count","usage":{"cached_input_tokens":1024,"input_tokens":2048,"output_tokens":20}}
{"turn_id":"fixture-codex-turn","type":"turn.completed","usage":{"cache_write_input_tokens":128,"cached_input_tokens":1024,"input_tokens":4096,"output_tokens":30,"reasoning_output_tokens":12,"total_tokens":4126}}
""",
}

_ASSIGNMENT_VECTORS = r"""{
  "algorithm": "hmac-sha256-u64-v1",
  "assignment_unit": "experiment_subject",
  "config": {
    "control_model": "model-control",
    "experiment_id": "jev-routing-v1",
    "feature_schema_version": "features-v1",
    "policy_version": "policy-2026-09-29",
    "router_name": "jev",
    "router_version": "1.0.0",
    "variants": [
      {"model": "model-control", "variant_id": "control", "weight": 0.5},
      {"model": "model-treatment", "variant_id": "treatment", "weight": 0.5}
    ]
  },
  "secret": "public-phase4-test-secret",
  "vectors": [
    {"assignment_key": "subject-001", "assignment_key_hash": "dd653ee82de87012aa58849d303a3707025bcc568251bed2b50f6c98af3216d0", "bucket": 0.13753355569280226, "variant_id": "control"},
    {"assignment_key": "subject-002", "assignment_key_hash": "f7c8ec8ad647fe2a9ff741b70e80e893f0e043fc9cf105ce64af752c1db7f4fe", "bucket": 0.6919266302034648, "variant_id": "treatment"},
    {"assignment_key": "subject-003", "assignment_key_hash": "94b1f770a5540c6d8bc074401352d31336f2b836ab636f94b140820ead86441c", "bucket": 0.7637140759970137, "variant_id": "treatment"},
    {"assignment_key": "subject-004", "assignment_key_hash": "defb4aa2be13b4437ca22a8ad66b2c91095b3e18bb526927885291c4588a0685", "bucket": 0.19307702998731682, "variant_id": "control"}
  ]
}
"""

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


def assignment_vectors():
    return json.loads(_ASSIGNMENT_VECTORS)


def historical_assignment():
    return json.loads(_HISTORICAL_ASSIGNMENT)


@pytest.fixture
def metrics_fixtures(tmp_path):
    directory = tmp_path / "provider-fixtures"
    directory.mkdir()
    for name, payload in JSONL.items():
        (directory / name).write_bytes(payload.encode("utf-8"))
    return directory


def configured():
    return {"experiment_id": "routing-test", "router_name": "bobi-arm", "router_version": "1",
        "policy_version": "p1", "feature_schema_version": "jev-features-v1", "control_model": "control",
        "variants": [{"variant_id": "control", "weight": 0.5, "model": "control"},
                     {"variant_id": "treatment", "weight": 0.5, "policy": "static"}],
        "policy": {"name": "static", "version": "v1", "brain": "codex", "mode": "shadow",
            "candidate_models": ["control", "cheap"], "scope": {"roles": ["engineer"],
            "entry_points": ["session_start"]}, "egress": {"prompt": "none"},
            "credential_env": "ROUTING_TEST_KEY", "options": {"model": "cheap"}}}


def context(raw, arm):
    config = ExperimentConfig.from_mapping(raw)
    subject = next(str(index) for index in range(100) if assign_variant(config, b"test-secret",
        assignment_unit="experiment_subject", assignment_key=str(index))[0].variant_id == arm)
    return RoutingContext("agent", "session_start", "codex", "configured", False,
                          "Do a task", "engineer", True, experiment_subject=subject)


def decision():
    fields = historical_assignment()
    metadata = json.loads(fields.pop("metadata_json"))
    fields["candidate_models"] = tuple(json.loads(fields.pop("candidate_models_json")))
    return RouterDecision(**fields, config_fingerprint=metadata["config_fingerprint"],
        expected_weight=metadata["expected_weight"],
        expected_weights=tuple(metadata["expected_weights"].items()),
        entry_point="session_start", policy_metadata_json='{"status":"not_called"}')
