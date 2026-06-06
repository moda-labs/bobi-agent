"""Tests for modastack doctor health checks."""

import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

from modastack.browser import CheckResult
from modastack.doctor import run_doctor


class TestRunDoctor:

    @patch("modastack.doctor._check_recent_events")
    @patch("modastack.doctor._check_event_server")
    @patch("modastack.doctor._check_workflows")
    @patch("modastack.doctor._check_local_config")
    @patch("modastack.doctor._check_project_config")
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


class TestCheckProjectConfig:

    def test_passes_when_exists(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("task_tracking:\n  project: TEST\n")
        with patch("modastack.sdk.get_project_root", return_value=tmp_path):
            from modastack.doctor import _check_project_config
            r = _check_project_config()
        assert r.ok

    def test_fails_when_no_repo(self):
        with patch("modastack.sdk.get_project_root", return_value=None):
            from modastack.doctor import _check_project_config
            r = _check_project_config()
        assert not r.ok
        assert "not inside" in r.detail


class TestCheckMachineConfig:

    def test_passes_when_exists(self, tmp_path):
        machine_config = tmp_path / "config.yaml"
        machine_config.write_text("event_server:\n  url: https://events.test\n")
        with patch("modastack.config._machine_config_path", return_value=machine_config):
            from modastack.doctor import _check_local_config
            r = _check_local_config()
        assert r.ok

    def test_fails_when_missing(self, tmp_path):
        missing = tmp_path / "nonexistent.yaml"
        with patch("modastack.config._machine_config_path", return_value=missing):
            from modastack.doctor import _check_local_config
            r = _check_local_config()
        assert not r.ok
        assert "missing" in r.detail
