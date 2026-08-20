# Integration Test Coverage Map

Subsystem → integration test mapping. Every major subsystem must have ≥1
integration test that runs in CI. Two gating mechanisms, easy to conflate:

- **Marker `claude`**: the whole file needs the `claude` CLI. CI's
  `integration-fast` job deselects these (`-m "not claude and not docker"`);
  they run locally only — there is no CI job with a claude CLI (see the
  note at the bottom of `ci.yml`).
- **dual-brain**: the file binds `bobi_env` to `dual_brain_env` and runs
  every test twice — a `[stub]` leg that always runs (including in
  `integration-fast`) and a `[claude]` leg gated by a *skipif* on the CLI
  being installed, not by the marker. `-m "not claude"` does NOT deselect
  the `[claude]` legs; they skip in CI because the CLI is absent.

| Subsystem | Integration Test File(s) | Tests | Marker | Coverage |
|-----------|--------------------------|-------|--------|----------|
| **cli** | `test_cli_commands.py` | 25 | — | Full: every CLI command exercised against isolated install |
| **config** | `test_agent_yaml_config.py`, `test_config_resolution.py` | 7+8 | — | Full: YAML loading, env var interpolation, dotenv chain, deployment state, channels |
| **session** | `test_session_lifecycle.py` | 7 | — | Full: lifecycle (start → idle → message → stop), registry tracking; rotation is covered at unit level (`tests/test_context_rotation.py`) |
| **subagent** | `test_agent_launch.py`, `test_subagent_executor.py` | 5+8 | dual-brain (launch only) | Full: launch, build_prompt, session naming, lifecycle events, requires gating |
| **sdk** | `test_manager_sdk.py` | 4 | claude | Good: connect, multi-turn, resume, inject |
| **registry** | `test_registry.py` | 11 | — | Full: fetch, update, browse, multi-registry, cache |
| **inbox** | `test_inbox_transport.py` | 4 | — | Good: round-trip, blocking, teardown, concurrent |
| **events** | `test_event_server.py`, `test_event_isolation.py`, `test_e2e_event_flow.py` | 30+ | — / dual-brain (e2e flow) | Full: lifecycle, webhooks, WS drain, bubble isolation, scheduler |
| **workflow** | `test_agent_launch.py`, `test_workflow_orchestrator.py` | 5+8 | dual-brain (launch) | Full: schema loading, state persistence, routing, await/resume, variable resolution |
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
