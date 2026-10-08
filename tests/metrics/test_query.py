"""Operational query and command contract regressions."""

import httpx
import json
import sqlite3
import pytest
from bobi.admin_client import AdminClientError, run_admin_command
from bobi.cli import main, metrics
from bobi.metrics.query import MAX_RESPONSE_BYTES, MAX_ROWS, MetricsQueries, MetricsQueryError
from bobi.metrics.store import connect, migrate
from click.testing import CliRunner
from tests.metrics.helpers import seed_dashboard


def test_dashboard_canonical_usage_routing_and_privacy(tmp_path):
    seed_dashboard(tmp_path, 1_000_000)
    queries = MetricsQueries(tmp_path)
    args = {"from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z"}
    summary = queries.summary({**args, "include_dashboard": True})
    assert summary["totals"]["input_tokens"] == 720
    assert summary["totals"]["output_tokens"] == 140
    assert summary["totals"]["cache_write_input_tokens"] is None
    assert summary["buckets"][0]["input_tokens"] == 720
    assert summary["buckets"][0]["is_estimated"] == 0
    assert summary["routing"]["policy_calls"] == 2
    assert summary["routing"]["routed_turns"] == 3
    assert summary["routing"]["fallback_turns"] == 1
    turns = queries.turns(args)["turns"]
    assert turns[0]["session_fallback_reason"] == "invalid_config"
    assert turns[0]["usage"]["input_tokens"] is None
    assert turns[1]["route_reused"] == 1
    assert turns[1]["usage"]["input_tokens"] == 320
    assert turns[-1]["route_reused"] == 0
    assert turns[-1]["confidence"] == 0.72
    assert turns[-1]["usage"]["output_tokens"] == 0
    assert turns[-1]["tool_count"] == 0
    assert turns[-1]["costs"] == {"reported_cost_usd": None, "estimated_cost_usd": None}
    detail = queries.turn({"turn_id": "t-pro", "include_policy": True})
    assert detail["router_decisions"][0]["confidence"] == 0.96
    assert "private-task" not in json.dumps([summary, turns, detail])
    filtered = queries.summary({**args, "include_dashboard": True, "model": "deepseek-v4-pro"})
    assert filtered["totals"]["input_tokens"] == 620
    assert filtered["routing"]["turns"] == 2


def test_dashboard_keyset_filters_and_lifecycle_names(tmp_path):
    seed_dashboard(tmp_path, 1_000_000)
    queries = MetricsQueries(tmp_path)
    args = {"from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z", "limit": 2}
    first = queries.turns({**args, "session_name": "worker-a"})
    second = queries.turns({**args, "session_name": "worker-a", "cursor": first["next_cursor"]})
    assert [turn["turn_id"] for turn in first["turns"] + second["turns"]] == [
        "t-unknown", "t-reused", "t-pro", "t-routine",
    ]
    assert second["next_cursor"] is None
    assert len(queries.turns({**args, "session_id": "s2"})["turns"]) == 1
    assert queries.turns({**args, "session_name": "absent"})["turns"] == []
    with pytest.raises(MetricsQueryError, match="cursor"):
        queries.turns({**args, "session_name": "other", "cursor": first["next_cursor"]})

def test_dashboard_sessions_and_canonical_turn_breakdown(tmp_path):
    seed_dashboard(tmp_path, 1_000_000)
    queries = MetricsQueries(tmp_path)
    args = {"from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z", "limit": 1}
    first = queries.sessions(args)
    second = queries.sessions({**args, "cursor": first["next_cursor"]})
    assert {first["sessions"][0]["session_id"], second["sessions"][0]["session_id"]} == {"s1", "s2"}
    assert first["sessions"][0]["session_name"] == second["sessions"][0]["session_name"] == "worker-a"
    assert second["next_cursor"] is None
    detail = queries.turn({"turn_id": "t-reused", "include_breakdown": True})
    assert detail["session"]["session_id"] == "s1"
    assert detail["best_usage"][0]["input_tokens"] == 320
    assert detail["best_usage"][0]["is_estimated"] == 0
    assert detail["usage_totals"]["input_tokens"] == 320
    assert detail["invocation_usage"][0]["is_estimated"] == 1
    assert len(detail["usage_measurements"]) > len(detail["best_usage"])
    assert "raw_usage_json" not in detail["best_usage"][0]
    assert "private-task" not in json.dumps(detail)


def test_dashboard_marks_estimates_without_promoting_unknowns(tmp_path):
    seed_dashboard(tmp_path, 1_000_000)
    writer = connect(tmp_path / "state/metrics/metrics.db")
    writer.execute("DELETE FROM usage_measurements WHERE measurement_id='terminal'")
    writer.commit()
    writer.close()
    result = MetricsQueries(tmp_path).summary({
        "from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z", "include_dashboard": True,
    })
    assert result["totals"]["input_tokens"] == 1399
    assert result["buckets"][0]["is_estimated"] == 1
    assert result["totals"]["cache_write_input_tokens"] is None
    assert result["coverage"]["estimated_invocations"] == 1
    assert result["coverage"]["unknown_invocations"] == 1


def test_dashboard_read_snapshot_does_not_block_wal_writer(tmp_path):
    seed_dashboard(tmp_path, 1_000_000)
    queries = MetricsQueries(tmp_path)

    def read_snapshot(reader):
        original = reader.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        writer = connect(queries.db_path)
        writer.execute("UPDATE turns SET status='failed' WHERE turn_id='t-routine'")
        writer.commit()
        writer.close()
        assert reader.execute("SELECT status FROM turns WHERE turn_id='t-routine'").fetchone()[0] == "completed"
        assert original == 4
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("DELETE FROM turns")
        return {}

    queries._run(read_snapshot)
    assert queries.turn({"turn_id": "t-routine"})["turn"]["status"] == "failed"




