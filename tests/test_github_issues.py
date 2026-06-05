"""Tests for GitHub Issues adapter."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from modastack.github_issues import scan_github_issues, bootstrap_labels, WORKFLOW_LABELS
from modastack.config import RepoConfig


def _make_repo_config(path, trigger_labels=None, skip_labels=None, project="TEST"):
    return RepoConfig(
        path=path,
        task_tracking="github-issues",
        project=project,
        trigger_labels=trigger_labels or ["agent"],
        skip_labels=skip_labels or ["blocked", "human-only"],
    )


class TestScanGithubIssues:

    @patch("modastack.github_issues.subprocess.run")
    def test_groups_by_state(self, mock_run):
        rc = _make_repo_config(Path("/repo"))

        issues = [
            {
                "number": 1,
                "title": "Todo issue",
                "body": "desc",
                "labels": [{"name": "agent"}, {"name": "status:todo"}],
                "assignees": [{"login": "moda-bot"}],
                "comments": [],
            },
            {
                "number": 2,
                "title": "In progress issue",
                "body": "",
                "labels": [{"name": "agent"}, {"name": "status:in-progress"}],
                "assignees": [{"login": "moda-bot"}],
                "comments": [],
            },
        ]
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issues))

        result = scan_github_issues(rc)
        assert "Todo" in result
        assert "In Progress" in result
        assert len(result["Todo"]) == 1
        assert result["Todo"][0]["identifier"] == "TEST-1"
        assert result["In Progress"][0]["identifier"] == "TEST-2"

    @patch("modastack.github_issues.subprocess.run")
    def test_passes_assignee_me_to_gh(self, mock_run):
        """Verifies gh is called with --assignee @me."""
        rc = _make_repo_config(Path("/repo"))
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps([]))

        scan_github_issues(rc)
        args = mock_run.call_args[0][0]
        assert "--assignee" in args
        assert "@me" in args

    @patch("modastack.github_issues.subprocess.run")
    def test_filters_skip_labels(self, mock_run):
        rc = _make_repo_config(Path("/repo"), skip_labels=["blocked"])

        issues = [
            {
                "number": 1,
                "title": "Blocked agent issue",
                "body": "",
                "labels": [{"name": "agent"}, {"name": "blocked"}],
                "assignees": [{"login": "moda-bot"}],
                "comments": [],
            },
        ]
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issues))

        result = scan_github_issues(rc)
        assert result == {}

    @patch("modastack.github_issues.subprocess.run")
    def test_gh_failure_returns_empty(self, mock_run):
        rc = _make_repo_config(Path("/repo"))
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        assert scan_github_issues(rc) == {}

    @patch("modastack.github_issues.subprocess.run")
    def test_default_state_is_todo(self, mock_run):
        """Issues without a status label default to Todo."""
        rc = _make_repo_config(Path("/repo"))

        issues = [
            {
                "number": 1,
                "title": "No status",
                "body": "",
                "labels": [{"name": "agent"}],
                "assignees": [{"login": "moda-bot"}],
                "comments": [],
            },
        ]
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issues))

        result = scan_github_issues(rc)
        assert "Todo" in result

    @patch("modastack.github_issues.subprocess.run")
    def test_normalizes_comments(self, mock_run):
        rc = _make_repo_config(Path("/repo"))

        issues = [
            {
                "number": 1,
                "title": "With comments",
                "body": "",
                "labels": [{"name": "agent"}],
                "assignees": [{"login": "moda-bot"}],
                "comments": [
                    {"body": "First comment", "author": {"login": "user1"}},
                    {"body": "Second comment", "author": {"login": "user2"}},
                ],
            },
        ]
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issues))

        result = scan_github_issues(rc)
        issue = result["Todo"][0]
        comments = issue["comments"]["nodes"]
        assert len(comments) == 2
        assert comments[0]["body"] == "First comment"

    @patch("modastack.github_issues.subprocess.run")
    def test_uses_path_name_when_no_project(self, mock_run):
        rc = _make_repo_config(Path("/my-repo"), project="")

        issues = [
            {
                "number": 1,
                "title": "Test",
                "body": "",
                "labels": [{"name": "agent"}],
                "assignees": [{"login": "moda-bot"}],
                "comments": [],
            },
        ]
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issues))

        result = scan_github_issues(rc)
        issue = result["Todo"][0]
        assert issue["identifier"].startswith("MY-REP")


class TestBootstrapLabels:

    @patch("modastack.github_issues.subprocess.run")
    def test_creates_missing_labels(self, mock_run):
        existing = [{"name": "bug"}]
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps(existing)),  # list
            MagicMock(returncode=0),  # create status:todo
            MagicMock(returncode=0),  # create status:in-progress
            MagicMock(returncode=0),  # create status:blocked
            MagicMock(returncode=0),  # create status:in-review
            MagicMock(returncode=0),  # create agent
        ]
        actions = bootstrap_labels(Path("/repo"))
        assert any("Created" in a for a in actions)

    @patch("modastack.github_issues.subprocess.run")
    def test_skips_existing_labels(self, mock_run):
        existing = [{"name": l[0]} for l in WORKFLOW_LABELS]
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(existing))

        actions = bootstrap_labels(Path("/repo"))
        assert actions == ["Labels already configured"]

    @patch("modastack.github_issues.subprocess.run")
    def test_handles_list_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="auth error")
        actions = bootstrap_labels(Path("/repo"))
        assert any("Failed" in a for a in actions)
