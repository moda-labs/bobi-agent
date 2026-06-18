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
        with patch("modastack.doctor.bound_root", return_value=tmp_path):
            from modastack.doctor import _check_local_config
            r = _check_local_config()
        assert r.ok

    def test_fails_when_missing(self, tmp_path):
        with patch("modastack.doctor.bound_root", return_value=tmp_path):
            from modastack.doctor import _check_local_config
            r = _check_local_config()
        assert not r.ok
        assert "missing" in r.detail


# --- Single .modastack root ---

class TestCheckSingleRoot:

    def test_passes_with_only_root_modastack(self, tmp_path):
        (tmp_path / ".modastack" / "worktrees" / "x").mkdir(parents=True)
        (tmp_path / "jobtack").mkdir()
        with patch("modastack.doctor.bound_root", return_value=tmp_path):
            from modastack.doctor import _check_single_root
            r = _check_single_root()
        assert r.ok

    def test_flags_stray_in_repo_checkout(self, tmp_path):
        (tmp_path / ".modastack").mkdir()
        (tmp_path / "jobtack" / ".modastack" / "state").mkdir(parents=True)
        (tmp_path / "repos" / "other" / ".modastack").mkdir(parents=True)
        with patch("modastack.doctor.bound_root", return_value=tmp_path):
            from modastack.doctor import _check_single_root
            r = _check_single_root()
        assert not r.ok
        assert "jobtack" in r.detail
        assert "repos/other" in r.detail
        assert "state-only" in r.detail

    def test_flags_stray_at_any_depth(self, tmp_path):
        """Depth must not bound the scan — monorepo packages and worktree
        layouts nest .modastack leftovers 3+ levels down."""
        (tmp_path / ".modastack").mkdir()
        deep = tmp_path / "repos" / "org" / "app" / ".modastack"
        deep.mkdir(parents=True)
        with patch("modastack.doctor.bound_root", return_value=tmp_path):
            from modastack.doctor import _check_single_root
            r = _check_single_root()
        assert not r.ok
        assert "repos/org/app" in r.detail

    def test_classifies_nested_install_as_capture_risk(self, tmp_path):
        """A stray containing agent.yaml CAPTURES root resolution — it must
        be called out separately from removable state-only leftovers."""
        (tmp_path / ".modastack").mkdir()
        nested = tmp_path / "checkout" / ".modastack"
        nested.mkdir(parents=True)
        (nested / "agent.yaml").write_text("name: rogue\n")
        with patch("modastack.doctor.bound_root", return_value=tmp_path):
            from modastack.doctor import _check_single_root
            r = _check_single_root()
        assert not r.ok
        assert "checkout" in r.detail
        assert "CAPTURE" in r.detail
        assert "hijack" in r.hint

    def test_ignores_roots_own_modastack_subtree(self, tmp_path):
        """worktrees/sessions inside the root's own .modastack aren't strays,
        even when a checkout inside it carries a .modastack dir."""
        stray_in_worktree = tmp_path / ".modastack" / "worktrees" / ".modastack"
        stray_in_worktree.mkdir(parents=True)
        with patch("modastack.doctor.bound_root", return_value=tmp_path):
            from modastack.doctor import _check_single_root
            r = _check_single_root()
        assert r.ok

    def test_passes_without_bound_root(self):
        with patch("modastack.doctor.bound_root", return_value=None):
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
        with patch("modastack.doctor.bound_root", return_value=tmp_path):
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
        with patch("modastack.doctor.bound_root", return_value=tmp_path):
            from modastack.doctor import _check_package_requires
            results = _check_package_requires()
        assert len(results) == 1
        assert not results[0].ok
        assert "run setup" in results[0].hint

    def test_no_requires(self, tmp_path):
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "agent.yaml").write_text("entry_point: manager\n")
        with patch("modastack.doctor.bound_root", return_value=tmp_path):
            from modastack.doctor import _check_package_requires
            results = _check_package_requires()
        assert results == []

    def test_no_project_root(self):
        with patch("modastack.doctor.bound_root", return_value=None):
            from modastack.doctor import _check_package_requires
            results = _check_package_requires()
        assert results == []


# --- Codex CLI ---


class TestCheckCodexCLI:

    def test_found(self):
        with patch("shutil.which", return_value="/usr/local/bin/codex"):
            from modastack.doctor import _check_codex_cli
            r = _check_codex_cli()
        assert r.ok
        assert r.name == "Codex CLI"

    def test_not_found(self):
        with patch("shutil.which", return_value=None):
            from modastack.doctor import _check_codex_cli
            r = _check_codex_cli()
        assert not r.ok
        assert "not found" in r.detail
        assert "npm install" in r.hint


class TestCheckCodexAuth:

    def test_not_installed(self):
        with patch("shutil.which", return_value=None):
            from modastack.doctor import _check_codex_auth
            r = _check_codex_auth()
        assert not r.ok
        assert "not installed" in r.detail

    def test_authenticated_with_api_key(self):
        import subprocess
        with patch("shutil.which", return_value="/usr/local/bin/codex"), \
             patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test123"}), \
             patch("subprocess.run") as mock_run:
            # --version succeeds, exec succeeds
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="1.0.0\n", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="hello\n", stderr=""),
            ]
            from modastack.doctor import _check_codex_auth
            r = _check_codex_auth()
        assert r.ok
        assert "API key" in r.detail

    def test_authenticated_with_subscription(self):
        import subprocess
        with patch("shutil.which", return_value="/usr/local/bin/codex"), \
             patch.dict("os.environ", {}, clear=True), \
             patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="1.0.0\n", stderr=""),
                subprocess.CompletedProcess([], 0, stdout="hello\n", stderr=""),
            ]
            from modastack.doctor import _check_codex_auth
            r = _check_codex_auth()
        assert r.ok
        assert "subscription" in r.detail

    def test_auth_failure(self):
        import subprocess
        with patch("shutil.which", return_value="/usr/local/bin/codex"), \
             patch.dict("os.environ", {}, clear=True), \
             patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="1.0.0\n", stderr=""),
                subprocess.CompletedProcess([], 1, stdout="", stderr="Error: auth required, please login"),
            ]
            from modastack.doctor import _check_codex_auth
            r = _check_codex_auth()
        assert not r.ok
        assert "authentication failed" in r.detail
        assert "OPENAI_API_KEY" in r.hint
        assert "codex auth login" in r.hint

    def test_version_check_fails(self):
        import subprocess
        with patch("shutil.which", return_value="/usr/local/bin/codex"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 1, stdout="", stderr="segfault")
            from modastack.doctor import _check_codex_auth
            r = _check_codex_auth()
        assert not r.ok
        assert "unhealthy" in r.detail

    def test_timeout(self):
        import subprocess
        with patch("shutil.which", return_value="/usr/local/bin/codex"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 10)):
            from modastack.doctor import _check_codex_auth
            r = _check_codex_auth()
        assert not r.ok
        assert "timed out" in r.detail