@pytest.fixture
def metrics_root(tmp_path):
    root = tmp_path / "agent"
    conn = connect(root / "state" / "metrics" / "metrics.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO sessions(session_id,session_name,brain,provider,project,started_at_us,status) "
        "VALUES('s1','manager','claude','anthropic','/private/project/path',1000000,'running')"
    )
    conn.execute(
        "INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,is_user_initiated,"
        "started_at_us,ended_at_us,wall_duration_ms,status,prompt_sha256,prompt_bytes) "
        "VALUES('t1','s1',0,'user',1,2000000,3000000,1000,'completed','safehash',12)"
    )
    conn.execute(
        "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,"
        "model_selected,started_at_us,ended_at_us,wall_duration_ms,status) "
        "VALUES('i1','t1',0,'anthropic','sonnet',2100000,2900000,800,'completed')"
    )
    conn.execute(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,invocation_id,provider,"
        "model,measurement_source,is_estimated,token_semantics_version,input_tokens,"
        "cache_read_input_tokens,cache_write_input_tokens,cache_write_5m_input_tokens,"
        "output_tokens,observed_at_us) VALUES"
        "('u1','invocation','t1','i1','anthropic','sonnet','provider_stream',0,1,100,20,10,10,30,3000000)"
    )
    conn.execute(
        "INSERT INTO cost_measurements(cost_measurement_id,scope,session_id,turn_id,invocation_id,"
        "provider,model,amount_usd,measurement_source,is_estimated,observed_at_us) "
        "VALUES('c1','invocation','s1','t1','i1','anthropic','sonnet',0.25,'provider_stream',0,3000000)"
    )
    conn.execute(
        "INSERT INTO tool_executions(tool_execution_id,turn_id,triggering_invocation_id,tool_name,"
        "tool_kind,input_sha256,input_bytes,output_sha256,output_bytes,output_estimated_tokens,"
        "started_at_us,ended_at_us,status,attribution_method,metadata_json) "
        "VALUES('x1','t1','i1','view_file','file_read','ih',10,'oh',50,13,2200000,2300000,"
        "'completed','local_tokenizer','{\"secret\":\"must-not-leak\"}')"
    )
    conn.execute(
        "INSERT INTO router_decisions(router_decision_id,turn_id,experiment_id,variant_id,"
        "assignment_status,router_name,router_version,candidate_models_json,model_selected,"
        "router_latency_ms,decided_at_us) VALUES"
        "('r1','t1','exp1','control','assigned','jev','1','[\"sonnet\"]','sonnet',4.5,2050000)"
    )
    conn.commit()
    conn.close()
    return root


def test_turn_returns_exact_dimensions_and_no_private_metadata(metrics_root):
    result = MetricsQueries(metrics_root).turn({"turn_id": "t1"})
    assert "prompt_sha256" not in result["turn"]
    assert "input_sha256" not in result["tool_executions"][0]
    assert "output_sha256" not in result["tool_executions"][0]
    assert "assignment_key_hash" not in result["router_decisions"][0]
    assert "router_reason" not in result["router_decisions"][0]
    assert result["usage_measurements"][0]["is_estimated"] == 0
    assert result["usage_measurements"][0]["cache_write_5m_input_tokens"] == 10
    assert result["usage_measurements"][0]["cache_write_1h_input_tokens"] is None
    assert result["coverage"] == pytest.approx({
        "exact_invocations": 1,
        "estimated_invocations": 0,
        "unknown_invocations": 0,
        "events_dropped": None,
        "collector_lag_ms": None,
        "retained_from": 2000000,
        "retained_to": 3000000,
    })
    assert "metadata_json" not in result["tool_executions"][0]
    assert "safehash" not in json.dumps(result)
    assert '"ih"' not in json.dumps(result)
    assert '"oh"' not in json.dumps(result)
    assert "must-not-leak" not in json.dumps(result)

    session = MetricsQueries(metrics_root).session({"session_id": "s1"})
    assert "project" not in session["session"]
    assert "/private/project/path" not in json.dumps(session)


def test_public_query_projections_do_not_expand_with_private_schema_columns(metrics_root):
    db = metrics_root / "state" / "metrics" / "metrics.db"
    conn = connect(db)
    conn.execute("ALTER TABLE sessions ADD COLUMN future_secret TEXT")
    conn.execute("ALTER TABLE turns ADD COLUMN future_secret TEXT")
    conn.execute("ALTER TABLE workflow_steps ADD COLUMN future_secret TEXT")
    conn.execute("ALTER TABLE llm_invocations ADD COLUMN future_secret TEXT")
    conn.execute("UPDATE sessions SET future_secret='session-secret'")
    conn.execute("UPDATE turns SET future_secret='turn-secret'")
    conn.execute("UPDATE llm_invocations SET future_secret='invocation-secret'")
    conn.commit()
    conn.close()

    queries = MetricsQueries(metrics_root)
    payload = {
        "session": queries.session({"session_id": "s1"}),
        "turn": queries.turn({"turn_id": "t1"}),
    }

    rendered = json.dumps(payload)
    assert "future_secret" not in rendered
    assert "session-secret" not in rendered
    assert "turn-secret" not in rendered
    assert "invocation-secret" not in rendered


