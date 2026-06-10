"""Tests for modastack doctor health checks."""

from pathlib import Path
from unittest.mock import patch

import pytest


# --- CheckResult ---

from modastack.doctor import CheckResult


class TestCheckResult:
    def test_ok_result(self):
        r = CheckResult("Test", ok=True, detail="all good")
        assert r.ok
        assert r.detail == "all good"

    def test_failed_result(self):
        r = CheckResult("Test", ok=False, detail="missing", hint="fix it")
        assert not r.ok
        assert r.hint == "fix it"


# --- Claude CLI ---

class TestCheckCLI:
    def test_found(self):
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            from modastack.doctor import _check_claude_cli
            r = _check_claude_cli()
        assert r.ok


# --- Project ---


class TestCheckProjectConfig:

    def test_passes_when_exists(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text("entry_point: manager\nevent_server_url: https://events.test\n")
        with patch("modastack.sdk.get_project_root", return_value=tmp_path):
            from modastack.doctor import _check_local_config
            r = _check_local_config()
        assert r.ok

    def test_fails_when_missing(self, tmp_path):
        with patch("modastack.sdk.get_project_root", return_value=tmp_path):
            from modastack.doctor import _check_local_config
            r = _check_local_config()
        assert not r.ok
        assert "missing" in r.detail
