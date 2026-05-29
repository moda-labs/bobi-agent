"""Tests for manager session management.

Unit tests mock subprocess/filesystem. Integration tests at the bottom
use real tmux sessions and the actual hook script — skip in CI with:
    pytest tests/test_manager_session.py --ignore-glob='*Integration*'
"""

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from modastack.manager.session import (
    _session_exists,
    _get_saved_session_id,
    _save_session_id,
    _read_last_activity,
    _activity_line_count,
    _clear_activity_log,
    _send_keys,
    detect_state,
    capture,
    inject,
    is_alive,
    ACTIVITY_LOG,
    SESSION_NAME,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def activity_log(tmp_path, monkeypatch):
    """Point ACTIVITY_LOG at a temp file."""
    log_path = tmp_path / "activity.jsonl"
    monkeypatch.setattr("modastack.manager.session.ACTIVITY_LOG", log_path)
    return log_path


def _write_activity(log_path: Path, event: str, ts: float = None, session_id: str = "ses_test"):
    """Helper: append an activity entry."""
    entry = {"event": event, "ts": ts or time.time(), "session_id": session_id}
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# _session_exists
# ---------------------------------------------------------------------------

class TestSessionExists:

    @patch("modastack.manager.session.subprocess.run")
    def test_returns_true_when_session_exists(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert _session_exists() is True

    @patch("modastack.manager.session.subprocess.run")
    def test_returns_false_when_no_session(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        assert _session_exists() is False


# ---------------------------------------------------------------------------
# Session ID persistence
# ---------------------------------------------------------------------------

class TestSessionId:

    def test_save_and_load(self, tmp_path, monkeypatch):
        id_path = tmp_path / "session_id"
        monkeypatch.setattr("modastack.manager.session.SESSION_ID_PATH", id_path)
        _save_session_id("ses_abc123")
        assert _get_saved_session_id() == "ses_abc123"

    def test_load_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("modastack.manager.session.SESSION_ID_PATH", tmp_path / "nonexistent")
        assert _get_saved_session_id() == ""


# ---------------------------------------------------------------------------
# Activity log helpers
# ---------------------------------------------------------------------------

class TestReadLastActivity:

    def test_returns_none_when_file_missing(self, activity_log):
        assert _read_last_activity() is None

    def test_returns_none_when_file_empty(self, activity_log):
        activity_log.write_text("")
        assert _read_last_activity() is None

    def test_reads_last_entry(self, activity_log):
        _write_activity(activity_log, "UserPromptSubmit")
        _write_activity(activity_log, "Stop")
        result = _read_last_activity()
        assert result["event"] == "Stop"

    def test_reads_single_entry(self, activity_log):
        _write_activity(activity_log, "UserPromptSubmit")
        result = _read_last_activity()
        assert result["event"] == "UserPromptSubmit"

    def test_handles_corrupt_json(self, activity_log):
        activity_log.write_text('{"event": "Stop"}\nnot json\n')
        # Last line is corrupt — should return None
        assert _read_last_activity() is None

    def test_handles_corrupt_json_with_valid_last(self, activity_log):
        activity_log.write_text('not json\n{"event": "Stop", "ts": 1.0, "session_id": "x"}\n')
        result = _read_last_activity()
        assert result["event"] == "Stop"

    def test_preserves_all_fields(self, activity_log):
        _write_activity(activity_log, "UserPromptSubmit", ts=123.456, session_id="ses_xyz")
        result = _read_last_activity()
        assert result["event"] == "UserPromptSubmit"
        assert result["ts"] == 123.456
        assert result["session_id"] == "ses_xyz"


class TestActivityLineCount:

    def test_zero_when_missing(self, activity_log):
        assert _activity_line_count() == 0

    def test_zero_when_empty(self, activity_log):
        activity_log.write_text("")
        assert _activity_line_count() == 0

    def test_counts_lines(self, activity_log):
        _write_activity(activity_log, "UserPromptSubmit")
        _write_activity(activity_log, "Stop")
        _write_activity(activity_log, "UserPromptSubmit")
        assert _activity_line_count() == 3


class TestClearActivityLog:

    def test_creates_parent_dirs(self, tmp_path, monkeypatch):
        log_path = tmp_path / "deep" / "nested" / "activity.jsonl"
        monkeypatch.setattr("modastack.manager.session.ACTIVITY_LOG", log_path)
        _clear_activity_log()
        assert log_path.exists()
        assert log_path.read_text() == ""

    def test_truncates_existing(self, activity_log):
        _write_activity(activity_log, "Stop")
        _write_activity(activity_log, "UserPromptSubmit")
        assert _activity_line_count() == 2
        _clear_activity_log()
        assert _activity_line_count() == 0
        assert activity_log.read_text() == ""


# ---------------------------------------------------------------------------
# detect_state — activity-log based
# ---------------------------------------------------------------------------

class TestDetectState:

    @patch("modastack.manager.session._session_exists", return_value=False)
    def test_exited_when_no_session(self, _):
        assert detect_state() == "exited"

    @patch("modastack.manager.session._session_exists", return_value=True)
    def test_waiting_input_on_stop(self, _, activity_log):
        _write_activity(activity_log, "UserPromptSubmit")
        _write_activity(activity_log, "Stop")
        assert detect_state() == "waiting_input"

    @patch("modastack.manager.session._session_exists", return_value=True)
    def test_working_on_prompt_submit(self, _, activity_log):
        _write_activity(activity_log, "UserPromptSubmit")
        assert detect_state() == "working"

    @patch("modastack.manager.session._session_exists", return_value=True)
    def test_unknown_when_no_activity(self, _, activity_log):
        assert detect_state() == "unknown"

    @patch("modastack.manager.session._session_exists", return_value=True)
    def test_unknown_on_empty_file(self, _, activity_log):
        activity_log.write_text("")
        assert detect_state() == "unknown"

    @patch("modastack.manager.session._session_exists", return_value=True)
    def test_unknown_on_unrecognized_event(self, _, activity_log):
        _write_activity(activity_log, "SessionStart")
        assert detect_state() == "unknown"

    @patch("modastack.manager.session._session_exists", return_value=True)
    def test_state_reflects_most_recent(self, _, activity_log):
        _write_activity(activity_log, "Stop")
        assert detect_state() == "waiting_input"
        _write_activity(activity_log, "UserPromptSubmit")
        assert detect_state() == "working"
        _write_activity(activity_log, "Stop")
        assert detect_state() == "waiting_input"


# ---------------------------------------------------------------------------
# _send_keys — tmux interface
# ---------------------------------------------------------------------------

class TestSendKeys:
    """_send_keys now delegates to tmux.send_text — mock at that level.
    Low-level tests (newline collapsing, enter sending, length routing)
    are in test_tmux.py::TestSendTextRouting.
    """

    @patch("modastack.manager.session.send_text", return_value=True)
    def test_success(self, mock_send):
        assert _send_keys("hello world") is True
        mock_send.assert_called_once_with("moda-manager", "hello world", verify=False)

    @patch("modastack.manager.session.send_text", return_value=False)
    def test_returns_false_on_failure(self, mock_send):
        assert _send_keys("hello") is False


# ---------------------------------------------------------------------------
# inject — send-keys + UserPromptSubmit confirmation
# ---------------------------------------------------------------------------

class TestInject:

    @patch("modastack.manager.session.time.sleep")
    @patch("modastack.manager.session._send_keys", return_value=False)
    def test_returns_false_when_send_keys_fails(self, _, __, activity_log):
        assert inject("test") is False

    @patch("modastack.manager.session.time.sleep")
    @patch("modastack.manager.session._send_keys")
    def test_returns_true_when_confirmed(self, mock_send, _, activity_log):
        # Simulate hook writing UserPromptSubmit when send-keys fires
        def send_side_effect(text):
            _write_activity(activity_log, "UserPromptSubmit")
            return True
        mock_send.side_effect = send_side_effect
        assert inject("test") is True

    @patch("modastack.manager.session.time.sleep")
    @patch("modastack.manager.session._send_keys", return_value=True)
    def test_returns_false_when_no_confirmation(self, mock_send, mock_sleep, activity_log):
        # No activity written — inject should time out
        # Override sleep to not actually wait
        result = inject("test")
        assert result is False
        assert mock_sleep.call_count == 30  # waited all 30 iterations

    @patch("modastack.manager.session.time.sleep")
    @patch("modastack.manager.session._send_keys", return_value=True)
    def test_ignores_stale_activity(self, _, __, activity_log):
        # Pre-existing Stop entry from before injection
        _write_activity(activity_log, "Stop")
        pre_count = _activity_line_count()
        # No NEW UserPromptSubmit appears — should fail
        # (the Stop is stale, not a response to our injection)
        assert inject("test") is False

    @patch("modastack.manager.session.time.sleep")
    @patch("modastack.manager.session._send_keys")
    def test_detects_new_activity_after_stale(self, mock_send, _, activity_log):
        # Pre-existing entry
        _write_activity(activity_log, "Stop")

        # Hook fires UserPromptSubmit when send-keys is called
        def send_side_effect(text):
            _write_activity(activity_log, "UserPromptSubmit")
            return True
        mock_send.side_effect = send_side_effect
        assert inject("test") is True


# ---------------------------------------------------------------------------
# capture — tmux pane (debugging only)
# ---------------------------------------------------------------------------

class TestCapture:

    @patch("modastack.manager.session.subprocess.run")
    def test_captures_pane_content(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="line1\nline2\n")
        result = capture(lines=10)
        assert result == "line1\nline2\n"
        cmd = mock_run.call_args[0][0]
        assert "-10" in cmd

    @patch("modastack.manager.session.subprocess.run")
    def test_returns_empty_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="can't find pane")
        result = capture(lines=10)
        assert result == ""


# ---------------------------------------------------------------------------
# is_alive
# ---------------------------------------------------------------------------

class TestIsAlive:

    @patch("modastack.manager.session._session_exists", return_value=True)
    def test_alive(self, _):
        assert is_alive() is True

    @patch("modastack.manager.session._session_exists", return_value=False)
    def test_not_alive(self, _):
        assert is_alive() is False


# ---------------------------------------------------------------------------
# Hook script — verify the actual script writes correct JSON
# ---------------------------------------------------------------------------

class TestHookScript:
    """Test the actual .claude/hooks/session-state.sh script."""

    HOOK_SCRIPT = Path(__file__).parent.parent / ".claude" / "hooks" / "session-state.sh"

    @pytest.fixture
    def hook_log(self, tmp_path, monkeypatch):
        """Point the hook script at a temp directory via HOME override."""
        manager_dir = tmp_path / ".modastack" / "manager"
        manager_dir.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        return manager_dir / "activity.jsonl"

    def test_script_exists_and_is_executable(self):
        assert self.HOOK_SCRIPT.exists(), f"Hook script not found at {self.HOOK_SCRIPT}"
        import os
        assert os.access(self.HOOK_SCRIPT, os.X_OK), "Hook script is not executable"

    def test_writes_user_prompt_submit(self, hook_log):
        import subprocess
        input_json = json.dumps({
            "hook_event_name": "UserPromptSubmit",
            "session_id": "ses_test123",
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": "/tmp",
        })
        result = subprocess.run(
            ["bash", str(self.HOOK_SCRIPT)],
            input=input_json, capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Hook script failed: {result.stderr}"
        assert hook_log.exists(), "Activity log not created"

        entry = json.loads(hook_log.read_text().strip())
        assert entry["event"] == "UserPromptSubmit"
        assert entry["session_id"] == "ses_test123"
        assert isinstance(entry["ts"], float)

    def test_writes_stop(self, hook_log):
        import subprocess
        input_json = json.dumps({
            "hook_event_name": "Stop",
            "session_id": "ses_test456",
        })
        result = subprocess.run(
            ["bash", str(self.HOOK_SCRIPT)],
            input=input_json, capture_output=True, text=True,
        )
        assert result.returncode == 0
        entry = json.loads(hook_log.read_text().strip())
        assert entry["event"] == "Stop"
        assert entry["session_id"] == "ses_test456"

    def test_appends_multiple_entries(self, hook_log):
        import subprocess
        for event in ["UserPromptSubmit", "Stop", "UserPromptSubmit"]:
            input_json = json.dumps({"hook_event_name": event, "session_id": "ses_x"})
            subprocess.run(
                ["bash", str(self.HOOK_SCRIPT)],
                input=input_json, capture_output=True, text=True,
            )
        lines = hook_log.read_text().strip().splitlines()
        assert len(lines) == 3
        assert json.loads(lines[0])["event"] == "UserPromptSubmit"
        assert json.loads(lines[1])["event"] == "Stop"
        assert json.loads(lines[2])["event"] == "UserPromptSubmit"

    def test_creates_directory_if_missing(self, tmp_path, monkeypatch):
        import subprocess
        # Point HOME at a fresh dir with no .modastack
        fresh = tmp_path / "fresh_home"
        fresh.mkdir()
        monkeypatch.setenv("HOME", str(fresh))

        input_json = json.dumps({"hook_event_name": "Stop", "session_id": "ses_new"})
        result = subprocess.run(
            ["bash", str(self.HOOK_SCRIPT)],
            input=input_json, capture_output=True, text=True,
        )
        assert result.returncode == 0
        log_path = fresh / ".modastack" / "manager" / "activity.jsonl"
        assert log_path.exists()

    def test_handles_missing_session_id(self, hook_log):
        import subprocess
        input_json = json.dumps({"hook_event_name": "Stop"})
        result = subprocess.run(
            ["bash", str(self.HOOK_SCRIPT)],
            input=input_json, capture_output=True, text=True,
        )
        assert result.returncode == 0
        entry = json.loads(hook_log.read_text().strip())
        assert entry["event"] == "Stop"
        assert entry["session_id"] == ""


# ---------------------------------------------------------------------------
# Settings configuration
# ---------------------------------------------------------------------------

class TestHookSettings:
    """Verify .claude/settings.json has hooks properly configured."""

    SETTINGS_PATH = Path(__file__).parent.parent / ".claude" / "settings.json"

    def test_settings_has_hooks(self):
        settings = json.loads(self.SETTINGS_PATH.read_text())
        assert "hooks" in settings

    def test_user_prompt_submit_hook(self):
        settings = json.loads(self.SETTINGS_PATH.read_text())
        hooks = settings["hooks"]
        assert "UserPromptSubmit" in hooks
        hook_entries = hooks["UserPromptSubmit"]
        assert len(hook_entries) >= 1
        cmd = hook_entries[0]["hooks"][0]["command"]
        assert "session-state.sh" in cmd

    def test_stop_hook(self):
        settings = json.loads(self.SETTINGS_PATH.read_text())
        hooks = settings["hooks"]
        assert "Stop" in hooks
        hook_entries = hooks["Stop"]
        assert len(hook_entries) >= 1
        cmd = hook_entries[0]["hooks"][0]["command"]
        assert "session-state.sh" in cmd

    def test_hook_timeouts_are_short(self):
        settings = json.loads(self.SETTINGS_PATH.read_text())
        for event_name in ("UserPromptSubmit", "Stop"):
            for group in settings["hooks"][event_name]:
                for hook in group["hooks"]:
                    assert hook.get("timeout", 30) <= 10, \
                        f"{event_name} hook timeout too high — should be fast"


# ---------------------------------------------------------------------------
# install_hooks — CLI hook installation
# ---------------------------------------------------------------------------

class TestInstallHooks:

    def test_installs_hook_script(self, tmp_path):
        from modastack.cli import install_hooks
        actions = install_hooks(tmp_path)
        hook_path = tmp_path / ".claude" / "hooks" / "session-state.sh"
        assert hook_path.exists()
        import os
        assert os.access(hook_path, os.X_OK)
        assert any("session-state.sh" in a for a in actions)

    def test_creates_settings_with_hooks(self, tmp_path):
        from modastack.cli import install_hooks
        install_hooks(tmp_path)
        settings_path = tmp_path / ".claude" / "settings.json"
        assert settings_path.exists()
        settings = json.loads(settings_path.read_text())
        assert "UserPromptSubmit" in settings["hooks"]
        assert "Stop" in settings["hooks"]

    def test_merges_into_existing_settings(self, tmp_path):
        from modastack.cli import install_hooks
        # Pre-existing settings with other config
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(json.dumps({
            "permissions": {"allow": ["Bash"]},
            "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
        }))

        install_hooks(tmp_path)

        settings = json.loads(settings_path.read_text())
        # Original config preserved
        assert settings["permissions"]["allow"] == ["Bash"]
        assert "PreToolUse" in settings["hooks"]
        # New hooks added
        assert "UserPromptSubmit" in settings["hooks"]
        assert "Stop" in settings["hooks"]

    def test_idempotent(self, tmp_path):
        from modastack.cli import install_hooks
        install_hooks(tmp_path)
        first = json.loads((tmp_path / ".claude" / "settings.json").read_text())

        actions = install_hooks(tmp_path)
        second = json.loads((tmp_path / ".claude" / "settings.json").read_text())

        # No "Configured hooks" action on second run — already present
        assert not any("Configured hooks" in a for a in actions)
        assert first["hooks"] == second["hooks"]

    def test_installed_hook_script_works(self, tmp_path, monkeypatch):
        """Verify the copied hook script actually writes valid JSON."""
        from modastack.cli import install_hooks
        install_hooks(tmp_path)

        import subprocess
        monkeypatch.setenv("HOME", str(tmp_path))
        hook_path = tmp_path / ".claude" / "hooks" / "session-state.sh"
        input_json = json.dumps({"hook_event_name": "Stop", "session_id": "ses_test"})
        result = subprocess.run(
            ["bash", str(hook_path)],
            input=input_json, capture_output=True, text=True,
        )
        assert result.returncode == 0
        activity = tmp_path / ".modastack" / "manager" / "activity.jsonl"
        entry = json.loads(activity.read_text().strip())
        assert entry["event"] == "Stop"


# ---------------------------------------------------------------------------
# Integration: tmux + hooks end-to-end
#
# These tests start real tmux sessions and verify the full pipeline.
# They require tmux to be installed. Skip if not available.
# ---------------------------------------------------------------------------

def _has_tmux():
    import shutil
    return shutil.which("tmux") is not None


@pytest.mark.skipif(not _has_tmux(), reason="tmux not installed")
class TestTmuxIntegration:
    """Real tmux sessions — no mocks."""

    TEST_SESSION = "modastack-test-session"

    @pytest.fixture(autouse=True)
    def cleanup_session(self):
        yield
        import subprocess
        subprocess.run(["tmux", "kill-session", "-t", self.TEST_SESSION], capture_output=True)

    def test_send_keys_to_real_session(self):
        import subprocess
        # Start a session running cat (waits for stdin)
        subprocess.run([
            "tmux", "new-session", "-d", "-s", self.TEST_SESSION,
            "-x", "200", "-y", "50", "cat",
        ])
        time.sleep(0.5)

        # Send text
        result = subprocess.run(
            ["tmux", "send-keys", "-t", self.TEST_SESSION, "-l", "hello world"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0

        subprocess.run(["tmux", "send-keys", "-t", self.TEST_SESSION, "Enter"])
        time.sleep(0.5)

        # Capture pane and verify text appeared
        cap = subprocess.run(
            ["tmux", "capture-pane", "-t", self.TEST_SESSION, "-p"],
            capture_output=True, text=True,
        )
        assert "hello world" in cap.stdout

    def test_send_keys_fails_for_nonexistent_session(self):
        import subprocess
        result = subprocess.run(
            ["tmux", "send-keys", "-t", "nonexistent-session-xyz", "-l", "test"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0

    def test_has_session_returns_correct_values(self):
        import subprocess
        # Before creating
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.TEST_SESSION],
            capture_output=True,
        )
        assert result.returncode != 0

        # Create it
        subprocess.run([
            "tmux", "new-session", "-d", "-s", self.TEST_SESSION, "sleep", "30",
        ])
        time.sleep(0.3)

        # After creating
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.TEST_SESSION],
            capture_output=True,
        )
        assert result.returncode == 0


@pytest.mark.skipif(not _has_tmux(), reason="tmux not installed")
class TestHookPipelineIntegration:
    """End-to-end: tmux session → hook script → activity log.

    Uses a fake "claude" script that reads input, fires the hook script
    to simulate what Claude Code's harness does, then waits for more input.
    This validates the full inject → hook → detect_state pipeline.
    """

    TEST_SESSION = "modastack-test-hooks"

    @pytest.fixture
    def pipeline(self, tmp_path, monkeypatch):
        import subprocess

        activity_log = tmp_path / ".modastack" / "manager" / "activity.jsonl"
        monkeypatch.setattr("modastack.manager.session.ACTIVITY_LOG", activity_log)
        monkeypatch.setattr("modastack.manager.session.SESSION_NAME", self.TEST_SESSION)
        monkeypatch.setenv("HOME", str(tmp_path))

        hook_script = Path(__file__).parent.parent / ".claude" / "hooks" / "session-state.sh"

        # Fake claude: reads a line, fires UserPromptSubmit hook, sleeps, fires Stop hook
        fake_claude = tmp_path / "fake_claude.sh"
        fake_claude.write_text(f"""#!/bin/bash
while true; do
    read -r line
    if [ -z "$line" ]; then continue; fi
    echo '{{"hook_event_name":"UserPromptSubmit","session_id":"ses_fake"}}' | bash {hook_script}
    sleep 2
    echo '{{"hook_event_name":"Stop","session_id":"ses_fake"}}' | bash {hook_script}
done
""")
        fake_claude.chmod(0o755)

        subprocess.run(["tmux", "kill-session", "-t", self.TEST_SESSION], capture_output=True)
        subprocess.run([
            "tmux", "new-session", "-d", "-s", self.TEST_SESSION,
            "-x", "200", "-y", "50",
            "bash", str(fake_claude),
        ])
        time.sleep(1)

        yield {"activity_log": activity_log, "session": self.TEST_SESSION}

        subprocess.run(["tmux", "kill-session", "-t", self.TEST_SESSION], capture_output=True)

    def test_inject_triggers_hooks(self, pipeline):
        from modastack.tmux import send_text
        activity_log = pipeline["activity_log"]

        assert not activity_log.exists() or activity_log.stat().st_size == 0

        send_text(self.TEST_SESSION, "do something", verify=False)
        time.sleep(4)

        assert activity_log.exists()
        lines = activity_log.read_text().strip().splitlines()
        assert len(lines) >= 2

        events = [json.loads(l)["event"] for l in lines]
        assert "UserPromptSubmit" in events
        assert "Stop" in events

    def test_detect_state_reflects_hook_events(self, pipeline):
        from modastack.tmux import send_text
        from modastack.manager.session import detect_state

        # Before any injection — no activity log, falls back to pane detection
        assert detect_state() in ("unknown", "exited")

        send_text(self.TEST_SESSION, "first prompt", verify=False)
        # Wait for full cycle: UserPromptSubmit + 2s sleep + Stop
        time.sleep(5)
        assert detect_state() == "waiting_input"

    def test_multiple_inject_cycles(self, pipeline):
        from modastack.tmux import send_text
        from modastack.manager.session import detect_state
        activity_log = pipeline["activity_log"]

        for i in range(3):
            send_text(self.TEST_SESSION, f"prompt {i}", verify=False)
            time.sleep(6)

        lines = activity_log.read_text().strip().splitlines()
        events = [json.loads(l)["event"] for l in lines]

        # Should have 3 pairs of UserPromptSubmit + Stop
        assert events.count("UserPromptSubmit") == 3
        assert events.count("Stop") == 3
        assert detect_state() == "waiting_input"
