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
        sup.run()
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




# --- supervisor: load-grace verdict gate (#903) ---------------------------

class StateRecordingObserver(RecordingObserver):
    """RecordingObserver that also captures the per-poll SupervisorState."""

    def __init__(self):
        super().__init__()
        self.states = []

    def poll(self, state):
        self.states.append(state)


def _active_load(pid, prev):
    return {"active": True, "load1": 8.0, "ncpu": 2, "pegged": True,
            "busy_descendants": 2, "sample": {"9": 100}}


def _inactive_load(pid, prev):
    return {"active": False, "load1": 8.0, "ncpu": 2, "pegged": True,
            "busy_descendants": 0, "sample": {"9": 100}}


class TestSupervisorLoadGrace:

    def _run(self, health, load_fn=_active_load, *, cfg=None, clock=None,
             observer=None, polls=6, stop_after_restart=False):
        """Drive run() to completion and return (sup, spawns, obs, code).

        A fixed-dict ``health`` repeats every poll; a callable is re-invoked.
        Two stop modes: ``polls`` bounds a deferral scenario (the stop lands on
        the final poll itself, so the last observer state still reflects that
        poll's verdict); ``stop_after_restart`` hooks the backoff sleep so a
        restart-expecting scenario stops right after the relaunch.
        """
        spawns = []
        sup_holder = {}
        count = {"n": 0}

        def spawn():
            fp = FakeProc()
            spawns.append(fp)
            return fp

        def health_wrapped():
            count["n"] += 1
            if not stop_after_restart and count["n"] > polls:
                sup_holder["sup"].request_stop()
            return health() if callable(health) else health

        obs = observer or StateRecordingObserver()
        sup = Supervisor([], cfg or _cfg(), now_fn=clock or Clock(),
                         spawn_fn=spawn, health_fn=health_wrapped,
                         load_fn=load_fn, sleep_fn=lambda d: None,
                         observer=obs)
        sup_holder["sup"] = sup

        if stop_after_restart:
            def sleep(d):
                if len(spawns) >= 2:
                    sup.request_stop()
            sup._sleep = sleep

        code = sup.run()
        return sup, spawns, obs, code

    def _deferrals(self, obs):
        return [s.load_grace for s in obs.states if s.load_grace]

    # --- deferral at each verdict point -----------------------------------

    def test_probe_miss_verdict_defers(self):
        """A confirmed probe-miss streak on a previously-healthy manager defers
        instead of restarting while the load evidence holds - the #903 trap."""
        reads = {"n": 0}

        def health():
            reads["n"] += 1
            if reads["n"] == 1:
                return {"manager": {"status": "idle", "idle_seconds": 1}}
            return None  # probe misses from poll 2 on

        sup, spawns, obs, code = self._run(health, polls=8)
        assert code == 0
        assert len(spawns) == 1  # never restarted
        assert self._deferrals(obs)[-1]["deferred"] == "probe_miss"
        assert obs.states[-1].restart_count == 0  # nothing charged

    def test_stalled_turn_verdict_defers(self):
        sup, spawns, obs, code = self._run(
            {"manager": {"status": "running", "idle_seconds": 9999}})
        assert code == 0
        assert len(spawns) == 1
        assert self._deferrals(obs)[-1]["deferred"] == "stalled_turn"
        assert obs.states[-1].restart_count == 0

    def test_dead_director_verdict_defers(self):
        """A load-induced status=error on a busy manager is ambiguous too:
        deferred, not the immediate #12 restart."""
        sup, spawns, obs, code = self._run(
            {"manager": {"status": "error", "idle_seconds": 0}})
        assert code == 0
        assert len(spawns) == 1
        assert self._deferrals(obs)[-1]["deferred"] == "dead_director"
        assert obs.states[-1].restart_count == 0

    # --- the gate only opens on real evidence -----------------------------

    def test_evidence_inactive_restarts_anyway(self):
        """Pegged host but NO busy descendants: not legitimately busy, so the
        verdict fires and the budget charges exactly as before."""
        sup, spawns, obs, code = self._run(
            {"manager": {"status": "error", "idle_seconds": 0}},
            load_fn=_inactive_load, stop_after_restart=True)
        assert code == 0
        assert len(spawns) == 2  # restarted
        assert not self._deferrals(obs)
        assert obs.states[-1].restart_count == 1  # charged

    def test_load_fn_raises_fails_closed(self):
        """Unreadable evidence must never defer a restart - the fail-closed
        rule holds even when the /proc walk itself throws."""

        def boom(pid, prev):
            raise OSError("proc unavailable")

        sup, spawns, obs, code = self._run(
            {"manager": {"status": "error", "idle_seconds": 0}},
            load_fn=boom, stop_after_restart=True)
        assert code == 0
        assert len(spawns) == 2  # restarted
        assert obs.states[-1].restart_count == 1

    def test_disabled_knob_bypasses_the_gate(self):
        sup, spawns, obs, code = self._run(
            {"manager": {"status": "error", "idle_seconds": 0}},
            cfg=_cfg(load_grace_enabled=0), stop_after_restart=True)
        assert code == 0
        assert len(spawns) == 2  # restarted immediately, no deferral
        assert not self._deferrals(obs)

    # --- spell and cap -----------------------------------------------------

    def test_spell_cap_forces_the_verdict_through(self):
        """One continuous unresponsive stretch longer than load_grace_max
        drops the deferral so a genuinely dead manager still escalates."""
        # Clock steps 1s per now() call; the error path calls now() twice per
        # poll (defer + report), so each poll advances the spell ~2s.
        clock = Clock(start=1000.0, step=1.0)
        sup, spawns, obs, code = self._run(
            {"manager": {"status": "error", "idle_seconds": 0}},
            cfg=_cfg(load_grace_max=3.0), clock=clock,
            stop_after_restart=True)
        assert code == 0
        assert len(spawns) == 2  # the cap let the verdict fire
        assert obs.states[-1].restart_count == 1
        # The first polls deferred before the cap dropped the exemption.
        assert len(self._deferrals(obs)) >= 1

    def test_working_poll_resets_the_spell(self):
        """A live, non-stalled health response between stretches resets the
        cap, so intermittent busy periods never accumulate into a kill."""
        # Each error poll advances the spell ~2s; stretches are 2 polls long
        # (~2s at the second verdict), under the 3s cap ONLY if each working
        # poll resets it - cumulatively the errors span far more than 3s.
        seq = iter([
            {"manager": {"status": "error", "idle_seconds": 0}},
            {"manager": {"status": "error", "idle_seconds": 0}},
            {"manager": {"status": "idle", "idle_seconds": 1}},
            {"manager": {"status": "error", "idle_seconds": 0}},
            {"manager": {"status": "error", "idle_seconds": 0}},
            {"manager": {"status": "idle", "idle_seconds": 1}},
            {"manager": {"status": "error", "idle_seconds": 0}},
            {"manager": {"status": "error", "idle_seconds": 0}},
            {"manager": {"status": "idle", "idle_seconds": 1}},
            {"manager": {"status": "error", "idle_seconds": 0}},
        ])

        def health():
            try:
                return next(seq)
            except StopIteration:
                return {"manager": {"status": "idle", "idle_seconds": 1}}

        sup, spawns, obs, code = self._run(
            health, cfg=_cfg(load_grace_max=3.0),
            clock=Clock(start=1000.0, step=1.0), polls=10)
        assert code == 0
        assert len(spawns) == 1  # no stretch ever exceeded the cap
        assert self._deferrals(obs)[-1]["deferred"] == "dead_director"
        # The working polls between stretches reported a cleared block.
        cleared = [s for s in obs.states if s.health
                   and s.health["manager"]["status"] == "idle"]
        assert cleared and all(s.load_grace is None for s in cleared)

    def test_inactive_evidence_between_verdicts_resets_the_spell(self):
        """Evidence is sampled every poll, so a drop must clear the current
        grace spell even when that poll is only the first confirmation read.

        Otherwise separate busy stretches accumulate against one cap and the
        heartbeat keeps claiming a deferral while the current evidence is
        inactive.
        """
        samples = iter([
            _active_load(None, None),    # stalled confirm 1
            _active_load(None, None),    # stalled confirm 2: defer
            _inactive_load(None, None),  # next stretch confirm 1: must clear
            _active_load(None, None),    # confirm 2: a NEW spell
            _active_load(None, None),
        ])

        def load_fn(pid, prev):
            return next(samples)

        _sup, spawns, obs, code = self._run(
            {"manager": {"status": "running", "idle_seconds": 9999}},
            load_fn=load_fn, cfg=_cfg(load_grace_max=3.0),
            clock=Clock(start=1000.0, step=1.0), polls=4)

        assert code == 0
        assert len(spawns) == 1
        assert obs.states[2].load_grace is None
        deferrals = self._deferrals(obs)
        assert len(deferrals) >= 2
        assert deferrals[1]["since"] > deferrals[0]["since"]

    # --- the crash path stays authoritative --------------------------------

    def test_crash_path_never_deferred(self):
        """A REAL child exit charges and relaunches even under active load
        evidence - the gate only defers ambiguous verdicts, never deaths."""
        spawns = []
        obs = StateRecordingObserver()

        def spawn():
            # First child crashes after one poll; the relaunch stays up.
            fp = FakeProc().with_poll_sequence([None, 3]) if not spawns \
                else FakeProc()
            spawns.append(fp)
            return fp

        sup = Supervisor([], _cfg(min_healthy_uptime=0),
                         now_fn=Clock(start=1000.0, step=1.0),
                         spawn_fn=spawn,
                         health_fn=lambda: {"manager": {"status": "idle",
                                                        "idle_seconds": 1}},
                         load_fn=_active_load, sleep_fn=lambda d: None,
                         observer=obs)

        def sleep(d):
            if len(spawns) >= 2:
                sup.request_stop()

        sup._sleep = sleep
        code = sup.run()
        assert code == 0
        assert len(spawns) == 2  # relaunched despite the active evidence
        restarts = [f for e, f in obs.events if e == "manager_restarted"]
        assert restarts and restarts[0]["reason"] == "crash"

    # --- the first-verdict baseline problem --------------------------------

    def test_first_verdict_of_a_heavy_period_defers(self):
        """The per-poll baseline refresh means the FIRST verdict of a heavy
        period already has a prior CPU sample - it defers too, not just
        verdicts that arrive once a deferral is underway."""
        # load_fn models the real sampler: no previous sample => no busy
        # delta yet (fail closed), then active once a baseline exists.
        def load_fn(pid, prev):
            active = prev is not None
            return {"active": active, "load1": 8.0, "ncpu": 2,
                    "pegged": True, "busy_descendants": 1 if active else 0,
                    "sample": {"9": 100}}

        seq = iter([
            {"manager": {"status": "idle", "idle_seconds": 1}},   # baseline
            {"manager": {"status": "idle", "idle_seconds": 1}},   # baseline
            {"manager": {"status": "error", "idle_seconds": 0}},  # first verdict
            {"manager": {"status": "error", "idle_seconds": 0}},
        ])

        def health():
            try:
                return next(seq)
            except StopIteration:
                return {"manager": {"status": "idle", "idle_seconds": 1}}

        sup, spawns, obs, code = self._run(health, load_fn=load_fn, polls=5)
        assert code == 0
        assert len(spawns) == 1  # the first error verdict deferred too
        assert self._deferrals(obs)[-1]["deferred"] == "dead_director"
        # The deferral block carries the diagnosis fields for the heartbeat.
        block = self._deferrals(obs)[-1]
        assert set(block) >= {"active", "since", "spell_s", "deferred",
                              "load1", "ncpu", "busy_descendants"}
        assert block["load1"] == 8.0
        assert block["busy_descendants"] == 1
