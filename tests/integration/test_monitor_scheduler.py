"""Integration tests for the monitor scheduler subsystem.

Exercises MonitorRegistry loading, MonitorScheduler lifecycle, all
monitor flavors (command, check, notify), dedup, and state persistence
— against a real filesystem with real YAML configs.
"""

import json
import textwrap
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from bobi.monitors.schema import Condition, Monitor


def _make_scheduler(tmp_path, monitors, publish=None, checks=None, now=None):
    """Create a MonitorScheduler with given monitors and state path."""
    from bobi.monitors.scheduler import MonitorScheduler

    class FakeRegistry:
        def effective_monitors(self):
            return monitors
        def projects_for(self, _m):
            return []

    return MonitorScheduler(
        publish=publish or (lambda event, data: True),
        state_path=tmp_path / "monitor_state.json",
        now=now or (lambda: datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)),
        registry_loader=lambda **kw: FakeRegistry(),
        spawn_check=lambda _m, _c, _cb: None,
    )


class TestMonitorRegistryLoading:
    """MonitorRegistry loads and merges defaults + project monitors."""

    def test_loads_project_monitors(self, tmp_path):
        """Monitors from run/package/monitors.yaml are loaded."""
        config_dir = tmp_path / "package"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text("entry_point: manager\n")
        (config_dir / "monitors.yaml").write_text(yaml.dump({
            "monitors": [
                {"name": "my-check", "command": "echo ok", "interval": "5m"},
            ]
        }))

        from bobi.monitors.registry import MonitorRegistry
        reg = MonitorRegistry.load(project_path=tmp_path)

        effective = reg.effective_monitors()
        assert any(m.name == "my-check" for m in effective)

    def test_project_disables_default(self, tmp_path):
        """enabled: false in project config disables a default monitor."""
        config_dir = tmp_path / "package"
        monitors_dir = config_dir / "monitors"
        monitors_dir.mkdir(parents=True)

        # Default monitor
        (monitors_dir / "defaults.yaml").write_text(yaml.dump({
            "monitors": [
                {"name": "pr_conflicts", "check": "pr_conflicts", "interval": "15m"},
            ]
        }))

        # Project override disables it
        (config_dir / "monitors.yaml").write_text(yaml.dump({
            "monitors": [
                {"name": "pr_conflicts", "enabled": False},
            ]
        }))
        (config_dir / "agent.yaml").write_text("entry_point: manager\n")

        from bobi.monitors.registry import MonitorRegistry
        reg = MonitorRegistry.load(project_path=tmp_path)

        effective = reg.effective_monitors()
        # The default should still be in globals but the opt-out prevents it
        # from running for this project
        assert "pr_conflicts" in reg.opt_outs


class TestSchedulerTick:
    """Scheduler tick runs due monitors."""

    def test_first_tick_runs_monitor(self, tmp_path):
        """A monitor with no prior state runs on first tick."""
        fired = []
        m = Monitor(name="new-check", command="echo '[]'", interval="5m")

        sched = _make_scheduler(
            tmp_path, [m],
            publish=lambda ev, data: (fired.append(ev), True)[1],
        )
        sched.tick()

        # Command with empty output → no events, but state records last_run
        assert "new-check" in sched.state
        assert "last_run" in sched.state["new-check"]

    def test_not_due_within_interval(self, tmp_path):
        """A monitor that ran recently is skipped."""
        m = Monitor(name="recent", command="echo '[]'", interval="5m")
        run_count = []

        t = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        sched = _make_scheduler(
            tmp_path, [m],
            now=lambda: t,
        )

        # First tick runs
        sched.tick()

        # Advance 1 minute (less than 5m interval)
        t_new = t + timedelta(minutes=1)
        sched._now = lambda: t_new
        state_before = json.loads((tmp_path / "monitor_state.json").read_text())

        sched.tick()

        # State unchanged (didn't run again)
        state_after = json.loads((tmp_path / "monitor_state.json").read_text())
        assert state_before["recent"]["last_run"] == state_after["recent"]["last_run"]


