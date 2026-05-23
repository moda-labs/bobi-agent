"""Tests for session management — sync, cleanup."""

from pathlib import Path
from unittest.mock import patch, call, MagicMock

from modastack.session import sync_main_branch, cleanup_worktree


class TestSyncMainBranch:

    @patch("modastack.session.subprocess.run")
    def test_fetches_and_resets(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="refs/remotes/origin/HEAD -> refs/remotes/origin/main\n")
        repo = Path("/tmp/repo")

        result = sync_main_branch(repo)

        assert result is True
        calls = mock_run.call_args_list
        assert any("fetch" in str(c) for c in calls)
        assert any("reset" in str(c) for c in calls)

    @patch("modastack.session.subprocess.run")
    def test_fetch_failure_returns_false(self, mock_run):
        # symbolic-ref succeeds
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="refs/remotes/origin/HEAD\n"),
            MagicMock(returncode=1, stderr="network error"),  # fetch fails
        ]
        result = sync_main_branch(Path("/tmp/repo"))
        assert result is False

    @patch("modastack.session.subprocess.run")
    def test_detects_default_branch(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="refs/remotes/origin/develop\n",
        )
        sync_main_branch(Path("/tmp/repo"))

        reset_call = [c for c in mock_run.call_args_list if "reset" in str(c)]
        assert any("origin/develop" in str(c) for c in reset_call)

    @patch("modastack.session.subprocess.run")
    def test_falls_back_to_main(self, mock_run):
        # symbolic-ref fails (no HEAD set)
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout=""),  # symbolic-ref fails
            MagicMock(returncode=0),              # fetch succeeds
            MagicMock(returncode=0),              # reset succeeds
        ]
        result = sync_main_branch(Path("/tmp/repo"))
        assert result is True
        reset_call = mock_run.call_args_list[2]
        assert "origin/main" in str(reset_call)


class TestCleanupWorktree:

    @patch("modastack.session.time.sleep")
    @patch("modastack.session.kill_session")
    @patch("modastack.session.session_exists", return_value=True)
    @patch("modastack.session.subprocess.run")
    def test_kills_session_first(self, mock_run, mock_exists, mock_kill, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0)
        cleanup_worktree("BET-11", Path("/tmp/repo"))
        mock_kill.assert_called_once_with("BET-11")

    @patch("modastack.session.time.sleep")
    @patch("modastack.session.kill_session")
    @patch("modastack.session.session_exists", return_value=False)
    @patch("modastack.session.subprocess.run")
    def test_skips_kill_if_no_session(self, mock_run, mock_exists, mock_kill, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0)
        cleanup_worktree("BET-11", Path("/tmp/repo"))
        mock_kill.assert_not_called()

    @patch("modastack.session.time.sleep")
    @patch("modastack.session.kill_session")
    @patch("modastack.session.session_exists", return_value=False)
    @patch("modastack.session.subprocess.run")
    def test_removes_branch(self, mock_run, mock_exists, mock_kill, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0)
        cleanup_worktree("BET-11", Path("/tmp/repo"))

        branch_calls = [c for c in mock_run.call_args_list if "branch" in str(c)]
        assert len(branch_calls) >= 1
        assert "agent/bet-11" in str(branch_calls[0])

    @patch("modastack.session.time.sleep")
    @patch("modastack.session.kill_session")
    @patch("modastack.session.session_exists", return_value=False)
    @patch("modastack.session.subprocess.run")
    def test_handles_missing_worktree(self, mock_run, mock_exists, mock_kill, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0)
        # worktree path doesn't exist — should not error
        cleanup_worktree("BET-99", Path("/tmp/nonexistent"))
