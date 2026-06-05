"""Unit tests for native monitor check runners — pr_conflicts, stale_prs,
slug resolution, ISO parsing, and the CHECKS registry."""

import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modastack.monitors.checks import (
    CHECKS,
    Condition,
    _parse_iso,
    _repo_slug,
    _gh_pr_list,
    _slug_cache,
    pr_conflicts,
    stale_prs,
)


@pytest.fixture(autouse=True)
def clear_slug_cache():
    """Ensure slug cache doesn't leak between tests."""
    _slug_cache.clear()
    yield
    _slug_cache.clear()


# ---------------------------------------------------------------------------
# Condition dataclass
# ---------------------------------------------------------------------------

class TestCondition:
    def test_fields(self):
        c = Condition(key="repo#1", data={"pr_number": 1})
        assert c.key == "repo#1"
        assert c.data == {"pr_number": 1}

    def test_default_data(self):
        c = Condition(key="k")
        assert c.data == {}


# ---------------------------------------------------------------------------
# _parse_iso
# ---------------------------------------------------------------------------

class TestParseIso:
    def test_iso_with_z_suffix(self):
        dt = _parse_iso("2024-06-01T12:00:00Z")
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 6
        assert dt.tzinfo is not None

    def test_iso_with_offset(self):
        dt = _parse_iso("2024-06-01T12:00:00+05:00")
        assert dt is not None
        assert dt.utcoffset().total_seconds() == 5 * 3600

    def test_naive_datetime_gets_utc(self):
        dt = _parse_iso("2024-06-01T12:00:00")
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_empty_string(self):
        assert _parse_iso("") is None

    def test_invalid_string(self):
        assert _parse_iso("not-a-date") is None

    def test_none_like(self):
        assert _parse_iso("") is None


# ---------------------------------------------------------------------------
# _repo_slug
# ---------------------------------------------------------------------------

class TestRepoSlug:
    @patch("modastack.monitors.checks.subprocess.run")
    def test_uses_gh_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="moda-labs/modastack\n"
        )
        slug = _repo_slug(Path("/dev/modastack"))
        assert slug == "moda-labs/modastack"

    @patch("modastack.monitors.checks.subprocess.run")
    def test_falls_back_to_dirname(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        slug = _repo_slug(Path("/dev/myproject"))
        assert slug == "myproject"

    @patch("modastack.monitors.checks.subprocess.run")
    def test_handles_os_error(self, mock_run):
        mock_run.side_effect = OSError("gh not found")
        slug = _repo_slug(Path("/dev/myproject"))
        assert slug == "myproject"

    @patch("modastack.monitors.checks.subprocess.run")
    def test_handles_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="gh", timeout=30)
        slug = _repo_slug(Path("/dev/myproject"))
        assert slug == "myproject"

    @patch("modastack.monitors.checks.subprocess.run")
    def test_caches_result(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="org/repo\n"
        )
        slug1 = _repo_slug(Path("/dev/cached"))
        slug2 = _repo_slug(Path("/dev/cached"))
        assert slug1 == slug2 == "org/repo"
        assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# _gh_pr_list
# ---------------------------------------------------------------------------

class TestGhPrList:
    @patch("modastack.monitors.checks.subprocess.run")
    def test_parses_json_output(self, mock_run):
        prs = [{"number": 1, "title": "fix"}]
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(prs)
        )
        result = _gh_pr_list(Path("/dev/repo"), ["number", "title"])
        assert result == prs

    @patch("modastack.monitors.checks.subprocess.run")
    def test_returns_empty_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stderr="error", stdout=""
        )
        assert _gh_pr_list(Path("/dev/repo"), ["number"]) == []

    @patch("modastack.monitors.checks.subprocess.run")
    def test_returns_empty_on_os_error(self, mock_run):
        mock_run.side_effect = OSError("gh not found")
        assert _gh_pr_list(Path("/dev/repo"), ["number"]) == []

    @patch("modastack.monitors.checks.subprocess.run")
    def test_returns_empty_on_bad_json(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="not json"
        )
        assert _gh_pr_list(Path("/dev/repo"), ["number"]) == []

    @patch("modastack.monitors.checks.subprocess.run")
    def test_returns_empty_on_empty_stdout(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout=""
        )
        assert _gh_pr_list(Path("/dev/repo"), ["number"]) == []


# ---------------------------------------------------------------------------
# pr_conflicts
# ---------------------------------------------------------------------------

