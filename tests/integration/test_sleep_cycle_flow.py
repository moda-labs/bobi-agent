"""Integration test for the sleep-cycle flow (#456, #695, #714).

Drives the real spawn path end to end: scheduler spawn helper -> real
`monitors curator --request` subprocess -> real sleep-cycle agent session ->
long_term_memory.md written -> summary JSON parsed back by the scheduler's
waiter.

This is the test #682/#695 were missing: every unit test stubs
subprocess.Popen, so an argv that the real CLI rejects (the `subagents
launch --role` contract), a runner that swallows the sleep-cycle summary
(the check-verdict wrapper), or a prompt handed to the brain as argv rather
than stdin (#714 codex E2BIG) passed CI while the sleep cycle never worked
in production.

Requires the `claude` CLI to be installed. Skipped in CI.
"""

import time
from pathlib import Path

import pytest

from bobi import paths
from bobi.monitors.schema import Monitor
from bobi.monitors.sleep_cycle import build_sleep_cycle_task

from .conftest import requires_claude

pytestmark = pytest.mark.claude

TRANSCRIPT = """\
=== session wf-adhoc-test-repo-9 (messages 1-3) ===
[user] Which linter should we standardize on?
[assistant] Comparing ruff and flake8 on this repo: ruff covers the same
rules and runs 30x faster. Standardizing on ruff over flake8.
[user] Sounds good, decision made: ruff over flake8, do not revisit.
"""


@requires_claude
@pytest.mark.timeout(300)
class TestSleepCycleFlow:
    def test_sleep_cycle_spawn_writes_memory_and_reports_summary(self, bobi_env):
        """The real sleep-cycle subprocess writes long_term_memory.md and its
        summary reaches on_result - the full contract the scheduler's cursor
        advance depends on."""
        from bobi.monitors.scheduler import _default_spawn_sleep_cycle

        prompt = (Path(paths.__file__).parent / "prompts"
                  / "sleep_cycle.md").read_text()
        task = build_sleep_cycle_task(prompt, TRANSCRIPT, "", {})
        monitor = Monitor(name="sleep-cycle", sleep_cycle=True,
                          event="system/memory.updated", interval="6h")

        memory = paths.long_term_memory_path(bobi_env.project_path)
        cursor = paths.long_term_memory_cursor_path(bobi_env.project_path)
        results = []
        try:
            _default_spawn_sleep_cycle(monitor, str(bobi_env.project_path),
                                       task, results.append)
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline and not results:
                time.sleep(2)

            assert results, "sleep-cycle subprocess produced no result in time"
            summary = results[0]
            assert isinstance(summary, dict), (
                f"indeterminate sleep-cycle result: {summary!r}")
            assert summary.get("success") is True, f"summary: {summary}"
            assert memory.is_file(), "sleep cycle did not write long_term_memory.md"
            text = memory.read_text()
            assert "## Facts" in text and "## Decisions" in text, text
            assert "ruff" in text, f"seed decision not distilled: {text}"
            assert not list(
                (paths.state_dir() / "sleep-cycle").glob("task-*.md")), \
                "sleep-cycle task file not cleaned up"
        finally:
            memory.unlink(missing_ok=True)
            cursor.unlink(missing_ok=True)
