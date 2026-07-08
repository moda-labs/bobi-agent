"""Unit tests for Session._process_message and the asyncio.Event wake mechanism.

Tests verify that _process_message wakes immediately (no polling) when
the session transitions to waiting_input, stopped, or error.
"""

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from bobi.brain import TurnResult
from bobi.inbox import Message
from bobi.session import Session


@pytest.fixture
def session(bobi_install):
    """Create a Session without starting it (no Claude client needed)."""
    s = Session(name="test-wake", cwd=str(bobi_install.repo_path))
    # Simulate what _run does: create the asyncio.Event
    s._input_ready = asyncio.Event()
    # Stub out SDK calls that _process_message uses
    s._client = None
    # Replace inbox.respond with a mock so we can verify calls
    s.inbox.respond = MagicMock()
    return s


def _make_msg(wait=False):
    return Message(id="m1", sender="test", text="hello", wait=wait)


def _fake_client(session, drain_response="response text"):
    """Attach a fake client with query() and a fake _drain_turn."""

    class FakeClient:
        async def query(self, text):
            pass

    async def fake_drain():
        session._set_state("waiting_input")
        return drain_response

    session._client = FakeClient()
    session._drain_turn = fake_drain


def test_emit_session_unreachable_alert_posts_expected_event(monkeypatch):
    from bobi import session as session_mod

    posted = []
    monkeypatch.setattr(
        "bobi.events.publish.post_event",
        lambda event_type, payload: posted.append((event_type, payload)),
    )

    session_mod._emit_session_unreachable_alert(
        session="director",
        state="working",
        message_id="msg-1",
        sender="inbox",
        wait=True,
        elapsed=121.25,
    )

    assert posted == [
        (
            "system/session.unreachable",
            {
                "session": "director",
                "state": "working",
                "message_id": "msg-1",
                "sender": "inbox",
                "wait": True,
                "elapsed_seconds": 121.2,
                "text": (
                    "Session 'director' has been unreachable for 121s while "
                    "a queued message waits; current state: working."
                ),
            },
        )
    ]