def test_summary_uses_best_invocation_usage_without_double_counting(metrics_root):
    result = MetricsQueries(metrics_root).summary({"window_seconds": 10, "end_at": 4})
    assert result["totals"]["input_tokens"] == 100
    assert result["totals"]["cache_read_input_tokens"] == 20
    assert result["totals"]["reported_cost_usd"] == 0.25
    assert result["totals"]["turns"] == 1
    assert result["totals"]["invocations"] == 1
    assert result["totals"]["tool_executions"] == 1

    grouped = MetricsQueries(metrics_root).summary({
        "from": "1970-01-01T00:00:00Z",
        "to": "1970-01-01T00:00:04Z",
        "group_by": ["model", "experiment_id", "variant_id"],
        "model": "sonnet",
        "experiment_id": "exp1",
        "variant_id": "control",
    })
    assert grouped["groups"] == [pytest.approx({
        "model": "sonnet",
        "experiment_id": "exp1",
        "variant_id": "control",
        "input_tokens": 100,
        "uncached_input_tokens": None,
        "cache_read_input_tokens": 20,
        "cache_write_input_tokens": 10,
        "cache_write_5m_input_tokens": 10,
        "cache_write_1h_input_tokens": None,
        "cache_write_unknown_ttl_input_tokens": None,
        "output_tokens": 30,
        "reasoning_output_tokens": None,
        "turns": 1,
        "invocations": 1,
    })]


def test_summary_uses_turn_usage_when_invocation_coverage_is_partial(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,"
        "model_selected,started_at_us,ended_at_us,wall_duration_ms,status) "
        "VALUES('i2','t1',1,'anthropic','sonnet',2910000,2950000,40,'completed')"
    )
    conn.execute(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,provider,model,"
        "measurement_source,is_estimated,token_semantics_version,input_tokens,output_tokens,"
        "observed_at_us) VALUES"
        "('u-turn','turn','t1','anthropic','sonnet','provider_stream',0,1,250,70,3000000)"
    )
    conn.commit()
    conn.close()

    result = MetricsQueries(metrics_root).summary({
        "window_seconds": 10,
        "end_at": 4,
        "group_by": ["model"],
    })

    assert result["totals"]["input_tokens"] == 250
    assert result["totals"]["output_tokens"] == 70
    assert result["totals"]["invocations"] == 2
    assert result["groups"] == [pytest.approx({
        "model": "sonnet",
        "input_tokens": 250,
        "uncached_input_tokens": None,
        "cache_read_input_tokens": None,
        "cache_write_input_tokens": None,
        "cache_write_5m_input_tokens": None,
        "cache_write_1h_input_tokens": None,
        "cache_write_unknown_ttl_input_tokens": None,
        "output_tokens": 70,
        "reasoning_output_tokens": None,
        "turns": 1,
        "invocations": 2,
    })]


