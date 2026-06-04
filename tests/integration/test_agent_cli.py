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
             "--repo", str(tmp_path), "--task", "say hello #99"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).parent.parent.parent),
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "eng-99" in result.stdout
        assert elapsed < 5, f"adhoc took {elapsed:.1f}s — should return immediately"

    def test_workflow_returns_immediately(self, tmp_path):
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "--workflow", "issue-lifecycle",
             "--repo", "moda-labs/jobtack", "--issue", "42"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).parent.parent.parent),
        )
        elapsed = time.monotonic() - start

        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "wf-issue-lifecycle-jobtack-42" in result.stdout
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
        assert "eng-88" in result.stdout
        assert elapsed < 5

    def test_validation_no_task_or_workflow(self):
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent", "--repo", "/tmp"],
            capture_output=True, text=True, timeout=5,
            cwd=str(Path(__file__).parent.parent.parent),
        )
        assert result.returncode != 0
        assert "Specify --task or --workflow" in result.stderr

    def test_validation_both_task_and_workflow(self):
        result = subprocess.run(
            [sys.executable, "-m", "modastack.cli", "agent",
             "--task", "X", "--workflow", "Y", "--issue", "1"],
            capture_output=True, text=True, timeout=5,
            cwd=str(Path(__file__).parent.parent.parent),
        )
        assert result.returncode != 0
        assert "not both" in result.stderr
