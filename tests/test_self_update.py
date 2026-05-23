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


class TestChangelogParsing:
    def test_extract_entries_between_versions(self):
        from manager.events.pollers import _extract_changelog_entries

        changelog = """\
# Changelog

## 0.3.0 — 2026-06-01

- New feature A
- New feature B

## 0.2.1 — 2026-05-23

- Fix threading
- Fix orphan detection

## 0.2.0 — 2026-05-20

- Initial release
"""
        result = _extract_changelog_entries(changelog, "0.2.1", "0.3.0")
        assert "New feature A" in result
        assert "New feature B" in result
        assert "Fix threading" not in result

    def test_extract_entries_no_match(self):
        from manager.events.pollers import _extract_changelog_entries

        changelog = "# Changelog\n\n## 0.1.0\n\n- Something\n"
        result = _extract_changelog_entries(changelog, "0.0.1", "0.2.0")
        assert result == ""

    def test_extract_entries_adjacent_versions(self):
        from manager.events.pollers import _extract_changelog_entries

        changelog = """\
# Changelog

## 0.2.1 — 2026-05-23

- Self-updating support

## 0.2.0 — 2026-05-20

- Event-driven architecture
"""
        result = _extract_changelog_entries(changelog, "0.2.0", "0.2.1")
        assert "Self-updating support" in result
        assert "Event-driven" not in result


class TestVersionPoller:
    def _make_run_side_effect(self, remote_version="0.3.0", local_version="0.2.1",
                              fetch_ok=True, changelog="- New stuff"):
        def side_effect(cmd, **kwargs):
            mock = MagicMock()
            if cmd[:4] == ["git", "fetch", "origin", "main"]:
                mock.returncode = 0 if fetch_ok else 1
                mock.stderr = "" if fetch_ok else "network error"
            elif cmd == ["git", "show", "origin/main:VERSION"]:
                mock.returncode = 0
                mock.stdout = remote_version
            elif cmd == ["git", "show", "origin/main:CHANGELOG.md"]:
                mock.returncode = 0
                mock.stdout = f"# Changelog\n\n## {remote_version}\n\n{changelog}\n\n## {local_version}\n\n- Old\n"
            else:
                mock.returncode = 0
                mock.stdout = ""
            return mock
        return side_effect

    @patch("manager.events.pollers.get_bus")
    @patch("subprocess.run")
    def test_pushes_event_when_update_available(self, mock_run, mock_get_bus, tmp_path):
        from manager.events.pollers import _poll_version

        version_file = tmp_path / "VERSION"
        version_file.write_text("0.2.1")

        bus = MagicMock()
        mock_get_bus.return_value = bus
        mock_run.side_effect = self._make_run_side_effect()

        with patch("manager.events.pollers._get_modastack_root", return_value=tmp_path):
            # interval=0 makes it run once and return
            _poll_version(interval=0)

        bus.push.assert_called_once()
        call_args = bus.push.call_args
        assert call_args[0][0] == "system.update_available"
        assert call_args[0][2]["new_version"] == "0.3.0"
        assert call_args[0][2]["current_version"] == "0.2.1"

    @patch("manager.events.pollers.get_bus")
    @patch("subprocess.run")
    def test_no_event_when_versions_equal(self, mock_run, mock_get_bus, tmp_path):
        from manager.events.pollers import _poll_version

        version_file = tmp_path / "VERSION"
        version_file.write_text("0.2.1")

        bus = MagicMock()
        mock_get_bus.return_value = bus
        mock_run.side_effect = self._make_run_side_effect(remote_version="0.2.1")

        with patch("manager.events.pollers._get_modastack_root", return_value=tmp_path):
            _poll_version(interval=0)

        bus.push.assert_not_called()

    @patch("manager.events.pollers.get_bus")
    @patch("subprocess.run")
    def test_no_event_on_fetch_failure(self, mock_run, mock_get_bus, tmp_path):
        from manager.events.pollers import _poll_version

        version_file = tmp_path / "VERSION"
        version_file.write_text("0.2.1")

        bus = MagicMock()
        mock_get_bus.return_value = bus
        mock_run.side_effect = self._make_run_side_effect(fetch_ok=False)

        with patch("manager.events.pollers._get_modastack_root", return_value=tmp_path):
            _poll_version(interval=0)

        bus.push.assert_not_called()


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