def test_exact_turn_usage_wins_over_exact_invocation_usage(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute("UPDATE usage_measurements SET output_tokens=0 WHERE measurement_id='u1'")
    conn.execute(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,provider,model,"
        "measurement_source,is_estimated,token_semantics_version,input_tokens,"
        "cache_read_input_tokens,cache_write_input_tokens,cache_write_5m_input_tokens,"
        "output_tokens,observed_at_us) VALUES"
        "('u-turn','turn','t1','anthropic','sonnet','provider_stream',0,1,100,20,10,10,30,3000001)"
    )
    conn.commit()
    conn.close()

    queries = MetricsQueries(metrics_root)
    summary = queries.summary({"window_seconds": 10, "end_at": 4})

    assert summary["totals"]["input_tokens"] == 100
    assert summary["totals"]["output_tokens"] == 30
    assert summary["coverage"]["usage_granularity"] == "turn"


def test_exact_turn_usage_remains_canonical_when_invocation_totals_match(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,provider,model,"
        "measurement_source,is_estimated,token_semantics_version,input_tokens,"
        "cache_read_input_tokens,cache_write_input_tokens,cache_write_5m_input_tokens,"
        "output_tokens,observed_at_us) VALUES"
        "('u-turn','turn','t1','anthropic','sonnet','provider_stream',0,1,100,20,10,10,30,3000001)"
    )
    conn.commit()
    conn.close()

    summary = MetricsQueries(metrics_root).summary({"window_seconds": 10, "end_at": 4})

    assert summary["totals"]["output_tokens"] == 30
    assert summary["coverage"]["usage_granularity"] == "turn"


def test_turn_usage_for_one_model_does_not_hide_another_model(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,"
        "model_selected,started_at_us,ended_at_us,wall_duration_ms,status) "
        "VALUES('i2','t1',1,'anthropic','haiku',2910000,2950000,40,'completed')"
    )
    conn.execute(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,invocation_id,provider,"
        "model,measurement_source,is_estimated,token_semantics_version,input_tokens,"
        "output_tokens,observed_at_us) VALUES"
        "('u2','invocation','t1','i2','anthropic','haiku','provider_stream',0,1,40,5,3000000)"
    )
    conn.execute(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,provider,model,"
        "measurement_source,is_estimated,token_semantics_version,input_tokens,output_tokens,"
        "observed_at_us) VALUES"
        "('u-turn','turn','t1','anthropic','sonnet','provider_stream',0,1,100,30,3000001)"
    )
    conn.commit()
    conn.close()

    result = MetricsQueries(metrics_root).summary({
        "window_seconds": 10,
        "end_at": 4,
        "group_by": ["model"],
    })

    assert result["totals"]["input_tokens"] == 140
    assert result["totals"]["output_tokens"] == 35
    assert [(row["model"], row["input_tokens"]) for row in result["groups"]] == [
        ("haiku", 40),
        ("sonnet", 100),
    ]


def test_single_invocation_model_alias_does_not_double_count_turn(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute("UPDATE llm_invocations SET model_selected='claude-current'")
    conn.execute("UPDATE usage_measurements SET model='claude-current' WHERE scope='invocation'")
    conn.execute(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,provider,model,"
        "measurement_source,is_estimated,token_semantics_version,input_tokens,output_tokens,"
        "observed_at_us) VALUES"
        "('u-turn','turn','t1','anthropic','claude-versioned','provider_stream',0,1,100,30,3000001)"
    )
    conn.commit()
    conn.close()

    result = MetricsQueries(metrics_root).summary({
        "window_seconds": 10,
        "end_at": 4,
        "group_by": ["model"],
    })

    assert result["totals"]["input_tokens"] == 100
    assert result["totals"]["output_tokens"] == 30
    assert [(row["model"], row["input_tokens"]) for row in result["groups"]] == [
        ("claude-versioned", 100),
    ]


def test_exact_turn_usage_precedes_complete_estimated_invocation_usage(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute("DELETE FROM usage_measurements WHERE measurement_id='u1'")
    conn.execute(
        "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,"
        "model_selected,started_at_us,ended_at_us,wall_duration_ms,status) "
        "VALUES('i2','t1',1,'anthropic','sonnet',2910000,2950000,40,'completed')"
    )
    conn.execute(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,provider,model,"
        "measurement_source,is_estimated,token_semantics_version,input_tokens,output_tokens,"
        "observed_at_us) VALUES"
        "('u-turn-exact','turn','t1','anthropic','sonnet','provider_stream',0,1,1000,100,3000000)"
    )
    conn.executemany(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,invocation_id,provider,"
        "model,measurement_source,is_estimated,estimator_name,estimator_version,"
        "token_semantics_version,input_tokens,output_tokens,observed_at_us) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("u-i1-est", "invocation", "t1", "i1", "anthropic", "sonnet",
             "calibrated_estimator", 1, "fixture", "1", 1, 300, 30, 2800000),
            ("u-i2-est", "invocation", "t1", "i2", "anthropic", "sonnet",
             "calibrated_estimator", 1, "fixture", "1", 1, 400, 40, 2900000),
        ],
    )
    conn.commit()
    conn.close()

    queries = MetricsQueries(metrics_root)
    summary = queries.summary({"window_seconds": 10, "end_at": 4})

    assert summary["totals"]["input_tokens"] == 1000
    assert summary["totals"]["output_tokens"] == 100
    assert summary["totals"]["invocations"] == 2
    assert summary["coverage"]["usage_granularity"] == "turn"
    assert summary["coverage"]["invocation_granularity_turns"] == 0
    assert summary["coverage"]["turn_granularity_turns"] == 1


def test_summary_counts_equal_sized_turn_fallbacks_independently(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,is_user_initiated,"
        "started_at_us,ended_at_us,wall_duration_ms,status) "
        "VALUES('t2','s1',1,'user',1,3100000,3900000,800,'completed')"
    )
    for turn_id, base_index in (("t1", 1), ("t2", 0)):
        if turn_id == "t1":
            conn.execute(
                "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,"
                "model_selected,started_at_us,status) "
                "VALUES('i2','t1',1,'anthropic','sonnet',2910000,'completed')"
            )
        else:
            conn.executemany(
                "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,"
                "model_selected,started_at_us,status) VALUES(?,?,?,?,?,?,?)",
                [
                    ("i3", "t2", base_index, "anthropic", "sonnet", 3200000, "completed"),
                    ("i4", "t2", base_index + 1, "anthropic", "sonnet", 3300000, "completed"),
                ],
            )
    conn.executemany(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,provider,model,"
        "measurement_source,is_estimated,token_semantics_version,input_tokens,output_tokens,"
        "observed_at_us) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("u-turn-1", "turn", "t1", "anthropic", "sonnet", "provider_stream", 0, 1, 250, 70, 3000000),
            ("u-turn-2", "turn", "t2", "anthropic", "sonnet", "provider_stream", 0, 1, 200, 60, 3900000),
        ],
    )
    conn.commit()
    conn.close()

    result = MetricsQueries(metrics_root).summary({
        "window_seconds": 10,
        "end_at": 4,
        "group_by": ["model"],
    })

    assert result["totals"]["turns"] == 2
    assert result["totals"]["invocations"] == 4
    assert result["groups"][0]["invocations"] == 4


def test_model_filter_does_not_split_turn_fallback_across_child_models(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,"
        "model_selected,started_at_us,ended_at_us,wall_duration_ms,status) "
        "VALUES('i2','t1',1,'anthropic','haiku',2910000,2950000,40,'completed')"
    )
    conn.execute(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,provider,model,"
        "measurement_source,is_estimated,token_semantics_version,input_tokens,output_tokens,"
        "observed_at_us) VALUES"
        "('u-turn','turn','t1','anthropic','mixed','provider_stream',0,1,250,70,3000000)"
    )
    conn.commit()
    conn.close()

    result = MetricsQueries(metrics_root).summary({
        "window_seconds": 10,
        "end_at": 4,
        "model": "haiku",
    })

    assert result["totals"]["input_tokens"] is None
    assert result["totals"]["turns"] == 0
    assert result["coverage"] == pytest.approx({
        "exact_invocations": 0,
        "estimated_invocations": 0,
        "unknown_invocations": 1,
        "events_dropped": None,
        "collector_lag_ms": None,
        "retained_from": 2000000,
        "retained_to": 3000000,
        "usage_granularity": "none",
        "invocation_granularity_turns": 0,
        "turn_granularity_turns": 0,
    })


def test_summary_reports_mixed_usage_granularity_across_turns(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,is_user_initiated,"
        "started_at_us,ended_at_us,status) "
        "VALUES('t2','s1',1,'user',1,3100000,3900000,'completed')"
    )
    conn.execute(
        "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,"
        "model_selected,started_at_us,status) "
        "VALUES('i2','t2',0,'anthropic','sonnet',3200000,'completed')"
    )
    conn.execute(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,provider,model,"
        "measurement_source,is_estimated,token_semantics_version,input_tokens,output_tokens,"
        "observed_at_us) VALUES"
        "('u-turn-2','turn','t2','anthropic','sonnet','provider_stream',0,1,200,60,3900000)"
    )
    conn.commit()
    conn.close()

    result = MetricsQueries(metrics_root).summary({"window_seconds": 10, "end_at": 4})

    assert result["totals"]["input_tokens"] == 300
    assert result["coverage"]["usage_granularity"] == "mixed"
    assert result["coverage"]["invocation_granularity_turns"] == 1
    assert result["coverage"]["turn_granularity_turns"] == 1