class TestPrConflicts:
    @patch("modastack.monitors.checks._gh_pr_list")
    @patch("modastack.monitors.checks._repo_slug")
    def test_detects_conflicting_prs(self, mock_slug, mock_prs):
        mock_slug.return_value = "org/repo"
        mock_prs.return_value = [
            {"number": 1, "title": "clean", "url": "u1", "mergeable": "MERGEABLE", "headRefName": "b1"},
            {"number": 2, "title": "broken", "url": "u2", "mergeable": "CONFLICTING", "headRefName": "b2"},
            {"number": 3, "title": "unknown", "url": "u3", "mergeable": "UNKNOWN", "headRefName": "b3"},
        ]
        monitor = MagicMock()
        conditions = pr_conflicts(monitor, [Path("/dev/repo")])
        assert len(conditions) == 1
        assert conditions[0].key == "org/repo#2"
        assert conditions[0].data["pr_number"] == 2
        assert conditions[0].data["title"] == "broken"
        assert conditions[0].data["branch"] == "b2"

    @patch("modastack.monitors.checks._gh_pr_list")
    @patch("modastack.monitors.checks._repo_slug")
    def test_no_conflicts(self, mock_slug, mock_prs):
        mock_slug.return_value = "org/repo"
        mock_prs.return_value = [
            {"number": 1, "mergeable": "MERGEABLE"},
        ]
        assert pr_conflicts(MagicMock(), [Path("/dev/repo")]) == []

    @patch("modastack.monitors.checks._gh_pr_list")
    @patch("modastack.monitors.checks._repo_slug")
    def test_multiple_repos(self, mock_slug, mock_prs):
        mock_slug.side_effect = ["org/a", "org/b"]
        mock_prs.side_effect = [
            [{"number": 1, "title": "", "url": "", "mergeable": "CONFLICTING", "headRefName": ""}],
            [{"number": 2, "title": "", "url": "", "mergeable": "CONFLICTING", "headRefName": ""}],
        ]
        conditions = pr_conflicts(MagicMock(), [Path("/a"), Path("/b")])
        assert len(conditions) == 2
        assert conditions[0].key == "org/a#1"
        assert conditions[1].key == "org/b#2"

    @patch("modastack.monitors.checks._gh_pr_list")
    @patch("modastack.monitors.checks._repo_slug")
    def test_empty_pr_list(self, mock_slug, mock_prs):
        mock_slug.return_value = "org/repo"
        mock_prs.return_value = []
        assert pr_conflicts(MagicMock(), [Path("/dev/repo")]) == []


# ---------------------------------------------------------------------------
# stale_prs
# ---------------------------------------------------------------------------

class TestStalePrs:
    @patch("modastack.monitors.checks._gh_pr_list")
    @patch("modastack.monitors.checks._repo_slug")
    def test_detects_stale_prs(self, mock_slug, mock_prs):
        mock_slug.return_value = "org/repo"
        old_time = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        fresh_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        mock_prs.return_value = [
            {"number": 1, "title": "old", "url": "u1", "updatedAt": old_time, "isDraft": False},
            {"number": 2, "title": "fresh", "url": "u2", "updatedAt": fresh_time, "isDraft": False},
        ]
        monitor = MagicMock()
        monitor.extra = {}
        conditions = stale_prs(monitor, [Path("/dev/repo")])
        assert len(conditions) == 1
        assert conditions[0].key == "org/repo#1"
        assert conditions[0].data["idle_hours"] > 48

    @patch("modastack.monitors.checks._gh_pr_list")
    @patch("modastack.monitors.checks._repo_slug")
    def test_skips_drafts(self, mock_slug, mock_prs):
        mock_slug.return_value = "org/repo"
        old_time = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        mock_prs.return_value = [
            {"number": 1, "title": "draft", "url": "u1", "updatedAt": old_time, "isDraft": True},
        ]
        monitor = MagicMock()
        monitor.extra = {}
        assert stale_prs(monitor, [Path("/dev/repo")]) == []

    @patch("modastack.monitors.checks._gh_pr_list")
    @patch("modastack.monitors.checks._repo_slug")
    def test_custom_threshold(self, mock_slug, mock_prs):
        mock_slug.return_value = "org/repo"
        time_25h_ago = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        mock_prs.return_value = [
            {"number": 1, "title": "recent-ish", "url": "u1",
             "updatedAt": time_25h_ago, "isDraft": False},
        ]
        monitor = MagicMock()
        monitor.extra = {"threshold_hours": "24"}
        conditions = stale_prs(monitor, [Path("/dev/repo")])
        assert len(conditions) == 1

        monitor.extra = {"threshold_hours": "48"}
        conditions = stale_prs(monitor, [Path("/dev/repo")])
        assert len(conditions) == 0

    @patch("modastack.monitors.checks._gh_pr_list")
    @patch("modastack.monitors.checks._repo_slug")
    def test_skips_invalid_date(self, mock_slug, mock_prs):
        mock_slug.return_value = "org/repo"
        mock_prs.return_value = [
            {"number": 1, "title": "bad date", "url": "u1",
             "updatedAt": "invalid", "isDraft": False},
        ]
        monitor = MagicMock()
        monitor.extra = {}
        assert stale_prs(monitor, [Path("/dev/repo")]) == []

    @patch("modastack.monitors.checks._gh_pr_list")
    @patch("modastack.monitors.checks._repo_slug")
    def test_empty_repos(self, mock_slug, mock_prs):
        assert stale_prs(MagicMock(extra={}), []) == []


# ---------------------------------------------------------------------------
# CHECKS registry
# ---------------------------------------------------------------------------

class TestChecksRegistry:
    def test_contains_pr_conflicts(self):
        assert "pr_conflicts" in CHECKS
        assert CHECKS["pr_conflicts"] is pr_conflicts

    def test_contains_stale_prs(self):
        assert "stale_prs" in CHECKS
        assert CHECKS["stale_prs"] is stale_prs
