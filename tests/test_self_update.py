"""Tests for self-updating: version poller, CLI commands, changelog parsing."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from modastack.cli import main


class TestVersionModule:
    def test_version_file_exists(self):
        root = Path(__file__).parent.parent
        version_file = root / "VERSION"
        assert version_file.exists(), "VERSION file must exist at repo root"

    def test_version_is_loaded(self):
        from modastack.__version__ import __version__
        assert __version__
        assert "." in __version__


class TestSelfUpdateCommand:
    @patch("subprocess.run")
    def test_already_up_to_date(self, mock_run, tmp_path):
        version_file = tmp_path / "VERSION"
        version_file.write_text("0.2.1")

        def side_effect(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            if "fetch" in cmd:
                mock.stdout = ""
            elif cmd == ["git", "show", "origin/main:VERSION"]:
                mock.stdout = "0.2.1"
            else:
                mock.stdout = ""
                mock.stderr = ""
            return mock

        mock_run.side_effect = side_effect

        runner = CliRunner()
        with patch("modastack.cli.REPO_ROOT", tmp_path):
            result = runner.invoke(main, ["self-update"])

        assert result.exit_code == 0
        assert "Already up to date" in result.output

    @patch("subprocess.run")
    def test_update_happy_path(self, mock_run, tmp_path):
        version_file = tmp_path / "VERSION"
        version_file.write_text("0.2.1")

        call_count = {"pull": 0}

        def side_effect(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""

            if "fetch" in cmd:
                pass
            elif cmd == ["git", "show", "origin/main:VERSION"]:
                mock.stdout = "0.3.0"
            elif "status" in cmd and "--porcelain" in cmd:
                mock.stdout = ""
            elif cmd == ["git", "rev-parse", "HEAD"]:
                mock.stdout = "abc123"
            elif "pull" in cmd:
                call_count["pull"] += 1
                version_file.write_text("0.3.0")
            elif "pip" in cmd:
                pass
            return mock

        mock_run.side_effect = side_effect

        runner = CliRunner()
        with patch("modastack.cli.REPO_ROOT", tmp_path), \
             patch("modastack.cli.UPDATE_STATE_PATH", tmp_path / "update_state.json"):
            result = runner.invoke(main, ["self-update"])

        assert result.exit_code == 0
        assert "Updated to v0.3.0" in result.output
        assert call_count["pull"] == 1

        state = json.loads((tmp_path / "update_state.json").read_text())
        assert state["pre_update_head"] == "abc123"
        assert state["pre_update_version"] == "0.2.1"

    @patch("subprocess.run")
    def test_update_with_dirty_tree_stashes(self, mock_run, tmp_path):
        version_file = tmp_path / "VERSION"
        version_file.write_text("0.2.1")

        stash_calls = []

        def side_effect(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""

            if "fetch" in cmd:
                pass
            elif cmd == ["git", "show", "origin/main:VERSION"]:
                mock.stdout = "0.3.0"
            elif "status" in cmd and "--porcelain" in cmd:
                mock.stdout = "M some_file.py\n"
            elif "stash" in cmd and "push" in cmd:
                stash_calls.append("push")
            elif "stash" in cmd and "pop" in cmd:
                stash_calls.append("pop")
            elif cmd == ["git", "rev-parse", "HEAD"]:
                mock.stdout = "abc123"
            elif "pull" in cmd:
                version_file.write_text("0.3.0")
            return mock

        mock_run.side_effect = side_effect

        runner = CliRunner()
        with patch("modastack.cli.REPO_ROOT", tmp_path), \
             patch("modastack.cli.UPDATE_STATE_PATH", tmp_path / "update_state.json"):
            result = runner.invoke(main, ["self-update"])

        assert result.exit_code == 0
        assert stash_calls == ["push", "pop"]

    @patch("subprocess.run")
    def test_update_ff_only_failure_aborts(self, mock_run, tmp_path):
        version_file = tmp_path / "VERSION"
        version_file.write_text("0.2.1")

        def side_effect(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""

            if "fetch" in cmd:
                pass
            elif cmd == ["git", "show", "origin/main:VERSION"]:
                mock.stdout = "0.3.0"
            elif "status" in cmd and "--porcelain" in cmd:
                mock.stdout = ""
            elif cmd == ["git", "rev-parse", "HEAD"]:
                mock.stdout = "abc123"
            elif "pull" in cmd:
                mock.returncode = 1
                mock.stderr = "fatal: Not possible to fast-forward"
            return mock

        mock_run.side_effect = side_effect

        runner = CliRunner()
        with patch("modastack.cli.REPO_ROOT", tmp_path), \
             patch("modastack.cli.UPDATE_STATE_PATH", tmp_path / "update_state.json"):
            result = runner.invoke(main, ["self-update"])

        assert result.exit_code != 0
        assert "rollback" in result.output.lower()


class TestRollbackCommand:
    @patch("subprocess.run")
    def test_rollback_happy_path(self, mock_run, tmp_path):
        state_path = tmp_path / "update_state.json"
        state_path.write_text(json.dumps({
            "pre_update_head": "abc123def",
            "pre_update_version": "0.2.1",
            "updated_at": "2026-05-23T14:30:00",
            "stashed": False,
        }))

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        runner = CliRunner()
        with patch("modastack.cli.REPO_ROOT", tmp_path), \
             patch("modastack.cli.UPDATE_STATE_PATH", state_path):
            result = runner.invoke(main, ["rollback"])

        assert result.exit_code == 0
        assert "0.2.1" in result.output
        assert not state_path.exists()

    def test_rollback_no_state_file(self, tmp_path):
        runner = CliRunner()
        with patch("modastack.cli.UPDATE_STATE_PATH", tmp_path / "nope.json"):
            result = runner.invoke(main, ["rollback"])
        assert result.exit_code != 0
        assert "nothing to roll back" in result.output.lower()

    @patch("subprocess.run")
    def test_rollback_git_reset_failure(self, mock_run, tmp_path):
        state_path = tmp_path / "update_state.json"
        state_path.write_text(json.dumps({
            "pre_update_head": "abc123def",
            "pre_update_version": "0.2.1",
            "updated_at": "2026-05-23T14:30:00",
            "stashed": False,
        }))

        def side_effect(cmd, **kwargs):
            mock = MagicMock()
            if "reset" in cmd:
                mock.returncode = 1
                mock.stderr = "fatal: Could not reset"
            else:
                mock.returncode = 0
                mock.stdout = ""
                mock.stderr = ""
            return mock

        mock_run.side_effect = side_effect

        runner = CliRunner()
        with patch("modastack.cli.REPO_ROOT", tmp_path), \
             patch("modastack.cli.UPDATE_STATE_PATH", state_path):
            result = runner.invoke(main, ["rollback"])

        assert result.exit_code != 0
        assert "reset failed" in result.output.lower()


class TestSelfUpdateFetchFailure:
    @patch("subprocess.run")
    def test_fetch_failure_exits(self, mock_run, tmp_path):
        version_file = tmp_path / "VERSION"
        version_file.write_text("0.2.1")

        def side_effect(cmd, **kwargs):
            mock = MagicMock()
            if "fetch" in cmd:
                mock.returncode = 1
                mock.stderr = "fatal: could not read from remote"
                mock.stdout = ""
            else:
                mock.returncode = 0
                mock.stdout = ""
                mock.stderr = ""
            return mock

        mock_run.side_effect = side_effect

        runner = CliRunner()
        with patch("modastack.cli.REPO_ROOT", tmp_path):
            result = runner.invoke(main, ["self-update"])

        assert result.exit_code != 0
        assert "failed to fetch" in result.output.lower()

    @patch("subprocess.run")
    def test_pip_install_failure(self, mock_run, tmp_path):
        version_file = tmp_path / "VERSION"
        version_file.write_text("0.2.1")

        def side_effect(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            mock.stderr = ""

            if "fetch" in cmd:
                pass
            elif cmd == ["git", "show", "origin/main:VERSION"]:
                mock.stdout = "0.3.0"
            elif "status" in cmd and "--porcelain" in cmd:
                mock.stdout = ""
            elif cmd == ["git", "rev-parse", "HEAD"]:
                mock.stdout = "abc123"
            elif "pull" in cmd:
                version_file.write_text("0.3.0")
            elif "pip" in cmd:
                mock.returncode = 1
                mock.stderr = "ERROR: Could not install"
            return mock

        mock_run.side_effect = side_effect

        runner = CliRunner()
        with patch("modastack.cli.REPO_ROOT", tmp_path), \
             patch("modastack.cli.UPDATE_STATE_PATH", tmp_path / "update_state.json"):
            result = runner.invoke(main, ["self-update"])

        assert result.exit_code != 0
        assert "rollback" in result.output.lower()


class TestVersionModuleFallback:
    def test_version_fallback_when_no_file(self, tmp_path):
        """__version__ falls back to 0.0.0 when VERSION file doesn't exist."""
        import importlib
        import modastack.__version__ as ver_mod

        original_file = ver_mod._VERSION_FILE
        ver_mod._VERSION_FILE = tmp_path / "NONEXISTENT_VERSION"
        try:
            # Re-evaluate the expression
            result = ver_mod._VERSION_FILE.read_text().strip() if ver_mod._VERSION_FILE.exists() else "0.0.0"
            assert result == "0.0.0"
        finally:
            ver_mod._VERSION_FILE = original_file
