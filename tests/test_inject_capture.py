"""Unit tests for session.inject_capture — atomic response capture.

These do NOT require the claude CLI: they stub out the asyncio plumbing
so the focus is the contract that the reply is snapshotted under
`_inject_lock` and returned bound to *this* inject, immune to a concurrent
inject overwriting the shared `_last_response` global.
"""

from modastack.manager import session


class _FakeFuture:
    def result(self, timeout=None):
        return None

    def cancel(self):
        pass


def _patch_runtime(monkeypatch, reply: str):
    """Make inject_capture believe the manager is idle and a turn produced `reply`."""
    monkeypatch.setattr(session, "_client", object())
    monkeypatch.setattr(session, "_loop", object())
    monkeypatch.setattr(session, "_state", "waiting_input")
    monkeypatch.setattr(session, "_last_response", "")
    monkeypatch.setattr(session, "log_activity", lambda *a, **k: None)

    def fake_run(coro, loop):
        coro.close()  # we never actually run the drain coroutine
        # Simulate _drain_turn storing this turn's reply into the global.
        session._last_response = reply
        return _FakeFuture()

    monkeypatch.setattr(session.asyncio, "run_coroutine_threadsafe", fake_run)


def test_inject_capture_returns_turn_response(monkeypatch):
    _patch_runtime(monkeypatch, "reply-to-A")

    ok, response = session.inject_capture("event A")

    assert ok is True
    assert response == "reply-to-A"


def test_capture_survives_concurrent_overwrite(monkeypatch):
    """The returned reply must not change when another turn later clobbers
    the shared global — this is the race the two-step read suffered from."""
    _patch_runtime(monkeypatch, "reply-to-A")

    ok, response = session.inject_capture("event A")
    assert ok is True
    assert response == "reply-to-A"

    # A concurrent inject (different thread) overwrites the shared global.
    session._last_response = "reply-to-B"

    # The value already handed to caller A is unaffected. The old pattern
    # (inject() then read_last_response()) would have read "reply-to-B" here.
    assert response == "reply-to-A"
    assert session.read_last_response() == "reply-to-B"


def test_inject_capture_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(session, "_client", None)
    monkeypatch.setattr(session, "_loop", None)

    ok, response = session.inject_capture("event A")

    assert ok is False
    assert response == ""


def test_inject_capture_busy_fails_fast(monkeypatch):
    monkeypatch.setattr(session, "_client", object())
    monkeypatch.setattr(session, "_loop", object())
    monkeypatch.setattr(session, "_state", "working")

    ok, response = session.inject_capture("event A", wait_for_ready=0)

    assert ok is False
    assert response == ""


def test_inject_wrapper_still_returns_bool(monkeypatch):
    _patch_runtime(monkeypatch, "reply-to-A")

    result = session.inject("event A")

    assert result is True
