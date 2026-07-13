# Issue 747: Enforce the long-term memory cap in the sleep cycle

## Problem

`long_term_memory.md` is required to stay under `MAX_MEMORY_CHARS` (`24_000`), but the cap is currently enforced only by prompt instruction. The sleep-cycle agent can write an oversized file, return `success: true`, and the scheduler will accept the result.

The current fallback is lossy and quiet:

- `load_long_term_memory()` truncates oversized content with a raw prefix slice.
- Because the document order is `## Facts` then `## Decisions`, prefix truncation drops Decisions first.
- `_spawn_sleep_cycle()` reads current memory through `load_long_term_memory()`, so an already oversized source file is hidden from the next sleep-cycle run.
- If there are no new transcript rows and no seed, `_spawn_sleep_cycle()` returns without launching the agent, so an existing oversized file cannot self-heal.
- `_on_sleep_cycle_result()` trusts the model-reported `bytes` value instead of validating the actual artifact on disk.

The result is a one-way ratchet: once the file grows past the cap, normal successful sleep-cycle runs can leave it oversized indefinitely, while working agents silently receive a truncated prompt.

## Goals

- Make the `24_000` character cap a deterministic scheduler invariant, not just a prompt instruction.
- Force a compaction run when the existing `long_term_memory.md` is over cap, even if there are no new transcript messages.
- Validate the written artifact after every successful sleep-cycle run before advancing the cursor.
- Make read-time truncation loud enough to diagnose if the deterministic enforcement still fails.
- Avoid structurally favoring Facts over Decisions in any remaining defensive truncation path.

## Non-Goals

- Do not raise `MAX_MEMORY_CHARS`.
- Do not build a separate Decisions spill file.
- Do not change the long-term memory document format beyond preserving the existing `## Facts` / `## Decisions` sections.
- Do not solve the secondary `MAX_SLEEP_CYCLE_INPUT_CHARS` backlog in this issue.

## Proposed Behavior

### 1. Add uncapped memory reads for the sleep-cycle scheduler

Add a helper in `bobi/memory.py` that reads the raw `long_term_memory.md` content without applying the prompt-injection cap. The existing `load_long_term_memory()` remains the prompt safety backstop for working agents.

The scheduler should use the uncapped read when constructing the sleep-cycle task so the sleep-cycle agent sees the full artifact it must compress.

The uncapped helper must preserve the migration behavior of `load_long_term_memory()`: when called with a runtime `state` directory, it must run the existing long-term-memory migration path before reading. This keeps legacy `policy.md` state compatible with the new scheduler behavior.

### 2. Detect an existing over-cap artifact before dispatch

In `_spawn_sleep_cycle()`:

- Read the current memory uncapped.
- Compute whether `len(current_memory) > MAX_MEMORY_CHARS`.
- If over cap, dispatch the sleep-cycle agent even when there are no new transcript rows and no seed.
- In the generated task, include an explicit deterministic note that the current `long_term_memory.md` exceeds the cap and must be rewritten under the cap.
- If there are no transcript rows, call `build_sleep_cycle_task()` with an empty transcript and `highest_id=None`; a successful compaction-only run must not advance the cursor.
- The task must contain the full uncapped current memory, not the truncated prompt-injection version or `[memory truncated]` marker.

This preserves the existing cursor semantics: only ingested transcript rows move the cursor.

### 3. Validate the artifact after a successful result

After `_on_sleep_cycle_result()` receives `{"success": true, ...}`, validate the actual `long_term_memory.md` file when either:

- `result["updated"]` is true, or
- the pre-dispatch file was over cap and the run was expected to compact it.

Validation should read the file from disk and compare `len(content)` to `MAX_MEMORY_CHARS`. The model-reported `bytes` field is informational only.

The callback must receive explicit dispatch context rather than recomputing intent from the result:

- the memory path to validate, normally `paths.long_term_memory_path(root)` after `paths.migrate_long_term_memory_state(root)`;
- whether compaction was required before dispatch;
- the existing `highest_id` and `cursor_path` values.

If the file is still over cap:

- Do not advance the cursor.
- Do not publish the configured memory event, `system/memory.updated`, or the compatibility `system/policy.updated` alias.
- Publish a monitor error explaining that the sleep-cycle output exceeded the cap.
- Leave the same transcript window and/or compaction condition to retry on the next scheduled interval.

This turns over-cap output into a failed sleep-cycle run instead of accepting a bad artifact.

An over-cap compaction run that returns `{"success": true, "updated": false}` while the file remains oversized must be rejected the same way. The model cannot opt out of compaction by reporting no durable changes.

The scheduled retry behavior is intentional for this issue: a failed compaction is retried on the monitor's next configured interval. This spec does not add immediate retry or backoff.

### 4. Make read-time truncation loud and less biased

`load_long_term_memory()` should log a warning whenever it truncates an oversized file, including the observed size and cap.

