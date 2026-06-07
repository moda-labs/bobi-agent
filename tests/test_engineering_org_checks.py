"""Unit tests for engineering_org monitor checks — pr_conflicts, stale_prs,
project_health, and the CHECKS registry."""

import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

import importlib.util
import sys

from modastack.monitors.schema import Condition, Monitor
from modastack.monitors.registry import MonitorRegistry

_checks_path = Path(__file__).parent.parent / "agents" / "engineering_org" / "monitors" / "github_checks.py"
_spec = importlib.util.spec_from_file_location("modastack.monitors.engineering_org_checks", _checks_path)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["modastack.monitors.engineering_org_checks"] = _mod
_spec.loader.exec_module(_mod)

CHECKS = _mod.CHECKS
_repo_slug = _mod._repo_slug
_gh_pr_list = _mod._gh_pr_list
_gh_issue_list = _mod._gh_issue_list
_gh_run_list_main = _mod._gh_run_list_main
_parse_iso = _mod._parse_iso
_title_words = _mod._title_words
_slug_cache = _mod._slug_cache
pr_conflicts = _mod.pr_conflicts
stale_prs = _mod.stale_prs
project_health = _mod.project_health

_MOD_PATH = "modastack.monitors.engineering_org_checks"


@pytest.fixture(autouse=True)
def clear_slug_cache():
    """Ensure slug cache doesn't leak between tests."""
    _slug_cache.clear()
    yield
    _slug_cache.clear()


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

    def test_contains_project_health(self):
        assert "project_health" in CHECKS
        assert CHECKS["project_health"] is project_health


# ---------------------------------------------------------------------------
# Monitor definition loads via MonitorRegistry
# ---------------------------------------------------------------------------

class TestMonitorDefinitionLoads:
    def test_defaults_yaml_loads_project_health(self):
        defaults_path = Path(__file__).parent.parent / "agents" / "engineering_org" / "monitors" / "defaults.yaml"
        raw = yaml.safe_load(defaults_path.read_text()) or {}
        monitors = raw.get("monitors", [])
        names = [m["name"] for m in monitors]
        assert "project_health" in names
        ph = next(m for m in monitors if m["name"] == "project_health")
        assert ph["check"] == "project_health"
        assert ph["event"] == "monitor/project.health_check"
        assert ph["interval"] == "30m"

    def test_all_three_monitors_present(self):
        defaults_path = Path(__file__).parent.parent / "agents" / "engineering_org" / "monitors" / "defaults.yaml"
        raw = yaml.safe_load(defaults_path.read_text()) or {}
        monitors = raw.get("monitors", [])
        names = {m["name"] for m in monitors}
        assert names == {"pr-conflict-check", "stale-pr-check", "project_health"}

    def test_registry_loads_engineering_org_defaults(self):
        """MonitorRegistry.load with agent_name='engineering_org' picks up defaults."""
        reg = MonitorRegistry.load(agent_name="engineering_org")
        names = {m.name for m in reg.effective_monitors()}
        assert "project_health" in names
        assert "pr-conflict-check" in names
        assert "stale-pr-check" in names


# ---------------------------------------------------------------------------
# _title_words helper
# ---------------------------------------------------------------------------

class TestTitleWords:
    def test_extracts_meaningful_words(self):
        words = _title_words("Fix the broken login page")
        assert "fix" in words
        assert "broken" in words
        assert "login" in words
        assert "page" in words
        # "the" is a stop word
        assert "the" not in words

    def test_filters_short_words(self):
        words = _title_words("An AI bug")
        assert "bug" in words
        # "an" is stop word, "ai" is only 2 chars
        assert "an" not in words
        assert "ai" not in words

    def test_empty_title(self):
        assert _title_words("") == set()


# ---------------------------------------------------------------------------
# project_health — CI on main
# ---------------------------------------------------------------------------

