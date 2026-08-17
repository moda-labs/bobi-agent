"""Tests for bobi.gitutil - the shared git/GitHub shell-outs."""

import subprocess
from unittest.mock import patch

from bobi.gitutil import github_token


class TestGithubToken:
    """github_token prefers the environment, falls back to the gh CLI, and
    reports "no token" as an empty string rather than raising."""

    def test_prefers_the_environment(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok-from-env")

        with patch("subprocess.run") as mock_run:
            assert github_token() == "tok-from-env"

        mock_run.assert_not_called()

    def test_falls_back_to_the_gh_cli(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="tok-from-gh\n", stderr="",
            )
            assert github_token() == "tok-from-gh"

        assert mock_run.call_args[0][0] == ["gh", "auth", "token"]

    def test_empty_when_gh_is_not_authenticated(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="not logged in",
            )
            assert github_token() == ""

    def test_empty_when_gh_is_not_installed(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with patch("subprocess.run", side_effect=FileNotFoundError("gh")):
            assert github_token() == ""

    def test_empty_when_gh_hangs_past_the_timeout(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=5)):
            assert github_token() == ""

    def test_registry_shares_the_same_code_path(self, monkeypatch):
        """bobi.registry kept its private name as a delegate, not a second
        implementation."""
        from bobi import registry

        monkeypatch.setenv("GITHUB_TOKEN", "tok-shared")
        assert registry._github_token() == "tok-shared"
