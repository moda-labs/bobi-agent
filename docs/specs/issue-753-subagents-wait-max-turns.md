# Issue 753: make `subagents launch --wait` block on the launched agent

## Problem

`bobi agent <agent> subagents launch --wait` is documented as "Block until the
agent completes", but the current implementation routes `--wait` through the
monitoring-check harness. That harness wraps the supplied task in check-verdict
instructions, runs as role `monitor`, and caps execution at `CHECK_MAX_TURNS`
(currently 8). Real delegated work can therefore be killed at turn 9 and reported
to callers as a failed check instead of as the launched agent's real result.

The current escape hatch, `--agent-wait`, has the advertised blocking-agent
semantics but is hidden from help output. Users following `--help` discover
`--wait`, not `--agent-wait`, and get the check harness by default.

A related observability defect makes this hard to diagnose: when the underlying
brain reports a max-turn cap, the failure often surfaces as `unknown error`.
Operators should see an explicit error such as `max_turns_reached (max=8,
turns=9)` so they can distinguish "the agent hit its configured turn budget"
from an API failure or malformed output.

## Root Cause

- Before this change, `bobi/cli.py` declared `--wait` with the blocking-agent
  help text, but `_dispatch_agent()` called `_run_check()` for public `--wait`.
- `_run_check()` uses `run_check_blocking()`, which intentionally applies
  `CHECK_MAX_TURNS` and check-verdict parsing. That is appropriate for monitor
  checks, not for user-visible subagent delegation.
- Existing docs and prompts show `subagents launch --wait --task "Investigate X"`
  as a real-work delegation pattern, so the public contract and implementation
  are inverted.
- `bobi/subagent.py` sets `AgentResult.error` from `TurnResult.result_text` and
  falls back to `unknown error`; provider-specific terminal metadata such as
  max-turn attachments is not normalized into actionable text.

## Solution

Change the CLI contract so names match behavior:

- `--wait` blocks on the requested agent.
- A new explicit `--as-check` flag runs the short-lived monitoring-check harness.
- The hidden `--agent-wait` escape hatch is removed in the same cutover. The only
  known callers are Bobi-Agent processes, and stale callers will fail with normal
  CLI usage output instead of preserving a second spelling.
- `--post-event` is only valid with `--as-check`, because completion events are a
  monitor-check concern rather than a general synchronous-agent concern. A
  repo-wide search shows the only current `subagents launch --post-event`
  plumbing is `_run_check()` itself; ordinary agent lifecycle events already use
  `_emit_lifecycle_event()`.

Normalize max-turn failures into explicit errors:

- Extend the normalized brain result enough to represent a max-turn terminal
  condition without leaking SDK-specific message objects into `subagent.py`.
- For Claude, inspect the SDK terminal result/metadata that carries
  `max_turns_reached` and render it as a stable error string.
- For Codex, preserve the existing `turn.failed`/`error` message. Codex-specific
  max-turn schema discovery is out of scope unless an implementation fixture
  already exposes a stable field.
- Keep the existing `unknown error` fallback only for genuinely empty terminal
  error payloads.

## Scope

In scope:

- `bobi/cli.py` option semantics and help text for `subagents launch`.
- Internal monitor scheduler calls that currently depend on `--wait` as a check
  harness; they should call `--as-check`.
- Documentation and role-prompt examples that currently advertise `--wait` for
  ambiguous or incorrect behavior.
- Regression tests for:
  - `--wait` calls the real agent-wait path, not `run_check_blocking()`.
  - `--as-check` calls `run_check_blocking()` and still prints check verdict JSON.
  - invalid combinations such as `--as-check --wait` and `--post-event` without
    `--as-check` are usage errors.
  - `--as-check` preserves the old check-mode stdout JSON, retry behavior,
    `--post-event` behavior, and exit codes.
  - max-turn failures propagate as actionable errors in check, curator, and
    ordinary agent launch paths.

Out of scope:

- Raising or removing `CHECK_MAX_TURNS` for real monitor checks.
- Changing gate semantics or `GATE_MAX_TURNS`.
- Reworking the sleep-cycle curator budget beyond making max-turn failure
  visible. If `CURATOR_MAX_TURNS = 10` remains too low after error reporting is
  fixed, that should be a follow-up with its own budget tradeoff.
- Changing release metadata (`VERSION`, `pyproject.toml` version,
  `CHANGELOG.md`).

## Technical Approach

1. Update `subagents launch` options:
   - Add `--as-check`, `is_flag=True`, with help text like "Run the task as a
     short-lived monitoring check".
   - Change `--wait` help to "Block until the launched agent completes".
   - Remove hidden `--agent-wait` rather than carrying a compatibility alias.

2. Split dispatch clearly:
   - If `as_check` is true, call `_run_check()` and return.
   - If `post_event` is provided without `as_check`, exit with a usage error.
   - If `as_check` is combined with `wait`, exit with a usage error. Check mode
     is already synchronous; accepting both would make flag precedence ambiguous.
   - If `wait` is true, call `_run_agent_wait()`.
   - Otherwise launch asynchronously with `launch_agent()`.

3. Define the dispatch/output matrix:
   - `adhoc + --wait`: run `_run_agent_wait()`, print final agent text on stdout,
     return 0 on success and 1 on agent failure.
   - `adhoc + --as-check`: run `_run_check()`, preserve the old check-mode stdout
     verdict JSON, retry behavior, `--post-event` behavior, and exit codes.
   - `non-adhoc + --wait`: return a usage error until a blocking workflow runner
     exists. The current `_run_agent_wait()` only supports adhoc; the fix must
     fail loudly instead of silently falling back to the check harness.
   - `non-adhoc + --as-check`: allowed, because check mode ignores workflow
     semantics and runs the supplied task as a monitor check.
   - no wait/check flags: preserve existing asynchronous `launch_agent()` behavior.

