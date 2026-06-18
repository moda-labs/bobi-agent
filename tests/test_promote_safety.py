"""Regression tests for the release promote step in publish-pypi.yml (#347).

The promote step upgrades prod and restarts the director. Two safety
invariants must hold:

1. `git fetch --tags --force` — tolerate re-pointed release tags.
2. All fallible prep (fetch, checkout, install) happens BEFORE
   `modastack stop`, so a failure never leaves prod down. A trap
   restarts the director if anything fails after stop.

These tests parse the workflow YAML and assert the invariants
structurally, so they break if someone reintroduces the old ordering.
"""

import yaml
import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "publish-pypi.yml"


def _promote_script() -> str:
    """Extract the shell script from the 'Promote release to production' step."""
    with open(WORKFLOW) as f:
        wf = yaml.safe_load(f)
    for job in wf["jobs"].values():
        for step in job.get("steps", []):
            if "Promote" in step.get("name", ""):
                return step["run"]
    raise AssertionError("Promote step not found in publish-pypi.yml")


def _line_index(script: str, pattern: str) -> int:
    """Return the 0-based line index of the first line matching pattern."""
    for i, line in enumerate(script.splitlines()):
        if re.search(pattern, line):
            return i
    raise AssertionError(f"Pattern {pattern!r} not found in promote script")


class TestPromoteSafety:
    """Structural assertions on the promote step's command ordering."""

    def test_fetch_tags_uses_force(self):
        """Bug 1: re-pointed tags must not break the fetch (#347)."""
        script = _promote_script()
        assert "--force" in script, "git fetch --tags must use --force to tolerate re-pointed tags"
        # Specifically on the fetch line
        fetch_line = [l for l in script.splitlines() if "git" in l and "fetch" in l and "--tags" in l]
        assert fetch_line, "Expected a 'git fetch --tags' line"
        assert "--force" in fetch_line[0], "The git fetch --tags line must include --force"

    def test_fetch_before_stop(self):
        """Bug 2: fetch must happen before stop so a fetch failure doesn't leave prod down."""
        script = _promote_script()
        fetch_idx = _line_index(script, r"git.*fetch.*--tags")
        stop_idx = _line_index(script, r"modastack stop")
        assert fetch_idx < stop_idx, (
            f"git fetch (line {fetch_idx}) must come BEFORE modastack stop (line {stop_idx})"
        )

    def test_checkout_before_stop(self):
        """Bug 2: checkout must happen before stop."""
        script = _promote_script()
        checkout_idx = _line_index(script, r"git.*checkout.*refs/tags")
        stop_idx = _line_index(script, r"modastack stop")
        assert checkout_idx < stop_idx, (
            f"git checkout (line {checkout_idx}) must come BEFORE modastack stop (line {stop_idx})"
        )

    def test_install_before_stop(self):
        """Bug 2: modastack install must happen before stop."""
        script = _promote_script()
        install_idx = _line_index(script, r"modastack install")
        stop_idx = _line_index(script, r"modastack stop")
        assert install_idx < stop_idx, (
            f"modastack install (line {install_idx}) must come BEFORE modastack stop (line {stop_idx})"
        )

    def test_trap_set_before_stop(self):
        """Bug 2: ERR trap must be set before stop so failures after stop trigger recovery."""
        script = _promote_script()
        trap_idx = _line_index(script, r"trap\s+\S+\s+ERR")
        stop_idx = _line_index(script, r"modastack stop")
        assert trap_idx < stop_idx, (
            f"ERR trap (line {trap_idx}) must be set BEFORE modastack stop (line {stop_idx})"
        )

    def test_start_after_stop(self):
        """Sanity: modastack start must come after modastack stop."""
        script = _promote_script()
        stop_idx = _line_index(script, r"modastack stop")
        # Find the non-trap start (the main start line)
        lines = script.splitlines()
        start_idx = None
        for i, line in enumerate(lines):
            if "modastack start" in line and "trap" not in lines[max(0, i-3):i+1].__repr__() and i > stop_idx:
                # Check it's not inside the trap function
                if "restart_on_failure" not in line and "echo" not in line:
                    start_idx = i
                    break
        assert start_idx is not None, "Expected a modastack start line after stop"
        assert start_idx > stop_idx, "modastack start must come after modastack stop"
