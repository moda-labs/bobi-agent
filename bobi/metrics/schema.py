"""Executable SQLite v1 schema for the fine-grained metrics read model."""

SCHEMA_VERSION = 1

SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_events (
    event_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    producer_id TEXT NOT NULL,
    producer_sequence INTEGER NOT NULL,
    emitted_at_us INTEGER NOT NULL,
    received_at_us INTEGER NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    invocation_id TEXT,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    segment_path TEXT NOT NULL,
    segment_offset INTEGER NOT NULL,
    projection_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (projection_state IN ('pending', 'projected', 'quarantined')),
    projection_attempts INTEGER NOT NULL DEFAULT 0,
    projection_not_before_us INTEGER,
    projected_at_us INTEGER,
    projection_error TEXT,
    UNIQUE (producer_id, producer_sequence),
    UNIQUE (segment_path, segment_offset)
);

CREATE TABLE IF NOT EXISTS spool_cursors (
    segment_path TEXT PRIMARY KEY,
    producer_id TEXT NOT NULL,
    committed_offset INTEGER NOT NULL CHECK (committed_offset >= 0),
    last_event_id TEXT,
    updated_at_us INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    session_name TEXT NOT NULL,
    provider_session_id TEXT,
    brain TEXT NOT NULL,
    provider TEXT NOT NULL,
    role TEXT,
    run_key TEXT,
    project TEXT,
    workflow_name TEXT,
    started_at_us INTEGER NOT NULL,
    ended_at_us INTEGER,
    status TEXT NOT NULL,
    terminal_error_kind TEXT,
    parent_session_id TEXT REFERENCES sessions(session_id) DEFERRABLE INITIALLY DEFERRED,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) DEFERRABLE INITIALLY DEFERRED,
    turn_index INTEGER NOT NULL,
    trigger_kind TEXT NOT NULL,
    trigger_id TEXT,
    is_user_initiated INTEGER NOT NULL CHECK (is_user_initiated IN (0, 1)),
    prompt_template_id TEXT,
    prompt_template_version TEXT,
    prompt_sha256 TEXT,
    prompt_bytes INTEGER,
    started_at_us INTEGER NOT NULL,
    first_output_at_us INTEGER,
    ended_at_us INTEGER,
    wall_duration_ms REAL,
    provider_duration_ms REAL,
    provider_api_duration_ms REAL,
    status TEXT NOT NULL,
    error_kind TEXT,
    provider_turn_id TEXT,
    UNIQUE (session_id, turn_index)
);

CREATE TABLE IF NOT EXISTS workflow_steps (
    workflow_step_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) DEFERRABLE INITIALLY DEFERRED,
    turn_id TEXT REFERENCES turns(turn_id) DEFERRABLE INITIALLY DEFERRED,
    run_key TEXT,
    workflow_name TEXT NOT NULL,
    step_name TEXT NOT NULL,
    step_index INTEGER,
    attempt INTEGER NOT NULL DEFAULT 1,
    step_type TEXT NOT NULL,
    started_at_us INTEGER NOT NULL,
    ended_at_us INTEGER,
    status TEXT NOT NULL,
    error_kind TEXT
);