class TestProjectHealthCiMain:
    @patch(f"{_MOD_PATH}._gh_issue_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_pr_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_run_list_main")
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_detects_failing_ci_on_main(self, mock_slug, mock_runs, mock_prs, mock_issues):
        mock_runs.return_value = [{"conclusion": "failure"}]
        conditions = project_health(MagicMock(), [Path("/dev/repo")])
        ci_conditions = [c for c in conditions if c.key.startswith("ci:")]
        assert len(ci_conditions) == 1
        assert ci_conditions[0].key == "ci:org/repo:main"
        assert ci_conditions[0].data["check"] == "ci_main"

    @patch(f"{_MOD_PATH}._gh_issue_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_pr_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_run_list_main")
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_no_condition_when_ci_passes(self, mock_slug, mock_runs, mock_prs, mock_issues):
        mock_runs.return_value = [{"conclusion": "success"}]
        conditions = project_health(MagicMock(), [Path("/dev/repo")])
        ci_conditions = [c for c in conditions if c.key.startswith("ci:")]
        assert ci_conditions == []

    @patch(f"{_MOD_PATH}._gh_issue_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_pr_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_run_list_main")
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_no_condition_when_no_runs(self, mock_slug, mock_runs, mock_prs, mock_issues):
        mock_runs.return_value = []
        conditions = project_health(MagicMock(), [Path("/dev/repo")])
        ci_conditions = [c for c in conditions if c.key.startswith("ci:")]
        assert ci_conditions == []


# ---------------------------------------------------------------------------
# project_health — unreviewed PRs
# ---------------------------------------------------------------------------

class TestProjectHealthUnreviewedPrs:
    @patch(f"{_MOD_PATH}._gh_issue_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_run_list_main", return_value=[])
    @patch(f"{_MOD_PATH}._gh_pr_list")
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_detects_unreviewed_pr_over_24h(self, mock_slug, mock_prs, mock_runs, mock_issues):
        old_time = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
        mock_prs.return_value = [
            {"number": 5, "title": "new feature", "url": "u5",
             "createdAt": old_time, "isDraft": False,
             "reviews": [], "statusCheckRollup": []},
        ]
        conditions = project_health(MagicMock(), [Path("/dev/repo")])
        unreviewed = [c for c in conditions if c.key.startswith("unreviewed:")]
        assert len(unreviewed) == 1
        assert unreviewed[0].key == "unreviewed:org/repo#5"
        assert unreviewed[0].data["age_hours"] >= 36

    @patch(f"{_MOD_PATH}._gh_issue_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_run_list_main", return_value=[])
    @patch(f"{_MOD_PATH}._gh_pr_list")
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_skips_reviewed_pr(self, mock_slug, mock_prs, mock_runs, mock_issues):
        old_time = (datetime.now(timezone.utc) - timedelta(hours=36)).isoformat()
        mock_prs.return_value = [
            {"number": 5, "title": "reviewed", "url": "u5",
             "createdAt": old_time, "isDraft": False,
             "reviews": [{"state": "APPROVED"}], "statusCheckRollup": []},
        ]
        conditions = project_health(MagicMock(), [Path("/dev/repo")])
        unreviewed = [c for c in conditions if c.key.startswith("unreviewed:")]
        assert unreviewed == []

    @patch(f"{_MOD_PATH}._gh_issue_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_run_list_main", return_value=[])
    @patch(f"{_MOD_PATH}._gh_pr_list")
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_skips_fresh_unreviewed_pr(self, mock_slug, mock_prs, mock_runs, mock_issues):
        recent_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        mock_prs.return_value = [
            {"number": 5, "title": "just opened", "url": "u5",
             "createdAt": recent_time, "isDraft": False,
             "reviews": [], "statusCheckRollup": []},
        ]
        conditions = project_health(MagicMock(), [Path("/dev/repo")])
        unreviewed = [c for c in conditions if c.key.startswith("unreviewed:")]
        assert unreviewed == []

    @patch(f"{_MOD_PATH}._gh_issue_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_run_list_main", return_value=[])
    @patch(f"{_MOD_PATH}._gh_pr_list")
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_skips_draft_prs(self, mock_slug, mock_prs, mock_runs, mock_issues):
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        mock_prs.return_value = [
            {"number": 5, "title": "wip", "url": "u5",
             "createdAt": old_time, "isDraft": True,
             "reviews": [], "statusCheckRollup": []},
        ]
        conditions = project_health(MagicMock(), [Path("/dev/repo")])
        unreviewed = [c for c in conditions if c.key.startswith("unreviewed:")]
        assert unreviewed == []


