"""Unit tests for the supervisor's deduped Slack incident alerting (issue #4).

The alerter is a SupervisorObserver: these tests drive it with the exact
lifecycle events + poll states the supervisor emits (and, for the end-to-end
shape, through a real Supervisor with injected doubles), asserting the incident
model: soft alert once at crash-loop onset, loud alert per exhaustion cycle
with the persisted machine-cycle counter, recovery closes and resets, and every
path is fail-open.
"""

from bobi.supervisor.alerting import SlackAlerter
from bobi.supervisor.config import SupervisorConfig
from bobi.supervisor.supervision import (
    EXIT_BUDGET_EXHAUSTED,
    CompositeObserver,
    Supervisor,
    SupervisorState,
)


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
                min_healthy_uptime=120, term_grace=0, machine_restart_cap=10)
    base.update(kw)
    return SupervisorConfig(**base)


IDENTITY = {"fleet": "ci", "instance": "canary"}


def _alerter(tmp_path, clock=None, config=None, posts=None, emitted=None,
             post_result=True):
    posts = posts if posts is not None else []
    emitted = emitted if emitted is not None else []

    def post(message):
        posts.append(message)
        return post_result

    def emit(event, **fields):
        emitted.append((event, fields))

    a = SlackAlerter(project_root=None, config=config or _cfg(),
                     identity=IDENTITY, now_fn=clock or Clock(),
                     post_fn=post, lifecycle_fn=emit,
                     state_path=tmp_path / "incident.json")
    return a, posts, emitted


_HEALTHY_BODY = {"manager": {"status": "idle", "idle_seconds": 1}}


def _state(*, ever_healthy=True, desired="running", health=_HEALTHY_BODY,
           health_fail_count=0):
    return SupervisorState(
        health=health, child_alive=True, supervisor_uptime_s=10.0,
        restart_count=0, last_restart_reason=None, last_restart_at=None,
        ever_healthy=ever_healthy, health_fail_count=health_fail_count,
        desired_state=desired,
    )


# --- crash-loop onset (the soft alert) -------------------------------------

class TestSoftAlert:

    def test_second_charged_restart_posts_once(self, tmp_path):
        a, posts, emitted = _alerter(tmp_path)
        a.lifecycle("manager_restarted", reason="crash", restart_count=1,
                    charged=True)
        assert posts == []  # one fast crash can be a transient
        a.lifecycle("manager_restarted", reason="crash", restart_count=2,
                    charged=True, exit_code=1, healthy_uptime_s=0.0)
        assert len(posts) == 1
        assert "ci/canary is crash-looping" in posts[0]
        assert "exit_code=1" in posts[0]
        assert "fly logs -a ci-canary" in posts[0]
        # dedup: further charged restarts in the same incident do not repost
        a.lifecycle("manager_restarted", reason="crash", restart_count=3,
                    charged=True)
        assert len(posts) == 1
        # the incident edge also landed on the lifecycle trail
        assert ("crash_looping", {"reason": "crash", "restart_count": 2}) \
            in emitted

    def test_dead_and_wedge_reasons_also_open_incidents(self, tmp_path):
        for reason in ("dead", "wedge"):
            a, posts, _ = _alerter(tmp_path / reason)
            a.lifecycle("manager_restarted", reason=reason, restart_count=2,
                        charged=True)
            assert len(posts) == 1, reason

    def test_operator_restart_never_alerts(self, tmp_path):
        a, posts, _ = _alerter(tmp_path)
        a.lifecycle("manager_restarted", reason="operator", restart_count=5)
        assert posts == []

    def test_uncharged_transient_crash_never_opens_an_incident(self, tmp_path):
        # The trap: a transient healthy-uptime crash is NOT charged but its
        # manager_restarted event carries the stale window count of earlier
        # charged restarts. After a recovery cleared the incident, that single
        # transient must not reopen one and post a false crash-looping alert.
        a, posts, _ = _alerter(tmp_path)
        a.lifecycle("manager_restarted", reason="crash", restart_count=2,
                    charged=False, exit_code=1, healthy_uptime_s=200.0)
        assert posts == []
        assert not (tmp_path / "incident.json").exists()


# --- budget exhaustion (the loud alert) -------------------------------------