CREATE TABLE IF NOT EXISTS router_decisions (
    router_decision_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id) DEFERRABLE INITIALLY DEFERRED,
    experiment_id TEXT,
    variant_id TEXT,
    assignment_unit TEXT,
    assignment_status TEXT NOT NULL DEFAULT 'assigned'
        CHECK (assignment_status IN ('assigned', 'unassigned')),
    assignment_key_hash TEXT,
    assignment_algorithm TEXT,
    cohort TEXT,
    router_name TEXT NOT NULL,
    router_version TEXT NOT NULL,
    policy_version TEXT,
    feature_schema_version TEXT,
    candidate_models_json TEXT NOT NULL,
    model_selected TEXT NOT NULL,
    control_model TEXT,
    router_score REAL,
    router_reason TEXT,
    router_latency_ms REAL NOT NULL,
    fallback_reason TEXT,
    decided_at_us INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS llm_invocations (
    invocation_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id) DEFERRABLE INITIALLY DEFERRED,
    workflow_step_id TEXT REFERENCES workflow_steps(workflow_step_id) DEFERRABLE INITIALLY DEFERRED,
    router_decision_id TEXT REFERENCES router_decisions(router_decision_id) DEFERRABLE INITIALLY DEFERRED,
    parent_invocation_id TEXT REFERENCES llm_invocations(invocation_id) DEFERRABLE INITIALLY DEFERRED,
    invocation_index INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model_requested TEXT,
    model_selected TEXT NOT NULL,
    provider_event_id TEXT,
    provider_request_id TEXT,
    started_at_us INTEGER NOT NULL,
    first_token_at_us INTEGER,
    ended_at_us INTEGER,
    wall_duration_ms REAL,
    provider_latency_ms REAL,
    time_to_first_token_ms REAL,
    status TEXT NOT NULL,
    stop_reason TEXT,
    error_kind TEXT,
    UNIQUE (turn_id, invocation_index)
);