class TestNotifyMonitor:
    """Notify monitors fire every time they're due."""

    def test_fires_each_time(self, tmp_path):
        fired = []
        m = Monitor(name="standup", notify=True, interval="1m",
                    event="monitor/standup", description="Daily standup")

        t = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        sched = _make_scheduler(
            tmp_path, [m],
            publish=lambda ev, data: (fired.append(data), True)[1],
            now=lambda: t,
        )

        sched.tick()
        assert len(fired) == 1
        assert fired[0]["description"] == "Daily standup"

        # Advance past interval
        t2 = t + timedelta(minutes=2)
        sched._now = lambda: t2
        sched.tick()
        assert len(fired) == 2  # fires again (not deduped)


class TestCommandMonitor:
    """Command monitors run shell commands and parse JSON output."""

    def test_json_output_fires_events(self, tmp_path):
        fired = []
        m = Monitor(
            name="cmd-test",
            command='echo \'[{"id":"x1","msg":"found"}]\'',
            event="test/cmd",
            interval="1m",
        )

        sched = _make_scheduler(
            tmp_path, [m],
            publish=lambda ev, data: (fired.append(data), True)[1],
        )
        sched.tick()

        assert len(fired) == 1
        assert fired[0]["msg"] == "found"

    def test_empty_output_clears_active(self, tmp_path):
        """Empty command output means 'all clear' — active conditions drop."""
        m = Monitor(
            name="clearable",
            command="echo ''",
            event="test/clear",
            interval="1m",
        )

        sched = _make_scheduler(tmp_path, [m])
        sched.tick()

        assert sched.state.get("clearable", {}).get("active", []) == []

    def test_failed_command_is_indeterminate(self, tmp_path):
        """Failed command doesn't clear state — leaves it untouched."""
        fired = []
        m = Monitor(
            name="failing",
            command="exit 1",
            event="test/fail",
            interval="1m",
        )

        sched = _make_scheduler(
            tmp_path, [m],
            publish=lambda ev, data: (fired.append(data), True)[1],
        )
        sched.tick()

        assert len(fired) == 0  # nothing published
        # State has last_run but no active conditions
        assert "failing" in sched.state


class TestDedup:
    """Reconciliation deduplicates conditions by key."""

    def test_same_condition_not_refired(self, tmp_path):
        fired = []
        m = Monitor(
            name="dedup",
            command='echo \'[{"id":"same"}]\'',
            event="test/dedup",
            interval="1m",
        )

        t = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        sched = _make_scheduler(
            tmp_path, [m],
            publish=lambda ev, data: (fired.append(data), True)[1],
            now=lambda: t,
        )

        sched.tick()
        assert len(fired) == 1

        # Advance and tick again — same condition, no new event
        t2 = t + timedelta(minutes=5)
        sched._now = lambda: t2
        sched.tick()
        assert len(fired) == 1  # still 1

    def test_resolved_then_recurs_refires(self, tmp_path):
        """A condition that clears and reappears fires again."""
        fired = []
        script = tmp_path / "check.sh"

        # First: condition present
        script.write_text('#!/bin/sh\necho \'[{"id":"flap"}]\'')
        script.chmod(0o755)

        m = Monitor(name="flap", command=str(script), event="test/flap", interval="1m")

        t = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        sched = _make_scheduler(
            tmp_path, [m],
            publish=lambda ev, data: (fired.append(data), True)[1],
            now=lambda: t,
        )

        sched.tick()
        assert len(fired) == 1

        # Condition resolves
        t2 = t + timedelta(minutes=5)
        sched._now = lambda: t2
        script.write_text("#!/bin/sh\necho '[]'")
        sched.tick()
        assert sched.state["flap"].get("active", []) == []

        # Condition recurs
        t3 = t2 + timedelta(minutes=5)
        sched._now = lambda: t3
        script.write_text('#!/bin/sh\necho \'[{"id":"flap"}]\'')
        sched.tick()
        assert len(fired) == 2  # refired


