"""Unit tests for the sidecar supervision core - the ported #464 watchdog.

Ported verbatim (behaviorally) from this repo's tests/test_watchdog.py when
the supervision logic was lifted into the sidecar (issue #5), and returned
here with the sidecar by the repo reorg (Lane 1, Phase 2).
They drive the supervisor state machine with injected time/sleep/health/spawn
so the discriminator, bounded-retry/backoff, crash-loop containment and
fail-open paths are exercised without real processes or wall-clock waits. The
real-process acceptance + negative tests live in test_supervision_restart.py.
"""

import pytest

from bobi.supervisor.config import SupervisorConfig
from bobi.supervisor.supervision import (
    ACTIVE_STATES,
    DEAD_STATES,
    EXIT_BUDGET_EXHAUSTED,
    RestartBudget,
    Supervisor,
    is_wedged,
)


# --- the wedge discriminator ---------------------------------------------

class TestIsWedged:

    @pytest.mark.parametrize("status,idle,expected", [
        # active + past threshold => wedged
        ("running", 601, True),
        ("starting", 601, True),
        # active but under threshold => not wedged (live long turn)
        ("running", 599, False),
        ("running", 600, False),  # strictly greater
        # idle is never wedged, no matter how stale (the trap)
        ("idle", 10_000, False),
        ("stopped", 10_000, False),
        ("done", 10_000, False),
        ("error", 10_000, False),
        # uncertainty must never trigger a restart (fail-open)
        (None, 10_000, False),
        ("running", None, False),
        ("running", "nan-ish", False),
        # non-finite idle is corrupt input, not a wedge (json accepts Infinity)
        ("running", float("inf"), False),
        ("running", "inf", False),
        ("running", float("nan"), False),
    ])
    def test_discriminator(self, status, idle, expected):
        assert is_wedged(status, idle, 600) is expected

    def test_active_states_are_starting_and_running(self):
        assert ACTIVE_STATES == frozenset({"starting", "running"})

    def test_dead_states_is_exactly_error(self):
        # `error` is the one positive death signal; everything else unknown
        # must stay fail-open (#12).
        assert DEAD_STATES == frozenset({"error"})


# --- the windowed restart budget -----------------------------------------

class TestRestartBudget:

    def test_exhausts_after_max_restarts(self):
        b = RestartBudget(max_restarts=3, window=1800, backoff=(1, 2, 3))
        now = 1000.0
        assert not b.exhausted(now)
        for _ in range(3):
            b.record(now)
        assert b.count(now) == 3
        assert b.exhausted(now)

    def test_stamps_age_out_of_window(self):
        b = RestartBudget(max_restarts=2, window=100, backoff=(1,))
        b.record(1000.0)
        b.record(1001.0)
        assert b.exhausted(1001.0)
        # 200s later both stamps have aged out
        assert b.count(1201.0) == 0
        assert not b.exhausted(1201.0)

    def test_backoff_sequence_clamps_to_last(self):
        b = RestartBudget(max_restarts=5, window=1800, backoff=(30, 60, 120))
        assert b.backoff_for(1) == 30
        assert b.backoff_for(2) == 60
        assert b.backoff_for(3) == 120
        assert b.backoff_for(4) == 120  # clamps
        assert b.backoff_for(99) == 120


# --- test doubles ---------------------------------------------------------

class FakeProc:
    """A stand-in subprocess. ``returncode=None`` => alive forever."""

    def __init__(self, returncode=None):
        self.pid = 4242
        self._rc = returncode
        self.terminated = False
        self.killed = False
        self._poll_seq = None

    def with_poll_sequence(self, seq):
        self._poll_seq = list(seq)
        return self

    def poll(self):
        if self._poll_seq:
            return self._poll_seq.pop(0)
        return self._rc

    def terminate(self):
        self.terminated = True
        self._rc = -15

    def kill(self):
        self.killed = True
        self._rc = -9

    def wait(self, timeout=None):
        return self._rc


class Clock:
    def __init__(self, start=1000.0, step=0.0):
        self.t = start
        self.step = step

    def __call__(self):
        v = self.t
        self.t += self.step
        return v


