"""Tests for ManagerSession.inject_capture — routes through inbox.deliver()."""

from pathlib import Path
from unittest.mock import patch

from modastack.manager.session import ManagerSession, set_default_session
from modastack.manager import session as session_mod


def test_inject_capture_returns_response():
    s = ManagerSession(project_path=Path("/tmp/test-repo"))
    with patch("modastack.inbox.deliver", return_value=(True, "reply-to-A")):
        ok, response = s.inject_capture("event A")
    assert ok is True
    assert response == "reply-to-A"


def test_inject_capture_failure_returns_empty():
    s = ManagerSession(project_path=Path("/tmp/test-repo"))
    with patch("modastack.inbox.deliver", return_value=(False, "session not found")):
        ok, response = s.inject_capture("event A")
    assert ok is False
    assert response == "session not found"


def test_inject_wrapper_returns_bool():
    s = ManagerSession(project_path=Path("/tmp/test-repo"))
    with patch("modastack.inbox.deliver", return_value=(True, "")):
        result = s.inject("event A")
    assert result is True


def test_module_level_wrappers_delegate():
    s = ManagerSession(project_path=Path("/tmp/test-repo"))
    set_default_session(s)
    try:
        with patch("modastack.inbox.deliver", return_value=(True, "reply-via-wrapper")):
            ok, response = session_mod.inject_capture("test")
            assert ok is True
            assert response == "reply-via-wrapper"

            assert session_mod.inject("test") is True
    finally:
        set_default_session(None)
