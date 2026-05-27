"""Tests for tmux primitives — pure-function state detection, send routing, paste verification."""

import re
from unittest.mock import patch, MagicMock

from modastack.tmux import (
    determine_agent_state,
    _normalize_for_match,
    _verify_paste,
    send_text,
    LONG_MESSAGE_THRESHOLD,
)


class TestDetermineAgentState:

    def test_waiting_input(self):
        pane = "\n".join([
            "Some output",
            "❯ ",
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
        ])
        result = determine_agent_state(pane, has_children=True)
        assert result["state"] == "waiting_input"

    def test_working(self):
        pane = "\n".join([
            "Running tests...",
            "test_foo PASSED",
            "test_bar PASSED",
        ])
        result = determine_agent_state(pane, has_children=True)
        assert result["state"] == "working"

    def test_exited_no_children(self):
        pane = "\n".join(["some old output", "still here"])
        result = determine_agent_state(pane, has_children=False)
        assert result["state"] == "exited"

    def test_exited_empty_pane_no_children(self):
        result = determine_agent_state("", has_children=False)
        assert result["state"] == "exited"

    def test_unknown_empty_pane_with_children(self):
        result = determine_agent_state("", has_children=True)
        assert result["state"] == "unknown"

    def test_permission_blocked_yn(self):
        pane = "\n".join([
            "Working on something...",
            "Reading file.py",
            "  Allow Read for /path/to/file? (y/n)",
        ])
        result = determine_agent_state(pane, has_children=True)
        assert result["state"] == "permission_blocked"
        assert "(y/n)" in result["prompt_line"]

    def test_permission_blocked_allow_once(self):
        pane = "\n".join([
            "Working on something...",
            "Yes, allow once",
            "No, deny",
        ])
        result = determine_agent_state(pane, has_children=True)
        assert result["state"] == "permission_blocked"

    def test_asking_question_numbered_options(self):
        pane = "\n".join([
            "Which option?",
            "  1. Option A",
            "  2. Option B",
            "  3. Option C",
        ])
        result = determine_agent_state(pane, has_children=True)
        assert result["state"] == "asking_question"
        assert len(result["options"]) == 3

    def test_asking_question_beats_permission(self):
        pane = "\n".join([
            "Which option?",
            "  1. Option A",
            "  2. Option B",
            "  3. Allow all",
        ])
        result = determine_agent_state(pane, has_children=True)
        assert result["state"] == "asking_question"

    def test_working_no_prompt(self):
        pane = "\n".join([
            "● Building the feature...",
            "  Read 3 files",
            "  Edit(src/main.py)",
        ])
        result = determine_agent_state(pane, has_children=True)
        assert result["state"] == "working"


class TestNormalizeForMatch:

    def test_strips_non_alnum(self):
        assert _normalize_for_match("Hello, World!") == "helloworld"

    def test_lowercases(self):
        assert _normalize_for_match("ABC123") == "abc123"

    def test_strips_ansi(self):
        assert _normalize_for_match("\x1b[32mgreen\x1b[0m") == "32mgreen0m"

    def test_empty(self):
        assert _normalize_for_match("") == ""


class TestVerifyPaste:

    @patch("modastack.tmux.capture_pane")
    @patch("modastack.tmux.time.monotonic")
    def test_pasted_text_indicator(self, mock_time, mock_capture):
        mock_time.side_effect = [0.0, 0.1]
        mock_capture.return_value = "some text\n[Pasted text ...]\nmore"
        assert _verify_paste("test-session", "hello world") is True

    @patch("modastack.tmux.capture_pane")
    @patch("modastack.tmux.time.monotonic")
    def test_fuzzy_match_tail(self, mock_time, mock_capture):
        mock_time.side_effect = [0.0, 0.1]
        mock_capture.return_value = "❯ implement the feature and push"
        assert _verify_paste("test-session", "implement the feature and push") is True

    @patch("modastack.tmux.capture_pane")
    @patch("modastack.tmux.time.monotonic")
    def test_timeout_returns_false(self, mock_time, mock_capture):
        mock_time.side_effect = [0.0, 10.0]
        mock_capture.return_value = "totally different content"
        assert _verify_paste("test-session", "hello world") is False

    @patch("modastack.tmux.capture_pane")
    @patch("modastack.tmux.time.monotonic")
    def test_empty_message_always_true(self, mock_time, mock_capture):
        mock_time.side_effect = [0.0, 0.1]
        mock_capture.return_value = "whatever"
        assert _verify_paste("test-session", "") is True


class TestSendTextRouting:

    @patch("modastack.tmux._send_enter")
    @patch("modastack.tmux._verify_paste", return_value=True)
    @patch("modastack.tmux._send_short", return_value=True)
    def test_short_message_uses_send_keys(self, mock_short, mock_verify, mock_enter):
        result = send_text("test-session", "short message")
        assert result is True
        mock_short.assert_called_once()

    @patch("modastack.tmux._send_enter")
    @patch("modastack.tmux._verify_paste", return_value=True)
    @patch("modastack.tmux._send_long", return_value=True)
    def test_long_message_uses_load_buffer(self, mock_long, mock_verify, mock_enter):
        long_msg = "x" * (LONG_MESSAGE_THRESHOLD + 100)
        result = send_text("test-session", long_msg)
        assert result is True
        mock_long.assert_called_once()

    @patch("modastack.tmux._send_enter")
    @patch("modastack.tmux._verify_paste", return_value=True)
    @patch("modastack.tmux._send_short", return_value=True)
    def test_collapses_newlines(self, mock_short, mock_verify, mock_enter):
        send_text("test-session", "line one\nline two\nline three")
        call_args = mock_short.call_args[0]
        assert call_args[1] == "line one line two line three"

    @patch("modastack.tmux._send_enter")
    @patch("modastack.tmux._send_short", return_value=False)
    def test_returns_false_on_send_failure(self, mock_short, mock_enter):
        result = send_text("test-session", "hello")
        assert result is False
        mock_enter.assert_not_called()

    @patch("modastack.tmux._send_enter")
    @patch("modastack.tmux._verify_paste", return_value=False)
    @patch("modastack.tmux._send_short", return_value=True)
    def test_sends_enter_even_if_verify_fails(self, mock_short, mock_verify, mock_enter):
        result = send_text("test-session", "hello")
        assert result is True
        mock_enter.assert_called_once()

    @patch("modastack.tmux._send_enter")
    @patch("modastack.tmux._send_short", return_value=True)
    def test_skip_verify(self, mock_short, mock_enter):
        result = send_text("test-session", "hello", verify=False)
        assert result is True
