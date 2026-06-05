"""Tests for modastack doctor health checks."""

import shutil
from unittest.mock import patch, MagicMock

from modastack.browser import CheckResult
from modastack.doctor import run_doctor


class TestRunDoctor:

    @patch("modastack.doctor._check_recent_events")
    @patch("modastack.doctor._check_event_server")
    @patch("modastack.doctor._check_workflows")
    @patch("modastack.doctor._check_repos")
    @patch("modastack.doctor._check_global_config")
    @patch("modastack.doctor._check_claude_cli")
    def test_returns_all_checks(self, m1, m2, m3, m4, m5, m6):
        for m in (m1, m2, m3, m4, m5, m6):
            m.return_value = CheckResult(name="test", ok=True, detail="ok")
        results = run_doctor()
        assert len(results) == 6
        assert all(r.ok for r in results)


class TestCheckClaudeCli:

    def test_passes_when_found(self):
        with patch.object(shutil, "which", return_value="/usr/local/bin/claude"):
            from modastack.doctor import _check_claude_cli
            r = _check_claude_cli()
        assert r.ok
        assert "found" in r.detail

    def test_fails_when_missing(self):
        with patch.object(shutil, "which", return_value=None):
            from modastack.doctor import _check_claude_cli
            r = _check_claude_cli()
        assert not r.ok
        assert "not found" in r.detail


class TestCheckGlobalConfig:

    def test_passes_when_exists(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("repos: []\n")
        with patch("modastack.config.GLOBAL_CONFIG_PATH", config_file):
            from modastack.doctor import _check_global_config
            r = _check_global_config()
        assert r.ok

    def test_fails_when_missing(self, tmp_path):
        missing = tmp_path / "nope.yaml"
        with patch("modastack.config.GLOBAL_CONFIG_PATH", missing):
            from modastack.doctor import _check_global_config
            r = _check_global_config()
        assert not r.ok
        assert "missing" in r.detail


class TestCheckRepos:

    @patch("modastack.doctor.GlobalConfig.load")
    def test_fails_with_no_repos(self, mock_load):
        from modastack.config import GlobalConfig
        mock_load.return_value = GlobalConfig(repos=[])
        from modastack.doctor import _check_repos
        r = _check_repos()
        assert not r.ok
        assert "none registered" in r.detail

    @patch("modastack.doctor.GlobalConfig.load")
    def test_passes_when_all_exist(self, mock_load, tmp_path):
        from modastack.config import GlobalConfig
        repo1 = tmp_path / "repo1"
        repo1.mkdir()
        mock_load.return_value = GlobalConfig(repos=[repo1])
        from modastack.doctor import _check_repos
        r = _check_repos()
        assert r.ok
        assert "1 registered" in r.detail

    @patch("modastack.doctor.GlobalConfig.load")
    def test_fails_when_repo_missing(self, mock_load, tmp_path):
        from modastack.config import GlobalConfig
        missing = tmp_path / "gone"
        mock_load.return_value = GlobalConfig(repos=[missing])
        from modastack.doctor import _check_repos
        r = _check_repos()
        assert not r.ok
        assert "missing" in r.detail.lower()
