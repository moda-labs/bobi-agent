import json

import pytest

from bobi.metrics.query import (
    MAX_RESPONSE_BYTES,
    MAX_ROWS,
    MetricsQueries,
    MetricsQueryError,
)
from bobi.metrics.store import connect, migrate


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
    experiment = queries.experiment({"experiment_id": "exp1"})
    hotspots = queries.hotspots({
        "window_seconds": 10,
        "end_at": 4,
        "scope": "turn",
        "metric": "output_tokens",
    })

    assert summary["totals"]["input_tokens"] == 100
    assert summary["totals"]["output_tokens"] == 30
    assert summary["coverage"]["usage_granularity"] == "turn"
    assert experiment["variants"][0]["tokens"]["output_tokens"] == 30
    assert experiment["variants"][0]["coverage"]["usage_granularity"] == "turn"
    assert hotspots["hotspots"][0]["value"] == 30


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
    experiment = queries.experiment({"experiment_id": "exp1"})
    turn_hotspots = queries.hotspots({
        "window_seconds": 10,
        "end_at": 4,
        "scope": "turn",
        "metric": "input_tokens",
    })
    invocation_hotspots = queries.hotspots({
        "window_seconds": 10,
        "end_at": 4,
        "scope": "invocation",
        "metric": "input_tokens",
    })

    assert summary["totals"]["input_tokens"] == 1000
    assert summary["totals"]["output_tokens"] == 100
    assert summary["totals"]["invocations"] == 2
    assert summary["coverage"]["usage_granularity"] == "turn"
    assert summary["coverage"]["invocation_granularity_turns"] == 0
    assert summary["coverage"]["turn_granularity_turns"] == 1
    assert experiment["variants"][0]["tokens"]["input_tokens"] == 1000
    assert experiment["variants"][0]["coverage"]["usage_granularity"] == "turn"
    assert experiment["variants"][0]["coverage"]["exact_measurement_turns"] == 1
    assert experiment["diagnostics"]["coverage_rates"]["control"]["exact_rate"] == 1
    assert turn_hotspots["hotspots"][0]["value"] == 1000
    assert turn_hotspots["hotspots"][0]["is_estimated"] == 0
    assert [row["value"] for row in invocation_hotspots["hotspots"]] == [400, 300]
    assert all(row["is_estimated"] == 1 for row in invocation_hotspots["hotspots"])


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
    estimated = queries.hotspots({
        "window_seconds": 10,
        "end_at": 4,
        "scope": "invocation",
        "metric": "estimated_cost",
    })

    assert result["totals"]["reported_cost_usd"] == pytest.approx(0.25)
    assert result["totals"]["estimated_cost_usd"] is None
    assert estimated["hotspots"] == []


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
    experiment = queries.experiment({"experiment_id": "exp1"})
    turn_hotspots = queries.hotspots({
        "window_seconds": 10,
        "end_at": 4,
        "scope": "turn",
        "metric": "reported_cost",
    })
    estimated_turn_hotspots = queries.hotspots({
        "window_seconds": 10,
        "end_at": 4,
        "scope": "turn",
        "metric": "estimated_cost",
    })

    assert summary["totals"]["reported_cost_usd"] == pytest.approx(1.00)
    assert summary["totals"]["estimated_cost_usd"] is None
    assert experiment["variants"][0]["reported_cost_usd"] == pytest.approx(1.00)
    assert experiment["variants"][0]["estimated_cost_usd"] is None
    assert turn_hotspots["hotspots"][0]["value"] == pytest.approx(1.00)
    assert turn_hotspots["hotspots"][0]["is_estimated"] == 0
    assert estimated_turn_hotspots["hotspots"] == []


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
    hotspots = MetricsQueries(metrics_root).hotspots({
        "window_seconds": 10,
        "end_at": 4,
        "scope": "invocation",
        "metric": "input_tokens",
    })
    assert [row["id"] for row in hotspots["hotspots"]] == ["i1"]
    assert hotspots["hotspots"][0]["value"] == 100
    assert hotspots["hotspots"][0]["is_estimated"] == 0


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


