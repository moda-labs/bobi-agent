"""Unit tests for ManagerSession.inject_capture — atomic response capture.

These do NOT require the claude CLI: they stub out the asyncio plumbing
so the focus is the contract that the reply is snapshotted under
`_inject_lock` and returned bound to *this* inject, immune to a concurrent
inject overwriting the shared `_last_response`.
"""

import asyncio
from pathlib import Path

from modastack.manager.session import ManagerSession, set_default_session
from modastack.manager import session as session_mod


class _FakeFuture:
    def result(self, timeout=None):
        return None

    def cancel(self):
        pass


def _make_session(monkeypatch, reply: str) -> ManagerSession:
    s = ManagerSession(repo_path=Path("/tmp/test-repo"))
    s._client = object()
    s._loop = object()
    s._state = "waiting_input"
    s._last_response = ""
    monkeypatch.setattr(session_mod, "log_activity", lambda *a, **k: None)

    def fake_run(coro, loop):
        coro.close()
        s._last_response = reply
        return _FakeFuture()

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", fake_run)
    set_default_session(s)
    return s


def test_inject_capture_returns_turn_response(monkeypatch):
    s = _make_session(monkeypatch, "reply-to-A")

    ok, response = s.inject_capture("event A")

    assert ok is True
    assert response == "reply-to-A"


def test_capture_survives_concurrent_overwrite(monkeypatch):
    s = _make_session(monkeypatch, "reply-to-A")

    ok, response = s.inject_capture("event A")
    assert ok is True
    assert response == "reply-to-A"

    s._last_response = "reply-to-B"

    assert response == "reply-to-A"
    assert s.read_last_response() == "reply-to-B"


def test_inject_capture_failure_returns_empty(monkeypatch):
    s = ManagerSession(repo_path=Path("/tmp/test-repo"))
    s._client = None
    s._loop = None

    ok, response = s.inject_capture("event A")

    assert ok is False
    assert response == ""


def test_inject_capture_busy_fails_fast(monkeypatch):
    s = ManagerSession(repo_path=Path("/tmp/test-repo"))
    s._client = object()
    s._loop = object()
    s._state = "working"

    ok, response = s.inject_capture("event A", wait_for_ready=0)

    assert ok is False
    assert response == ""


def test_inject_wrapper_still_returns_bool(monkeypatch):
    s = _make_session(monkeypatch, "reply-to-A")

    result = s.inject("event A")

    assert result is True


def test_module_level_wrappers_delegate(monkeypatch):
    """Module-level inject/inject_capture delegate to _default_session."""
    s = _make_session(monkeypatch, "reply-via-wrapper")

    ok, response = session_mod.inject_capture("test")
    assert ok is True
    assert response == "reply-via-wrapper"

    assert session_mod.inject("test") is True