class TestInputReadyWake:
    """Verify _process_message wakes on asyncio.Event transitions."""

    @pytest.mark.asyncio
    async def test_wake_on_waiting_input(self, session):
        """Message is processed when session transitions to waiting_input."""
        session._set_state("waiting_input")
        _fake_client(session)

        acked = []
        msg = _make_msg(wait=True)
        msg.ack = lambda: acked.append(msg.id)
        await session._process_message(msg)

        session.inbox.respond.assert_called_once_with(msg, "response text")
        assert acked == ["m1"]

    @pytest.mark.asyncio
    async def test_wake_on_stopped(self, session):
        """Message is rejected when session transitions to stopped."""
        session._set_state("working")

        acked = []
        msg = _make_msg(wait=True)
        msg.ack = lambda: acked.append(msg.id)

        async def transition():
            await asyncio.sleep(0.05)
            session._set_state("stopped")

        asyncio.create_task(transition())
        await session._process_message(msg)

        session.inbox.respond.assert_called_once_with(msg, "session stopped")
        assert acked == []

    @pytest.mark.asyncio
    async def test_wake_on_error(self, session):
        """Message is rejected when session transitions to error."""
        session._set_state("working")

        acked = []
        msg = _make_msg(wait=True)
        msg.ack = lambda: acked.append(msg.id)

        async def transition():
            await asyncio.sleep(0.05)
            session._set_state("error")

        asyncio.create_task(transition())
        await session._process_message(msg)

        session.inbox.respond.assert_called_once_with(msg, "session error")
        assert acked == []

    @pytest.mark.asyncio
    async def test_terminal_drop_logs_warning(self, session, caplog):
        """Terminal-state drops should be visible and replayable."""
        session._set_state("error")
        msg = Message(id="m1", sender="slack", text="hello from a user", wait=False)
        msg.ack = MagicMock()

        await session._process_message(msg)

        msg.ack.assert_not_called()
        assert "Dropping inbox message" in caplog.text
        assert "state=error" in caplog.text

    @pytest.mark.asyncio
    async def test_no_poll_latency(self, session):
        """Transition to waiting_input wakes waiter instantly, not after 0.5s."""
        session._set_state("working")
        _fake_client(session)

        msg = _make_msg(wait=False)

        async def transition():
            await asyncio.sleep(0.01)
            session._set_state("waiting_input")

        asyncio.create_task(transition())

        start = time.monotonic()
        await session._process_message(msg)
        elapsed = time.monotonic() - start

        # Should complete in well under 0.5s (the old poll interval)
        assert elapsed < 0.3

    @pytest.mark.asyncio
    async def test_message_waits_through_long_not_ready_period(self, session, monkeypatch):
        """A queued baton must survive a long rotation/reconnect wait.

        The old readiness wait had a hard deadline and returned "session not
        ready", dropping the queued message even if the session recovered later.
        """
        monkeypatch.setattr("bobi.session.SESSION_UNREACHABLE_ALERT_AFTER", 0.02)
        monkeypatch.setattr("bobi.session.SESSION_READY_WAIT_POLL", 0.01)
        monkeypatch.setattr(
            "bobi.session._emit_session_unreachable_alert",
            lambda *a, **k: None,
        )
        session._set_state("working")
        session._input_ready.clear()
        _fake_client(session, drain_response="preserved")

        msg = _make_msg(wait=True)

        async def transition():
            await asyncio.sleep(0.08)
            session._set_state("waiting_input")

        asyncio.create_task(transition())
        await session._process_message(msg)

        session.inbox.respond.assert_called_once_with(msg, "preserved")

    @pytest.mark.asyncio
    async def test_unreachable_wait_emits_single_liveness_alert(self, session, monkeypatch):
        """Waiting roughly two minutes for readiness emits a best-effort alert."""
        monkeypatch.setattr("bobi.session.SESSION_UNREACHABLE_ALERT_AFTER", 0.0)
        monkeypatch.setattr("bobi.session.SESSION_READY_WAIT_POLL", 0.01)
        alerts = []
        monkeypatch.setattr(
            "bobi.session._emit_session_unreachable_alert",
            lambda **payload: alerts.append(payload),
        )
        session._set_state("working")
        session._input_ready.clear()
        _fake_client(session)

        msg = _make_msg(wait=False)

        async def transition():
            await asyncio.sleep(0.07)
            session._set_state("waiting_input")

        asyncio.create_task(transition())
        await session._process_message(msg)

        assert len(alerts) == 1
        assert alerts[0]["session"] == session.name
        assert alerts[0]["state"] == "working"
        assert alerts[0]["message_id"] == msg.id

    @pytest.mark.asyncio
    async def test_stale_ready_event_does_not_spin_or_drop_message(self, session, monkeypatch):
        """A stale readiness event while still working must not busy-loop."""
        monkeypatch.setattr("bobi.session.SESSION_UNREACHABLE_ALERT_AFTER", 0.02)
        monkeypatch.setattr("bobi.session.SESSION_READY_WAIT_POLL", 0.01)
        monkeypatch.setattr(
            "bobi.session._emit_session_unreachable_alert",
            lambda *a, **k: None,
        )
        session._set_state("working")
        # Simulate a stale event left set while the state still says not ready.
        session._input_ready.set()
        _fake_client(session, drain_response="after stale event")

        msg = _make_msg(wait=True)

        async def transition():
            await asyncio.sleep(0.05)
            session._set_state("waiting_input")

        asyncio.create_task(transition())
        await asyncio.wait_for(session._process_message(msg), timeout=1.0)

        session.inbox.respond.assert_called_once_with(msg, "after stale event")

    @pytest.mark.asyncio
    async def test_unreachable_alert_rearms_after_terminal_wait(self, session, monkeypatch):
        """A terminal wait exit must not suppress future unreachable alerts."""
        monkeypatch.setattr("bobi.session.SESSION_UNREACHABLE_ALERT_AFTER", 0.0)
        monkeypatch.setattr("bobi.session.SESSION_READY_WAIT_POLL", 0.01)
        alerts = []
        monkeypatch.setattr(
            "bobi.session._emit_session_unreachable_alert",
            lambda **payload: alerts.append(payload),
        )

        session._set_state("working")
        session._input_ready.clear()

        async def stop():
            await asyncio.sleep(0.02)
            session._set_state("error")

        asyncio.create_task(stop())
        await session._process_message(_make_msg(wait=True))

        assert len(alerts) == 1

        session._set_state("working")
        session._input_ready.clear()
        _fake_client(session, drain_response="rearmed")

        async def recover():
            await asyncio.sleep(0.02)
            session._set_state("waiting_input")

        asyncio.create_task(recover())
        await session._process_message(_make_msg(wait=True))

        assert len(alerts) == 2

    @pytest.mark.asyncio
    async def test_unreachable_alert_rearms_after_cancelled_wait(self, session, monkeypatch):
        """A cancelled wait must not leave alert suppression stuck on."""
        monkeypatch.setattr("bobi.session.SESSION_UNREACHABLE_ALERT_AFTER", 0.0)
        monkeypatch.setattr("bobi.session.SESSION_READY_WAIT_POLL", 0.01)
        alerts = []
        monkeypatch.setattr(
            "bobi.session._emit_session_unreachable_alert",
            lambda **payload: alerts.append(payload),
        )
        session._set_state("working")
        session._input_ready.clear()

        task = asyncio.create_task(session._process_message(_make_msg(wait=True)))
        await asyncio.sleep(0.02)

        assert len(alerts) == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        session._set_state("working")
        session._input_ready.clear()
        _fake_client(session, drain_response="after cancel")

        async def recover():
            await asyncio.sleep(0.02)
            session._set_state("waiting_input")

        asyncio.create_task(recover())
        await session._process_message(_make_msg(wait=True))

        assert len(alerts) == 2

    @pytest.mark.asyncio
    async def test_nonblocking_msg_on_stopped(self, session):
        """Non-blocking message on stopped session returns without error."""
        session._set_state("stopped")

        msg = _make_msg(wait=False)
        await session._process_message(msg)

        # No respond call expected for non-blocking messages
        session.inbox.respond.assert_not_called()