class TestStatePersistence:
    """Monitor state survives scheduler restart."""

    def test_state_persists_to_disk(self, tmp_path):
        m = Monitor(name="persist", command="echo '[]'", interval="1m")

        sched = _make_scheduler(tmp_path, [m])
        sched.tick()

        state_file = tmp_path / "monitor_state.json"
        assert state_file.exists()

        data = json.loads(state_file.read_text())
        assert "persist" in data

    def test_new_scheduler_reads_state(self, tmp_path):
        """A new scheduler instance picks up persisted state."""
        m = Monitor(name="survive", command="echo '[]'", interval="5m")

        t = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        sched1 = _make_scheduler(tmp_path, [m], now=lambda: t)
        sched1.tick()

        # Create new scheduler — should load state from disk
        t2 = t + timedelta(minutes=1)
        sched2 = _make_scheduler(tmp_path, [m], now=lambda: t2)

        assert "survive" in sched2.state
        assert "last_run" in sched2.state["survive"]


class TestRelevanceGateFlow:
    """End-to-end two-tier semantic gate (#630): a YAML-defined command
    monitor with `relevance:` pulls items mechanically, sends only new items
    to the gate, publishes only relevant verdicts, and never re-judges across
    scheduler restarts."""

    def _yaml_monitor(self, tmp_path, script):
        """Load the gated monitor through the real registry from real YAML."""
        config_dir = tmp_path / "package"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "agent.yaml").write_text("entry_point: manager\n")
        (config_dir / "monitors.yaml").write_text(yaml.dump({
            "monitors": [{
                "name": "billing-emails",
                "command": str(script),
                "interval": "5m",
                "relevance": "emails about billing problems",
                "event": "monitor/email.billing",
            }]
        }))
        from bobi.monitors.registry import MonitorRegistry
        reg = MonitorRegistry.load(project_path=tmp_path)
        m = next(m for m in reg.effective_monitors()
                 if m.name == "billing-emails")
        assert m.relevance == "emails about billing problems"
        return m

    def _scheduler(self, tmp_path, monitors, gates, fired, now=None):
        from bobi.monitors.scheduler import MonitorScheduler

        class FakeRegistry:
            def effective_monitors(self):
                return monitors
            def projects_for(self, _m):
                return []

        def publish(event, data):
            fired.append({"event": event, "data": data})
            return True

        return MonitorScheduler(
            publish=publish,
            state_path=tmp_path / "monitor_state.json",
            now=now or (lambda: datetime(2026, 6, 1, 12, 0, 0,
                                         tzinfo=timezone.utc)),
            registry_loader=lambda **kw: FakeRegistry(),
            spawn_gate=lambda m, cwd, items, cb: gates.append((m, items, cb)),
        )

    def test_full_gate_flow_with_restart(self, tmp_path):
        script = tmp_path / "poll.sh"
        script.write_text(
            '#!/bin/sh\n'
            'echo \'[{"id": "m1", "subject": "refund overcharged"},'
            ' {"id": "m2", "subject": "lunch on friday"}]\'\n')
        script.chmod(0o755)

        m = self._yaml_monitor(tmp_path, script)
        gates, fired = [], []
        t = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        sched = self._scheduler(tmp_path, [m], gates, fired, now=lambda: t)

        # Tick 1: the command runs mechanically, nothing publishes yet,
        # both new items ride to the gate.
        sched.tick()
        assert fired == []
        assert len(gates) == 1
        _, items, on_verdict = gates[0]
        assert sorted(c.key for c in items) == ["m1", "m2"]

        # Verdict: only m1 is about billing.
        on_verdict({"success": True, "relevant": ["m1"]})
        assert len(fired) == 1
        assert fired[0]["event"] == "monitor/email.billing"
        assert fired[0]["data"]["subject"] == "refund overcharged"
        assert fired[0]["data"]["finding_key"] == "m1"

        # Both judged keys persisted to disk.
        state = json.loads((tmp_path / "monitor_state.json").read_text())
        assert set(state[m.state_key]["active"]) == {"m1", "m2"}

        # Tick 2 (same items, past the interval): zero gate calls, zero events.
        t2 = t + timedelta(minutes=6)
        sched._now = lambda: t2
        sched.tick()
        assert len(gates) == 1
        assert len(fired) == 1

        # Restart: a fresh scheduler reads the persisted state and still
        # does not re-judge the same items.
        sched2 = self._scheduler(tmp_path, [m], gates, fired,
                                 now=lambda: t2 + timedelta(minutes=6))
        sched2.tick()
        assert len(gates) == 1
        assert len(fired) == 1

    def test_new_item_after_verdict_is_gated_alone(self, tmp_path):
        script = tmp_path / "poll.sh"
        script.write_text('#!/bin/sh\necho \'[{"id": "m1", "s": "x"}]\'\n')
        script.chmod(0o755)

        m = self._yaml_monitor(tmp_path, script)
        gates, fired = [], []
        t = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        sched = self._scheduler(tmp_path, [m], gates, fired, now=lambda: t)

        sched.tick()
        gates[0][2]({"success": True, "relevant": []})  # m1 irrelevant

        # A new item appears next interval; only it is judged.
        script.write_text(
            '#!/bin/sh\n'
            'echo \'[{"id": "m1", "s": "x"}, {"id": "m3", "s": "billing"}]\'\n')
        sched._now = lambda: t + timedelta(minutes=6)
        sched.tick()
        assert len(gates) == 2
        assert [c.key for c in gates[1][1]] == ["m3"]
        gates[1][2]({"success": True, "relevant": ["m3"]})
        assert [f["data"]["finding_key"] for f in fired] == ["m3"]


