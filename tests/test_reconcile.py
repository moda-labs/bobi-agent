"""MDS-65 §4.6 — dead-man reconciler tests.

A run can be stranded three ways: a crash that never reaches the terminal emit,
a swallowed terminal bus POST, or a hang past the declared timeout. The
reconciler reads state.json (the durable source of truth) and closes each,
re-emitting an honest lifecycle event — idempotently, so healthy completions are
never double-delivered.
"""

import json
import logging
import os
import shutil
import signal
import sys
import time

import pytest

from bobi.sdk import (
    SessionEntry, SessionRegistry, get_registry, pid_alive,
    TERMINAL_COMPLETED, TERMINAL_FAILED, TERMINAL_CRASHED,
)
from bobi import reconcile
from bobi.reconcile import _default_emit, reconcile_sessions, RECONCILE_GRACE


@pytest.fixture(autouse=True)
def bound_root(tmp_path, monkeypatch):
    monkeypatch.setattr("bobi.paths._root", tmp_path)


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

    @pytest.mark.parametrize("what_landed,expected", [
        ("a-cancel", "cancelled"), ("a-relaunch", "running"),
    ])
    def test_a_stale_read_closes_nothing(self, monkeypatch, what_landed, expected):
        """Cancel writes before it signals, so a read older than the death can miss it."""
        name = _seed("wf-x-stale", status="running", pid=999999, project="r")
        is_dead_run = reconcile.is_dead_run

        def something_lands_after_the_read(entry):
            dead = is_dead_run(entry)
            if what_landed == "a-cancel":
                get_registry().update(name, status="cancelled", pid=0)
            else:
                _seed(name, status="running", pid=os.getpid(), project="r")
            return dead

        monkeypatch.setattr(reconcile, "is_dead_run", something_lands_after_the_read)
        emit = _Emitter()

        assert reconcile_sessions(emit=emit) == []

        assert get_registry().get(name).status == expected
        assert emit.calls == []

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