def _cfg(**kw):
    base = dict(poll_interval=0, stall_threshold=600, confirm_polls=2,
                max_restarts=3, restart_window=1800, backoff=(0.1, 0.2, 0.3),
                min_healthy_uptime=120, term_grace=0)
    base.update(kw)
    return SupervisorConfig(**base)


# --- supervisor: wedge restart -------------------------------------------

class TestSupervisorWedge:

    def test_expected_busy_suppresses_ambiguous_restart_chain(self):
        """The exact MOD-364 trigger mix must not consume restart budget while
        a sanctioned heavy command holds a bounded busy lease."""
        spawns = []
        responses = iter([
            {"manager": {"status": "running", "idle_seconds": 0}},
            None,
            None,
            {"manager": {"status": "running", "idle_seconds": 601}},
            {"manager": {"status": "running", "idle_seconds": 632}},
            {"manager": {"status": "error", "idle_seconds": 0}},
            {"manager": {"status": "error", "idle_seconds": 0}},
        ])

        def spawn():
            proc = FakeProc()
            spawns.append(proc)
            return proc

        sup = Supervisor(
            [], _cfg(), now_fn=Clock(), sleep_fn=lambda _: None,
            spawn_fn=spawn, health_fn=lambda: next(responses),
            busy_fn=lambda: {"active": True, "lease_count": 1,
                             "expires_at": 9999.0},
        )
        sup._respawn()

        for _ in range(7):
            assert sup._cycle() is None

        state = sup._report()
        assert len(spawns) == 1
        assert state.restart_count == 0
        assert state.expected_busy == {
            "active": True,
            "lease_count": 1,
            "expires_at": 9999.0,
        }

    def test_expected_busy_does_not_mask_a_real_child_crash(self):
        spawns = []

        def spawn():
            proc = FakeProc(returncode=1 if not spawns else None)
            spawns.append(proc)
            return proc

        sup = Supervisor(
            [], _cfg(), now_fn=Clock(), sleep_fn=lambda _: None,
            spawn_fn=spawn, health_fn=lambda: None,
            busy_fn=lambda: {"active": True, "lease_count": 1,
                             "expires_at": 9999.0},
        )
        sup._respawn()

        assert sup._cycle() is None
        assert len(spawns) == 2
        assert sup._report().restart_count == 1

    def test_confirmed_wedge_restarts_manager(self):
        spawns = []

        def spawn():
            fp = FakeProc()
            spawns.append(fp)
            return fp

        sleeps = []

        sup = Supervisor([], _cfg(), now_fn=Clock(),
                         spawn_fn=spawn,
                         health_fn=lambda: {"manager": {"status": "running",
                                                        "idle_seconds": 9999}})

        def sleep(d):
            sleeps.append(d)
            if len(spawns) >= 2:  # stop once a restart has occurred
                sup.request_stop()

        sup._sleep = sleep
        code = sup.run()

        assert code == 0
        assert len(spawns) == 2          # initial + one restart
        assert spawns[0].terminated      # the wedged child was killed
        # a single confirmed wedge requires confirm_polls reads first
        assert 0.1 in sleeps             # backoff was applied

    def test_single_stalled_read_does_not_restart(self):
        """confirm_polls debounces a one-off stalled sample."""
        spawns = []

        def spawn():
            fp = FakeProc()
            spawns.append(fp)
            return fp

        # alternate wedged / healthy so the confirm counter never reaches 2
        seq = iter([
            {"manager": {"status": "running", "idle_seconds": 9999}},
            {"manager": {"status": "idle", "idle_seconds": 9999}},
            {"manager": {"status": "running", "idle_seconds": 9999}},
            {"manager": {"status": "idle", "idle_seconds": 9999}},
        ])
        calls = {"n": 0}

        def health():
            calls["n"] += 1
            try:
                return next(seq)
            except StopIteration:
                sup.request_stop()
                return {"manager": {"status": "idle", "idle_seconds": 1}}

        sup = Supervisor([], _cfg(), now_fn=Clock(), spawn_fn=spawn,
                         health_fn=health, sleep_fn=lambda d: None)
        code = sup.run()
        assert code == 0
        assert len(spawns) == 1  # never restarted


