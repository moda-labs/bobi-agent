"""Integration tests for the unified modastack agent command.

Verifies the full CLI → subprocess pipeline for both adhoc and workflow modes.
"""

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from modastack.sdk import SessionRegistry, get_registry, set_repo_root

PROJECT_ROOT = Path(__file__).parent.parent.parent


def _cleanup_session(name: str) -> None:
    """Mark a session done and remove its directory so the next run doesn't collide."""
    set_repo_root(PROJECT_ROOT)
    registry = get_registry()
    registry.mark_done(name)
    session_dir = SessionRegistry.session_dir(name)
    if session_dir.exists():
        shutil.rmtree(session_dir)


class TestAgentCLI:
    """Test that modastack agent returns immediately in both modes."""

    def test_adhoc_returns_immediately(self, tmp_path):
        session_name = f"wf-adhoc-{tmp_path.name}-99"
        _cleanup_session(session_name)

        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "-w", "adhoc", "--role", "engineer",
             "--repo", str(tmp_path), "--task", "say hello #99"],
            capture_output=True, text=True, timeout=10,
            cwd=str(PROJECT_ROOT),
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert elapsed < 5, f"adhoc took {elapsed:.1f}s — should return immediately"

        _cleanup_session(session_name)

    def test_workflow_returns_immediately(self, tmp_path):
        session_name = "wf-issue-lifecycle-modastack-42"
        _cleanup_session(session_name)

        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "-w", "issue-lifecycle", "--role", "engineer",
             "--repo", str(PROJECT_ROOT),
             "--task", "Work on #42"],
            capture_output=True, text=True, timeout=10,
            cwd=str(PROJECT_ROOT),
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "wf-issue-lifecycle" in result.stdout
        assert elapsed < 5, f"workflow took {elapsed:.1f}s — should return immediately"

        _cleanup_session(session_name)

    def test_validation_missing_workflow(self):
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "--role", "engineer", "--repo", "/tmp", "--task", "X"],
            capture_output=True, text=True, timeout=5,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode != 0
        assert "--workflow" in result.stderr or "required" in result.stderr.lower()

    def test_validation_missing_role(self):
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "-w", "adhoc", "--repo", "/tmp", "--task", "X"],
            capture_output=True, text=True, timeout=5,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode != 0
        assert "--role" in result.stderr or "required" in result.stderr.lower()
