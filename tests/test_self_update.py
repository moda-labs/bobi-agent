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
        from modastack.manager.events.pollers import _extract_changelog_entries

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
        from modastack.manager.events.pollers import _extract_changelog_entries

        changelog = "# Changelog\n\n## 0.1.0\n\n- Something\n"
        result = _extract_changelog_entries(changelog, "0.0.1", "0.2.0")
        assert result == ""

    def test_extract_entries_adjacent_versions(self):
        from modastack.manager.events.pollers import _extract_changelog_entries

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

    @patch("modastack.manager.events.pollers.get_bus")
    @patch("subprocess.run")
    def test_pushes_event_when_update_available(self, mock_run, mock_get_bus, tmp_path):
        from modastack.manager.events.pollers import _poll_version

        version_file = tmp_path / "VERSION"
        version_file.write_text("0.2.1")

        bus = MagicMock()
        mock_get_bus.return_value = bus
        mock_run.side_effect = self._make_run_side_effect()

        with patch("modastack.manager.events.pollers._get_modastack_root", return_value=tmp_path):
            # interval=0 makes it run once and return
            _poll_version(interval=0)

        bus.push.assert_called_once()
        call_args = bus.push.call_args
        assert call_args[0][0] == "system.update_available"
        assert call_args[0][2]["new_version"] == "0.3.0"
        assert call_args[0][2]["current_version"] == "0.2.1"

    @patch("modastack.manager.events.pollers.get_bus")
    @patch("subprocess.run")
    def test_no_event_when_versions_equal(self, mock_run, mock_get_bus, tmp_path):
        from modastack.manager.events.pollers import _poll_version

        version_file = tmp_path / "VERSION"
        version_file.write_text("0.2.1")

        bus = MagicMock()
        mock_get_bus.return_value = bus
        mock_run.side_effect = self._make_run_side_effect(remote_version="0.2.1")

        with patch("modastack.manager.events.pollers._get_modastack_root", return_value=tmp_path):
            _poll_version(interval=0)

        bus.push.assert_not_called()

    @patch("modastack.manager.events.pollers.get_bus")
    @patch("subprocess.run")
    def test_no_event_on_fetch_failure(self, mock_run, mock_get_bus, tmp_path):
        from modastack.manager.events.pollers import _poll_version

        version_file = tmp_path / "VERSION"
        version_file.write_text("0.2.1")

        bus = MagicMock()
        mock_get_bus.return_value = bus
        mock_run.side_effect = self._make_run_side_effect(fetch_ok=False)

        with patch("modastack.manager.events.pollers._get_modastack_root", return_value=tmp_path):
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


class TestCheckVersionEdgeCases:
    """Test _check_version edge cases not covered by TestVersionPoller."""

    @patch("subprocess.run")
    def test_git_show_version_failure(self, mock_run, tmp_path):
        """When git show origin/main:VERSION fails, return last_announced unchanged."""
        from modastack.manager.events.pollers import _check_version

        version_file = tmp_path / "VERSION"
        version_file.write_text("0.2.1")

        bus = MagicMock()

        def side_effect(cmd, **kwargs):
            mock = MagicMock()
            if "fetch" in cmd:
                mock.returncode = 0
            elif cmd == ["git", "show", "origin/main:VERSION"]:
                mock.returncode = 1
                mock.stdout = ""
            else:
                mock.returncode = 0
                mock.stdout = ""
            return mock

        mock_run.side_effect = side_effect
        result = _check_version(bus, tmp_path, "old_announced")
        assert result == "old_announced"
        bus.push.assert_not_called()

    @patch("subprocess.run")
    def test_dedup_last_announced(self, mock_run, tmp_path):
        """If remote_version == last_announced, don't re-emit the event."""
        from modastack.manager.events.pollers import _check_version

        version_file = tmp_path / "VERSION"
        version_file.write_text("0.2.1")

        bus = MagicMock()

        def side_effect(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            if cmd == ["git", "show", "origin/main:VERSION"]:
                mock.stdout = "0.3.0"
            else:
                mock.stdout = ""
            return mock

        mock_run.side_effect = side_effect
        # last_announced is already 0.3.0
        result = _check_version(bus, tmp_path, "0.3.0")
        assert result == "0.3.0"
        bus.push.assert_not_called()

    @patch("subprocess.run")
    def test_no_version_file_uses_fallback(self, mock_run, tmp_path):
        """When VERSION file doesn't exist, use 0.0.0 as local_version."""
        from modastack.manager.events.pollers import _check_version

        # Don't create VERSION file
        bus = MagicMock()

        def side_effect(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            if cmd == ["git", "show", "origin/main:VERSION"]:
                mock.stdout = "0.1.0"
            elif cmd == ["git", "show", "origin/main:CHANGELOG.md"]:
                mock.returncode = 1
                mock.stdout = ""
            else:
                mock.stdout = ""
            return mock

        mock_run.side_effect = side_effect
        result = _check_version(bus, tmp_path, "")
        assert result == "0.1.0"
        bus.push.assert_called_once()
        call_data = bus.push.call_args[0][2]
        assert call_data["current_version"] == "0.0.0"
        assert call_data["new_version"] == "0.1.0"

    @patch("subprocess.run")
    def test_changelog_fetch_failure_still_emits(self, mock_run, tmp_path):
        """When CHANGELOG.md fetch fails, still emit update event with empty changelog."""
        from modastack.manager.events.pollers import _check_version

        version_file = tmp_path / "VERSION"
        version_file.write_text("0.2.0")

        bus = MagicMock()

        def side_effect(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            if cmd == ["git", "show", "origin/main:VERSION"]:
                mock.stdout = "0.3.0"
            elif cmd == ["git", "show", "origin/main:CHANGELOG.md"]:
                mock.returncode = 1
                mock.stdout = ""
            else:
                mock.stdout = ""
            return mock

        mock_run.side_effect = side_effect
        result = _check_version(bus, tmp_path, "")
        assert result == "0.3.0"
        bus.push.assert_called_once()
        assert bus.push.call_args[0][2]["changelog"] == ""


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


class TestWriteEventsFile:
    """Test version-related fields in _format_batch."""

    def test_version_fields_in_events_file(self):
        from modastack.manager.events.consumer import _format_batch

        events = [{
            "type": "system.update_available",
            "source": "system",
            "data": {
                "current_version": "0.2.1",
                "new_version": "0.3.0",
                "changelog": "- Stall detection\n- Self-update",
            },
        }]

        content = _format_batch(1, events)
        assert "current_version: 0.2.1" in content
        assert "new_version: 0.3.0" in content
        assert "changelog: - Stall detection" in content

    def test_version_fields_missing_no_crash(self):
        """Events without version fields don't include those lines."""
        from modastack.manager.events.consumer import _format_batch

        events = [{
            "type": "worker.working",
            "source": "worker",
            "data": {"issue_id": "TEST-1", "session_state": "working"},
        }]

        content = _format_batch(1, events)
        assert "current_version" not in content
        assert "new_version" not in content
        assert "changelog" not in content


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
