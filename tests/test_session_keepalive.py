"""Regression tests for the in-flight-turn keepalive (#721).

The wedge test the self-heal watchdog (and its port in the deploy supervisor
sidecar) runs reads a single liveness scalar: the freshness of ``last_activity``,
exposed as ``idle_seconds`` in the manager health payload. ``_drain_turn`` stamps
``last_activity`` once at turn start and then blocks in ``receive_response()``
with no intermediate registry writes. A ``Task`` subagent call parks the director
inside that await with no messages until the child returns, so for the whole
child run ``last_activity`` freezes — byte-identical, from the outside, to a
wedge (``status=running``, ``idle_seconds`` climbing past threshold). The wedge
test then kills a healthy parent mid-task.

The keepalive refreshes ``last_activity`` on a cadence driven by the event loop
itself. These tests pin both halves of the fix:

* a director blocked on a live child (loop pumping) keeps reading alive, and
* a director genuinely wedged in-turn (loop not progressing) still freezes, so
  the stall stays detectable — the fix must not blanket-suppress the stall test.
"""

import asyncio
import time

import pytest

from bobi.brain import TurnResult
from bobi.sdk import SessionEntry, get_registry
from bobi.session import Session


def _register(session, install):
    """Register the session so registry.update()/get() have a state file."""
    get_registry().register(
        SessionEntry(
            name=session.name,
            role="director",
            status="idle",
            cwd=str(install.repo_path),
        )
    )


def _result():
    return TurnResult(
        session_id="sess-1",
        is_error=False,
        api_error_status=None,
        total_cost_usd=0.0,
        duration_ms=10,
        num_turns=1,
        result_text="ok",
    )


class _BlockedOnChildClient:
    """receive_response() that parks the turn on an *awaited* child.

    Models a director polling a live Task subagent: the coroutine yields no
    messages for a while but keeps awaiting, so the event loop stays free to
    run the keepalive. Captures last_activity mid-block so the test can prove
    the heartbeat advanced it while the turn was in flight.
    """

    provider = "anthropic"

    def __init__(self, session, block_s):
        self._session = session
        self._block_s = block_s
        self.mid_activity = None
        self.turn_start_activity = None

    async def query(self, text):  # pragma: no cover - unused here
        pass

    async def receive_response(self):
        reg = get_registry()
        self.turn_start_activity = reg.get(self._session.name).last_activity
        # Await in small slices so the loop keeps pumping the keepalive task,
        # exactly as it would while a real child subagent runs.
        slept = 0.0
        step = self._block_s / 10
        while slept < self._block_s:
            await asyncio.sleep(step)
            slept += step
        self.mid_activity = reg.get(self._session.name).last_activity
        yield _result()


class _WedgedClient:
    """receive_response() that blocks the loop *synchronously* — a true wedge.

    A synchronous hang (time.sleep, not an await) freezes the event loop, so no
    task — including the keepalive — can run. last_activity must stay frozen,
    proving the keepalive reflects real loop liveness, not merely "a turn was
    started."
    """

    provider = "anthropic"

    def __init__(self, session, block_s):
        self._session = session
        self._block_s = block_s
        self.wedged_activity = None
        self.turn_start_activity = None

    async def query(self, text):  # pragma: no cover - unused here
        pass

    async def receive_response(self):
        reg = get_registry()
        self.turn_start_activity = reg.get(self._session.name).last_activity
        # No await between the two reads: the loop cannot interleave the
        # keepalive, so whatever it observes is purely the frozen turn-start
        # stamp if — and only if — the keepalive never ran.
        time.sleep(self._block_s)
        self.wedged_activity = reg.get(self._session.name).last_activity
        yield _result()


@pytest.fixture
def session(bobi_install):
    s = Session(name="test-keepalive", cwd=str(bobi_install.repo_path))
    s._input_ready = asyncio.Event()
    return s


