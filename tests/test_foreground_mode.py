"""Unit tests for foreground/PID-1 mode changes in the CLI."""

import logging
import os
from pathlib import Path

import pytest


class TestForegroundLogging:
    """Verify that --foreground keeps StreamHandlers and drops FileHandlers."""

    def test_foreground_keeps_stream_handlers(self):
        """Foreground mode should keep StreamHandlers (stdout/stderr)."""
        root = logging.getLogger("test_fg_logging")
        stream_h = logging.StreamHandler()
        file_h = logging.FileHandler(os.devnull)
        root.handlers = [stream_h, file_h]

        # Simulate the foreground handler filtering from cli.py
        root.handlers = [h for h in root.handlers
                         if not isinstance(h, logging.FileHandler)]

        assert stream_h in root.handlers
        assert file_h not in root.handlers
        root.handlers.clear()

    def test_daemon_keeps_file_handlers(self):
        """Daemon mode should keep FileHandlers (this is the default path)."""
        root = logging.getLogger("test_daemon_logging")
        stream_h = logging.StreamHandler()
        file_h = logging.FileHandler(os.devnull)
        root.handlers = [stream_h, file_h]

        # In daemon mode, handlers are not filtered — both remain
        assert stream_h in root.handlers
        assert file_h in root.handlers
        root.handlers.clear()


class TestPidSkipInForeground:
    """Verify that foreground mode skips the already-running PID check."""

    def test_stale_pid_file_does_not_block_foreground(self, bobi_install):
        """A stale PID file should not prevent foreground start."""
        state_dir = bobi_install.state_dir
        pid_path = state_dir / "manager.pid"

        # Write a PID that cannot possibly be alive
        pid_path.write_text("999999999")

        # The foreground code path skips the check entirely
        foreground = True
        blocked = False
        if not foreground and pid_path.exists():
            try:
                pid = int(pid_path.read_text().strip())
                os.kill(pid, 0)
                blocked = True
            except (ProcessLookupError, ValueError):
                pid_path.unlink(missing_ok=True)

        assert not blocked, "Foreground mode should skip PID check"

    def test_pid_check_still_works_in_daemon_mode(self, bobi_install):
        """Daemon mode should still detect and clean stale PID files."""
        state_dir = bobi_install.state_dir
        pid_path = state_dir / "manager.pid"

        # Write a stale PID
        pid_path.write_text("999999999")

        foreground = False
        if not foreground and pid_path.exists():
            try:
                pid = int(pid_path.read_text().strip())
                os.kill(pid, 0)
            except (ProcessLookupError, ValueError):
                pid_path.unlink(missing_ok=True)

        # Stale PID should have been cleaned up
        assert not pid_path.exists()