class TestSetState:
    """Verify _set_state fires the asyncio.Event correctly."""

    def test_fires_event_on_waiting_input(self, session):
        session._input_ready.clear()
        session._set_state("waiting_input")
        assert session._input_ready.is_set()

    def test_fires_event_on_error(self, session):
        session._input_ready.clear()
        session._set_state("error")
        assert session._input_ready.is_set()

    def test_fires_event_on_stopped(self, session):
        session._input_ready.clear()
        session._set_state("stopped")
        assert session._input_ready.is_set()

    def test_does_not_fire_on_working(self, session):
        session._input_ready.clear()
        session._set_state("working")
        assert not session._input_ready.is_set()

    def test_does_not_fire_on_running(self, session):
        session._input_ready.clear()
        session._set_state("running")
        assert not session._input_ready.is_set()


class TestSubscriptionResilience:
    """#409: a registration read-timeout at init must not kill the session.

    Regression for the lead/sub-agent crashes: a timed-out event-server
    registration handshake re-raised and the session went to ``error`` state
    and died. Events are queued/sequenced/resumable, so a transient timeout
    must instead boot the session and retry registration in the background.
    """

    def test_registration_timeout_is_nonfatal_and_retries(
        self, session, monkeypatch
    ):
        monkeypatch.setattr("bobi.session.SUBSCRIPTION_RETRY_BASE", 0.01)
        fake_sub = MagicMock()
        calls = {"n": 0}

        def fake_start(name, keys, root, register_attempts=3):
            calls["n"] += 1
            if calls["n"] == 1:
                # First (foreground) attempt times out, as in the incident.
                raise TimeoutError("The read operation timed out")
            return fake_sub

        monkeypatch.setattr(
            "bobi.subagent._start_event_subscription", fake_start
        )
        # A non-inbox topic makes this a coordinator — the exact case that used
        # to be FATAL (the re-raise killed managers/leads at init).
        session._subscribe = ["github:o/r"]

        # Must not raise and must not flip the session into error state.
        session._start_subscription()
        assert session.detect_state() != "error"
        assert session._subscription is None  # first attempt failed → not wired

        # The background thread retries and wires the subscription in.
        deadline = time.time() + 5
        while session._subscription is None and time.time() < deadline:
            time.sleep(0.01)

        assert session._subscription is fake_sub
        assert calls["n"] >= 2

        session._sub_retry_stop.set()

    def test_persistent_failure_never_terminates(self, session, monkeypatch):
        monkeypatch.setattr("bobi.session.SUBSCRIPTION_RETRY_BASE", 0.01)
        attempts = {"n": 0}

        def always_timeout(name, keys, root, register_attempts=3):
            attempts["n"] += 1
            raise TimeoutError("The read operation timed out")

        monkeypatch.setattr(
            "bobi.subagent._start_event_subscription", always_timeout
        )
        session._subscribe = ["github:o/r"]

        session._start_subscription()
        assert session.detect_state() != "error"

        # It keeps retrying (logged, not fatal) — observe repeated attempts.
        deadline = time.time() + 2
        while attempts["n"] < 3 and time.time() < deadline:
            time.sleep(0.01)
        assert attempts["n"] >= 3
        assert session._subscription is None

        # The retry loop honors the stop signal (the stop() contract).
        session._sub_retry_stop.set()
        session._sub_retry_thread.join(timeout=2)
        assert not session._sub_retry_thread.is_alive()

    def test_stop_tears_down_background_wired_subscription(
        self, session, monkeypatch
    ):
        """A subscription wired in by the background retry must be torn down by
        stop() — never leaked. Regression for the shutdown TOCTOU."""
        monkeypatch.setattr("bobi.session.SUBSCRIPTION_RETRY_BASE", 0.01)
        fake_sub = MagicMock()
        calls = {"n": 0}

        def fake_start(name, keys, root, register_attempts=3):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("The read operation timed out")
            return fake_sub

        monkeypatch.setattr(
            "bobi.subagent._start_event_subscription", fake_start
        )
        session._subscribe = ["github:o/r"]
        session._start_subscription()

        deadline = time.time() + 5
        while session._subscription is None and time.time() < deadline:
            time.sleep(0.01)
        assert session._subscription is fake_sub

        session.stop()
        fake_sub.stop.assert_called_once()
        assert session._subscription is None