# ---------------------------------------------------------------------------
# project_health — broken builds on PR branches
# ---------------------------------------------------------------------------

class TestProjectHealthBrokenBuilds:
    @patch(f"{_MOD_PATH}._gh_issue_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_run_list_main", return_value=[])
    @patch(f"{_MOD_PATH}._gh_pr_list")
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_detects_failing_checks_on_pr(self, mock_slug, mock_prs, mock_runs, mock_issues):
        mock_prs.return_value = [
            {"number": 10, "title": "broken build", "url": "u10",
             "createdAt": datetime.now(timezone.utc).isoformat(), "isDraft": False,
             "reviews": [{"state": "COMMENTED"}],
             "statusCheckRollup": [
                 {"conclusion": "SUCCESS"},
                 {"conclusion": "FAILURE"},
                 {"conclusion": "FAILURE"},
             ]},
        ]
        conditions = project_health(MagicMock(), [Path("/dev/repo")])
        ci_fail = [c for c in conditions if c.key.startswith("ci-fail:")]
        assert len(ci_fail) == 1
        assert ci_fail[0].key == "ci-fail:org/repo#10"
        assert ci_fail[0].data["failed_checks"] == 2

    @patch(f"{_MOD_PATH}._gh_issue_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_run_list_main", return_value=[])
    @patch(f"{_MOD_PATH}._gh_pr_list")
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_no_condition_when_checks_pass(self, mock_slug, mock_prs, mock_runs, mock_issues):
        mock_prs.return_value = [
            {"number": 10, "title": "all good", "url": "u10",
             "createdAt": datetime.now(timezone.utc).isoformat(), "isDraft": False,
             "reviews": [{"state": "APPROVED"}],
             "statusCheckRollup": [
                 {"conclusion": "SUCCESS"},
             ]},
        ]
        conditions = project_health(MagicMock(), [Path("/dev/repo")])
        ci_fail = [c for c in conditions if c.key.startswith("ci-fail:")]
        assert ci_fail == []


# ---------------------------------------------------------------------------
# project_health — duplicate issues
# ---------------------------------------------------------------------------

class TestProjectHealthDuplicateIssues:
    @patch(f"{_MOD_PATH}._gh_pr_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_run_list_main", return_value=[])
    @patch(f"{_MOD_PATH}._gh_issue_list")
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_detects_duplicate_issues(self, mock_slug, mock_issues, mock_runs, mock_prs):
        mock_issues.return_value = [
            {"number": 1, "title": "Login page throws error when submitting form", "url": "u1"},
            {"number": 5, "title": "Login page throws error on form submission", "url": "u5"},
        ]
        conditions = project_health(MagicMock(), [Path("/dev/repo")])
        dups = [c for c in conditions if c.key.startswith("dup:")]
        assert len(dups) == 1
        assert dups[0].key == "dup:org/repo#1+5"
        assert dups[0].data["check"] == "duplicate_issues"
        assert dups[0].data["issue_a"] == 1
        assert dups[0].data["issue_b"] == 5

    @patch(f"{_MOD_PATH}._gh_pr_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_run_list_main", return_value=[])
    @patch(f"{_MOD_PATH}._gh_issue_list")
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_no_duplicates_for_different_issues(self, mock_slug, mock_issues, mock_runs, mock_prs):
        mock_issues.return_value = [
            {"number": 1, "title": "Login page throws error", "url": "u1"},
            {"number": 2, "title": "Deploy pipeline broken on staging", "url": "u2"},
        ]
        conditions = project_health(MagicMock(), [Path("/dev/repo")])
        dups = [c for c in conditions if c.key.startswith("dup:")]
        assert dups == []

    @patch(f"{_MOD_PATH}._gh_pr_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_run_list_main", return_value=[])
    @patch(f"{_MOD_PATH}._gh_issue_list")
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_canonical_key_ordering(self, mock_slug, mock_issues, mock_runs, mock_prs):
        """Duplicate key always uses lower#higher regardless of iteration order."""
        mock_issues.return_value = [
            {"number": 10, "title": "Login page throws error when submitting form", "url": "u10"},
            {"number": 3, "title": "Login page throws error on form submission", "url": "u3"},
        ]
        conditions = project_health(MagicMock(), [Path("/dev/repo")])
        dups = [c for c in conditions if c.key.startswith("dup:")]
        assert len(dups) == 1
        assert dups[0].key == "dup:org/repo#3+10"

    @patch(f"{_MOD_PATH}._gh_pr_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_run_list_main", return_value=[])
    @patch(f"{_MOD_PATH}._gh_issue_list")
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_empty_issues(self, mock_slug, mock_issues, mock_runs, mock_prs):
        mock_issues.return_value = []
        conditions = project_health(MagicMock(), [Path("/dev/repo")])
        dups = [c for c in conditions if c.key.startswith("dup:")]
        assert dups == []