# --- supervisor: dead-director restart (#12) ------------------------------

class RecordingObserver:
    """Duck-typed SupervisorObserver capturing lifecycle events."""

    def __init__(self):
        self.events = []

    def poll(self, state):
        pass

    def lifecycle(self, event, **fields):
        self.events.append((event, fields))


class TestSupervisorDeadDirector:

    def test_error_status_restarts_immediately_no_confirm(self):
        """status=error is a positive death signal: restart on the FIRST poll
        that reports it - no idle test, no confirm_polls debounce (contrast
        with the wedge path, which needs confirm_polls=2 stalled reads)."""
        spawns = []
        health_reads = {"n": 0}
        reads_at_restart = []
        obs = RecordingObserver()

        def spawn():
            fp = FakeProc()
            if spawns:  # a relaunch: record how many health reads it took
                reads_at_restart.append(health_reads["n"])
            spawns.append(fp)
            return fp

        def health():
            health_reads["n"] += 1
            if len(spawns) >= 2:
                sup.request_stop()
                return {"manager": {"status": "idle", "idle_seconds": 1}}
            return {"manager": {"status": "error", "idle_seconds": 0}}

        sup = Supervisor([], _cfg(), now_fn=Clock(), spawn_fn=spawn,
                         health_fn=health, sleep_fn=lambda d: None,
                         observer=obs)
        code = sup.run()
        assert code == 0
        assert len(spawns) == 2
        assert spawns[0].terminated      # the dead child was killed first
        assert reads_at_restart == [1]   # one status=error read sufficed
        restarts = [f for e, f in obs.events if e == "manager_restarted"]
        assert restarts and restarts[0]["reason"] == "dead"

    def test_error_loop_exhausts_budget_and_exits_nonzero(self):
        """Repeated error-deaths draw on the shared RestartBudget and escalate
        via exit-70 exactly like the wedge path."""
        spawns = []
        announced = []
        obs = RecordingObserver()

        def spawn():
            fp = FakeProc()
            spawns.append(fp)
            return fp

        sup = Supervisor([], _cfg(max_restarts=3),
                         now_fn=Clock(),  # fixed time => all within window
                         spawn_fn=spawn,
                         health_fn=lambda: {"manager": {"status": "error",
                                                        "idle_seconds": 0}},
                         announce_fn=announced.append,
                         sleep_fn=lambda d: None, observer=obs)
        code = sup.run()
        assert code == EXIT_BUDGET_EXHAUSTED
        # initial launch + exactly max_restarts relaunches, then escalate
        assert len(spawns) == 1 + 3
        assert announced, "budget exhaustion must escalate"
        assert "dead-director loop" in announced[0]
        exhausted = [f for e, f in obs.events if e == "budget_exhausted"]
        assert exhausted and exhausted[0]["reason"] == "dead-director loop"

    def test_unknown_status_stays_fail_open(self):
        """Only the explicit death signal restarts. An unrecognized status is
        uncertainty, and uncertainty must never trigger a restart."""
        spawns = []

        def spawn():
            fp = FakeProc()
            spawns.append(fp)
            return fp

        n = {"i": 0}

        def health():
            n["i"] += 1
            if n["i"] >= 6:
                sup.request_stop()
            return {"manager": {"status": "mystery", "idle_seconds": 99_999}}

        sup = Supervisor([], _cfg(), now_fn=Clock(), spawn_fn=spawn,
                         health_fn=health, sleep_fn=lambda d: None)
        code = sup.run()
        assert code == 0
        assert len(spawns) == 1  # never restarted


# --- supervisor: connection-failure path ---------------------------------

