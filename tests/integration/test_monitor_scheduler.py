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

from modastack.monitors.schema import Condition, Monitor


def _make_scheduler(tmp_path, monitors, publish=None, checks=None, now=None):
    """Create a MonitorScheduler with given monitors and state path."""
    from modastack.monitors.scheduler import MonitorScheduler

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
        """Monitors from .modastack/monitors.yaml are loaded."""
        config_dir = tmp_path / ".modastack"
        config_dir.mkdir()
        (config_dir / "agent.yaml").write_text("entry_point: manager\n")
        (config_dir / "monitors.yaml").write_text(yaml.dump({
            "monitors": [
                {"name": "my-check", "command": "echo ok", "interval": "5m"},
            ]
        }))

        from modastack.monitors.registry import MonitorRegistry
        reg = MonitorRegistry.load(project_path=tmp_path)

        effective = reg.effective_monitors()
        assert any(m.name == "my-check" for m in effective)

    def test_project_disables_default(self, tmp_path):
        """enabled: false in project config disables a default monitor."""
        config_dir = tmp_path / ".modastack"
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

        from modastack.monitors.registry import MonitorRegistry
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