class TestDeadlineStopsTheTree:
    """V17: the real cancel at a run's deadline, on a real process tree.

    Without the #1124 change this is what a restart does to a verified-live run
    past its deadline: the whole tree stops, the entry goes `failed`, and one
    event goes out.
    """

    def _past_deadline(self, name, pid):
        return _seed(name, status="running", pid=pid, project="r", timeout=10,
                     started_at=time.time() - (10 + RECONCILE_GRACE + 60))

    def test_the_tree_stops_and_failed_is_written_once(
            self, process_tree, monkeypatch):
        tree = process_tree()
        name = self._past_deadline("wf-t", tree.start())
        written = []
        update = SessionRegistry.update

        def record_status(registry, session, **fields):
            if "status" in fields:
                written.append(fields["status"])
            update(registry, session, **fields)

        monkeypatch.setattr(SessionRegistry, "update", record_status)
        emit = _Emitter()

        actions = reconcile_sessions(emit=emit)

        tree.assert_stopped()
        assert written == [TERMINAL_FAILED]
        entry = get_registry().get(name)
        assert entry.status == TERMINAL_FAILED
        assert entry.pid == 0
        assert entry.reconciled_at > 0
        assert entry.emit_confirmed is True
        assert entry.error == ("agent exceeded its 10s timeout without reporting "
                               "a terminal status")
        assert emit.types() == ["agent/session.failed"]
        assert actions == [{"name": name, "action": "timeout", "emitted": True}]

    def test_a_wait_child_stopped_with_its_parent_is_not_announced_as_crashed(
            self, process_tree):
        tree = process_tree()
        parent = self._past_deadline("wf-a-parent", tree.start())
        cli = tree.pids()["cli"]
        child = _seed("wf-b-child", status="running", pid=cli, project="r")

        class WaitsForTheChildToBeReaped(_Emitter):
            """The parent's event goes out before the sweep reaches the child.

            A stopped process can sit as a zombie until init collects it, and a
            zombie still reads as alive. Past that point the sweep's own read of
            the child says `running` with a dead pid, and only a fresh read sees
            the cancel.
            """

            def __call__(self, event_type, data):
                deadline = time.monotonic() + 5
                while pid_alive(cli) and time.monotonic() < deadline:
                    time.sleep(0.01)
                return super().__call__(event_type, data)

        emit = WaitsForTheChildToBeReaped()

        reconcile_sessions(emit=emit)

        tree.assert_stopped()
        assert get_registry().get(parent).status == TERMINAL_FAILED
        assert get_registry().get(child).status == "cancelled"
        assert [data["run_key"] for _, data in emit.calls] == [parent]

    @pytest.mark.parametrize("case", ["no-pid", "recycled-pid", "no-token"])
    def test_a_run_with_no_verified_tree_still_closes_as_failed(
            self, process_tree, signal_seam, case):
        """C5, C3 and C4 through the real cancel, as a restart reaches them."""
        root = 0 if case == "no-pid" else process_tree().start()
        name = self._past_deadline("wf-t", root)
        if case == "no-pid":
            get_registry().update(name, status="starting")
        elif case == "recycled-pid":
            get_registry().update(name, pid_start="proc:another-boot:1")
        else:
            get_registry().update(name, pid_start="")
        emit = _Emitter()

        actions = reconcile_sessions(emit=emit)

        entry = get_registry().get(name)
        assert (entry.status, entry.pid) == (TERMINAL_FAILED, 0)
        assert entry.reconciled_at > 0
        assert entry.error == ("agent exceeded its 10s timeout without reporting "
                               "a terminal status")
        assert emit.types() == ["agent/session.failed"]
        assert actions == [{"name": name, "action": "timeout", "emitted": True}]
        expected = [(root, signal.SIGTERM)] if case == "no-token" else []
        assert signal_seam.sent == expected

    def test_a_relaunch_during_the_wait_is_not_closed_or_announced(self):
        """`failed` does not block a launch, so a new run can land while a tree dies."""
        name = self._past_deadline("wf-t", os.getpid())

        def relaunch(session):
            _seed(session, status="running", pid=os.getpid(), started_at=time.time())

        emit = _Emitter()

        assert reconcile_sessions(emit=emit, cancel=relaunch) == []

        entry = get_registry().get(name)
        assert (entry.status, entry.pid) == ("running", os.getpid())
        assert emit.calls == []

    def test_a_runner_rewriting_running_during_the_wait_still_ends_failed(
            self, process_tree, signal_seam):
        tree = process_tree()
        root = tree.start_detached(on_term="linger")
        name = self._past_deadline("wf-t", root)

        def runners_last_write(pid, _sig):
            if pid == root:
                get_registry().update(name, status="running")

        signal_seam.after = runners_last_write
        emit = _Emitter()

        reconcile_sessions(emit=emit)

        assert get_registry().get(name).status == TERMINAL_FAILED
        assert emit.types() == ["agent/session.failed"]

    def test_an_entry_removed_during_the_wait_ends_only_its_own_branch(self):
        """The wait can last 15s, and a later run still has to be closed."""
        gone = _seed("wf-a-gone", status="running", pid=os.getpid(),
                     project="r", timeout=10,
                     started_at=time.time() - (10 + RECONCILE_GRACE + 60))
        later = _seed("wf-b-dead", status="running", pid=999999, project="r")
        emit = _Emitter()

        actions = reconcile_sessions(
            emit=emit,
            cancel=lambda name: shutil.rmtree(get_registry().session_dir(name)))

        assert [action["name"] for action in actions] == [later]
        assert [data["run_key"] for _, data in emit.calls] == [later]
        assert get_registry().get(gone) is None

    @pytest.mark.parametrize("raises_after_closing", [True, False],
                             ids=["after-the-entry-closed", "before-anything-happened"])
    def test_a_cancel_that_blows_up_is_announced_only_if_it_closed_the_run(
            self, process_tree, monkeypatch, raises_after_closing):
        """`failed` with no event would wait for the next restart to be heard."""
        name = self._past_deadline("wf-t", process_tree().start())

        def terminate(members, *, before_signal, **_kwargs):
            if raises_after_closing:
                before_signal(list(members))
            raise RuntimeError("boom")

        monkeypatch.setattr("bobi.proctree.terminate", terminate)
        emit = _Emitter()

        reconcile_sessions(emit=emit)

        entry = get_registry().get(name)
        if raises_after_closing:
            assert entry.status == TERMINAL_FAILED
            assert emit.types() == ["agent/session.failed"]
        else:
            assert entry.status == "running"
            assert emit.calls == []

    def test_a_survivor_is_named_in_the_error_and_the_event_still_goes(
            self, process_tree, signal_seam, short_graces, caplog):
        tree = process_tree()
        name = self._past_deadline("wf-t", tree.start())
        command = tree.pids()["command"]
        signal_seam.denied.add(command)
        emit = _Emitter()

        with caplog.at_level(logging.WARNING, logger="bobi.reconcile"):
            reconcile_sessions(emit=emit)

        assert "The run is marked failed, but these processes" in caplog.text
        entry = get_registry().get(name)
        assert entry.status == TERMINAL_FAILED
        assert entry.error == (
            "agent exceeded its 10s timeout without reporting a terminal status; "
            f"1 process could not be stopped (pid {command} python)")
        assert entry.error in emit.calls[0][1]["error"]

    def test_an_interrupt_mid_wait_leaves_a_run_the_next_sweep_announces(
            self, process_tree, signal_seam):
        tree = process_tree()
        root = tree.start()
        name = self._past_deadline("wf-t", root)
        interrupted = []

        def interrupt_once(pid, _sig):
            if pid == root and not interrupted:
                interrupted.append(pid)
                raise KeyboardInterrupt

        signal_seam.after = interrupt_once
        emit = _Emitter()

        with pytest.raises(KeyboardInterrupt):
            reconcile_sessions(emit=emit)

        tree.assert_stopped_soon()
        entry = get_registry().get(name)
        assert entry.status == TERMINAL_FAILED
        assert entry.emit_confirmed is False
        assert emit.calls == []

        emit_again = _Emitter()
        reconcile_sessions(emit=emit_again)
        assert emit_again.types() == ["agent/session.failed"]
        assert get_registry().get(name).emit_confirmed is True