class TestSupervisorHealthFailure:

    def test_boot_race_does_not_restart(self):
        """A child that has never been healthy is never restarted for an
        unreachable probe (fail-open during boot)."""
        spawns = []

        def spawn():
            fp = FakeProc()
            spawns.append(fp)
            return fp

        n = {"i": 0}

        def health():
            n["i"] += 1
            if n["i"] >= 5:
                sup.request_stop()
            return None  # port file never appears in this window

        sup = Supervisor([], _cfg(), now_fn=Clock(), spawn_fn=spawn,
                         health_fn=health, sleep_fn=lambda d: None)
        code = sup.run()
        assert code == 0
        assert len(spawns) == 1  # boot race never triggers a restart

    def test_health_failures_after_healthy_restart(self):
        spawns = []

        def spawn():
            fp = FakeProc()
            spawns.append(fp)
            return fp

        # healthy once (sets healthy_since), then connection failures
        seq = [
            {"manager": {"status": "idle", "idle_seconds": 1}},  # healthy
            None,  # fail 1
            None,  # fail 2 -> restart
        ]

        def health():
            if seq:
                return seq.pop(0)
            sup.request_stop()
            return {"manager": {"status": "idle", "idle_seconds": 1}}

        sup = Supervisor([], _cfg(), now_fn=Clock(), spawn_fn=spawn,
                         health_fn=health, sleep_fn=lambda d: None)
        code = sup.run()
        assert code == 0
        assert len(spawns) == 2
        assert spawns[0].terminated


# --- supervisor: crash-loop containment ----------------------------------

class TestSupervisorCrashLoop:

    def test_fast_crash_loop_exhausts_budget_and_exits_nonzero(self):
        spawns = []

        def spawn():
            fp = FakeProc(returncode=3)  # exits immediately, every launch
            spawns.append(fp)
            return fp

        sleeps = []
        announced = []

        sup = Supervisor([], _cfg(max_restarts=3),
                         now_fn=Clock(),  # fixed time => all within window
                         spawn_fn=spawn,
                         health_fn=lambda: None,
                         announce_fn=announced.append)
        sup._sleep = sleeps.append
        code = sup.run()

        assert code == EXIT_BUDGET_EXHAUSTED
        # initial launch + exactly max_restarts relaunches, then escalate
        assert len(spawns) == 1 + 3
        assert announced, "budget exhaustion must escalate"
        # backoff spaced the relaunches (not a tight loop); the zero-length
        # poll-interval sleeps are interleaved and ignored here.
        assert [s for s in sleeps if s] == [0.1, 0.2, 0.3]

    def test_director_never_live_crash_is_charged_as_fast_crash(self):
        """A boot-looping build whose health server answers but whose director
        never reaches running/idle must be charged to the budget (not credited
        as a transient just because /health responded)."""
        spawns = []

        def spawn():
            fp = (FakeProc().with_poll_sequence([None, None, 3])
                  if not spawns else FakeProc())
            spawns.append(fp)
            return fp

        # health answers, but the director is stuck "starting" (never live);
        # the big clock step would make uptime look long IF it were credited.
        sup = Supervisor([], _cfg(min_healthy_uptime=1),
                         now_fn=Clock(start=1000.0, step=10.0),
                         spawn_fn=spawn,
                         health_fn=lambda: {"manager": {"status": "starting",
                                                        "idle_seconds": 0}})

        def sleep(d):
            if len(spawns) >= 2:
                sup.request_stop()

        sup._sleep = sleep
        code = sup.run()
        assert len(spawns) == 2  # relaunched after the crash
        # charged as a fast crash, NOT credited as a transient (a stamp was
        # recorded against the shared budget)
        assert len(sup._budget._stamps) >= 1

    def test_transient_crash_relaunches_without_charging_budget(self):
        spawns = []

        def spawn():
            # first child runs healthy then crashes; relaunch stays up
            fp = (FakeProc().with_poll_sequence([None, 3])
                  if not spawns else FakeProc())
            spawns.append(fp)
            return fp

        # min_healthy_uptime=0 so any healthy time counts as a transient crash
        sup = Supervisor([], _cfg(min_healthy_uptime=0),
                         now_fn=Clock(start=1000.0, step=1.0),
                         spawn_fn=spawn,
                         health_fn=lambda: {"manager": {"status": "idle",
                                                        "idle_seconds": 1}})

        stops = {"n": 0}

        def sleep(d):
            stops["n"] += 1
            if len(spawns) >= 2 and stops["n"] > 2:
                sup.request_stop()

        sup._sleep = sleep
        code = sup.run()
        assert code == 0
        assert len(spawns) == 2  # relaunched after the transient crash
        # the transient crash did not consume the loop budget
        assert sup._budget.count(2000.0) == 0