def _result(is_error, api_error_status=None, session_id="sess-1"):
    """A normalized end-of-turn result, as a brain adapter yields it."""
    return TurnResult(
        session_id=session_id,
        is_error=is_error,
        api_error_status=api_error_status,
        total_cost_usd=0.0,
        duration_ms=10,
        num_turns=1,
        result_text="API Error: 529 Overloaded" if is_error else "ok",
    )


def _streaming_client(session, batches):
    """Fake SDK client: receive_response() yields one batch of messages per
    turn; query() records the prompts it was asked to send.

    A wedged session is one that silently stops calling query() — so the
    recorded prompts are the proof the model is (or isn't) still being asked.
    """

    class FakeClient:
        provider = "anthropic"

        def __init__(self):
            self.queries: list[str] = []
            self._batches = [list(b) for b in batches]

        async def query(self, text):
            self.queries.append(text)

        async def receive_response(self):
            batch = self._batches.pop(0) if self._batches else []
            for m in batch:
                yield m

    c = FakeClient()
    session._client = c
    return c


class TestTurnErrorRecovery:
    """Regression for the prod incident: a single transient ``529 Overloaded``
    turn error permanently wedged the live director session.

    ``_drain_turn`` set state to terminal ``error`` on ``ResultMessage.is_error``;
    nothing ever cleared it, so ``_process_message`` silently dropped every
    subsequent event (``state in ("stopped", "error")``) and the agent went
    deaf until a process restart. A transient turn error must instead leave
    the session ready for the next event.
    """

    @pytest.mark.asyncio
    async def test_turn_error_is_not_terminal(self, session, monkeypatch):
        """After a 529 turn, the session must be ready — not in ``error``."""
        monkeypatch.setattr("bobi.session.save_session_id", lambda *a, **k: None)
        session._set_state("working")
        _streaming_client(session, [[_result(is_error=True, api_error_status=529)]])

        await session._drain_turn()

        assert session.detect_state() == "waiting_input"
        assert session.detect_state() != "error"

    @pytest.mark.asyncio
    async def test_529_does_not_wedge_subsequent_messages(self, session, monkeypatch):
        """The core wedge: a 529 on one turn must not silence later events."""
        monkeypatch.setattr("bobi.session.save_session_id", lambda *a, **k: None)
        # Isolate recovery from retry so this asserts the wedge fix specifically.
        monkeypatch.setattr("bobi.session.TURN_RETRY_MAX_ATTEMPTS", 0)
        session._set_state("waiting_input")

        # Turn 1 — Anthropic 529s.
        c1 = _streaming_client(session, [[_result(is_error=True, api_error_status=529)]])
        await session._process_message(_make_msg(wait=True))
        assert c1.queries == ["hello"]  # the turn was attempted

        # Turn 2 — a fresh event must reach the model, not be dropped.
        c2 = _streaming_client(session, [[_result(is_error=False)]])
        await session._process_message(_make_msg(wait=True))
        assert c2.queries == ["hello"], "session went deaf after a 529 (wedged)"
        assert session.detect_state() == "waiting_input"

    @pytest.mark.asyncio
    async def test_transient_error_is_retried_in_band(self, session, monkeypatch):
        """A transient 529 should self-heal within the turn via bounded retry,
        so the triggering event is answered rather than dropped."""
        monkeypatch.setattr("bobi.session.save_session_id", lambda *a, **k: None)
        monkeypatch.setattr("bobi.session.TURN_RETRY_BASE", 0.0)
        monkeypatch.setattr("bobi.session.TURN_RETRY_MAX_ATTEMPTS", 3)
        session._set_state("waiting_input")

        # First turn 529s, the retry succeeds.
        c = _streaming_client(
            session,
            [
                [_result(is_error=True, api_error_status=529)],
                [_result(is_error=False)],
            ],
        )
        await session._process_message(_make_msg(wait=True))

        assert c.queries == ["hello", "hello"], "transient error was not retried"
        assert session.detect_state() == "waiting_input"
        assert not session._last_is_error

    @pytest.mark.asyncio
    async def test_nontransient_error_is_not_retried(self, session, monkeypatch):
        """A non-transient error (e.g. 400) must recover but NOT retry —
        re-sending the same bad turn would just fail again."""
        monkeypatch.setattr("bobi.session.save_session_id", lambda *a, **k: None)
        monkeypatch.setattr("bobi.session.TURN_RETRY_BASE", 0.0)
        monkeypatch.setattr("bobi.session.TURN_RETRY_MAX_ATTEMPTS", 3)
        session._set_state("waiting_input")

        c = _streaming_client(session, [[_result(is_error=True, api_error_status=400)]])
        await session._process_message(_make_msg(wait=True))

        assert c.queries == ["hello"], "non-transient error should not be retried"
        assert session.detect_state() == "waiting_input"  # still recovered

    @pytest.mark.asyncio
    async def test_dropped_message_stops_status_indicators(self, session, monkeypatch):
        """A message dropped because the session is genuinely stopped/error must
        still clear any Slack 'thinking…' refresh loop, which is otherwise only
        cleared at the end of a turn that never runs."""
        called = {"n": 0}
        monkeypatch.setattr(
            "bobi.events.channels.stop_all_refresh_loops",
            lambda: called.__setitem__("n", called["n"] + 1),
        )
        session._set_state("error")

        await session._process_message(_make_msg(wait=True))

        assert called["n"] >= 1, "status indicator was not cleared on a dropped message"
