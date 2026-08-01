"""Unit tests for the operator control plane on the supervision core (#9).

The admin listener turns an operator command into one of these thread-safe
requests; here we drive the supervisor's main loop with injected time/spawn/
health/sleep and assert the loop EXECUTES the request correctly: an operator
restart bounces the child and clears the crash budget, a stop holds it down
without the crash path relaunching it, and a start brings it back.
"""

from bobi.supervisor.config import SupervisorConfig
from bobi.supervisor.supervision import (
    EXIT_BUDGET_EXHAUSTED,
    Supervisor,
    SupervisorObserver,
)


class FakeProc:
    """A stand-in subprocess. ``returncode=None`` => alive forever."""

    def __init__(self, returncode=None):
        self.pid = 4242
        self._rc = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
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


class RecordingObserver(SupervisorObserver):
    def __init__(self):
        self.events = []
        self.polls = []

    def poll(self, state):
        self.polls.append(state)

    def lifecycle(self, event, **fields):
        self.events.append((event, fields))


def _cfg(**kw):
    base = dict(poll_interval=0, stall_threshold=600, confirm_polls=2,
                max_restarts=3, restart_window=1800, backoff=(0.1, 0.2, 0.3),
                min_healthy_uptime=0, term_grace=0)
    base.update(kw)
    return SupervisorConfig(**base)


def _healthy():
    return {"manager": {"status": "idle", "idle_seconds": 1}}


def _spawner(spawns):
    def spawn():
        fp = FakeProc()
        spawns.append(fp)
        return fp
    return spawn


def _lifecycle_names(obs):
    return [e for e, _ in obs.events]


# --- operator restart -----------------------------------------------------

def test_operator_restart_bounces_child_and_resets_budget():
    spawns = []
    obs = RecordingObserver()
    sup = Supervisor([], _cfg(), now_fn=Clock(), spawn_fn=_spawner(spawns),
                     health_fn=_healthy, observer=obs)
    # Pre-charge the crash budget to exhaustion; an operator restart must clear
    # it (intentional bounce != crash) and proceed regardless.
    for _ in range(3):
        sup._budget.record(1000.0)
    assert sup._budget.exhausted(1000.0)

    sup.request_manager_restart()

    def sleep(_d):
        if len(spawns) >= 2:
            sup.request_stop()
    sup._sleep = sleep

    code = sup.run()
    assert code == 0                       # not EXIT_BUDGET_EXHAUSTED
    assert len(spawns) == 2                # initial + operator restart
    assert spawns[0].terminated            # old child killed
    assert sup._budget.count(1000.0) == 0  # budget cleared
    restarts = [f for e, f in obs.events if e == "manager_restarted"]
    assert restarts and restarts[0]["reason"] == "operator"


def test_operator_restart_survives_exhausted_budget_without_escalating():
    spawns = []
    announced = []
    sup = Supervisor([], _cfg(max_restarts=1), now_fn=Clock(),
                     spawn_fn=_spawner(spawns), health_fn=_healthy,
                     announce_fn=announced.append)
    sup._budget.record(1000.0)  # already at the limit (max_restarts=1)
    sup.request_manager_restart()

    def sleep(_d):
        if len(spawns) >= 2:
            sup.request_stop()
    sup._sleep = sleep

    code = sup.run()
    assert code == 0
    assert not announced, "an operator restart must not trip budget escalation"


# --- operator stop / start ------------------------------------------------

def test_operator_stop_holds_manager_down_no_relaunch():
    spawns = []
    obs = RecordingObserver()
    sup = Supervisor([], _cfg(), now_fn=Clock(), spawn_fn=_spawner(spawns),
                     health_fn=_healthy, observer=obs)
    iters = {"n": 0}

    def sleep(_d):
        iters["n"] += 1
        if iters["n"] == 1:
            sup.request_manager_stop()
        elif iters["n"] >= 5:
            sup.request_stop()
    sup._sleep = sleep

    code = sup.run()
    assert code == 0
    assert len(spawns) == 1                       # never respawned while stopped
    assert spawns[0].terminated                   # the child was stopped
    assert "manager_stopped" in _lifecycle_names(obs)
    stops = [f for e, f in obs.events if e == "manager_stopped"]
    assert any(f.get("reason") == "operator" for f in stops)
    assert sup.desired_state() == "stopped"
    # The heartbeat reports the intentional stop, not a probe failure.
    assert obs.polls[-1].desired_state == "stopped"


def test_operator_start_after_stop_respawns():
    spawns = []
    obs = RecordingObserver()
    sup = Supervisor([], _cfg(), now_fn=Clock(), spawn_fn=_spawner(spawns),
                     health_fn=_healthy, observer=obs)
    iters = {"n": 0}

    def sleep(_d):
        iters["n"] += 1
        if iters["n"] == 1:
            sup.request_manager_stop()
        elif iters["n"] == 3:
            sup.request_manager_start()
        elif iters["n"] >= 6:
            sup.request_stop()
    sup._sleep = sleep

    code = sup.run()
    assert code == 0
    assert len(spawns) == 2                        # initial + start after stop
    assert sup.desired_state() == "running"
    started = [f for e, f in obs.events if e == "manager_started"]
    # first is the boot start (no reason), then the operator start
    assert any(f.get("reason") == "operator" for f in started)


def test_stop_is_idempotent():
    spawns = []
    obs = RecordingObserver()
    sup = Supervisor([], _cfg(), now_fn=Clock(), spawn_fn=_spawner(spawns),
                     health_fn=_healthy, observer=obs)
    iters = {"n": 0}

    def sleep(_d):
        iters["n"] += 1
        if iters["n"] in (1, 2, 3):
            sup.request_manager_stop()
        elif iters["n"] >= 5:
            sup.request_stop()
    sup._sleep = sleep

    sup.run()
    # Only ONE operator-reason manager_stopped edge despite repeated stop
    # requests (the run() teardown emits its own reasonless manager_stopped).
    operator_stops = [f for e, f in obs.events
                      if e == "manager_stopped" and f.get("reason") == "operator"]
    assert len(operator_stops) == 1