def test_hotspots_and_experiment(metrics_root):
    queries = MetricsQueries(metrics_root)
    hotspots = queries.hotspots({
        "window_seconds": 10,
        "end_at": 4,
        "scope": "tool",
        "metric": "tool_result_bytes",
        "top_n": 10,
    })
    assert hotspots["hotspots"][0] == pytest.approx({
        "rank": 1,
        "scope": "tool",
        "id": "x1",
        "label": "view_file",
        "value": 50,
        "attribution_method": "local_tokenizer",
        "attribution_confidence": None,
    })
    experiment = queries.experiment({"experiment_id": "exp1"})
    variant = experiment["variants"][0]
    assert variant["sample_size"] == 1
    assert variant["completion_rate"] == 1
    assert variant["error_rate"] == 0
    assert variant["fallback_rate"] == 0
    assert variant["turn_latency_ms"] == 1000
    assert variant["tokens"]["input_tokens"] == 100
    assert variant["tokens"]["cache_write_5m_input_tokens"] == 10
    assert variant["tokens"]["cache_write_1h_input_tokens"] is None
    assert variant["reported_cost_usd"] == 0.25
    assert variant["estimated_cost_usd"] is None
    assert variant["coverage"] == {
        "exact_invocations": 1,
        "estimated_invocations": 0,
        "unknown_invocations": 0,
        "exact_measurement_turns": 1,
        "estimated_measurement_turns": 0,
        "unknown_measurement_turns": 0,
        "usage_granularity": "invocation",
        "invocation_granularity_turns": 1,
        "turn_granularity_turns": 0,
    }


def test_experiment_applies_filters_and_keeps_versions_separate(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute("UPDATE router_decisions SET cohort='alpha' WHERE router_decision_id='r1'")
    conn.executemany(
        "INSERT INTO experiment_outcomes(outcome_id,router_decision_id,outcome_name,"
        "outcome_value,outcome_text,outcome_definition_version,outcome_source,evaluator_name,"
        "evaluator_version,is_estimated,observed_at_us,metadata_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("o1", "r1", "quality", 0.8, "private", "v1", "evaluator", "judge", "1", 0, 3000000, '{"secret":1}'),
            ("o2", "r1", "quality", 0.6, "private", "v2", "evaluator", "judge", "2", 0, 3000000, '{"secret":2}'),
        ],
    )
    conn.commit()
    conn.close()

    result = MetricsQueries(metrics_root).experiment({
        "experiment_id": "exp1",
        "from": "1970-01-01T00:00:02Z",
        "to": "1970-01-01T00:00:03Z",
        "cohort": "alpha",
        "outcome": "quality",
        "outcome_definition_version": "v1",
        "evaluator_name": "judge",
        "evaluator_version": "1",
    })

    assert result["variants"][0]["sample_size"] == 1
    assert result["outcomes"] == [pytest.approx({
        "variant_id": "control",
        "outcome_name": "quality",
        "outcome_definition_version": "v1",
        "outcome_source": "evaluator",
        "evaluator_name": "judge",
        "evaluator_version": "1",
        "is_estimated": 0,
        "sample_size": 1,
        "mean_value": 0.8,
    })]
    assert "private" not in json.dumps(result)
    assert "secret" not in json.dumps(result)

    no_match = MetricsQueries(metrics_root).experiment({
        "experiment_id": "exp1",
        "cohort": "other",
    })
    assert no_match["variants"] == []
    assert no_match["outcomes"] == []


@pytest.mark.parametrize(
    ("scope", "metric", "expected_id", "expected_label", "expected_value"),
    [
        ("session", "input_tokens", "s1", "manager", 100),
        ("turn", "output_tokens", "t1", "t1", 30),
        ("invocation", "input_tokens", "i1", "sonnet", 100),
        ("session", "reported_cost", "s1", "manager", 0.25),
        ("turn", "reported_cost", "t1", "t1", 0.25),
        ("invocation", "reported_cost", "i1", "sonnet", 0.25),
        ("turn", "latency", "t1", "t1", 1000),
        ("invocation", "latency", "i1", "sonnet", 800),
        ("tool", "latency", "x1", "view_file", 100),
    ],
)
def test_hotspot_scope_metric_matrix(
    metrics_root, scope, metric, expected_id, expected_label, expected_value,
):
    result = MetricsQueries(metrics_root).hotspots({
        "window_seconds": 10,
        "end_at": 4,
        "scope": scope,
        "metric": metric,
    })

    assert result["hotspots"][0]["id"] == expected_id
    assert result["hotspots"][0]["label"] == expected_label
    assert result["hotspots"][0]["value"] == pytest.approx(expected_value)