class TestMonitorRunRecordsOnDisk:
    """A live tick leaves an outcome record under the runtime state dir.

    The unit tests cover the fold; this covers the wiring that makes a record
    land in a real BOBI_HOME — where the runs read model will look for it.
    """

    def _scheduler(self, bobi_install, monitors, publish=None):
        from bobi.monitors.scheduler import MonitorScheduler

        class FakeRegistry:
            def effective_monitors(self):
                return monitors

            def projects_for(self, _m):
                return []

        return MonitorScheduler(
            publish=publish or (lambda event, data: True),
            state_path=bobi_install.state_dir / "monitor_state.json",
            now=lambda: datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
            registry_loader=lambda **kw: FakeRegistry(),
            spawn_check=lambda _m, _c, _cb: None,
        )

    def test_a_tick_writes_a_record_under_run_state(self, bobi_install):
        from bobi.monitors import run_records

        m = Monitor(name="inbox", event="monitor/inbox", interval="5m",
                    command="echo '[{\"id\": \"e1\"}]'")
        self._scheduler(bobi_install, [m]).tick()

        record_file = bobi_install.state_dir / "monitor_runs" / "inbox.json"
        assert record_file.exists()
        data = json.loads(record_file.read_text())
        assert data["monitor"] == "inbox"
        assert len(data["runs"]) == 1
        assert data["runs"][0]["outcome"] == run_records.NOTIFIED
        assert data["runs"][0]["published"] == 1
        assert data["runs"][0]["flavor"] == "command"

    def test_three_outcomes_are_told_apart_after_the_fact(self, bobi_install):
        from bobi.monitors import run_records

        notified = Monitor(name="notified-one", event="monitor/a", interval="5m",
                           command="echo '[{\"id\": \"e1\"}]'")
        quiet = Monitor(name="quiet-one", event="monitor/b", interval="5m",
                        command="echo '[]'")
        failed = Monitor(name="failed-one", event="monitor/c", interval="5m",
                         command="exit 2")
        self._scheduler(bobi_install, [notified, quiet, failed]).tick()

        outcomes = {r.monitor: r.outcome for r in run_records.load_all()}
        assert outcomes == {
            "notified-one": run_records.NOTIFIED,
            "quiet-one": run_records.QUIET,
            "failed-one": run_records.FAILED,
        }
        assert "exited 2" in run_records.load("failed-one")[0].reason