@pytest.mark.asyncio
async def test_blocked_on_subagent_is_not_wedged(session, bobi_install, monkeypatch):
    """A turn blocked on a live child keeps last_activity fresh (idle low)."""
    monkeypatch.setattr("bobi.session.save_session_id", lambda *a, **k: None)
    monkeypatch.setattr("bobi.session.KEEPALIVE_INTERVAL", 0.02)
    _register(session, bobi_install)

    # Block for many keepalive intervals — long enough that a frozen
    # last_activity would look wedged, short enough to keep the test fast.
    client = _BlockedOnChildClient(session, block_s=0.4)
    session._client = client

    await session._drain_turn()

    assert client.turn_start_activity is not None
    # The heartbeat advanced last_activity *during* the blocked turn: the
    # mid-block stamp is strictly newer than the turn-start stamp.
    assert client.mid_activity > client.turn_start_activity, (
        "keepalive did not refresh last_activity while blocked on a child"
    )
    # The wedge test reads idle = now - last_activity. A wedged director would
    # still carry the frozen turn-start stamp, so its idle would be at least the
    # whole block; the keepalive keeps last_activity strictly newer than that.
    entry = get_registry().get(session.name)
    assert entry.last_activity > client.turn_start_activity
    assert (time.time() - entry.last_activity) < (time.time() - client.turn_start_activity)


@pytest.mark.asyncio
async def test_genuine_wedge_still_freezes_activity(session, bobi_install, monkeypatch):
    """A synchronously-frozen loop still freezes last_activity → still detected."""
    monkeypatch.setattr("bobi.session.save_session_id", lambda *a, **k: None)
    # Tiny interval: a keepalive that fired on wall-clock alone would refresh
    # many times over during the block. It must not, because the loop is frozen.
    monkeypatch.setattr("bobi.session.KEEPALIVE_INTERVAL", 0.001)
    _register(session, bobi_install)

    client = _WedgedClient(session, block_s=0.3)
    session._client = client

    await session._drain_turn()

    assert client.turn_start_activity is not None
    # last_activity did NOT advance during the synchronous hang — the wedge
    # remains visible to the stall test.
    assert client.wedged_activity == client.turn_start_activity, (
        "keepalive masked a genuine in-turn wedge (loop not progressing)"
    )


def test_no_status_update_preserves_status(bobi_install):
    """The heartbeat's registry.update(name) must only touch last_activity.

    The keepalive fires concurrently with the drain loop, which moves status to
    idle/error at turn end. If a bare update reset or recomputed status it could
    resurrect a stale state; assert it leaves status untouched.
    """
    reg = get_registry()
    reg.register(SessionEntry(name="s", role="director", status="error"))
    before = reg.get("s").last_activity

    time.sleep(0.01)
    reg.update("s")  # no status kwarg — exactly what the keepalive issues

    entry = reg.get("s")
    assert entry.status == "error"
    assert entry.last_activity > before


@pytest.mark.asyncio
async def test_heartbeat_write_failure_does_not_break_turn(session, bobi_install, monkeypatch):
    """A registry-write failure in the heartbeat must not kill the turn.

    If the heartbeat raised, the finally's ``await keepalive`` would re-raise
    and skip turn-complete cleanup. The turn must still finish cleanly.
    """
    monkeypatch.setattr("bobi.session.save_session_id", lambda *a, **k: None)
    monkeypatch.setattr("bobi.session.KEEPALIVE_INTERVAL", 0.01)
    _register(session, bobi_install)

    real_update = get_registry().update

    def _flaky_update(name, **kwargs):
        # Fail only the heartbeat's no-kwarg refresh; let status writes through.
        if not kwargs:
            raise OSError("disk full")
        return real_update(name, **kwargs)

    monkeypatch.setattr(get_registry(), "update", _flaky_update)

    client = _BlockedOnChildClient(session, block_s=0.05)
    session._client = client

    # Must not raise, and the turn must complete to a normal terminal state.
    await session._drain_turn()
    assert session.detect_state() == "waiting_input"


@pytest.mark.asyncio
async def test_keepalive_task_is_cleaned_up(session, bobi_install, monkeypatch):
    """The keepalive is cancelled the moment the turn ends — no leaked task."""
    monkeypatch.setattr("bobi.session.save_session_id", lambda *a, **k: None)
    monkeypatch.setattr("bobi.session.KEEPALIVE_INTERVAL", 0.01)
    _register(session, bobi_install)

    client = _BlockedOnChildClient(session, block_s=0.05)
    session._client = client

    before = {t for t in asyncio.all_tasks()}
    await session._drain_turn()
    # Let any just-cancelled task settle, then confirm none leaked.
    await asyncio.sleep(0.03)
    leaked = {t for t in asyncio.all_tasks()} - before
    leaked = {t for t in leaked if not t.done()}
    assert not leaked, "keepalive task outlived the turn"