def test_invocation_hotspot_combines_exact_multi_model_usage(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO usage_measurements(measurement_id,scope,turn_id,invocation_id,provider,"
        "model,measurement_source,is_estimated,token_semantics_version,input_tokens,"
        "output_tokens,observed_at_us) VALUES"
        "('u2','invocation','t1','i1','anthropic','haiku','provider_stream',0,1,40,10,3000000)"
    )
    conn.commit()
    conn.close()

    result = MetricsQueries(metrics_root).hotspots({
        "window_seconds": 10,
        "end_at": 4,
        "scope": "invocation",
        "metric": "input_tokens",
    })

    assert len(result["hotspots"]) == 1
    assert result["hotspots"][0]["id"] == "i1"
    assert result["hotspots"][0]["label"] == "mixed"
    assert result["hotspots"][0]["value"] == 140

    summary = MetricsQueries(metrics_root).summary({
        "window_seconds": 10,
        "end_at": 4,
    })
    assert summary["totals"]["invocations"] == 1


def test_invocation_cost_hotspot_combines_multi_model_partitions(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO cost_measurements(cost_measurement_id,scope,session_id,turn_id,invocation_id,"
        "provider,model,amount_usd,measurement_source,is_estimated,observed_at_us) "
        "VALUES('c2','invocation','s1','t1','i1','anthropic','haiku',0.10,"
        "'provider_stream',0,3000000)"
    )
    conn.commit()
    conn.close()

    result = MetricsQueries(metrics_root).hotspots({
        "window_seconds": 10,
        "end_at": 4,
        "scope": "invocation",
        "metric": "reported_cost",
    })

    assert len(result["hotspots"]) == 1
    assert result["hotspots"][0]["id"] == "i1"
    assert result["hotspots"][0]["label"] == "mixed"
    assert result["hotspots"][0]["value"] == pytest.approx(0.35)


def test_prompt_template_hotspots_aggregate_and_keep_unknown_out(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute("UPDATE turns SET prompt_template_id='review',prompt_template_version='1' WHERE turn_id='t1'")
    conn.commit()
    conn.close()

    queries = MetricsQueries(metrics_root)
    tokens = queries.hotspots({
        "window_seconds": 10, "end_at": 4, "scope": "prompt_template",
        "metric": "input_tokens",
    })
    latency = queries.hotspots({
        "window_seconds": 10, "end_at": 4, "scope": "prompt_template",
        "metric": "latency",
    })

    assert tokens["hotspots"][0]["id"] == "review"
    assert tokens["hotspots"][0]["label"] == "review"
    assert tokens["hotspots"][0]["value"] == 100
    assert latency["hotspots"][0]["id"] == "review"
    assert latency["hotspots"][0]["value"] == 1000


@pytest.mark.parametrize(
    ("scope", "metric"),
    [("tool", "input_tokens"), ("turn", "tool_result_bytes")],
)
def test_hotspots_reject_unsupported_scope_metric_pairs(metrics_root, scope, metric):
    with pytest.raises(MetricsQueryError) as error:
        MetricsQueries(metrics_root).hotspots({
            "window_seconds": 10,
            "end_at": 4,
            "scope": scope,
            "metric": metric,
        })
    assert error.value.code == "bad_request"


def test_hotspot_cursor_is_bound_to_filters(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO tool_executions(tool_execution_id,turn_id,triggering_invocation_id,tool_name,"
        "tool_kind,output_bytes,started_at_us,ended_at_us,status,attribution_method) "
        "VALUES('x2','t1','i1','edit_file','file_edit',40,2400000,2500000,'completed','local_tokenizer')"
    )
    conn.commit()
    conn.close()
    queries = MetricsQueries(metrics_root)
    first = queries.hotspots({
        "window_seconds": 10, "end_at": 4, "scope": "tool",
        "metric": "tool_result_bytes", "top_n": 1, "session": "s1",
    })
    assert first["next_cursor"]
    second = queries.hotspots({
        "window_seconds": 10, "end_at": 4, "scope": "tool",
        "metric": "tool_result_bytes", "top_n": 1, "session": "s1",
        "cursor": first["next_cursor"],
    })
    assert second["hotspots"][0]["rank"] == 2
    with pytest.raises(MetricsQueryError) as mismatch:
        queries.hotspots({
            "window_seconds": 10, "end_at": 4, "scope": "tool",
            "metric": "latency", "top_n": 1, "session": "s1",
            "cursor": first["next_cursor"],
        })
    assert mismatch.value.code == "invalid_cursor"


@pytest.mark.parametrize("cursor", [False, 0, [], {}])
def test_cursor_rejects_falsy_non_string_values(metrics_root, cursor):
    with pytest.raises(MetricsQueryError) as session_error:
        MetricsQueries(metrics_root).session({
            "session_id": "s1",
            "cursor": cursor,
        })
    assert session_error.value.code == "invalid_cursor"

    with pytest.raises(MetricsQueryError) as hotspot_error:
        MetricsQueries(metrics_root).hotspots({
            "window_seconds": 10,
            "end_at": 4,
            "scope": "tool",
            "metric": "latency",
            "cursor": cursor,
        })
    assert hotspot_error.value.code == "invalid_cursor"


def test_hotspot_keyset_cursor_is_stable_when_a_higher_rank_is_inserted(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO tool_executions(tool_execution_id,turn_id,triggering_invocation_id,tool_name,"
        "tool_kind,output_bytes,started_at_us,ended_at_us,status,attribution_method) "
        "VALUES('x2','t1','i1','edit_file','file_edit',40,2400000,2500000,'completed','local_tokenizer')"
    )
    conn.commit()
    conn.close()

    queries = MetricsQueries(metrics_root)
    first = queries.hotspots({
        "window_seconds": 10, "end_at": 4, "scope": "tool",
        "metric": "tool_result_bytes", "top_n": 1,
    })
    assert [row["id"] for row in first["hotspots"]] == ["x1"]

    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.execute(
        "INSERT INTO tool_executions(tool_execution_id,turn_id,triggering_invocation_id,tool_name,"
        "tool_kind,output_bytes,started_at_us,ended_at_us,status,attribution_method) "
        "VALUES('x0','t1','i1','read_large','file_read',60,2600000,2700000,'completed','local_tokenizer')"
    )
    conn.commit()
    conn.close()

    second = queries.hotspots({
        "window_seconds": 10, "end_at": 4, "scope": "tool",
        "metric": "tool_result_bytes", "top_n": 1,
        "cursor": first["next_cursor"],
    })
    assert [row["id"] for row in second["hotspots"]] == ["x2"]
    assert second["hotspots"][0]["rank"] == 2


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


def test_experiment_rejects_more_than_200_combined_rows(metrics_root):
    conn = connect(metrics_root / "state" / "metrics" / "metrics.db")
    conn.executemany(
        "INSERT INTO experiment_outcomes(outcome_id,router_decision_id,outcome_name,"
        "outcome_value,outcome_definition_version,outcome_source,evaluator_name,"
        "evaluator_version,is_estimated,observed_at_us) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            (f"o{index}", "r1", f"quality-{index}", 1, "v1", "runtime", "runtime", "1", 0, 3000000)
            for index in range(MAX_ROWS)
        ],
    )
    conn.commit()
    conn.close()

    with pytest.raises(MetricsQueryError) as error:
        MetricsQueries(metrics_root).experiment({"experiment_id": "exp1"})
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

    with pytest.raises(MetricsQueryError) as experiment_error:
        MetricsQueries(metrics_root).experiment({"experiment_id": invalid_id})
    assert experiment_error.value.code == "bad_request"

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