class TestPublishFailureIsNotDropped:
    """A finding whose publish failed must not vanish without a trace (#1006).

    Reproduces moda/roadmap-pm's 2026-08-10 standup: `standup-due` fired on
    schedule, its one finding failed to publish, the run record recorded
    "retrying next interval", and no retry ever ran and no event ever reached
    the instance - so `auto_dispatch` had nothing to route and the daily
    standup silently did not happen.

    Both flavors that reach the shared `_reconcile` chokepoint are covered
    because both were observed losing a real finding this way: the
    description flavor (roadmap-pm's `standup-due`) and the notify flavor
    (bobi-eng-team's `team-status-roundup`, which lost its 2026-08-10 18:00
    slot with the identical run record). Neither is config-specific - the
    two deployments share no agent.yaml, only this code path.

    The outage here is TRANSIENT: the transport comes back one tick after the
    firing and stays up for a full day of ticks. So the assertion is a
    deliberate disjunction - the finding is republished OR the failure is
    surfaced - which holds under either resolution the issue proposes and
    pins only the defect they share. It also means an implementation that
    "surfaces" the failure by posting one more event down the same dead
    transport does not pass: that is the silent drop again.
    """

    LA = "America/Los_Angeles"

    def _scheduler(self, state_dir, monitors, published, outage, clock,
                   spawn_check=None):
        from bobi.monitors.scheduler import MonitorScheduler

        class FakeRegistry:
            def effective_monitors(self):
                return monitors

            def projects_for(self, _m):
                return []

        def publish(event, data):
            # Mirrors post_event: a transport failure returns False and the
            # event is simply not on the bus. Nothing is recorded.
            if outage["down"]:
                return False
            published.append({"event": event, "data": data})
            return True

        return MonitorScheduler(
            publish=publish,
            state_path=state_dir / "monitor_state.json",
            now=lambda: clock["now"],
            registry_loader=lambda **kw: FakeRegistry(),
            spawn_check=spawn_check or (lambda _m, _c, _cb: None),
        )

    @staticmethod
    def _tick_through_to_the_next_slot(sched, clock, start, land=None):
        """Tick the way the manager does: every 30s, then hourly for a day.

        Stops before the next day's scheduled slot so a fresh firing can
        never be mistaken for a retry of the lost one. `land` resolves any
        out-of-band verdict left pending by the previous tick, which is where
        the real waiter thread delivers it - between ticks, never inside the
        one that dispatched the check.
        """
        for i in range(1, 21):  # the first ten minutes, tick by tick
            clock["now"] = start + timedelta(seconds=30 * i)
            sched.tick()
            if land:
                land()
        for hours in range(1, 24):  # then through to the next evening
            clock["now"] = start + timedelta(hours=hours)
            sched.tick()
            if land:
                land()

    def _assert_finding_was_lost(self, published, finding_key, event):
        """The defect: after the transport recovers, nobody ever hears."""
        republished = [p for p in published
                       if p["event"] == event
                       and p["data"].get("finding_key") == finding_key]
        surfaced = [p for p in published
                    if p["event"].endswith("monitor.error")]
        assert republished or surfaced, (
            f"{finding_key} failed to publish and was then dropped: it was "
            "never retried and no monitor.error ever surfaced it. The run "
            "record's 'retrying next interval' is the only trace it existed."
        )

    @staticmethod
    def _deliveries(published, finding_key, event) -> int:
        return len([p for p in published
                    if p["event"] == event
                    and p["data"].get("finding_key") == finding_key])

    @staticmethod
    def _state(state_dir, monitor) -> dict:
        return json.loads(
            (state_dir / "monitor_state.json").read_text())[monitor.state_key]

    def test_at_monitor_finding_survives_a_failed_publish(self, bobi_install):
        """roadmap-pm's standup: description flavor on a weekday `at:` slot."""
        from bobi.monitors import run_records

        finding_key = f"monitor/standup.due:2026-08-10:{self.LA}"
        m = Monitor(
            name="standup-due",
            description="Weekday standup is due (Mon, Tue, Thu, Fri).",
            at=["19:00"], days=["mon", "tue", "thu", "fri"], tz=self.LA,
            event="monitor/standup.due",
        )

        spawned, published, pending = [], [], []
        outage = {"down": True}
        # 18:50 PT Monday: the baseline tick an at-monitor needs before it
        # will fire at all.
        clock = {"now": datetime(2026, 8, 11, 1, 50, tzinfo=timezone.utc)}

        def spawn_check(monitor, _cwd, on_verdict):
            # The real hook Popens a check agent and hands its verdict back
            # from a WAITER THREAD when the process exits, minutes later.
            # Calling on_verdict inline instead would make the firing
            # reconcile inside the tick that dispatched it, which is the one
            # ordering the description flavor does not have.
            spawned.append(monitor.name)
            pending.append(on_verdict)

        def land_verdicts():
            for on_verdict in pending[:]:
                pending.remove(on_verdict)
                on_verdict({
                    "success": True,
                    "finding": True,
                    "summary": "Standup is due for Monday 2026-08-10.",
                    "details": {"key": finding_key, "date": "2026-08-10"},
                })

        sched = self._scheduler(bobi_install.state_dir, [m], published,
                                outage, clock, spawn_check=spawn_check)
        sched.tick()
        land_verdicts()
        assert spawned == []  # not due yet - this tick only baselines

        # 19:00:19 PT: the scheduled instant, exactly as observed in the
        # roadmap-pm run record. The check agent reports the finding and the
        # publish fails.
        fired_at = datetime(2026, 8, 11, 2, 0, 19, tzinfo=timezone.utc)
        clock["now"] = fired_at
        sched.tick()
        assert spawned == ["standup-due"]  # the schedule itself worked
        land_verdicts()  # the waiter thread lands it after the tick

        # The observed aftermath: nothing active, nothing published, and a
        # run record promising a retry. The promise is only worth anything if
        # the payload survived it, so that is what is asserted - #1006 was a
        # true-sounding sentence sitting next to no mechanism.
        state = self._state(bobi_install.state_dir, m)
        assert state["active"] == []
        assert state["pending_publish"] == {
            finding_key: {"summary": "Standup is due for Monday 2026-08-10.",
                          "text": "Standup is due for Monday 2026-08-10.",
                          "key": finding_key, "date": "2026-08-10"}}
        record = run_records.load("standup-due")[0]
        assert record.outcome == run_records.FAILED
        assert record.published == 0
        assert "parked for a mechanical retry" in record.reason

        # The transport recovers one tick later and stays up all day.
        outage["down"] = False
        self._tick_through_to_the_next_slot(sched, clock, fired_at,
                                            land=land_verdicts)

        self._assert_finding_was_lost(published, finding_key,
                                      "monitor/standup.due")

        # Delivered, and delivered ONCE: auto_dispatch routes this event, so a
        # retry that double-delivered would launch two standup workers. The
        # park is emptied by the delivery, not left to be re-sent forever.
        assert self._deliveries(published, finding_key,
                                "monitor/standup.due") == 1
        state = self._state(bobi_install.state_dir, m)
        assert "pending_publish" not in state
        assert state["active"] == [finding_key]

    def test_notify_monitor_finding_survives_a_failed_publish(self, bobi_install):
        """bobi-eng-team's roundup: notify flavor, same shared path.

        The notify key is the firing instant, so the lost finding cannot even
        in principle be re-detected - the next slot produces a different key
        for a different roundup.
        """
        from bobi.monitors import run_records

        m = Monitor(
            name="team-status-roundup", notify=True,
            description="Twice-daily status roundup.",
            at=["06:00", "18:00"], tz=self.LA,
            event="monitor/status.roundup_due",
        )

        published = []
        outage = {"down": True}
        clock = {"now": datetime(2026, 8, 11, 0, 50, tzinfo=timezone.utc)}

        sched = self._scheduler(bobi_install.state_dir, [m], published,
                                outage, clock)
        sched.tick()  # 17:50 PT - baseline only
        assert published == []

        # 18:00 PT: the slot that really was lost on 2026-08-10.
        fired_at = datetime(2026, 8, 11, 1, 0, 21, tzinfo=timezone.utc)
        clock["now"] = fired_at
        sched.tick()
        finding_key = fired_at.isoformat()

        # Byte-identical aftermath to the description flavor's: the shared
        # path, not the flavor, is what lost the finding.
        state = self._state(bobi_install.state_dir, m)
        assert state["active"] == []
        assert state["pending_publish"] == {
            finding_key: {"description": "Twice-daily status roundup."}}
        record = run_records.load("team-status-roundup")[0]
        assert record.outcome == run_records.FAILED
        assert record.published == 0
        assert "parked for a mechanical retry" in record.reason

        outage["down"] = False
        self._tick_through_to_the_next_slot(sched, clock, fired_at)

        self._assert_finding_was_lost(published, finding_key,
                                      "monitor/status.roundup_due")

        # Once, and the park is empty. The 06:00 slot inside this loop fires
        # its own roundup under its own key; neither firing re-sends the
        # other's.
        assert self._deliveries(published, finding_key,
                                "monitor/status.roundup_due") == 1
        assert "pending_publish" not in self._state(bobi_install.state_dir, m)