class TestExhaustionAlert:

    def test_posts_with_cycle_counter_and_suppresses_plain_announce(
            self, tmp_path):
        a, posts, _ = _alerter(tmp_path)
        a.lifecycle("budget_exhausted", reason="crash loop", restart_count=3)
        assert len(posts) == 1
        assert "restart budget exhausted (machine cycle 1/10)" in posts[0]
        assert "crash loop" in posts[0]
        # _escalate calls announce AFTER the lifecycle event: the rich alert
        # already posted, so the ported plain message is suppressed.
        a.announce_fallback("plain ported message")
        assert len(posts) == 1

    def test_plain_announce_fires_when_rich_post_failed(self, tmp_path):
        a, posts, _ = _alerter(tmp_path, post_result=False)
        a.lifecycle("budget_exhausted", reason="crash loop", restart_count=3)
        a.announce_fallback("plain ported message")
        assert posts[-1] == "plain ported message"

    def test_cycle_counter_persists_across_machine_restarts(self, tmp_path):
        # Each machine cycle is a fresh supervisor process = a fresh alerter
        # over the same volume state file.
        for expected_cycle in (1, 2, 3):
            a, posts, _ = _alerter(tmp_path)
            a.lifecycle("budget_exhausted", reason="crash loop",
                        restart_count=3)
            assert f"machine cycle {expected_cycle}/10" in posts[0]

    def test_at_cap_alert_is_marked_terminal(self, tmp_path):
        a, posts, _ = _alerter(tmp_path, config=_cfg(machine_restart_cap=2))
        a.lifecycle("budget_exhausted", reason="wedge loop", restart_count=3)
        assert "PARKED" not in posts[0]
        # even the pre-terminal alert says a human is needed now
        assert "needs a human" in posts[0]
        b, posts2, _ = _alerter(tmp_path, config=_cfg(machine_restart_cap=2))
        b.lifecycle("budget_exhausted", reason="wedge loop", restart_count=3)
        # terminal: stated as an inference (the supervisor cannot observe the
        # actual park), with a verify pointer
        assert "PARKED" in posts2[0]
        assert "Human intervention required" in posts2[0]
        assert "fly status -a ci-canary" in posts2[0]


# --- recovery ----------------------------------------------------------------

class TestRecovery:

    def test_healthy_past_min_uptime_closes_incident(self, tmp_path):
        clock = Clock(start=1000.0, step=0.0)
        a, posts, emitted = _alerter(tmp_path, clock=clock)
        a.lifecycle("manager_restarted", reason="crash", restart_count=2,
                    charged=True)
        assert len(posts) == 1

        a.poll(_state(ever_healthy=True))   # healthy_since = now
        clock.t += 200.0                    # past min_healthy_uptime (120)
        a.poll(_state(ever_healthy=True))
        assert len(posts) == 2
        assert "recovered" in posts[1]
        assert not (tmp_path / "incident.json").exists()
        assert any(e == "incident_recovered" for e, _ in emitted)

        # closed: further healthy polls do not repost
        clock.t += 500.0
        a.poll(_state(ever_healthy=True))
        assert len(posts) == 2

    def test_recovery_resets_the_machine_cycle_counter(self, tmp_path):
        clock = Clock(start=1000.0)
        a, posts, _ = _alerter(tmp_path, clock=clock)
        a.lifecycle("budget_exhausted", reason="crash loop", restart_count=3)
        assert "machine cycle 1/10" in posts[0]

        a.poll(_state(ever_healthy=True))
        clock.t += 200.0
        a.poll(_state(ever_healthy=True))   # closes + resets

        b, posts2, _ = _alerter(tmp_path, clock=clock)
        b.lifecycle("budget_exhausted", reason="crash loop", restart_count=3)
        assert "machine cycle 1/10" in posts2[0]  # back to 1, not 2

    def test_unhealthy_boot_resets_the_healthy_timer(self, tmp_path):
        clock = Clock(start=1000.0)
        a, posts, _ = _alerter(tmp_path, clock=clock)
        a.lifecycle("manager_restarted", reason="crash", restart_count=2,
                    charged=True)

        a.poll(_state(ever_healthy=True))
        clock.t += 100.0                     # not yet past 120
        a.poll(_state(ever_healthy=False))   # respawn: per-boot flag reset
        clock.t += 100.0
        a.poll(_state(ever_healthy=True))    # healthy again, timer restarts
        assert len(posts) == 1               # no premature recovery
        clock.t += 200.0
        a.poll(_state(ever_healthy=True))
        assert len(posts) == 2

    def test_operator_stop_does_not_count_as_recovery(self, tmp_path):
        clock = Clock(start=1000.0)
        a, posts, _ = _alerter(tmp_path, clock=clock)
        a.lifecycle("manager_restarted", reason="crash", restart_count=2,
                    charged=True)
        a.poll(_state(ever_healthy=True, desired="stopped"))
        clock.t += 500.0
        a.poll(_state(ever_healthy=True, desired="stopped"))
        assert len(posts) == 1  # incident stays open, no recovered post

    def test_wedging_director_is_not_a_recovery(self, tmp_path):
        # ever_healthy is a per-boot LATCH: it stays True while a director
        # wedges (idle climbing in an active turn) or its health thread dies.
        # Closing on the latch would post a false RECOVERED and zero the park
        # counter mid-incident; the gate must use the live verdict.
        clock = Clock(start=1000.0)
        a, posts, _ = _alerter(tmp_path, clock=clock)
        a.lifecycle("manager_restarted", reason="crash", restart_count=2,
                    charged=True)
        assert len(posts) == 1

        wedging = {"manager": {"status": "running", "idle_seconds": 9999}}
        a.poll(_state(ever_healthy=True, health=wedging))
        clock.t += 500.0  # far past min_healthy_uptime
        a.poll(_state(ever_healthy=True, health=wedging))
        assert len(posts) == 1  # no false recovery while wedging
        assert (tmp_path / "incident.json").exists()

        # Dead health thread under a live process: fail count climbing, the
        # last payload gone - also not a recovery.
        a.poll(_state(ever_healthy=True, health=None, health_fail_count=1))
        clock.t += 500.0
        a.poll(_state(ever_healthy=True, health=None, health_fail_count=2))
        assert len(posts) == 1

        # Actual recovery still closes it.
        a.poll(_state(ever_healthy=True))
        clock.t += 200.0
        a.poll(_state(ever_healthy=True))
        assert len(posts) == 2
        assert "recovered" in posts[1]


