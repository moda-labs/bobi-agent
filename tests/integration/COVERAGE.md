# Integration Test Coverage Map

Subsystem → integration test mapping. Every major subsystem must have ≥1
integration test that runs in CI. Tests marked `claude` require the
`claude` CLI and run in `integration-claude`; all others run in
`integration-fast` (auto-discovered via `pytest -m "not claude"`).

| Subsystem | Integration Test File(s) | Tests | Marker | Coverage |
|-----------|--------------------------|-------|--------|----------|
| **cli** | `test_cli_commands.py` | 25 | — | Full: every CLI command exercised against isolated install |
| **config** | `test_agent_yaml_config.py`, `test_config_resolution.py` | 7+8 | — | Full: YAML loading, env var interpolation, dotenv chain, deployment state, channels |
| **session** | `test_context_rotation.py`, `test_session_lifecycle.py` | 11+7 | — | Full: rotation fields, lifecycle (start → idle → message → stop), registry tracking |
| **subagent** | `test_agent_launch.py`, `test_subagent_executor.py` | 5+8 | claude (launch only) | Full: launch, build_prompt, session naming, lifecycle events, requires gating |
| **sdk** | `test_manager_sdk.py` | 4 | claude | Good: connect, multi-turn, resume, inject |
| **registry** | `test_registry.py` | 11 | — | Full: fetch, update, browse, multi-registry, cache |
| **inbox** | `test_inbox_transport.py` | 4 | — | Good: round-trip, blocking, teardown, concurrent |
| **events** | `test_event_server.py`, `test_event_isolation.py`, `test_e2e_event_flow.py` | 30+ | — / claude | Full: lifecycle, webhooks, WS drain, bubble isolation, scheduler |
| **workflow** | `test_agent_launch.py`, `test_workflow_orchestrator.py` | 5+8 | claude (launch) | Full: schema loading, state persistence, routing, await/resume, variable resolution |
| **kb** | `test_kb.py` | 16 | — | Full: create, add, search, FTS, hybrid, sidecar |
| **monitors** | `test_event_server.py` (scheduler), `test_monitor_scheduler.py` | 1+9 | — | Full: registry loading, scheduler lifecycle, command/check/notify, dedup, state persistence |
| **setup** | `test_setup_flow.py` | 1 | claude | Good: full create flow |

## Maintenance

When adding a new subsystem or major feature, add a row to this table
and at least one integration test. CI auto-discovers new test files
(no allowlist to update).

## Gaps filled by this audit (#282)

- **config**: Added `test_config_resolution.py` — dotenv → agent.yaml resolution chain, deployment state round-trip, channel parsing, credential lookup
- **session**: Added `test_session_lifecycle.py` — session start/stop lifecycle, registry integration, inbox wiring, state transitions
- **subagent**: Added `test_subagent_executor.py` — prompt building, session naming, lifecycle event emission, requires gating
- **workflow**: Added `test_workflow_orchestrator.py` — schema parsing, state machine, variable resolution, routing, await/resume
- **monitors**: Added `test_monitor_scheduler.py` — scheduler tick, command/check/notify flavors, dedup, state persistence
