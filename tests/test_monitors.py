"""Tests for the background monitoring system — schema, registry, scheduler."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from modastack.monitors.schema import Monitor, parse_at, parse_interval
from modastack.monitors import registry as registry_mod
from modastack.monitors.registry import MonitorRegistry
from modastack.monitors.schema import Condition
from modastack.monitors.scheduler import MonitorScheduler


# === Interval parsing ===

class TestParseInterval:
    def test_units(self):
        assert parse_interval("30s") == 30
        assert parse_interval("5m") == 300
        assert parse_interval("1h") == 3600
        assert parse_interval("2d") == 172800

    def test_bare_number_is_seconds(self):
        assert parse_interval("45") == 45
        assert parse_interval(45) == 45

    def test_invalid(self):
        for bad in ["", "abc", "5x", "-3m", "0"]:
            with pytest.raises(ValueError):
                parse_interval(bad)


# === At-time parsing ===

class TestParseAt:
    def test_single_and_list(self):
        assert parse_at("06:00") == [(6, 0)]
        assert parse_at(["06:00", "18:30"]) == [(6, 0), (18, 30)]

    def test_none_is_empty(self):
        assert parse_at(None) == []

    def test_invalid(self):
        for bad in ["6am", "25:00", "12:60", "noon", ""]:
            with pytest.raises(ValueError):
                parse_at(bad)


# === Monitor schema ===

class TestMonitor:
    def test_from_dict_defaults_event(self):
        m = Monitor.from_dict({"name": "foo"})
        assert m.event == "monitor/foo"
        assert m.enabled is True
        assert m.interval == "15m"

    def test_free_form_fields_go_to_extra(self):
        m = Monitor.from_dict({"name": "deploy", "url": "https://x", "threshold_hours": 6})
        assert m.extra == {"url": "https://x", "threshold_hours": 6}

    def test_requires_name(self):
        with pytest.raises(ValueError):
            Monitor.from_dict({"description": "no name"})

    def test_event_parts_splits_on_slash(self):
        assert Monitor(name="x", event="monitor/pr.conflict").event_parts == ("monitor", "pr.conflict")
        assert Monitor(name="x", event="bare").event_parts == ("monitor", "bare")

    def test_state_key_namespaces_project_scoped(self):
        assert Monitor(name="dh").state_key == "dh"
        assert Monitor(name="dh", project="/r/jobtack").state_key == "dh@/r/jobtack"

    def test_to_dict_roundtrip_disabled(self):
        m = Monitor.from_dict({"name": "x", "enabled": False, "url": "u"})
        d = m.to_dict()
        assert d["enabled"] is False
        assert d["url"] == "u"

    def test_at_tz_notify_fields(self):
        m = Monitor.from_dict({"name": "roundup", "at": ["06:00", "18:00"],
                               "tz": "America/Los_Angeles", "notify": True})
        assert m.at_times == [(6, 0), (18, 0)]
        assert m.notify is True
        d = m.to_dict()
        assert d["at"] == ["06:00", "18:00"]
        assert d["tz"] == "America/Los_Angeles"
        assert d["notify"] is True
        assert "interval" not in d  # at-monitors don't serialize an interval

    def test_single_at_string_becomes_list(self):
        m = Monitor.from_dict({"name": "x", "at": "06:00"})
        assert m.at == ["06:00"]


# === Registry merge ===

def _write(path: Path, monitors: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump({"monitors": monitors}))


class TestRegistryMerge:
    def test_project_specific_monitor_scoped(self, tmp_path):
        project = tmp_path / "jobtack"
        _write(project / ".modastack" / "monitors.yaml", [
            {"name": "deploy-health", "interval": "5m", "url": "https://j"},
        ])
        reg = MonitorRegistry.load(project_path=project)
        dh = [m for m in reg.effective_monitors() if m.name == "deploy-health"]
        assert len(dh) == 1
        assert dh[0].project == str(project)
        assert reg.projects_for(dh[0]) == [project]

    def test_project_opt_out_of_default(self, tmp_path):
        project = tmp_path / "jobtack"
        _write(project / ".modastack" / "monitors.yaml", [{"name": "stale-pr-check", "enabled": False}])
        reg = MonitorRegistry.load(project_path=project)
        stale = [m for m in reg.effective_monitors() if m.name == "stale-pr-check"]
        for s in stale:
            assert reg.projects_for(s) == []

    def test_project_override_of_default(self, tmp_path):
        project = tmp_path / "jobtack"
        _write(project / ".modastack" / "monitors.yaml", [{"name": "pr-conflict-check", "interval": "5m"}])
        reg = MonitorRegistry.load(project_path=project)
        glob = reg.globals.get("pr-conflict-check")
        if glob:
            assert reg.projects_for(glob) == []
        scoped = [m for m in reg.project_monitors if m.name == "pr-conflict-check"][0]
        assert reg.projects_for(scoped) == [project]


# === Registry writes ===

class TestRegistryWrites:
    def test_add_project_writes_monitors_file(self, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        MonitorRegistry.add_project(Monitor(name="dh", extra={"url": "u"}), repo)
        monitors_path = repo / ".modastack" / "monitors.yaml"
        assert monitors_path.exists()
        raw = yaml.safe_load(monitors_path.read_text())
        assert raw["monitors"][0]["name"] == "dh"

    def test_pause_unknown_returns_false(self):
        assert MonitorRegistry.pause("does-not-exist") is False


# === Scheduler ===

def _fixed_now():
    return datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _scheduler(tmp_path, monitors, check_results=None, spawned=None):
    """Build a scheduler over a hand-built registry and capture injected events.

    `spawned` (if a list) captures (monitor, cwd) tuples from spawn_check so
    description-only monitors don't launch real subprocesses in tests.
    """
    injected = []

    class FakeRegistry:
        def effective_monitors(self):
            return monitors

        def projects_for(self, m):
            return [Path("/repo")]

    sched = MonitorScheduler(
        inject_event=injected.append,
        state_path=tmp_path / "state.json",
        now=_fixed_now,
        registry_loader=lambda: FakeRegistry(),
        spawn_check=(lambda mon, cwd: spawned.append((mon, cwd)))
        if spawned is not None else (lambda mon, cwd: None),
    )
    return sched, injected


# === Scheduler ===

class TestSchedulerDue:
    def test_due_when_never_run(self, tmp_path):
        m = Monitor(name="x", interval="5m")
        sched, _ = _scheduler(tmp_path, [m])
        assert sched._due(m, _fixed_now()) is True

    def test_not_due_within_interval(self, tmp_path):
        m = Monitor(name="x", interval="5m")
        sched, _ = _scheduler(tmp_path, [m])
        sched.state["x"] = {"last_run": (_fixed_now() - timedelta(minutes=2)).isoformat()}
        assert sched._due(m, _fixed_now()) is False

    def test_due_after_interval(self, tmp_path):
        m = Monitor(name="x", interval="5m")
        sched, _ = _scheduler(tmp_path, [m])
        sched.state["x"] = {"last_run": (_fixed_now() - timedelta(minutes=6)).isoformat()}
        assert sched._due(m, _fixed_now()) is True


class TestSchedulerDueAt:
    # _fixed_now() is 2026-06-01 12:00 UTC. Monitors below use tz UTC so the
    # tests don't depend on the host timezone.
    def _monitor(self):
        return Monitor(name="roundup", at=["06:00", "18:00"], tz="UTC",
                       notify=True, event="monitor/status.roundup_due")

    def test_first_sight_records_baseline_without_firing(self, tmp_path):
        m = self._monitor()
        sched, _ = _scheduler(tmp_path, [m])
        # Noon: the 06:00 slot has passed, but starting mid-day must not fire it.
        assert sched._due(m, _fixed_now()) is False
        assert sched.state["roundup"]["last_run"] == _fixed_now().isoformat()

    def test_due_after_scheduled_time_crossed(self, tmp_path):
        m = self._monitor()
        sched, _ = _scheduler(tmp_path, [m])
        sched.state["roundup"] = {"last_run": _fixed_now().replace(hour=17).isoformat()}
        assert sched._due(m, _fixed_now().replace(hour=18, minute=5)) is True

    def test_not_due_between_slots(self, tmp_path):
        m = self._monitor()
        sched, _ = _scheduler(tmp_path, [m])
        sched.state["roundup"] = {"last_run": _fixed_now().replace(hour=6, minute=1).isoformat()}
        assert sched._due(m, _fixed_now()) is False

    def test_at_times_respect_timezone(self, tmp_path):
        # 06:00 Pacific is 13:00 UTC in June (PDT). Baseline at 10:00 UTC.
        m = Monitor(name="r2", at=["06:00"], tz="America/Los_Angeles", notify=True)
        sched, _ = _scheduler(tmp_path, [m])
        sched.state["r2"] = {"last_run": (_fixed_now() - timedelta(hours=2)).isoformat()}
        assert sched._due(m, _fixed_now()) is False  # 12:00 UTC = 5am PDT
        assert sched._due(m, _fixed_now() + timedelta(hours=1, minutes=5)) is True  # 6:05am PDT


class TestNotifyMonitor:
    def test_notify_fires_event_directly_every_run(self, tmp_path):
        m = Monitor(name="roundup", notify=True, event="monitor/status.roundup_due",
                    description="ping the leads")
        spawned = []
        sched, injected = _scheduler(tmp_path, [m], spawned=spawned)
        reg = sched._registry_loader()
        sched.run_monitor(m, reg, _fixed_now())
        sched.run_monitor(m, reg, _fixed_now() + timedelta(hours=12))
        assert spawned == []      # no out-of-band check agent
        assert len(injected) == 2  # no dedup — fires every time it's due
        ev = injected[0]
        assert ev["source"] == "monitor"
        assert ev["type"] == "status.roundup_due"
        assert ev["data"]["monitor"] == "roundup"
        assert ev["data"]["description"] == "ping the leads"


class TestSchedulerReconcile:
    def test_new_condition_fires_event_with_clean_type(self, tmp_path):
        m = Monitor(name="pr-conflict-check", event="monitor/pr.conflict_detected")
        sched, injected = _scheduler(tmp_path, [m])
        sched._reconcile(m, [Condition(key="r#1", data={"pr_number": 1, "repo": "r"})])
        assert len(injected) == 1
        ev = injected[0]
        assert ev["source"] == "monitor"
        assert ev["type"] == "pr.conflict_detected"
        assert ev["data"]["monitor"] == "pr-conflict-check"
        assert ev["data"]["pr_number"] == 1

    def test_unchanged_condition_does_not_refire(self, tmp_path):
        m = Monitor(name="x", event="monitor/x")
        sched, injected = _scheduler(tmp_path, [m])
        cond = [Condition(key="r#1", data={})]
        sched._reconcile(m, cond)
        sched._reconcile(m, cond)
        assert len(injected) == 1  # deduplicated

    def test_resolved_then_reappears_refires(self, tmp_path):
        m = Monitor(name="x", event="monitor/x")
        sched, injected = _scheduler(tmp_path, [m])
        sched._reconcile(m, [Condition(key="r#1", data={})])
        sched._reconcile(m, [])               # resolved -> drops from active
        sched._reconcile(m, [Condition(key="r#1", data={})])  # reappears
        assert len(injected) == 2

    def test_state_persists_across_instances(self, tmp_path):
        m = Monitor(name="x", event="monitor/x")
        sched, injected = _scheduler(tmp_path, [m])
        sched._reconcile(m, [Condition(key="r#1", data={})])
        sched._save_state()
        sched2, injected2 = _scheduler(tmp_path, [m])
        sched2._reconcile(m, [Condition(key="r#1", data={})])
        assert injected2 == []  # already known from persisted state


class TestSchedulerRun:
    def test_native_check_runs_and_marks_run(self, tmp_path):
        m = Monitor(name="x", event="monitor/x", check="pr_conflicts")
        sched, injected = _scheduler(tmp_path, [m])

        sched._checks["__test_check"] = lambda mon, repos: [Condition(key="k", data={"a": 1})]
        m.check = "__test_check"
        reg = sched._registry_loader()
        sched.run_monitor(m, reg, _fixed_now())
        assert len(injected) == 1
        assert sched.state["x"]["last_run"] == _fixed_now().isoformat()

    def test_description_only_spawns_check_not_inject(self, tmp_path):
        m = Monitor(name="custom", description="check the thing", event="monitor/custom")
        spawned = []
        sched, injected = _scheduler(tmp_path, [m], spawned=spawned)
        reg = sched._registry_loader()
        sched.run_monitor(m, reg, _fixed_now())
        # The manager is never injected into for a description-only monitor —
        # the check runs out-of-band and posts its own event on a finding.
        assert injected == []
        assert len(spawned) == 1
        mon, cwd = spawned[0]
        assert mon is m
        assert cwd == "/repo"  # first applicable project
        assert sched.state["custom"]["last_run"] == _fixed_now().isoformat()

    def test_unknown_check_is_skipped_gracefully(self, tmp_path):
        m = Monitor(name="x", event="monitor/x", check="nonexistent")
        sched, injected = _scheduler(tmp_path, [m])
        reg = sched._registry_loader()
        sched.run_monitor(m, reg, _fixed_now())  # should not raise
        assert injected == []
        assert "x" in sched.state  # still marked as run

    def test_tick_runs_due_monitors(self, tmp_path):
        m = Monitor(name="custom", event="monitor/custom")  # description-only
        spawned = []
        sched, injected = _scheduler(tmp_path, [m], spawned=spawned)
        sched.tick()
        assert len(spawned) == 1


class TestCommandMonitor:
    def test_command_runs_and_fires_events(self, tmp_path):
        m = Monitor(
            name="new-emails",
            command='echo \'[{"id": "msg1", "subject": "Hello"}, {"id": "msg2", "subject": "World"}]\'',
            event="email/received",
        )
        sched, injected = _scheduler(tmp_path, [m])
        reg = sched._registry_loader()
        sched.run_monitor(m, reg, _fixed_now())
        assert len(injected) == 2
        assert injected[0]["data"]["subject"] == "Hello"
        assert injected[1]["data"]["subject"] == "World"

    def test_command_deduplicates_by_id(self, tmp_path):
        m = Monitor(
            name="check",
            command='echo \'[{"id": "same", "v": 1}]\'',
            event="monitor/check",
        )
        sched, injected = _scheduler(tmp_path, [m])
        reg = sched._registry_loader()
        sched.run_monitor(m, reg, _fixed_now())
        sched.run_monitor(m, reg, _fixed_now())
        assert len(injected) == 1

    def test_command_deduplicates_by_hash(self, tmp_path):
        m = Monitor(
            name="check",
            command='echo \'[{"a": 1}]\'',
            event="monitor/check",
        )
        sched, injected = _scheduler(tmp_path, [m])
        reg = sched._registry_loader()
        sched.run_monitor(m, reg, _fixed_now())
        sched.run_monitor(m, reg, _fixed_now())
        assert len(injected) == 1

    def test_command_empty_output_clears_active(self, tmp_path):
        m = Monitor(
            name="check",
            command='echo \'[{"id": "x"}]\'',
            event="monitor/check",
        )
        sched, injected = _scheduler(tmp_path, [m])
        reg = sched._registry_loader()
        sched.run_monitor(m, reg, _fixed_now())
        assert len(injected) == 1

        m.command = "echo ''"
        sched.run_monitor(m, reg, _fixed_now())
        assert sched.state["check"]["active"] == []

    def test_command_failure_does_not_crash(self, tmp_path):
        m = Monitor(
            name="fail",
            command="exit 1",
            event="monitor/fail",
        )
        sched, injected = _scheduler(tmp_path, [m])
        reg = sched._registry_loader()
        sched.run_monitor(m, reg, _fixed_now())
        assert injected == []
        assert "fail" in sched.state

    def test_command_single_object_output(self, tmp_path):
        m = Monitor(
            name="single",
            command='echo \'{"id": "one", "status": "ok"}\'',
            event="monitor/single",
        )
        sched, injected = _scheduler(tmp_path, [m])
        reg = sched._registry_loader()
        sched.run_monitor(m, reg, _fixed_now())
        assert len(injected) == 1
        assert injected[0]["data"]["status"] == "ok"

    def test_command_takes_priority_over_check(self, tmp_path):
        """When both command and check are set, command wins."""
        m = Monitor(
            name="both",
            command='echo \'[{"id": "cmd"}]\'',
            check="pr_conflicts",
            event="monitor/both",
        )
        sched, injected = _scheduler(tmp_path, [m])
        reg = sched._registry_loader()
        sched.run_monitor(m, reg, _fixed_now())
        assert len(injected) == 1
        assert injected[0]["data"]["id"] == "cmd"