# --- fail-open ---------------------------------------------------------------

class TestFailOpen:

    def test_post_fn_raising_never_propagates(self, tmp_path):
        def post(message):
            raise RuntimeError("slack down")

        a = SlackAlerter(project_root=None, config=_cfg(), identity=IDENTITY,
                         now_fn=Clock(), post_fn=post,
                         state_path=tmp_path / "incident.json")
        a.lifecycle("manager_restarted", reason="crash", restart_count=2,
                    charged=True)
        a.lifecycle("budget_exhausted", reason="crash loop", restart_count=3)
        a.announce_fallback("plain")
        a.poll(_state())

    def test_unwritable_state_path_never_propagates(self, tmp_path):
        a, posts, _ = _alerter(tmp_path)
        a._state_path = tmp_path  # a directory: read/write/unlink all fail
        a.lifecycle("manager_restarted", reason="crash", restart_count=2,
                    charged=True)
        assert len(posts) == 1  # alerting still works, persistence is lost

    def test_corrupt_state_file_is_ignored(self, tmp_path):
        (tmp_path / "incident.json").write_text("{not json")
        a, posts, _ = _alerter(tmp_path)
        a.lifecycle("manager_restarted", reason="crash", restart_count=2,
                    charged=True)
        assert len(posts) == 1


# --- composition -------------------------------------------------------------

class FakeProc:
    def __init__(self, returncode=None):
        self.pid = 4242
        self._rc = returncode

    def poll(self):
        return self._rc

    def terminate(self):
        self._rc = -15

    def kill(self):
        self._rc = -9

    def wait(self, timeout=None):
        return self._rc


class TestComposition:

    def test_composite_isolates_a_failing_observer(self, tmp_path):
        seen = []

        class Bad:
            def poll(self, state):
                raise RuntimeError("boom")

            def lifecycle(self, event, **fields):
                raise RuntimeError("boom")

        class Good:
            def poll(self, state):
                seen.append("poll")

            def lifecycle(self, event, **fields):
                seen.append(event)

        comp = CompositeObserver([Bad(), Good()])
        comp.poll(_state())
        comp.lifecycle("manager_started")
        assert seen == ["poll", "manager_started"]

    def test_crash_loop_through_a_real_supervisor(self, tmp_path):
        """End-to-end shape: a boot-crashing child driven through the real
        Supervisor produces the soft alert at the 2nd charged restart and the
        exhaustion alert at budget exhaustion, with the plain announce
        suppressed."""
        posts = []
        a = SlackAlerter(project_root=None, config=_cfg(), identity=IDENTITY,
                         now_fn=Clock(), post_fn=lambda m: posts.append(m) or True,
                         state_path=tmp_path / "incident.json")
        sup = Supervisor([], _cfg(max_restarts=3), now_fn=Clock(),
                         spawn_fn=lambda: FakeProc(returncode=3),
                         health_fn=lambda: None,
                         observer=CompositeObserver([a]),
                         announce_fn=a.announce_fallback,
                         sleep_fn=lambda d: None)
        code = sup.run()
        assert code == EXIT_BUDGET_EXHAUSTED
        assert len(posts) == 2
        assert "crash-looping" in posts[0]
        assert "restart budget exhausted" in posts[1]
        assert "machine cycle 1/10" in posts[1]