def test_model_filter_counts_only_tools_triggered_by_selected_model(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,"
        "model_selected,started_at_us,ended_at_us,wall_duration_ms,status) "
        "VALUES('i2','t1',1,'anthropic','haiku',2910000,2950000,40,'completed')"
    )
    conn.execute(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,invocation_id,provider,"
        "model,measurement_source,is_estimated,token_semantics_version,input_tokens,"
        "output_tokens,observed_at_us) VALUES"
        "('u2','invocation','t1','i2','anthropic','haiku','provider_stream',0,1,40,10,2950000)"
    )
    conn.execute(
        "INSERT INTO tool_executions(tool_execution_id,turn_id,triggering_invocation_id,tool_name,"
        "tool_kind,started_at_us,status,attribution_method) "
        "VALUES('x2','t1','i2','edit_file','file_edit',2920000,'completed','unknown')"
    )
    conn.commit()
    conn.close()

    result = MetricsQueries(metrics_root).summary({
        "window_seconds": 10,
        "end_at": 4,
        "model": "haiku",
    })

    assert result["totals"]["input_tokens"] == 40
    assert result["totals"]["tool_executions"] == 1


def test_summary_cost_selects_one_granularity_without_double_counting(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO cost_measurements(cost_measurement_id,scope,session_id,turn_id,provider,"
        "model,amount_usd,measurement_source,is_estimated,observed_at_us) "
        "VALUES('c-turn','turn','s1','t1','anthropic','sonnet',0.30,'provider_stream',0,3000000)"
    )
    conn.commit()
    conn.close()

    result = MetricsQueries(metrics_root).summary({"window_seconds": 10, "end_at": 4})

    assert result["totals"]["reported_cost_usd"] == pytest.approx(0.25)
    assert MetricsQueries(metrics_root).turns({"window_seconds": 10, "end_at": 4})["turns"][0]["costs"] == {
        "reported_cost_usd": pytest.approx(0.25), "estimated_cost_usd": None,
    }


def test_exact_cost_suppresses_estimate_for_same_invocation(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO cost_measurements(cost_measurement_id,scope,session_id,turn_id,invocation_id,"
        "provider,model,amount_usd,measurement_source,is_estimated,observed_at_us) "
        "VALUES('c-est','invocation','s1','t1','i1','anthropic','other-model',0.20,"
        "'price_snapshot',1,2500000)"
    )
    conn.commit()
    conn.close()

    queries = MetricsQueries(metrics_root)
    result = queries.summary({"window_seconds": 10, "end_at": 4})

    assert result["totals"]["reported_cost_usd"] == pytest.approx(0.25)
    assert result["totals"]["estimated_cost_usd"] is None


def test_mixed_exact_and_estimated_invocation_costs_keep_one_granularity(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,"
        "model_selected,started_at_us,ended_at_us,wall_duration_ms,status) "
        "VALUES('i2','t1',1,'anthropic','haiku',2910000,2950000,40,'completed')"
    )
    conn.execute(
        "INSERT INTO cost_measurements(cost_measurement_id,scope,session_id,turn_id,invocation_id,"
        "provider,model,amount_usd,measurement_source,is_estimated,observed_at_us) "
        "VALUES('c-est-2','invocation','s1','t1','i2','anthropic','haiku',0.05,"
        "'price_snapshot',1,2950000)"
    )
    conn.commit()
    conn.close()

    result = MetricsQueries(metrics_root).summary({"window_seconds": 10, "end_at": 4})

    assert result["totals"]["reported_cost_usd"] == pytest.approx(0.25)
    assert result["totals"]["estimated_cost_usd"] == pytest.approx(0.05)
    assert MetricsQueries(metrics_root).turns({"window_seconds": 10, "end_at": 4})["turns"][0]["costs"] == {
        "reported_cost_usd": pytest.approx(0.25), "estimated_cost_usd": pytest.approx(0.05),
    }


def test_exact_turn_cost_precedes_complete_estimated_invocation_costs(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute("DELETE FROM cost_measurements WHERE cost_measurement_id='c1'")
    conn.execute(
        "INSERT INTO llm_invocations(invocation_id,turn_id,invocation_index,provider,"
        "model_selected,started_at_us,ended_at_us,wall_duration_ms,status) "
        "VALUES('i2','t1',1,'anthropic','sonnet',2910000,2950000,40,'completed')"
    )
    conn.execute(
        "INSERT INTO cost_measurements(cost_measurement_id,scope,session_id,turn_id,provider,"
        "model,amount_usd,measurement_source,is_estimated,observed_at_us) "
        "VALUES('c-turn-exact','turn','s1','t1','anthropic','sonnet',1.00,"
        "'provider_stream',0,3000000)"
    )
    conn.executemany(
        "INSERT INTO cost_measurements(cost_measurement_id,scope,session_id,turn_id,"
        "invocation_id,provider,model,amount_usd,measurement_source,is_estimated,"
        "observed_at_us) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("c-i1-est", "invocation", "s1", "t1", "i1", "anthropic", "sonnet",
             0.30, "price_snapshot", 1, 2800000),
            ("c-i2-est", "invocation", "s1", "t1", "i2", "anthropic", "sonnet",
             0.40, "price_snapshot", 1, 2900000),
        ],
    )
    conn.commit()
    conn.close()

    queries = MetricsQueries(metrics_root)
    summary = queries.summary({"window_seconds": 10, "end_at": 4})

    assert summary["totals"]["reported_cost_usd"] == pytest.approx(1.00)
    assert summary["totals"]["estimated_cost_usd"] is None


def test_exact_coverage_supersedes_estimate_for_same_invocation(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,invocation_id,provider,"
        "model,measurement_source,is_estimated,token_semantics_version,input_tokens,"
        "output_tokens,observed_at_us) VALUES"
        "('u-est','invocation','t1','i1','anthropic','other-model','calibrated_estimator',1,1,90,20,2500000)"
    )
    conn.commit()
    conn.close()

    result = MetricsQueries(metrics_root).turn({"turn_id": "t1"})
    assert result["coverage"]["exact_invocations"] == 1
    assert result["coverage"]["estimated_invocations"] == 0
    assert result["coverage"]["unknown_invocations"] == 0

    summary = MetricsQueries(metrics_root).summary({"window_seconds": 10, "end_at": 4})
    assert summary["totals"]["input_tokens"] == 100


def test_session_cursor_is_filter_bound(metrics_root):
    first = MetricsQueries(metrics_root).session({"session_id": "s1", "limit": 1})
    assert first["turns"][0]["turn_id"] == "t1"
    with pytest.raises(MetricsQueryError, match="cursor") as error:
        MetricsQueries(metrics_root).session({"session_id": "other", "limit": 1, "cursor": "bad"})
    assert error.value.code == "invalid_cursor"


def test_session_keyset_cursor_is_stable_when_an_earlier_turn_is_inserted(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,is_user_initiated,"
        "started_at_us,status) VALUES('t2','s1',1,'user',1,4000000,'completed')"
    )
    conn.commit()
    conn.close()

    queries = MetricsQueries(metrics_root)
    first = queries.session({"session_id": "s1", "limit": 1})
    assert [row["turn_id"] for row in first["turns"]] == ["t1"]

    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO turns(turn_id,session_id,turn_index,trigger_kind,is_user_initiated,"
        "started_at_us,status) VALUES('t0','s1',-1,'user',1,1000000,'completed')"
    )
    conn.commit()
    conn.close()

    second = queries.session({
        "session_id": "s1", "limit": 1, "cursor": first["next_cursor"],
    })
    assert [row["turn_id"] for row in second["turns"]] == ["t2"]


@pytest.mark.parametrize("cursor", [False, 0, [], {}])
def test_cursor_rejects_falsy_non_string_values(metrics_root, cursor):
    with pytest.raises(MetricsQueryError) as session_error:
        MetricsQueries(metrics_root).session({
            "session_id": "s1",
            "cursor": cursor,
        })
    assert session_error.value.code == "invalid_cursor"


def test_turn_rejects_more_than_200_combined_child_rows(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.executemany(
        "INSERT INTO workflow_steps(workflow_step_id,session_id,turn_id,workflow_name,"
        "step_name,step_index,step_type,started_at_us,status) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (f"w{index}", "s1", "t1", "wf", f"step-{index}", index, "task", 2000000 + index, "completed")
            for index in range(196)
        ],
    )
    conn.commit()
    conn.close()

    with pytest.raises(MetricsQueryError) as error:
        MetricsQueries(metrics_root).turn({"turn_id": "t1"})
    assert error.value.code == "query_too_large"


def test_serialized_response_is_capped_at_512_kib(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "UPDATE sessions SET session_name=? WHERE session_id='s1'",
        ("x" * (MAX_RESPONSE_BYTES + 1),),
    )
    conn.commit()
    conn.close()

    with pytest.raises(MetricsQueryError) as error:
        MetricsQueries(metrics_root).session({"session_id": "s1", "include_turns": False})
    assert error.value.code == "query_too_large"


@pytest.mark.parametrize(
    "args",
    [
        {"session_id": "s1", "limit": True},
        {"session_id": "s1", "include_turns": "false"},
    ],
)
def test_session_rejects_boolean_limit_and_non_boolean_include_turns(metrics_root, args):
    with pytest.raises(MetricsQueryError) as error:
        MetricsQueries(metrics_root).session(args)
    assert error.value.code == "bad_request"


def test_query_layer_rejects_boolean_window(metrics_root):
    with pytest.raises(MetricsQueryError) as error:
        MetricsQueries(metrics_root).summary({"window_seconds": True})
    assert error.value.code == "invalid_range"


def test_errors_are_machine_readable(metrics_root, tmp_path):
    with pytest.raises(MetricsQueryError) as missing:
        MetricsQueries(tmp_path).turn({"turn_id": "t1"})
    assert missing.value.code == "metrics_not_ready"
    with pytest.raises(MetricsQueryError) as unknown:
        MetricsQueries(metrics_root).turn({"turn_id": "missing"})
    assert unknown.value.code == "unknown_turn"
    with pytest.raises(MetricsQueryError) as too_large:
        MetricsQueries(metrics_root).session({"session_id": "s1", "limit": MAX_ROWS + 1})
    assert too_large.value.code == "query_too_large"


@pytest.mark.parametrize("invalid_id", [True, 1, [], {}])
def test_required_ids_reject_non_string_values(metrics_root, invalid_id):
    with pytest.raises(MetricsQueryError) as session_error:
        MetricsQueries(metrics_root).session({"session_id": invalid_id})
    assert session_error.value.code == "bad_request"

    with pytest.raises(MetricsQueryError) as turn_error:
        MetricsQueries(metrics_root).turn({"turn_id": invalid_id})
    assert turn_error.value.code == "bad_request"


    with pytest.raises(MetricsQueryError) as naive_time:
        MetricsQueries(metrics_root).summary({
            "from": "2026-09-01T00:00:00",
            "to": "2026-09-01T00:01:00Z",
        })
    assert naive_time.value.code == "invalid_range"


def test_sqlite_lock_is_retryable_metrics_busy(metrics_root, monkeypatch):
    def locked(_conn):
        raise __import__("sqlite3").OperationalError("database is locked")

    with pytest.raises(MetricsQueryError) as error:
        MetricsQueries(metrics_root)._run(locked)
    assert error.value.code == "metrics_busy"
    assert error.value.detail == {"retry_after_ms": 250}


def test_sqlite_progress_deadline_is_retryable_metrics_busy(metrics_root):
    queries = MetricsQueries(metrics_root, deadline_seconds=0)

    def expensive(conn):
        conn.execute(
            "WITH RECURSIVE count(value) AS ("
            "SELECT 1 UNION ALL SELECT value+1 FROM count WHERE value<1000000) "
            "SELECT SUM(value) FROM count"
        ).fetchone()
        return {"unexpected": True}

    with pytest.raises(MetricsQueryError) as error:
        queries._run(expensive)
    assert error.value.code == "metrics_busy"
    assert "deadline" in str(error.value)
    assert error.value.detail == {"retry_after_ms": 250}


def test_python_query_processing_respects_deadline(metrics_root, monkeypatch):
    now = [0.0]
    monkeypatch.setattr("bobi.metrics.query.time.monotonic", lambda: now[0])
    def processing(conn):
        now[0] = 2.0
        return {"late": True}
    with pytest.raises(MetricsQueryError) as error:
        MetricsQueries(metrics_root, deadline_seconds=1)._run(processing)
    assert error.value.code == "metrics_busy"
    assert error.value.detail == {"retry_after_ms": 250}


def test_metrics_status_reports_database_and_reconciliation(monkeypatch, tmp_path):
    db = tmp_path / "state" / "metrics" / "metrics.db"
    conn = connect(db)
    try:
        migrate(conn)
    finally:
        conn.close()
    monkeypatch.setattr("bobi.cli._detect_project_root", lambda: tmp_path)
    monkeypatch.setenv("BOBI_METRICS_MODE", "enabled")

    result = CliRunner().invoke(metrics, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "enabled"
    assert payload["database"]["ready"] is True
    assert payload["database"]["integrity"] == "ok"
    assert payload["reconciliation"]["uncovered_turns"] == 0
    assert {"maintenance", "retention", "backup"}.isdisjoint(payload)


def test_metrics_reconcile_invokes_one_turn(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("bobi.cli._detect_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "bobi.metrics.reconcile.reconcile_turn",
        lambda root, turn_id, wait, timeout: calls.append(
            (root, turn_id, wait, timeout)
        ) or {
            "status": "done",
            "exact_measurements_recovered": 1,
            "turn_id": turn_id,
        },
    )

    result = CliRunner().invoke(
        metrics,
        ["reconcile", "--turn-id", "turn-1", "--wait", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [(tmp_path, "turn-1", True, 60.0)]
    assert json.loads(result.output)["turn_id"] == "turn-1"


def test_metrics_calibrate_reports_qualified_groups(monkeypatch, tmp_path):
    monkeypatch.setattr("bobi.cli._detect_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "bobi.metrics.estimate.calibrate_estimators",
        lambda root: {
            "status": "done",
            "model_groups": 2,
            "qualified_model_groups": 1,
        },
    )

    result = CliRunner().invoke(metrics, ["calibrate", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["qualified_model_groups"] == 1


def test_metrics_commands_are_operational_only():
    assert set(metrics.commands) == {"status", "reconcile", "calibrate"}


class Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def test_client_maps_alias_and_polls_terminal_result(monkeypatch):
    calls = []
    monkeypatch.setattr("bobi.admin_client.http.post", lambda url, **kwargs: (
        calls.append(("POST", url, kwargs)) or Response(202, {"command_id": "c1", "status": "pending"})
    ))

    def get(url, **kwargs):
        calls.append(("GET", url, kwargs))
        if url.endswith("/fleet/instances/my%20fleet/agent%2Fone"):
            return Response(200, {"supervisor": {"version": "0.4.0"}})
        return Response(200, {
            "command_id": "c1", "status": "done", "result": {"usage_turn": {"turn": {"turn_id": "t1"}}},
        })

    monkeypatch.setattr("bobi.admin_client.http.get", get)

    result = run_admin_command(
        base_url="https://events.example.com/",
        token="secret",
        fleet="my fleet",
        instance="agent/one",
        alias="metrics_turn",
        args={"turn_id": "t1"},
        wait=True,
    )

    assert result["status"] == "done"
    assert calls[0][0] == "GET"
    assert calls[0][1].endswith("/fleet/instances/my%20fleet/agent%2Fone")
    assert calls[1][1].endswith("/fleet/instances/my%20fleet/agent%2Fone/commands")
    assert calls[1][2]["json"] == {"command": "usage_turn", "args": {"turn_id": "t1"}}
    assert calls[1][2]["headers"] == {"Authorization": "Bearer secret"}


@pytest.mark.parametrize("version", ["0.3.9", None, "development"])
def test_drilldown_rejects_unsupported_supervisor_without_post(monkeypatch, version):
    calls = []
    monkeypatch.setattr("bobi.admin_client.http.get", lambda url, **kwargs: (
        calls.append(("GET", url, kwargs)) or Response(200, {"supervisor": {"version": version}})
    ))
    monkeypatch.setattr("bobi.admin_client.http.post", lambda *args, **kwargs: (
        calls.append(("POST", args, kwargs)) or pytest.fail("unsupported drill-down must not be posted")
    ))

    with pytest.raises(AdminClientError, match=r"0\.4\.0 or newer is required"):
        run_admin_command(
            base_url="https://events.example.com",
            token="secret",
            fleet="f",
            instance="i",
            alias="metrics_turn",
            args={"turn_id": "t1"},
        )

    assert [call[0] for call in calls] == ["GET"]


def test_summary_remains_available_without_version_preflight(monkeypatch):
    calls = []
    monkeypatch.setattr("bobi.admin_client.http.get", lambda *args, **kwargs: (
        calls.append(("GET", args, kwargs)) or pytest.fail("summary must not preflight")
    ))
    monkeypatch.setattr("bobi.admin_client.http.post", lambda url, **kwargs: (
        calls.append(("POST", url, kwargs)) or Response(202, {"command_id": "c1", "status": "pending"})
    ))

    result = run_admin_command(
        base_url="https://events.example.com",
        token="secret",
        fleet="f",
        instance="i",
        alias="metrics_summary",
        args={},
    )

    assert result["command_id"] == "c1"
    assert [call[0] for call in calls] == ["POST"]


def test_client_wraps_transport_failure(monkeypatch):
    monkeypatch.setattr(
        "bobi.admin_client.http.post",
        lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")),
    )

    with pytest.raises(AdminClientError, match="admin command failed: offline"):
        run_admin_command(
            base_url="https://events.example.com",
            token="secret",
            fleet="f",
            instance="i",
            alias="metrics_summary",
            args={},
        )


def test_client_rejects_non_object_response(monkeypatch):
    monkeypatch.setattr(
        "bobi.admin_client.http.post",
        lambda *args, **kwargs: Response(202, []),
    )

    with pytest.raises(AdminClientError, match="non-object"):
        run_admin_command(
            base_url="https://events.example.com",
            token="secret",
            fleet="f",
            instance="i",
            alias="metrics_summary",
            args={},
        )


def test_wait_timeout_is_total_deadline_and_returns_pending(monkeypatch):
    clock = {"now": 10.0}
    request_timeouts = []
    monkeypatch.setattr("bobi.admin_client.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "bobi.admin_client.time.sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    monkeypatch.setattr(
        "bobi.admin_client.http.post",
        lambda *args, **kwargs: Response(202, {"command_id": "c1", "status": "pending"}),
    )

    def get(_url, **kwargs):
        request_timeouts.append(kwargs["timeout"])
        clock["now"] += 0.4
        return Response(200, {"command_id": "c1", "status": "pending"})

    monkeypatch.setattr("bobi.admin_client.http.get", get)

    result = run_admin_command(
        base_url="https://events.example.com",
        token="secret",
        fleet="f",
        instance="i",
        alias="metrics_summary",
        args={},
        wait=True,
        timeout=1.0,
        poll_interval=0.25,
    )

    assert result["status"] == "pending"
    assert clock["now"] <= 11.25
    assert request_timeouts
    assert all(0 < request_timeout <= 1.0 for request_timeout in request_timeouts)


def test_wait_timeout_includes_drilldown_preflight_and_submission(monkeypatch):
    clock = {"now": 10.0}
    request_timeouts = []
    monkeypatch.setattr("bobi.admin_client.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "bobi.admin_client.time.sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    def get(url, **kwargs):
        request_timeouts.append(("GET", kwargs["timeout"]))
        if url.endswith("/fleet/instances/f/i"):
            clock["now"] += 0.6
            return Response(200, {"supervisor": {"version": "0.4.0"}})
        clock["now"] += 0.1
        return Response(200, {"command_id": "c1", "status": "pending"})

    def post(_url, **kwargs):
        request_timeouts.append(("POST", kwargs["timeout"]))
        clock["now"] += 0.2
        return Response(202, {"command_id": "c1", "status": "pending"})

    monkeypatch.setattr("bobi.admin_client.http.get", get)
    monkeypatch.setattr("bobi.admin_client.http.post", post)

    result = run_admin_command(
        base_url="https://events.example.com",
        token="secret",
        fleet="f",
        instance="i",
        alias="metrics_turn",
        args={"turn_id": "t1"},
        wait=True,
        timeout=1.0,
        poll_interval=0.25,
    )

    assert result["status"] == "pending"
    assert clock["now"] == pytest.approx(11.0)
    assert request_timeouts[0] == ("GET", pytest.approx(1.0))
    assert request_timeouts[1] == ("POST", pytest.approx(0.4))
    assert request_timeouts[2] == ("GET", pytest.approx(0.2))


def test_timeout_exhausted_by_preflight_does_not_submit(monkeypatch):
    clock = {"now": 10.0}
    monkeypatch.setattr("bobi.admin_client.time.monotonic", lambda: clock["now"])

    def get(_url, **_kwargs):
        clock["now"] += 1.0
        return Response(200, {"supervisor": {"version": "0.4.0"}})

    monkeypatch.setattr("bobi.admin_client.http.get", get)
    monkeypatch.setattr(
        "bobi.admin_client.http.post",
        lambda *_args, **_kwargs: pytest.fail("expired command must not be submitted"),
    )

    with pytest.raises(AdminClientError, match="timed out before command submission"):
        run_admin_command(
            base_url="https://events.example.com",
            token="secret",
            fleet="f",
            instance="i",
            alias="metrics_turn",
            args={"turn_id": "t1"},
            timeout=1.0,
        )


def test_cli_rejects_non_object_args_without_network():
    result = CliRunner().invoke(main, [
        "admin", "metrics_turn", "--url", "https://events.example.com",
        "--fleet", "f", "--instance", "i", "--token", "secret",
        "--args", "[]", "--json",
    ])
    assert result.exit_code == 2
    assert "JSON object" in result.output


def test_cli_prints_wire_envelope(monkeypatch):
    monkeypatch.setattr("bobi.admin_client.run_admin_command", lambda **kwargs: {
        "command_id": "c1", "status": "done", "result": {"usage_turn": {}},
    })
    result = CliRunner().invoke(main, [
        "admin", "metrics_turn", "--url", "https://events.example.com",
        "--fleet", "f", "--instance", "i", "--token", "secret",
        "--args", '{"turn_id":"t1"}', "--wait", "--json",
    ])
    assert result.exit_code == 0, result.output
    assert '"status": "done"' in result.output
