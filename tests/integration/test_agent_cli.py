"""Integration tests for the unified modastack agent command.

Verifies the full CLI → subprocess pipeline for both adhoc and workflow modes.
"""

import subprocess
import sys
import time
from pathlib import Path

import pytest


class TestAgentCLI:
    """Test that modastack agent returns immediately in both modes."""

    def test_adhoc_returns_immediately(self, tmp_path):
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "-w", "adhoc", "--repo", str(tmp_path), "--task", "say hello #99"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).parent.parent.parent),
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert elapsed < 5, f"adhoc took {elapsed:.1f}s — should return immediately"

    def test_workflow_returns_immediately(self, tmp_path):
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "-w", "issue-lifecycle",
             "--repo", str(Path(__file__).parent.parent.parent),
             "--task", "Work on #42"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).parent.parent.parent),
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "wf-issue-lifecycle" in result.stdout
        assert elapsed < 5, f"workflow took {elapsed:.1f}s — should return immediately"

    def test_spawn_alias_returns_immediately(self, tmp_path):
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "spawn",
             "--repo", str(tmp_path), "--task", "say hello #88"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).parent.parent.parent),
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert elapsed < 5

    def test_validation_missing_workflow(self):
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "--repo", "/tmp", "--task", "X"],
            capture_output=True, text=True, timeout=5,
            cwd=str(Path(__file__).parent.parent.parent),
        )
        assert result.returncode != 0
        assert "--workflow" in result.stderr or "required" in result.stderr.lower()