# ---------------------------------------------------------------------------
# project_health — composite (all conditions at once)
# ---------------------------------------------------------------------------

class TestProjectHealthComposite:
    @patch(f"{_MOD_PATH}._gh_issue_list")
    @patch(f"{_MOD_PATH}._gh_pr_list")
    @patch(f"{_MOD_PATH}._gh_run_list_main")
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_returns_all_condition_types(self, mock_slug, mock_runs, mock_prs, mock_issues):
        mock_runs.return_value = [{"conclusion": "failure"}]
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        mock_prs.return_value = [
            {"number": 1, "title": "old unreviewed", "url": "u1",
             "createdAt": old_time, "isDraft": False,
             "reviews": [],
             "statusCheckRollup": [{"conclusion": "FAILURE"}]},
        ]
        mock_issues.return_value = [
            {"number": 10, "title": "Login page throws error when submitting form", "url": "u10"},
            {"number": 20, "title": "Login page throws error on form submission", "url": "u20"},
        ]
        conditions = project_health(MagicMock(), [Path("/dev/repo")])
        keys = {c.key.split(":")[0] for c in conditions}
        assert "ci" in keys        # CI on main
        assert "unreviewed" in keys # unreviewed PR
        assert "ci-fail" in keys    # broken build on PR
        assert "dup" in keys        # duplicate issues

    @patch(f"{_MOD_PATH}._gh_issue_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_pr_list", return_value=[])
    @patch(f"{_MOD_PATH}._gh_run_list_main", return_value=[])
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_empty_repo_returns_nothing(self, mock_slug, mock_runs, mock_prs, mock_issues):
        conditions = project_health(MagicMock(), [Path("/dev/repo")])
        assert conditions == []

    def test_empty_projects_list(self):
        conditions = project_health(MagicMock(), [])
        assert conditions == []


# ---------------------------------------------------------------------------
# Existing checks still work (smoke tests)
# ---------------------------------------------------------------------------

class TestExistingChecksStillWork:
    @patch(f"{_MOD_PATH}._gh_pr_list")
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_pr_conflicts_detects_conflicting(self, mock_slug, mock_prs):
        mock_prs.return_value = [
            {"number": 2, "title": "broken", "url": "u2",
             "mergeable": "CONFLICTING", "headRefName": "b2"},
        ]
        conditions = pr_conflicts(MagicMock(), [Path("/dev/repo")])
        assert len(conditions) == 1
        assert conditions[0].key == "org/repo#2"

    @patch(f"{_MOD_PATH}._gh_pr_list")
    @patch(f"{_MOD_PATH}._repo_slug", return_value="org/repo")
    def test_stale_prs_detects_stale(self, mock_slug, mock_prs):
        old_time = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        mock_prs.return_value = [
            {"number": 1, "title": "old", "url": "u1",
             "updatedAt": old_time, "isDraft": False},
        ]
        monitor = MagicMock()
        monitor.extra = {}
        conditions = stale_prs(monitor, [Path("/dev/repo")])
        assert len(conditions) == 1
        assert conditions[0].key == "org/repo#1"
