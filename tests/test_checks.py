"""Unit tests for native monitor check runners — pr_conflicts, stale_prs,
slug resolution, ISO parsing, and the CHECKS registry.

These tests load github_checks.py from the agent cache or skip if not found.
The checks module is agent-pack content, not framework code — it lives in
moda-agents and is fetched to ~/.modastack/agents/ at runtime.
"""

import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import importlib.util
import sys

from modastack.monitors.schema import Condition


def _find_checks_module() -> Path:
    """Find github_checks.py from project-local agents."""
    repo_root = Path(__file__).parent.parent
    search_dirs = [
        repo_root / "agents",
        repo_root / ".modastack" / "agents",
    ]
    for search_dir in search_dirs:
        if not search_dir.is_dir():
            continue
        for pack in search_dir.iterdir():
            candidate = pack / "monitors" / "github_checks.py"
            if candidate.exists():
                return candidate
    return None


_checks_path = _find_checks_module()
if _checks_path is None:
    pytest.skip(
        "github_checks.py not found — run: modastack agents update eng-team",
        allow_module_level=True,
    )

_spec = importlib.util.spec_from_file_location("modastack.monitors.checks", _checks_path)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["modastack.monitors.checks"] = _mod
_spec.loader.exec_module(_mod)

CHECKS = _mod.CHECKS
_parse_iso = _mod._parse_iso
_repo_slug = _mod._repo_slug
_gh_pr_list = _mod._gh_pr_list
_slug_cache = _mod._slug_cache
pr_conflicts = _mod.pr_conflicts
stale_prs = _mod.stale_prs


class TestRepoSlug:

    def test_slug_from_gh_cli(self):
        with patch("subprocess.run") as m:
            m.return_value = MagicMock(
                returncode=0,
                stdout="moda-labs/modastack\n",
            )
            _slug_cache.clear()
            assert _repo_slug(Path("/tmp/repo")) == "moda-labs/modastack"

    def test_slug_fallback_to_dir_name(self):
        with patch("subprocess.run") as m:
            m.return_value = MagicMock(returncode=1, stdout="")
            _slug_cache.clear()
            assert _repo_slug(Path("/tmp/my-repo")) == "my-repo"

    def test_slug_cached(self):
        _slug_cache.clear()
        _slug_cache["/tmp/cached"] = "cached/repo"
        assert _repo_slug(Path("/tmp/cached")) == "cached/repo"


class TestParseIso:

    def test_parse_with_z(self):
        dt = _parse_iso("2025-01-15T12:00:00Z")
        assert dt.tzinfo == timezone.utc
        assert dt.year == 2025

    def test_parse_with_offset(self):
        dt = _parse_iso("2025-01-15T12:00:00+00:00")
        assert dt.year == 2025


class TestPrConflicts:

    def _mock_monitor(self):
        return MagicMock()

    def test_no_prs_returns_empty(self):
        with patch.object(_mod, "_repo_slug", return_value="o/r"), \
             patch.object(_mod, "_gh_pr_list", return_value=[]):
            result = pr_conflicts(self._mock_monitor(), [Path("/tmp")])
            assert result == []

    def test_conflict_detected(self):
        prs = [{"number": 1, "headRefName": "feat", "url": "https://...",
                "mergeable": "CONFLICTING"}]
        with patch.object(_mod, "_repo_slug", return_value="o/r"), \
             patch.object(_mod, "_gh_pr_list", return_value=prs):
            result = pr_conflicts(self._mock_monitor(), [Path("/tmp")])
            assert len(result) == 1
            assert "o/r#1" in result[0].key

    def test_mergeable_pr_not_flagged(self):
        prs = [{"number": 2, "headRefName": "fix", "url": "https://...",
                "mergeable": "MERGEABLE"}]
        with patch.object(_mod, "_repo_slug", return_value="o/r"), \
             patch.object(_mod, "_gh_pr_list", return_value=prs):
            result = pr_conflicts(self._mock_monitor(), [Path("/tmp")])
            assert result == []


class TestStalePrs:

    def _mock_monitor(self):
        return MagicMock()

    def test_stale_pr_detected(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        prs = [{"number": 3, "headRefName": "old", "url": "https://...",
                "updatedAt": old}]
        with patch.object(_mod, "_repo_slug", return_value="o/r"), \
             patch.object(_mod, "_gh_pr_list", return_value=prs):
            result = stale_prs(self._mock_monitor(), [Path("/tmp")])
            assert len(result) == 1

    def test_recent_pr_not_flagged(self):
        recent = datetime.now(timezone.utc).isoformat()
        prs = [{"number": 4, "headRefName": "new", "url": "https://...",
                "updatedAt": recent}]
        with patch.object(_mod, "_repo_slug", return_value="o/r"), \
             patch.object(_mod, "_gh_pr_list", return_value=prs):
            result = stale_prs(self._mock_monitor(), [Path("/tmp")])
            assert result == []


class TestChecksRegistry:

    def test_registry_has_both_checks(self):
        assert "pr_conflicts" in CHECKS
        assert "stale_prs" in CHECKS

    def test_registry_values_are_callable(self):
        for fn in CHECKS.values():
            assert callable(fn)
