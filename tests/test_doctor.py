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
        with patch("modastack.paths.bound_root", return_value=tmp_path):
            from modastack.doctor import _check_local_config
            r = _check_local_config()
        assert r.ok

    def test_fails_when_missing(self, tmp_path):
        with patch("modastack.paths.bound_root", return_value=tmp_path):
            from modastack.doctor import _check_local_config
            r = _check_local_config()
        assert not r.ok
        assert "missing" in r.detail


# --- Single .modastack root ---

class TestCheckSingleRoot:

    def test_passes_with_only_root_modastack(self, tmp_path):
        (tmp_path / ".modastack" / "worktrees" / "x").mkdir(parents=True)
        (tmp_path / "jobtack").mkdir()
        with patch("modastack.paths.bound_root", return_value=tmp_path):
            from modastack.doctor import _check_single_root
            r = _check_single_root()
        assert r.ok

    def test_flags_stray_in_repo_checkout(self, tmp_path):
        (tmp_path / ".modastack").mkdir()
        (tmp_path / "jobtack" / ".modastack" / "state").mkdir(parents=True)
        (tmp_path / "repos" / "other" / ".modastack").mkdir(parents=True)
        with patch("modastack.paths.bound_root", return_value=tmp_path):
            from modastack.doctor import _check_single_root
            r = _check_single_root()
        assert not r.ok
        assert "jobtack" in r.detail
        assert "repos/other" in r.detail
        assert "hijack" in r.hint

    def test_ignores_roots_own_modastack_subtree(self, tmp_path):
        """worktrees/sessions inside the root's own .modastack aren't strays,
        even when a checkout inside it carries a .modastack dir."""
        stray_in_worktree = tmp_path / ".modastack" / "worktrees" / ".modastack"
        stray_in_worktree.mkdir(parents=True)
        with patch("modastack.paths.bound_root", return_value=tmp_path):
            from modastack.doctor import _check_single_root
            r = _check_single_root()
        assert r.ok

    def test_passes_without_bound_root(self):
        with patch("modastack.paths.bound_root", return_value=None):
            from modastack.doctor import _check_single_root
            r = _check_single_root()
        assert r.ok


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
        with patch("modastack.paths.bound_root", return_value=tmp_path):
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
        with patch("modastack.paths.bound_root", return_value=tmp_path):
            from modastack.doctor import _check_package_requires
            results = _check_package_requires()
        assert len(results) == 1
        assert not results[0].ok
        assert "run setup" in results[0].hint

    def test_no_requires(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "agent.yaml").write_text("entry_point: manager\n")
        with patch("modastack.paths.bound_root", return_value=tmp_path):
            from modastack.doctor import _check_package_requires
            results = _check_package_requires()
        assert results == []

    def test_no_project_root(self):
        with patch("modastack.paths.bound_root", return_value=None):
            from modastack.doctor import _check_package_requires
            results = _check_package_requires()
        assert results == []
