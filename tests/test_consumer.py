"""Tests for consumer — auto-clone on startup."""

from pathlib import Path
from unittest.mock import patch, MagicMock

from modastack.config import GlobalConfig, RepoEntry


class TestEnsureRepos:

    @patch("manager.events.consumer.subprocess.run")
    def test_skips_existing(self, mock_run):
        from manager.events.consumer import _ensure_repos

        with patch("manager.events.consumer.GlobalConfig.load") as mock_load:
            entry = RepoEntry(path=Path("/tmp"), remote="org/repo")
            mock_load.return_value = GlobalConfig(repos=[entry])
            _ensure_repos()
            mock_run.assert_not_called()

    @patch("modastack.setup.install_skill_symlinks", return_value=[])
    @patch("manager.events.consumer.subprocess.run")
    def test_clones_missing_with_remote(self, mock_run, mock_skills):
        from manager.events.consumer import _ensure_repos

        mock_run.return_value = MagicMock(returncode=0)
        missing_path = Path("/tmp/nonexistent_repo_xyz_test")

        with patch("manager.events.consumer.GlobalConfig.load") as mock_load:
            entry = RepoEntry(path=missing_path, remote="org/repo")
            mock_load.return_value = GlobalConfig(repos=[entry])
            _ensure_repos()
            mock_run.assert_called_once()
            assert "gh" in str(mock_run.call_args)

    @patch("manager.events.consumer.subprocess.run")
    def test_warns_missing_no_remote(self, mock_run):
        from manager.events.consumer import _ensure_repos

        missing_path = Path("/tmp/nonexistent_repo_xyz_test")

        with patch("manager.events.consumer.GlobalConfig.load") as mock_load:
            entry = RepoEntry(path=missing_path)
            mock_load.return_value = GlobalConfig(repos=[entry])
            _ensure_repos()
            mock_run.assert_not_called()

    @patch("manager.events.consumer.subprocess.run")
    def test_handles_clone_failure(self, mock_run):
        from manager.events.consumer import _ensure_repos

        mock_run.return_value = MagicMock(returncode=1, stderr="not found")
        missing_path = Path("/tmp/nonexistent_repo_xyz_test")

        with patch("manager.events.consumer.GlobalConfig.load") as mock_load:
            entry = RepoEntry(path=missing_path, remote="org/repo")
            mock_load.return_value = GlobalConfig(repos=[entry])
            # Should not raise
            _ensure_repos()