The defensive truncation should preserve both top-level sections when possible. A simple acceptable implementation is:

- Parse only top-level `## Facts` and `## Decisions` blocks by heading. Do not introduce a general Markdown parser or support arbitrary section reshaping.
- Allocate budget to keep both headings and content from both sections.
- If both sections are present, trim each section independently and append an explicit marker to any trimmed section.
- If one section is small, the other section can use the remaining budget.
- If the expected sections cannot be found, fall back to the current prefix cap plus warning. Preamble or unknown top-level sections may be dropped in the defensive truncation path.

This remains a backstop; normal operation should keep the underlying file under cap.

### 5. Keep the sleep-cycle prompt aligned

Update `bobi/prompts/sleep_cycle.md` so it reflects deterministic enforcement:

- It still tells the agent to stay under `24_000` characters.
- It explicitly says an over-cap rewrite will be rejected and retried.
- It says compaction-only runs may have no new transcript delta.
- It clarifies that the JSON `bytes` field remains informational; the scheduler enforces Python character length against `MAX_MEMORY_CHARS`.

## Technical Approach

- `bobi/memory.py`
  - Add an uncapped raw read helper for scheduler use.
  - Add warning logs to `load_long_term_memory()` when truncation occurs.
  - Add section-aware defensive truncation for the prompt-injection path.

- `bobi/monitors/sleep_cycle.py`
  - Extend `build_sleep_cycle_task()` to accept deterministic output-cap notes, or add an output-cap flag to the existing `flags` dict.

- `bobi/monitors/scheduler.py`
  - Use the uncapped read helper in `_spawn_sleep_cycle()`.
  - Dispatch compaction-only runs for existing over-cap memory.
  - Pass `memory_path` and `compaction_required` context into `_on_sleep_cycle_result()` so it can validate the artifact before advancing the cursor.
  - Treat still-over-cap output as failure and publish `system/monitor.error`.
  - Keep the deprecated curator wrappers on the same shared path so `_spawn_curator()` / `_on_curator_result()` observe the same cap invariant.

- `bobi/prompts/sleep_cycle.md`
  - Document that output cap validation is enforced by the scheduler.

## Verification Plan

Add regression tests before implementation.

- `tests/test_long_term_memory.py`
  - `load_long_term_memory()` logs a warning on oversized input.
  - Defensive truncation keeps both `## Facts` and `## Decisions` headings when both sections exist.
  - Defensive truncation keeps substantive content from both sections, not only the heading.
  - Defensive truncation marks each trimmed section explicitly.
  - Defensive truncation stays within a defined cap allowance and behaves deterministically when one section is tiny and the other is huge.
  - Fallback truncation logs a warning when section parsing fails.
  - The uncapped helper returns the full file after legacy migration.

- `tests/test_sleep_cycle.py`
  - `_spawn_sleep_cycle()` dispatches when `long_term_memory.md` is over cap and there are no transcript rows.
  - That dispatch includes the full uncapped current memory in the task.
  - The compaction-only dispatch uses `highest_id=None`, and success does not advance the cursor.
  - `_on_sleep_cycle_result()` rejects an over-cap compaction run that reports `updated: false`.
  - `_on_sleep_cycle_result()` rejects an updated file that remains over cap, publishes a monitor error, does not publish memory-updated, and does not advance the cursor.
  - Rejected output publishes no configured memory event and no compatibility alias.
  - `_on_sleep_cycle_result()` accepts an updated file under cap and preserves existing cursor/publish behavior.
  - Updated output exactly at `MAX_MEMORY_CHARS` is accepted; `MAX_MEMORY_CHARS + 1` is rejected.
  - Monitor error detail includes the actual character count, cap, logical artifact path, whether compaction was required, and whether the result claimed `updated: false`.
  - The compatibility curator path observes the same invariant through the shared sleep-cycle implementation.

Run:

```bash
pytest tests/test_long_term_memory.py tests/test_sleep_cycle.py
```

Then run the project test command used by the repository before opening the implementation PR.

## Rollout and Risk

The change is localized to the sleep-cycle scheduler and prompt-injection backstop. The main compatibility risk is rejecting outputs that the scheduler previously accepted. That is intentional, but the monitor error must be explicit so operators can diagnose repeated failure.

The cursor must not advance on rejected over-cap output. Otherwise the scheduler could lose transcript input while still failing to repair memory.

## Implementation Plan

1. Add failing tests for uncapped reads, warning logs, section-aware defensive truncation, compaction-only dispatch, and post-run artifact validation.
2. Add the uncapped read and section-aware truncation helpers in `bobi/memory.py`.
3. Update `_spawn_sleep_cycle()` to detect current over-cap memory and dispatch compaction-only runs.
4. Update `_on_sleep_cycle_result()` to validate the actual artifact before cursor advance and publishing.
5. Update the sleep-cycle prompt to describe deterministic rejection.
6. Run focused tests, then the repository test command.
