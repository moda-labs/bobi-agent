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


# --- Package requires ---


class TestCheckPackageRequires:

    def _write_config(self, tmp_path, requires_yaml):
        from textwrap import dedent
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "agent.yaml").write_text(dedent(f"""
            entry_point: manager
            requires:
{requires_yaml}
        """))

    def test_all_pass(self, tmp_path):
        self._write_config(tmp_path, """\
              - name: good-dep
                check: "true"
                fix: "install it" """)
        with patch("modastack.sdk.get_project_root", return_value=tmp_path):
            from modastack.doctor import _check_package_requires
            results = _check_package_requires()
        assert len(results) == 1
        assert results[0].ok
        assert "good-dep" in results[0].name

    def test_check_fails(self, tmp_path):
        self._write_config(tmp_path, """\
              - name: broken-dep
                check: "false"
                why: "needed for tests"
                fix: "run setup" """)
        with patch("modastack.sdk.get_project_root", return_value=tmp_path):
            from modastack.doctor import _check_package_requires
            results = _check_package_requires()
        assert len(results) == 1
        assert not results[0].ok
        assert "run setup" in results[0].hint

    def test_no_requires(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "agent.yaml").write_text("entry_point: manager\n")
        with patch("modastack.sdk.get_project_root", return_value=tmp_path):
            from modastack.doctor import _check_package_requires
            results = _check_package_requires()
        assert results == []

    def test_no_project_root(self):
        with patch("modastack.sdk.get_project_root", return_value=None):
            from modastack.doctor import _check_package_requires
            results = _check_package_requires()
        assert results == []