# Runs one real sweep with the real cancel, in a process of its own.
_SWEEP = r"""
import json
import sys
from pathlib import Path

from bobi import paths
from bobi.reconcile import reconcile_sessions

paths.bind_root(Path(sys.argv[1]))
emitted = []
actions = reconcile_sessions(emit=lambda event, data: emitted.append(event) or True)
print(json.dumps({"actions": actions, "emitted": emitted}))
"""


class TestAHonestSweep:
    """V23: a sweep reports only what happened (A3, A6)."""

    def test_a_sweep_inside_its_target_refuses_and_changes_nothing(
            self, tmp_path, disposable_caller):
        """A manager started from a tool call is a descendant of that run.

        The target has no token, so the only guard between it and a SIGTERM to
        this sweep's own ancestor is the ancestor check. The ancestor is a
        disposable session, so losing that guard fails this test and nothing
        else.
        """
        caller = disposable_caller.start(
            [sys.executable, "-c", _SWEEP, str(tmp_path)])
        name = _seed("wf-around-the-sweep", status="running", pid=caller,
                     project="r", timeout=10,
                     started_at=time.time() - (10 + RECONCILE_GRACE + 60))
        get_registry().update(name, pid_start="")

        result = disposable_caller.run()

        assert result["returncode"] == 0, result["stderr"]
        assert json.loads(result["stdout"]) == {"actions": [], "emitted": []}
        disposable_caller.assert_alive()
        entry = get_registry().get(name)
        assert (entry.status, entry.pid) == ("running", caller)

    def test_an_unreadable_table_leaves_the_run_for_the_next_sweep(
            self, process_tree, signal_seam, monkeypatch):
        root = process_tree().start()
        name = _seed("wf-t", status="running", pid=root, project="r", timeout=10,
                     started_at=time.time() - (10 + RECONCILE_GRACE + 60))
        monkeypatch.setattr("bobi.proctree.read_table", lambda: None)
        emit = _Emitter()

        assert reconcile_sessions(emit=emit) == []

        assert signal_seam.sent == []
        assert emit.calls == []
        entry = get_registry().get(name)
        assert (entry.status, entry.pid) == ("running", root)

    @pytest.mark.parametrize("landed", [True, False])
    def test_default_emit_reports_what_post_event_returned(
            self, monkeypatch, landed):
        monkeypatch.setattr("bobi.events.publish.post_event",
                            lambda *args, **kwargs: landed)
        name = _seed("wf-e", status=TERMINAL_FAILED, error="boom", project="r",
                     terminal_at=time.time())

        assert _default_emit("agent/session.failed", {"run_key": name}) is landed
        reconcile_sessions()

        assert get_registry().get(name).emit_confirmed is landed


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