4. Update internal check callers:
   - Change `bobi/monitors/scheduler.py` to invoke
     `subagents launch --as-check --non-interactive` for description-only
     monitor checks.
   - Update comments in `docs/MONITORS.md` that describe the old `--wait`
     behavior as a known hazard.
   - Run a repo-wide search for `--wait`, `run_check_blocking`, and `post_event`
     to catch tests, prompts, docs, workflows, and generated command strings that
     still assume check-mode `--wait`.

5. Normalize max-turn failure details:
   - Add a narrow provider-neutral terminal error model to `TurnResult`, for
     example `error_kind`, `error_message`, `max_turns`, `turn_count`, and
     `provider_code`. Avoid a generic provider-object dump.
   - In `bobi/brain/claude.py`, populate that field from SDK result metadata for
     the `max_turns_reached` attachment/stop condition. Tests must use a fixture
     matching the observed SDK shape:
     `{"type": "attachment", "attachment": {"type": "max_turns_reached",
     "maxTurns": 8, "turnCount": 9}}`.
   - In `bobi/subagent.py`, when the terminal result is unsuccessful or carries a
     structured terminal error, prefer a rendered structured error over the empty
     `result_text` fallback. The string should include the cap and observed turn
     count when available.
   - Ensure `_run_verdict_agent_blocking()` logs and returns that explicit error
     so `Check failed:` and curator failures are actionable.

6. Update public examples:
   - `bobi/prompts/base.md` should keep `--wait` for real blocking delegation
     once fixed.
   - Monitoring docs should refer to `--as-check` for check harness behavior.
   - CLI help tests should assert the visible flags make this distinction clear.

## Verification Plan

Automated tests:

- `tests/test_cli.py`
  - Replace the current `test_wait_mode_runs_check` expectation with a test that
    `--wait` calls the real blocking-agent path.
  - Add a new `--as-check` test proving check mode still calls
    `run_check_blocking()` and prints verdict JSON.
  - Add a help-output regression test that `--as-check` is visible.
  - Add a usage test for `--post-event` without `--as-check`.

- `tests/test_subagent_blocking.py`
  - Add a fake terminal brain result for max-turn exhaustion and assert
    `AgentResult.error` contains `max_turns_reached` plus the configured cap.
  - Add a check-run regression proving `run_check_blocking()` returns the same
    explicit error instead of `unknown error` after retries exhaust.
  - Add a curator regression if the failure is surfaced through the shared
    verdict runner.

- `tests/test_brain.py` / adapter-specific tests
  - Add coverage for Claude `ResultMessage` normalization from a max-turn
    terminal message into the new provider-neutral field.
  - Add Codex coverage if its JSON terminal shape can represent max-turn
    exhaustion.

Targeted test commands:

```bash
pytest tests/test_cli.py tests/test_subagent_blocking.py tests/test_brain.py tests/test_max_turns_cap.py -q
```

Full gate before implementation PR:

```bash
pytest tests/ --ignore=tests/integration/ --ignore=tests/e2e/ --timeout=30 -q
```

Manual smoke after implementation:

```bash
bobi agent <test-agent> subagents launch -w adhoc --role engineer --wait --task "Use several short turns to produce a final answer"
bobi agent <test-agent> subagents launch -w adhoc --role monitor --as-check --task "Report finding=false"
```

Expected manual behavior:

- The first command runs the real role and is not capped by `CHECK_MAX_TURNS`.
- The second command prints check verdict JSON and remains capped by
  `CHECK_MAX_TURNS`.
- Any max-turn exhaustion includes an explicit `max_turns_reached` error.

Exit-code contract:

- Successful real-agent `--wait`: 0.
- Failed real-agent `--wait`, including max-turn exhaustion: 1 with an actionable
  stderr error.
- Successful check-mode `--as-check`: 0 with verdict JSON on stdout.
- Failed check-mode `--as-check`: 1 with verdict JSON on stdout and actionable
  stderr error.
- CLI misuse: Click usage error exit code with a specific message naming the
  invalid flag combination.

## Implementation Plan

1. Write failing tests for CLI semantics and max-turn error rendering.
2. Add `--as-check`, update dispatch ordering, and remove the hidden
   `--agent-wait` alias.
3. Update monitor scheduler subprocess argv to use `--as-check`.
4. Add structured terminal error normalization in the brain adapter layer and
   render it in `subagent.py`.
5. Update docs/prompts to match the new public contract.
6. Run targeted tests, then the full pytest suite.
7. Open the implementation PR only after this spec is approved by a live human.

## Spec Review

Architecture / edge cases / tests:

- The check harness remains available and explicit; monitor cost controls are not
  weakened.
- The cutover intentionally removes hidden `--agent-wait`; stale callers fail
  fast with Click's normal unknown-option handling.
- The largest compatibility risk is any external automation relying on old
  `--wait` check semantics. Introducing `--as-check` and updating internal
  scheduler callers gives those automations a clear migration path.
- Tests cover both positive dispatch paths and misuse (`--post-event` without
  check mode).

UX / operator experience:

- CLI names become literal: `--wait` waits for the launched agent, `--as-check`
  runs a check.
- Help output should expose both public modes with unambiguous language.
- Max-turn errors become directly actionable in logs and command output.

Scope:

- This spec fixes the issue's two defects without changing monitor budgets or
  redesigning sleep-cycle distillation.
- The curator cap concern is intentionally limited to observability here; budget
  tuning can be evaluated with data after max-turn failures are visible.
