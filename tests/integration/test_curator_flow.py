"""Integration test for the policy-curator flow (#456, #695).

Drives the real spawn path end to end: scheduler spawn helper -> real
`monitors curator --request` subprocess -> real curator agent session ->
policy.md written -> summary JSON parsed back by the scheduler's waiter.

This is the test #682/#695 were missing: every unit test stubs
subprocess.Popen, so an argv that the real CLI rejects (the `subagents
launch --role` contract) or a runner that swallows the curator's summary
(the check-verdict wrapper) passed CI while the curator never worked in
production.

Requires the `claude` CLI to be installed. Skipped in CI.
"""

import time
from pathlib import Path

import pytest

from bobi import paths
from bobi.monitors.curator import build_curator_task
from bobi.monitors.schema import Monitor

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
class TestCuratorFlow:
    def test_curator_spawn_writes_policy_and_reports_summary(self, bobi_env):
        """The real curator subprocess writes policy.md and its summary
        reaches on_result - the full contract the scheduler's cursor
        advance depends on."""
        from bobi.monitors.scheduler import _default_spawn_curator

        prompt = (Path(paths.__file__).parent / "prompts"
                  / "curator.md").read_text()
        task = build_curator_task(prompt, TRANSCRIPT, "", {})
        monitor = Monitor(name="policy-curator", curator=True,
                          event="system/policy.updated", interval="6h")

        policy = paths.policy_path(bobi_env.project_path)
        cursor = paths.policy_cursor_path(bobi_env.project_path)
        results = []
        try:
            _default_spawn_curator(monitor, str(bobi_env.project_path),
                                   task, results.append)
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline and not results:
                time.sleep(2)

            assert results, "curator subprocess produced no result in time"
            summary = results[0]
            assert isinstance(summary, dict), (
                f"indeterminate curator result: {summary!r}")
            assert summary.get("success") is True, f"summary: {summary}"
            assert policy.is_file(), "curator did not write policy.md"
            text = policy.read_text()
            assert "## Facts" in text and "## Decisions" in text, text
            assert "ruff" in text, f"seed decision not distilled: {text}"
            assert not list(
                (paths.state_dir() / "curator").glob("task-*.md")), \
                "curator task file not cleaned up"
        finally:
            policy.unlink(missing_ok=True)
            cursor.unlink(missing_ok=True)