CREATE TABLE IF NOT EXISTS tool_executions (
    tool_execution_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id) DEFERRABLE INITIALLY DEFERRED,
    triggering_invocation_id TEXT NOT NULL REFERENCES llm_invocations(invocation_id) DEFERRABLE INITIALLY DEFERRED,
    consuming_invocation_id TEXT REFERENCES llm_invocations(invocation_id) DEFERRABLE INITIALLY DEFERRED,
    provider_tool_call_id TEXT,
    tool_name TEXT NOT NULL,
    tool_kind TEXT NOT NULL,
    input_sha256 TEXT,
    input_bytes INTEGER,
    output_sha256 TEXT,
    output_bytes INTEGER,
    output_estimated_tokens INTEGER,
    started_at_us INTEGER NOT NULL,
    ended_at_us INTEGER,
    status TEXT NOT NULL,
    is_error INTEGER NOT NULL DEFAULT 0 CHECK (is_error IN (0, 1)),
    attribution_method TEXT NOT NULL DEFAULT 'unknown',
    attribution_confidence REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS usage_measurements (
    measurement_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('turn', 'invocation')),
    turn_id TEXT NOT NULL REFERENCES turns(turn_id) DEFERRABLE INITIALLY DEFERRED,
    invocation_id TEXT REFERENCES llm_invocations(invocation_id) DEFERRABLE INITIALLY DEFERRED,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    provider_event_id TEXT,
    measurement_source TEXT NOT NULL,
    is_estimated INTEGER NOT NULL CHECK (is_estimated IN (0, 1)),
    estimator_name TEXT,
    estimator_version TEXT,
    token_semantics_version INTEGER NOT NULL,
    input_tokens INTEGER,
    uncached_input_tokens INTEGER,
    cache_read_input_tokens INTEGER,
    cache_write_input_tokens INTEGER,
    cache_write_5m_input_tokens INTEGER,
    cache_write_1h_input_tokens INTEGER,
    cache_write_unknown_ttl_input_tokens INTEGER,
    cache_write_breakdown_complete INTEGER NOT NULL DEFAULT 0
        CHECK (cache_write_breakdown_complete IN (0, 1)),
    output_tokens INTEGER,
    reasoning_output_tokens INTEGER,
    observed_at_us INTEGER NOT NULL,
    raw_usage_json TEXT,
    supersedes_measurement_id TEXT REFERENCES usage_measurements(measurement_id) DEFERRABLE INITIALLY DEFERRED,
    CHECK (input_tokens IS NULL OR input_tokens >= 0),
    CHECK (uncached_input_tokens IS NULL OR uncached_input_tokens >= 0),
    CHECK (cache_read_input_tokens IS NULL OR cache_read_input_tokens >= 0),
    CHECK (cache_write_input_tokens IS NULL OR cache_write_input_tokens >= 0),
    CHECK (cache_write_5m_input_tokens IS NULL OR cache_write_5m_input_tokens >= 0),
    CHECK (cache_write_1h_input_tokens IS NULL OR cache_write_1h_input_tokens >= 0),
    CHECK (cache_write_unknown_ttl_input_tokens IS NULL OR cache_write_unknown_ttl_input_tokens >= 0),
    CHECK (output_tokens IS NULL OR output_tokens >= 0),
    CHECK (reasoning_output_tokens IS NULL OR reasoning_output_tokens >= 0),
    CHECK (
        cache_write_breakdown_complete = 0
        OR cache_write_input_tokens =
            COALESCE(cache_write_5m_input_tokens, 0)
            + COALESCE(cache_write_1h_input_tokens, 0)
            + COALESCE(cache_write_unknown_ttl_input_tokens, 0)
    ),
    CHECK (
        (scope = 'turn' AND invocation_id IS NULL)
        OR (scope = 'invocation' AND invocation_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    price_snapshot_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    effective_at_us INTEGER NOT NULL,
    input_per_million REAL,
    cache_read_per_million REAL,
    cache_write_per_million REAL,
    cache_write_5m_per_million REAL,
    cache_write_1h_per_million REAL,
    output_per_million REAL,
    source TEXT NOT NULL,
    source_version TEXT,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_measurements (
    cost_measurement_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('session', 'turn', 'invocation')),
    session_id TEXT REFERENCES sessions(session_id) DEFERRABLE INITIALLY DEFERRED,
    turn_id TEXT REFERENCES turns(turn_id) DEFERRABLE INITIALLY DEFERRED,
    invocation_id TEXT REFERENCES llm_invocations(invocation_id) DEFERRABLE INITIALLY DEFERRED,
    provider TEXT NOT NULL,
    model TEXT,
    amount_usd REAL NOT NULL CHECK (amount_usd >= 0),
    measurement_source TEXT NOT NULL,
    is_estimated INTEGER NOT NULL CHECK (is_estimated IN (0, 1)),
    price_snapshot_id TEXT REFERENCES price_snapshots(price_snapshot_id) DEFERRABLE INITIALLY DEFERRED,
    provider_event_id TEXT,
    observed_at_us INTEGER NOT NULL,
    raw_cost_json TEXT,
    CHECK (
        (scope = 'session' AND session_id IS NOT NULL
            AND turn_id IS NULL AND invocation_id IS NULL)
        OR (scope = 'turn' AND session_id IS NOT NULL
            AND turn_id IS NOT NULL AND invocation_id IS NULL)
        OR (scope = 'invocation' AND session_id IS NOT NULL
            AND turn_id IS NOT NULL AND invocation_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS experiment_outcomes (
    outcome_id TEXT PRIMARY KEY,
    router_decision_id TEXT NOT NULL REFERENCES router_decisions(router_decision_id) DEFERRABLE INITIALLY DEFERRED,
    outcome_name TEXT NOT NULL,
    outcome_value REAL,
    outcome_text TEXT,
    outcome_definition_version TEXT NOT NULL,
    outcome_source TEXT NOT NULL CHECK (outcome_source IN ('runtime', 'evaluator', 'human')),
    evaluator_name TEXT NOT NULL,
    evaluator_version TEXT NOT NULL,
    is_estimated INTEGER NOT NULL DEFAULT 0 CHECK (is_estimated IN (0, 1)),
    observed_at_us INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sessions_provider_session ON sessions(provider, provider_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at_us);
CREATE INDEX IF NOT EXISTS idx_turns_session_started ON turns(session_id, started_at_us);
CREATE INDEX IF NOT EXISTS idx_turns_trigger ON turns(trigger_kind, trigger_id);
CREATE INDEX IF NOT EXISTS idx_steps_run ON workflow_steps(run_key, step_index, attempt);
CREATE INDEX IF NOT EXISTS idx_invocations_turn ON llm_invocations(turn_id, invocation_index);
CREATE INDEX IF NOT EXISTS idx_invocations_model_time ON llm_invocations(provider, model_selected, started_at_us);
CREATE INDEX IF NOT EXISTS idx_tools_turn ON tool_executions(turn_id, started_at_us);
CREATE INDEX IF NOT EXISTS idx_tools_name_time ON tool_executions(tool_name, started_at_us);
CREATE INDEX IF NOT EXISTS idx_usage_turn ON usage_measurements(turn_id, scope, is_estimated, observed_at_us);
CREATE INDEX IF NOT EXISTS idx_usage_invocation ON usage_measurements(invocation_id, is_estimated, observed_at_us);
CREATE INDEX IF NOT EXISTS idx_usage_supersedes ON usage_measurements(supersedes_measurement_id);
CREATE INDEX IF NOT EXISTS idx_cost_turn ON cost_measurements(turn_id, scope, is_estimated);
CREATE INDEX IF NOT EXISTS idx_cost_invocation ON cost_measurements(invocation_id, is_estimated);
CREATE INDEX IF NOT EXISTS idx_router_experiment ON router_decisions(experiment_id, variant_id, decided_at_us);
CREATE INDEX IF NOT EXISTS idx_raw_projection ON raw_events(projection_state, projection_not_before_us, received_at_us);

CREATE VIEW IF NOT EXISTS best_usage AS
WITH candidates AS (
    SELECT
        u.*,
        (u.input_tokens IS NOT NULL)
        + (u.uncached_input_tokens IS NOT NULL)
        + (u.cache_read_input_tokens IS NOT NULL)
        + (u.cache_write_input_tokens IS NOT NULL)
        + (u.cache_write_5m_input_tokens IS NOT NULL)
        + (u.cache_write_1h_input_tokens IS NOT NULL)
        + (u.cache_write_unknown_ttl_input_tokens IS NOT NULL)
        + (u.output_tokens IS NOT NULL)
        + (u.reasoning_output_tokens IS NOT NULL) AS completeness,
        CASE u.measurement_source
            WHEN 'provider_stream' THEN 50
            WHEN 'provider_reconciled' THEN 45
            WHEN 'claude_transcript' THEN 40
            WHEN 'codex_rollout' THEN 40
            WHEN 'local_tokenizer' THEN 20
            WHEN 'calibrated_estimator' THEN 10
            ELSE 0
        END AS source_priority
    FROM usage_measurements AS u
    WHERE NOT EXISTS (
        SELECT 1
        FROM usage_measurements AS newer
        WHERE newer.supersedes_measurement_id = u.measurement_id
    )
), ranked AS (
    SELECT
        candidates.*,
        ROW_NUMBER() OVER (
            PARTITION BY
                scope,
                turn_id,
                COALESCE(invocation_id, ''),
                provider,
                model
            ORDER BY
                is_estimated ASC,
                completeness DESC,
                source_priority DESC,
                token_semantics_version DESC,
                observed_at_us DESC,
                measurement_id DESC
        ) AS usage_rank
    FROM candidates
)
SELECT
    measurement_id,
    scope,
    turn_id,
    invocation_id,
    provider,
    model,
    provider_event_id,
    measurement_source,
    is_estimated,
    estimator_name,
    estimator_version,
    token_semantics_version,
    input_tokens,
    uncached_input_tokens,
    cache_read_input_tokens,
    cache_write_input_tokens,
    cache_write_5m_input_tokens,
    cache_write_1h_input_tokens,
    cache_write_unknown_ttl_input_tokens,
    cache_write_breakdown_complete,
    output_tokens,
    reasoning_output_tokens,
    observed_at_us,
    raw_usage_json,
    supersedes_measurement_id
FROM ranked
WHERE usage_rank = 1;
"""
