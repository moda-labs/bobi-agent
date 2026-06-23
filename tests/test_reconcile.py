"""MDS-65 §4.6 — dead-man reconciler tests.

A run can be stranded three ways: a crash that never reaches the terminal emit,
a swallowed terminal bus POST, or a hang past the declared timeout. The
reconciler reads state.json (the durable source of truth) and closes each,
re-emitting an honest lifecycle event — idempotently, so healthy completions are
never double-delivered.
"""

import time

import pytest

from modastack.sdk import (
    SessionEntry, get_registry,
    TERMINAL_COMPLETED, TERMINAL_FAILED, TERMINAL_CRASHED,
)
from modastack.reconcile import reconcile_sessions, RECONCILE_GRACE


@pytest.fixture(autouse=True)
def bound_root(tmp_path, monkeypatch):
    monkeypatch.setattr("modastack.paths._root", tmp_path)


class _Emitter:
    """Records emitted events; ``landed`` controls whether the POST 'succeeds'."""
    def __init__(self, landed=True):
        self.calls = []
        self._landed = landed

    def __call__(self, event_type, data):
        self.calls.append((event_type, data))
        return self._landed

    def types(self):
        return [t for t, _ in self.calls]


def _seed(name, **kw):
    entry = SessionEntry(name=name, run_key=kw.pop("run_key", name), **kw)
    get_registry().register(entry)
    return name


# --- (2) crash: live status + dead pid --------------------------------------

class TestCrashReconcile:
    def test_dead_pid_marked_crashed_and_emits_failed(self):
        name = _seed("wf-x-1", status="running", pid=999999, role="engineer",
                     project="r", requested_by={"slack_user": "U1"})
        emit = _Emitter()
        actions = reconcile_sessions(emit=emit, cancel=lambda n: None)

        entry = get_registry().get(name)
        assert entry.status == TERMINAL_CRASHED
        assert entry.reconciled_at > 0
        assert entry.emit_confirmed is True
        assert "agent/session.failed" in emit.types()
        payload = emit.calls[0][1]
        assert payload["requested_by"] == {"slack_user": "U1"}

    def test_second_sweep_is_noop(self):
        _seed("wf-x-2", status="running", pid=999999, project="r")
        emit1 = _Emitter()
        reconcile_sessions(emit=emit1, cancel=lambda n: None)
        emit2 = _Emitter()
        actions2 = reconcile_sessions(emit=emit2, cancel=lambda n: None)
        assert actions2 == []  # already terminal + emit_confirmed
        assert emit2.calls == []


# --- (3) timeout: alive past deadline ---------------------------------------

class TestTimeoutReconcile:
    def test_past_deadline_alive_pid_is_cancelled_and_failed(self):
        # alive pid (our own) started long ago with a tiny timeout
        name = _seed("wf-t-1", status="running", pid=__import__("os").getpid(),
                     project="r", timeout=10,
                     started_at=time.time() - (10 + RECONCILE_GRACE + 60))
        cancelled = []
        emit = _Emitter()
        reconcile_sessions(emit=emit, cancel=cancelled.append)

        entry = get_registry().get(name)
        assert entry.status == TERMINAL_FAILED
        assert cancelled == [name]
        assert "agent/session.failed" in emit.types()

    def test_within_deadline_is_left_running(self):
        name = _seed("wf-t-2", status="running", pid=__import__("os").getpid(),
                     project="r", timeout=3600, started_at=time.time() - 5)
        emit = _Emitter()
        actions = reconcile_sessions(emit=emit, cancel=lambda n: None)
        assert actions == []
        assert get_registry().get(name).status == "running"


# --- (1) terminal-but-unconfirmed: swallowed emit ---------------------------

class TestReEmitSwallowed:
    def test_unconfirmed_failed_is_reemitted(self):
        name = _seed("wf-e-1", status=TERMINAL_FAILED, error="boom",
                     project="r", emit_confirmed=False, terminal_at=time.time())
        emit = _Emitter()
        reconcile_sessions(emit=emit, cancel=lambda n: None)
        assert "agent/session.failed" in emit.types()
        assert get_registry().get(name).emit_confirmed is True

    def test_unconfirmed_completed_is_reemitted(self):
        name = _seed("wf-c-1", status=TERMINAL_COMPLETED, project="r",
                     emit_confirmed=False, terminal_at=time.time())
        emit = _Emitter()
        reconcile_sessions(emit=emit, cancel=lambda n: None)
        assert "agent/session.completed" in emit.types()

    def test_confirmed_terminal_is_not_reemitted(self):
        """A healthy, already-delivered completion must never be re-sent."""
        _seed("wf-c-2", status=TERMINAL_COMPLETED, project="r",
              emit_confirmed=True, terminal_at=time.time())
        emit = _Emitter()
        actions = reconcile_sessions(emit=emit, cancel=lambda n: None)
        assert actions == []
        assert emit.calls == []

    def test_failed_emit_stays_unconfirmed_for_retry(self):
        """If the re-emit POST doesn't land, emit_confirmed stays False so a
        later sweep retries (the bus may be down now, up later)."""
        name = _seed("wf-e-2", status=TERMINAL_FAILED, error="boom", project="r",
                     emit_confirmed=False, terminal_at=time.time())
        emit = _Emitter(landed=False)
        reconcile_sessions(emit=emit, cancel=lambda n: None)
        assert get_registry().get(name).emit_confirmed is False
        # next sweep with a working bus re-emits and confirms
        emit2 = _Emitter(landed=True)
        reconcile_sessions(emit=emit2, cancel=lambda n: None)
        assert get_registry().get(name).emit_confirmed is True


class TestReconcilerIgnoresHealthyActive:
    def test_idle_with_live_pid_untouched(self):
        name = _seed("wf-live-1", status="idle", pid=__import__("os").getpid(),
                     project="r", timeout=3600, started_at=time.time())
        emit = _Emitter()
        actions = reconcile_sessions(emit=emit, cancel=lambda n: None)
        assert actions == []
        assert get_registry().get(name).status == "idle"

    def test_pidless_active_without_timeout_untouched(self):
        # e.g. a just-registered 'starting' entry with no pid/timeout yet
        name = _seed("wf-start-1", status="starting", project="r")
        emit = _Emitter()
        actions = reconcile_sessions(emit=emit, cancel=lambda n: None)
        assert actions == []


class TestWaitingIsNotReconciled:
    def test_suspended_waiting_run_with_dead_pid_untouched(self):
        """A suspended workflow (status 'waiting') is dormant by design — its
        process has exited (dead pid) and a fresh one resumes it on the await
        event. The reconciler must NOT crash-reconcile it."""
        name = _seed("wf-wait-1", status="waiting", pid=999999, project="r",
                     requested_by={"slack_user": "U1"})
        emit = _Emitter()
        actions = reconcile_sessions(emit=emit, cancel=lambda n: None)
        assert actions == []
        assert emit.calls == []
        assert get_registry().get(name).status == "waiting"


class TestExcludeNames:
    def test_excluded_session_is_skipped(self):
        """The manager passes its own session name on startup so its previous
        process's dead entry isn't reported as a crashed sub-agent."""
        name = _seed("manager-app", status="running", pid=999999, project="r")
        emit = _Emitter()
        actions = reconcile_sessions(emit=emit, cancel=lambda n: None,
                                     exclude_names={name})
        assert actions == []
        assert emit.calls == []
        # untouched — still 'running', not flipped to crashed
        assert get_registry().get(name).status == "running"
