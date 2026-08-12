"""Tests for the background monitoring system — schema, registry, scheduler."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from bobi import paths
from bobi.monitors.schema import Monitor, parse_at, parse_days, parse_interval
from bobi.monitors import registry as registry_mod
from bobi.monitors.registry import MonitorRegistry
from bobi.monitors.schema import Condition
from bobi.monitors.scheduler import MonitorScheduler


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


# === Weekday parsing (days:) ===

class TestParseDays:
    def test_names_short_and_full(self):
        assert parse_days("sun") == {6}
        assert parse_days("Sunday") == {6}
        assert parse_days("MON") == {0}
        assert parse_days(["mon", "tue", "wed", "thu", "fri"]) == {0, 1, 2, 3, 4}

    def test_both_numberings_for_sunday(self):
        # cron 0=Sunday and ISO 7=Sunday both map to Python weekday 6 (D3).
        assert parse_days(0) == {6}
        assert parse_days(7) == {6}
        assert parse_days("0") == {6}
        assert parse_days("7") == {6}

    def test_numbers_one_through_six_are_mon_to_sat(self):
        assert parse_days([1, 2, 3, 4, 5, 6]) == {0, 1, 2, 3, 4, 5}

    def test_mixed_names_and_numbers_dedup(self):
        assert parse_days(["sun", 0, 7, "sunday"]) == {6}

    def test_empty_and_none_mean_every_day(self):
        assert parse_days(None) == set()
        assert parse_days([]) == set()
        assert parse_days("") == set()

    def test_invalid(self):
        for bad in ["funday", "8", "-1", "1.5", "su"]:
            with pytest.raises(ValueError):
                parse_days(bad)


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

    def test_days_roundtrip_and_weekdays_property(self):
        m = Monitor.from_dict({"name": "prep", "at": ["21:00"],
                               "tz": "America/Los_Angeles", "days": ["sun"],
                               "notify": True})
        assert m.weekdays == {6}
        d = m.to_dict()
        assert d["days"] == ["sun"]
        assert d["at"] == ["21:00"]
        # round-trips back to an equivalent monitor
        assert Monitor.from_dict(d).weekdays == {6}

    def test_bare_scalar_days_including_zero(self):
        # `days: 0` (cron Sunday) is a falsy int — must not be dropped.
        m = Monitor.from_dict({"name": "x", "at": ["09:00"], "days": 0})
        assert m.days == [0]
        assert m.weekdays == {6}

    def test_days_only_serialized_with_at(self):
        # days are meaningless without at: an interval monitor drops them.
        m = Monitor(name="x", interval="5m", days=["sun"])
        assert "days" not in m.to_dict()


# === Registry merge ===

def _write(path: Path, monitors: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump({"monitors": monitors}))


class TestRegistryMerge:
    def test_project_specific_monitor_scoped(self, tmp_path):
        project = tmp_path / "jobtack"
        _write(paths.package_dir(project) / "monitors.yaml", [
            {"name": "deploy-health", "interval": "5m", "url": "https://j"},
        ])
        reg = MonitorRegistry.load(project_path=project)
        dh = [m for m in reg.effective_monitors() if m.name == "deploy-health"]
        assert len(dh) == 1
        assert dh[0].project == str(project)
        assert reg.projects_for(dh[0]) == [project]

    def test_project_opt_out_of_default(self, tmp_path):
        project = tmp_path / "jobtack"
        _write(paths.package_dir(project) / "monitors.yaml", [{"name": "stale-pr-check", "enabled": False}])
        reg = MonitorRegistry.load(project_path=project)
        stale = [m for m in reg.effective_monitors() if m.name == "stale-pr-check"]
        for s in stale:
            assert reg.projects_for(s) == []

    def test_project_override_of_default(self, tmp_path):
        project = tmp_path / "jobtack"
        _write(paths.package_dir(project) / "monitors.yaml", [{"name": "pr-conflict-check", "interval": "5m"}])
        reg = MonitorRegistry.load(project_path=project)
        glob = reg.globals.get("pr-conflict-check")
        if glob:
            assert reg.projects_for(glob) == []
        scoped = [m for m in reg.project_monitors if m.name == "pr-conflict-check"][0]
        assert reg.projects_for(scoped) == [project]


# === Defaults path resolution ===

class TestDefaultsPath:
    def test_returns_installed_path_only(self, tmp_path):
        """_defaults_path must only return package/monitors/defaults.yaml —
        no framework fallback, no get_project_root fallback."""
        project = tmp_path / "proj"
        project.mkdir()
        result = registry_mod._defaults_path(project)
        assert result == paths.monitors_dir(project) / "defaults.yaml"

    def test_returns_none_without_project(self):
        """_defaults_path returns None when no project path is available."""
        result = registry_mod._defaults_path(None)
        # Without monkeypatching get_project_root, this may return None or a
        # real path — but it must never return a framework source path.
        if result is not None:
            assert "package" in str(result)

    def test_loads_defaults_from_installed_pack(self, tmp_path):
        """Registry loads defaults from package/monitors/defaults.yaml."""
        project = tmp_path / "proj"
        _write(paths.monitors_dir(project) / "defaults.yaml", [
            {"name": "my-check", "interval": "10m", "check": "pr_conflicts"},
        ])
        reg = MonitorRegistry.load(project_path=project)
        names = [m.name for m in reg.effective_monitors()]
        assert "my-check" in names


# === Install copies built-in defaults ===

class TestInstallFrameworkSleepCycleDefault:
    """sleep-cycle is a framework default (#471): seeded into EVERY composed
    team image. A pack ships no other framework monitors — only `sleep-cycle`
    is injected, and the pack's own monitors still resolve on top of it."""

    def test_install_injects_only_the_framework_sleep_cycle(self, tmp_path):
        """A pack with no monitors/ directory still gets exactly the framework
        sleep-cycle default (and nothing else) — opt-out is via `prune:`."""
        from bobi.cli import _install_pack

        pack = tmp_path / "minimal-pack"
        pack.mkdir()
        (pack / "agent.yaml").write_text("agent: minimal\n")

        project = tmp_path / "proj"
        project.mkdir()

        _install_pack(pack, project)

        defaults = paths.monitors_dir(project) / "defaults.yaml"
        assert defaults.exists()
        raw = yaml.safe_load(defaults.read_text())
        names = [m["name"] for m in raw["monitors"]]
        assert names == ["sleep-cycle"], (
            f"Only the framework sleep_cycle should be injected, got: {names}"
        )

    def test_install_pack_monitors_resolve_on_top_of_sleep_cycle(self, tmp_path):
        """When a pack ships its own monitors/, they compose ON TOP of the
        framework sleep_cycle (sleep_cycle first, as the base layer)."""
        from bobi.cli import _install_pack

        pack = tmp_path / "full-pack"
        pack.mkdir()
        (pack / "agent.yaml").write_text("agent: full\n")
        monitors_dir = pack / "monitors"
        monitors_dir.mkdir()
        _write(monitors_dir / "defaults.yaml", [
            {"name": "custom-check", "interval": "30m"},
        ])

        project = tmp_path / "proj"
        project.mkdir()

        _install_pack(pack, project)

        defaults = paths.monitors_dir(project) / "defaults.yaml"
        assert defaults.exists()
        raw = yaml.safe_load(defaults.read_text())
        names = [m["name"] for m in raw["monitors"]]
        assert names == ["sleep-cycle", "custom-check"], (
            f"Framework sleep_cycle + pack monitors expected, got: {names}"
        )


# === Registry writes ===

class TestRegistryWrites:
    def test_add_project_writes_monitors_file(self, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        MonitorRegistry.add_project(Monitor(name="dh", extra={"url": "u"}), repo)
        monitors_path = paths.package_dir(repo) / "monitors.yaml"
        assert monitors_path.exists()
        raw = yaml.safe_load(monitors_path.read_text())
        assert raw["monitors"][0]["name"] == "dh"

    def test_pause_unknown_returns_false(self):
        assert MonitorRegistry.pause("does-not-exist") is False


# === Scheduler ===

def _fixed_now():
    return datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _scheduler(tmp_path, monitors, check_results=None, spawned=None,
               publish=None, gates=None):
    """Build a scheduler over a hand-built registry and capture published events.

    `published` records {"event": ..., "data": ...} for every publish — the
    single path all monitor flavors fire through. Pass `publish` to override
    (e.g. to simulate event-server failures). `spawned` (if a list) captures
    (monitor, cwd, on_verdict) tuples from spawn_check so description-only
    monitors don't launch real subprocesses in tests. `gates` (if a list)
    likewise captures (monitor, cwd, items, on_verdict) from spawn_gate for
    relevance-gated monitors.
    """
    published = []

    def _record_publish(event, data):
        published.append({"event": event, "data": data})
        return True

    class FakeRegistry:
        def effective_monitors(self):
            return monitors

        def projects_for(self, m):
            return [Path("/repo")]

    sched = MonitorScheduler(
        publish=publish or _record_publish,
        state_path=tmp_path / "state.json",
        now=_fixed_now,
        registry_loader=lambda: FakeRegistry(),
        spawn_check=(lambda mon, cwd, on_verdict: spawned.append((mon, cwd, on_verdict)))
        if spawned is not None else (lambda mon, cwd, on_verdict: None),
        spawn_gate=(lambda mon, cwd, items, on_verdict:
                    gates.append((mon, cwd, items, on_verdict)))
        if gates is not None else (lambda mon, cwd, items, on_verdict: None),
    )
    return sched, published


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


class TestSchedulerWeekdayGating:
    """Weekly recurrence: a `days:` filter on the `at:`/`tz:` schedule.

    _fixed_now() is 2026-06-01 12:00 UTC, a **Monday**. The Sunday before is
    2026-05-31; the one before that is 2026-05-24.
    """
    def _weekly(self):
        return Monitor(name="prep", at=["21:00"], tz="UTC", days=["sun"],
                       notify=True, event="monitor/prep.weekly_due")

    def test_fires_live_on_configured_weekday(self, tmp_path):
        # Continuous operation: last fire was the previous Sunday; the tick just
        # after this Sunday's 21:00 slot (within the catch-up grace) fires.
        m = self._weekly()
        sched, _ = _scheduler(tmp_path, [m])
        sched.state["prep"] = {"last_run": datetime(2026, 5, 24, 21, 0,
                                                    tzinfo=timezone.utc).isoformat()}
        sun_210020 = datetime(2026, 5, 31, 21, 0, 20, tzinfo=timezone.utc)
        assert sched._due(m, sun_210020) is True

    def test_not_due_on_other_weekdays(self, tmp_path):
        m = self._weekly()
        sched, _ = _scheduler(tmp_path, [m])
        # last_run already at the most recent Sunday slot; a Wednesday 21:30
        # is NOT a new scheduled instant (no Wed firing).
        sched.state["prep"] = {"last_run": datetime(2026, 5, 31, 21, 0,
                                                    tzinfo=timezone.utc).isoformat()}
        wed_2130 = datetime(2026, 6, 3, 21, 30, tzinfo=timezone.utc)
        assert sched._due(m, wed_2130) is False

    def test_no_catch_up_skips_missed_run_and_rebaselines(self, tmp_path):
        # Manager down over the Sunday slot, comes back Monday noon: the weekly
        # run is SKIPPED (no catch-up, D8) and the baseline advances past it so
        # the stale slot is never retro-fired.
        m = self._weekly()
        sched, _ = _scheduler(tmp_path, [m])
        sched.state["prep"] = {"last_run": datetime(2026, 5, 30, 12, 0,
                                                    tzinfo=timezone.utc).isoformat()}
        assert sched._due(m, _fixed_now()) is False  # Monday noon, Sunday slot missed
        assert sched.state["prep"]["last_run"] == _fixed_now().isoformat()  # rebaselined
        # The next occurrence (the following Sunday) still fires live.
        next_sun = datetime(2026, 6, 7, 21, 0, 15, tzinfo=timezone.utc)
        assert sched._due(m, next_sun) is True

    def test_daily_at_still_catches_up(self, tmp_path):
        # Regression guard: an ungated daily at-monitor KEEPS catch-up — a slot
        # missed during downtime fires once, late.
        daily = Monitor(name="d", at=["21:00"], tz="UTC", notify=True)
        sched, _ = _scheduler(tmp_path, [daily])
        sched.state["d"] = {"last_run": datetime(2026, 5, 30, 12, 0,
                                                 tzinfo=timezone.utc).isoformat()}
        assert sched._due(daily, _fixed_now()) is True  # Monday noon, Sunday 21:00 missed

    def test_does_not_double_fire_same_instant(self, tmp_path):
        m = self._weekly()
        sched, _ = _scheduler(tmp_path, [m])
        # Already ran at the Sunday slot — a later Monday tick must not re-fire.
        sched.state["prep"] = {"last_run": datetime(2026, 5, 31, 21, 0,
                                                    tzinfo=timezone.utc).isoformat()}
        assert sched._due(m, _fixed_now()) is False

    def test_empty_days_is_identical_to_daily(self, tmp_path):
        # Regression guard: days:[] must behave exactly like today's daily at:.
        gated = Monitor(name="g", at=["21:00"], tz="UTC", days=[])
        daily = Monitor(name="g", at=["21:00"], tz="UTC")
        assert (MonitorScheduler._last_scheduled(gated, _fixed_now())
                == MonitorScheduler._last_scheduled(daily, _fixed_now()))

    def test_dst_keeps_wall_clock_time(self, tmp_path):
        # 'Sunday 21:00 LA' stays 21:00 local across the spring-forward boundary
        # (DST began 2026-03-08). Fixed `now` = Monday 2026-03-09 12:00 UTC.
        m = Monitor(name="p", at=["21:00"], tz="America/Los_Angeles", days=["sun"])
        now = datetime(2026, 3, 9, 12, 0, tzinfo=timezone.utc)
        scheduled = MonitorScheduler._last_scheduled(m, now)
        local = scheduled.astimezone(scheduled.tzinfo)
        assert (local.hour, local.minute) == (21, 0)
        assert local.weekday() == 6  # Sunday
        assert local.date() == datetime(2026, 3, 8).date()  # the DST-start Sunday


class TestWeeklyJobRouting:
    """End-to-end for the weekly prep-doc job: when the weekly notify monitor
    is due it publishes its event, and that event's topic is one the manager
    subscribes to — so a handler actually receives it."""

    def test_weekly_notify_fires_and_event_is_subscribable(self, tmp_path):
        from bobi.events.subscriptions import monitor_subscription_keys

        m = Monitor(name="weekly-prep-doc", at=["21:00"], tz="UTC", days=["sun"],
                    notify=True, event="monitor/prep.weekly_due",
                    description="Generate my prep doc for the upcoming week")
        sched, published = _scheduler(tmp_path, [m])
        # Last fired the previous Sunday; the tick just after this Sunday's slot.
        sched.state["weekly-prep-doc"] = {
            "last_run": datetime(2026, 5, 24, 21, 0, tzinfo=timezone.utc).isoformat()}
        sun_now = datetime(2026, 5, 31, 21, 0, 20, tzinfo=timezone.utc)
        assert sched._due(m, sun_now) is True
        sched.run_monitor(m, sched._registry_loader(), sun_now)

        assert [p["event"] for p in published] == ["monitor/prep.weekly_due"]
        # The manager subscribes to this topic (both bare + source-qualified).
        assert "monitor/prep.weekly_due" in monitor_subscription_keys([m.event])


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
        assert ev["event"] == "monitor/status.roundup_due"
        assert ev["data"]["monitor"] == "roundup"
        assert ev["data"]["description"] == "ping the leads"


class TestSchedulerReconcile:
    def test_new_condition_publishes_full_event(self, tmp_path):
        m = Monitor(name="pr-conflict-check", event="monitor/pr.conflict_detected")
        sched, injected = _scheduler(tmp_path, [m])
        sched._reconcile(m, [Condition(key="r#1", data={"pr_number": 1, "repo": "r"})])
        assert len(injected) == 1
        ev = injected[0]
        assert ev["event"] == "monitor/pr.conflict_detected"
        assert ev["data"]["monitor"] == "pr-conflict-check"
        assert ev["data"]["finding_key"] == "r#1"
        assert ev["data"]["pr_number"] == 1

    def test_condition_data_cannot_spoof_monitor_identity(self, tmp_path):
        m = Monitor(name="real-monitor", event="monitor/x")
        sched, injected = _scheduler(tmp_path, [m])
        sched._reconcile(m, [
            Condition(
                key="real-key",
                data={"monitor": "spoofed-monitor", "finding_key": "spoofed-key"},
            )
        ])
        assert injected[0]["data"]["monitor"] == "real-monitor"
        assert injected[0]["data"]["finding_key"] == "real-key"

    def test_non_string_condition_key_is_stringified_for_publish(self, tmp_path):
        m = Monitor(name="x", event="monitor/x")
        sched, injected = _scheduler(tmp_path, [m])
        sched._reconcile(m, [Condition(key=123, data={})])
        assert injected[0]["data"]["finding_key"] == "123"

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
        # Nothing publishes at spawn time for a description-only monitor —
        # detection runs out-of-band and reconciles when the verdict lands.
        assert injected == []
        assert len(spawned) == 1
        mon, cwd, on_verdict = spawned[0]
        assert mon is m
        assert cwd == "/repo"  # first applicable project
        assert callable(on_verdict)
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


class TestOutOfBandNativeChecks:
    """D004 — a check runner that pays for an agent must not hold the tick.

    The scheduler runs on ONE thread: `_loop` -> `tick()` -> `run_monitor` ->
    `_check_conditions` -> `check(...)` inline. `script_cache`'s self-heal
    calls `run_check_blocking` (attempts=2 x CHECK_TIMEOUT=600s) from there,
    so a regeneration stalls every other monitor for up to ~20 minutes:
    interval monitors drift, and a weekday-gated `at:` slot whose instant
    passes during the block is treated as a missed-while-down catch-up and
    skipped entirely.
    """

    @staticmethod
    def _blocking_check(release, started, result=None):
        def check(monitor, projects):
            started.set()
            release.wait(10)
            return result if result is not None else [
                Condition(key="k1", data={"id": "k1"})]
        check.out_of_band = True
        return check

    def test_a_blocking_check_does_not_hold_the_scheduler_thread(self, tmp_path):
        import threading
        import time

        started, release = threading.Event(), threading.Event()
        m = Monitor(name="slow", event="monitor/slow", check="slow_check")
        sched, injected = _scheduler(tmp_path, [m])
        sched._checks["slow_check"] = self._blocking_check(release, started)

        t0 = time.monotonic()
        sched.run_monitor(m, sched._registry_loader(), _fixed_now())
        elapsed = time.monotonic() - t0

        assert started.wait(5), "the check never ran"
        assert elapsed < 1.0, f"run_monitor blocked for {elapsed:.1f}s"

        release.set()
        deadline = time.monotonic() + 5
        while not injected and time.monotonic() < deadline:
            time.sleep(0.01)
        # It still reconciles and publishes — just off the scheduler thread.
        assert len(injected) == 1
        assert injected[0]["event"] == "monitor/slow"
        assert sched.state["slow"]["active"] == ["k1"]

    def test_an_in_flight_check_is_not_started_twice(self, tmp_path):
        import threading

        started, release = threading.Event(), threading.Event()
        calls = []
        m = Monitor(name="slow", event="monitor/slow", check="slow_check")
        sched, injected = _scheduler(tmp_path, [m])
        inner = self._blocking_check(release, started)

        def counting(monitor, projects):
            calls.append(monitor)
            return inner(monitor, projects)
        counting.out_of_band = True
        sched._checks["slow_check"] = counting

        reg = sched._registry_loader()
        sched.run_monitor(m, reg, _fixed_now())
        assert started.wait(5)
        # Ticks keep coming every 30s while a regeneration takes minutes.
        sched.run_monitor(m, reg, _fixed_now())
        sched.run_monitor(m, reg, _fixed_now())

        release.set()
        assert len(calls) == 1

    def test_an_ordinary_native_check_still_runs_inline(self, tmp_path):
        m = Monitor(name="x", event="monitor/x", check="plain")
        sched, injected = _scheduler(tmp_path, [m])
        sched._checks["plain"] = lambda mon, repos: [
            Condition(key="k", data={"a": 1})]
        sched.run_monitor(m, sched._registry_loader(), _fixed_now())
        # No thread, no waiting: it published before run_monitor returned.
        assert len(injected) == 1

    def test_a_raising_out_of_band_check_clears_its_in_flight_flag(self, tmp_path):
        import time

        calls = []

        def boom(monitor, projects):
            calls.append(monitor)
            raise RuntimeError("generation exploded")
        boom.out_of_band = True

        m = Monitor(name="x", event="monitor/x", check="boom")
        sched, injected = _scheduler(tmp_path, [m])
        sched._checks["boom"] = boom
        reg = sched._registry_loader()

        sched.run_monitor(m, reg, _fixed_now())
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        deadline = time.monotonic() + 5
        while sched._checks_in_flight and time.monotonic() < deadline:
            time.sleep(0.01)

        sched.run_monitor(m, reg, _fixed_now())
        deadline = time.monotonic() + 5
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(calls) == 2, "a failed run must not wedge the monitor"

    def test_the_script_cache_runner_is_marked_out_of_band(self):
        from bobi.monitors.script_cache_checks import script_cache

        # The one bundled runner that invokes the agent runtime.
        assert getattr(script_cache, "out_of_band", False) is True


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


# === Spawn check entry_point (#212) ===

class TestDefaultSpawnCheckEntryPoint:
    """Regression tests for #212: monitor spawn checks use entry_point
    for --role, matching how named start resolves the role."""

    def test_entry_point_used_for_role(self, tmp_path, monkeypatch):
        """entry_point from agent.yaml produces --role <entry_point>
        in the spawn command."""
        from bobi.monitors.scheduler import _default_spawn_check
        import bobi.sdk as sdk_mod

        # Minimal project: entry_point set, no defaults.role
        project = tmp_path / "proj"
        paths.state_dir(project)
        paths.package_dir(project).mkdir(parents=True)
        paths.agent_yaml_path(project).write_text(
            "agent: test-pack\nentry_point: support_manager\n"
        )

        monkeypatch.setattr(sdk_mod, "get_project_root", lambda: project)
        paths.bind_root(project)

        captured_cmds = []

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                captured_cmds.append(cmd)

            def communicate(self, timeout=None):
                return ("", "")

            def kill(self):
                pass

        monkeypatch.setattr("subprocess.Popen", FakePopen)

        m = Monitor(name="email-watch", description="check for new emails",
                    event="monitor/email-watch")
        _default_spawn_check(m, str(project), lambda verdict: None)

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert "--role" in cmd, f"--role missing from command: {cmd}"
        role_idx = cmd.index("--role")
        assert cmd[role_idx + 1] == "support_manager"
        assert "--as-check" in cmd
        assert "--wait" not in cmd
        # The check agent no longer publishes — the scheduler does, after
        # converting the verdict to conditions on the shared reconcile path.
        assert "--post-event" not in cmd

    def test_entry_point_used_even_when_defaults_role_set(self, tmp_path, monkeypatch):
        """entry_point is always used for monitor spawns, even when
        defaults.role is set (defaults.role is for ad-hoc launches)."""
        from bobi.monitors.scheduler import _default_spawn_check
        import bobi.sdk as sdk_mod

        project = tmp_path / "proj"
        paths.state_dir(project)
        paths.package_dir(project).mkdir(parents=True)
        paths.agent_yaml_path(project).write_text(
            "agent: test-pack\nentry_point: monitor_role\n"
            "defaults:\n  role: adhoc_role\n"
        )

        monkeypatch.setattr(sdk_mod, "get_project_root", lambda: project)
        paths.bind_root(project)

        captured_cmds = []

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                captured_cmds.append(cmd)

            def communicate(self, timeout=None):
                return ("", "")

            def kill(self):
                pass

        monkeypatch.setattr("subprocess.Popen", FakePopen)

        m = Monitor(name="check", description="check something",
                    event="monitor/check")
        _default_spawn_check(m, str(project), lambda verdict: None)

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        role_idx = cmd.index("--role")
        assert cmd[role_idx + 1] == "monitor_role"

    def test_no_entry_point_defaults_to_manager(self, tmp_path, monkeypatch):
        """No monitor role and no entry_point resolves --role to "manager" -
        the same default named start applies (Config.entry_role, #695), so a
        check spawn never fails on a config `bobi start` accepts."""
        from bobi.monitors.scheduler import _default_spawn_check
        import bobi.sdk as sdk_mod

        project = tmp_path / "proj"
        paths.state_dir(project)
        paths.package_dir(project).mkdir(parents=True)
        paths.agent_yaml_path(project).write_text("agent: test-pack\n")

        monkeypatch.setattr(sdk_mod, "get_project_root", lambda: project)
        paths.bind_root(project)

        captured_cmds = []

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                captured_cmds.append(cmd)

            def communicate(self, timeout=None):
                return ("", "")

            def kill(self):
                pass

        monkeypatch.setattr("subprocess.Popen", FakePopen)

        m = Monitor(name="check", description="check something",
                    event="monitor/check")
        _default_spawn_check(m, str(project), lambda verdict: None)

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert cmd[cmd.index("--role") + 1] == "manager"

    def test_description_only_monitor_spawns_check(self, tmp_path):
        """End-to-end: a description-only monitor invokes spawn_check
        (proving the check actually runs, not silently fails)."""
        m = Monitor(name="custom", description="check the thing",
                    event="monitor/custom")
        spawned = []
        sched, injected = _scheduler(tmp_path, [m], spawned=spawned)
        reg = sched._registry_loader()
        sched.run_monitor(m, reg, _fixed_now())
        assert len(spawned) == 1
        assert spawned[0][0] is m
        assert injected == []  # no direct injection — check runs out-of-band


# === Spawn sleep_cycle entry_point (#695) ===

class TestDefaultSpawnSleepCycleEntryPoint:
    """Regression tests for #695: sleep-cycle framework defaults have no
    role field, but subagents launch requires --role."""

    def test_entry_point_used_for_sleep_cycle_role_and_cli_parse(self, tmp_path, monkeypatch):
        from bobi.monitors.scheduler import _default_spawn_sleep_cycle

        project = tmp_path / "proj"
        paths.state_dir(project)
        paths.package_dir(project).mkdir(parents=True)
        paths.agent_yaml_path(project).write_text(
            "agent: test-pack\nentry_point: policy_manager\n"
        )
        role_dir = paths.roles_dir(project) / "policy_manager"
        role_dir.mkdir(parents=True)
        (role_dir / "ROLE.md").write_text("# Policy Manager\n")
        paths.bind_root(project)

        captured_cmds = []

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                captured_cmds.append(cmd)

            def communicate(self, timeout=None):
                return ('{"success": true, "updated": false}\n', "")

            def kill(self):
                pass

        monkeypatch.setattr("subprocess.Popen", FakePopen)

        m = Monitor(name="sleep-cycle", sleep_cycle=True,
                    event="system/memory.updated")
        _default_spawn_sleep_cycle(m, str(project), "curate policy", lambda result: None)

        assert len(captured_cmds) == 1
        cmd = captured_cmds[0]
        assert "launch" not in cmd
        assert cmd[cmd.index("monitors") + 1] == "curator"
        task_path = Path(cmd[cmd.index("--request") + 1])
        assert task_path.parent.name == "sleep-cycle"


# === Unified path: description-only verdicts flow through reconcile ===

class TestCheckVerdictFlow:
    """Description-only checks are just another condition detector: the
    scheduler converts the check agent's verdict into conditions and runs
    them through the same _reconcile -> publish path as every other flavor.
    The check agent never publishes and never dedups."""

    def _spawn(self, tmp_path, monitors=None):
        m = (monitors or [Monitor(name="email-watch", description="check inbox",
                                  event="monitor/support.email")])[0]
        spawned = []
        sched, published = _scheduler(tmp_path, [m], spawned=spawned)
        reg = sched._registry_loader()
        sched.run_monitor(m, reg, _fixed_now())
        _, _, on_verdict = spawned[0]
        return m, sched, published, on_verdict

    def test_finding_publishes_through_shared_path(self, tmp_path):
        m, sched, published, on_verdict = self._spawn(tmp_path)
        on_verdict({"success": True, "finding": True, "summary": "new email",
                    "details": {"key": "msg-123", "from": "a@b.example"}})
        assert len(published) == 1
        assert published[0]["event"] == "monitor/support.email"
        assert published[0]["data"]["monitor"] == "email-watch"
        assert published[0]["data"]["summary"] == "new email"
        assert published[0]["data"]["from"] == "a@b.example"

    def test_same_key_dedups_across_checks(self, tmp_path):
        """The same condition reported by successive checks fires once —
        dedup is the scheduler's, by details.key, not the agent's judgment."""
        m, sched, published, on_verdict = self._spawn(tmp_path)
        verdict = {"success": True, "finding": True, "summary": "new email",
                   "details": {"key": "msg-123"}}
        on_verdict(verdict)
        on_verdict(verdict)
        assert len(published) == 1

    def test_resolved_then_recurs_refires(self, tmp_path):
        m, sched, published, on_verdict = self._spawn(tmp_path)
        finding = {"success": True, "finding": True, "summary": "s",
                   "details": {"key": "msg-1"}}
        on_verdict(finding)
        on_verdict({"success": True, "finding": False})  # all clear -> resolved
        on_verdict(finding)                              # recurs -> refires
        assert len(published) == 2

    def test_indeterminate_leaves_state_untouched(self, tmp_path):
        """No verdict / failed check must not clear an active condition (it
        would refire spuriously) and must not fire anything itself."""
        m, sched, published, on_verdict = self._spawn(tmp_path)
        finding = {"success": True, "finding": True, "summary": "s",
                   "details": {"key": "msg-1"}}
        on_verdict(finding)
        on_verdict(None)                                  # no parseable verdict
        on_verdict({"success": False, "finding": False})  # failed check
        on_verdict(finding)                               # still active -> dedup
        assert len(published) == 1

    def test_missing_key_falls_back_to_summary_hash(self, tmp_path):
        m, sched, published, on_verdict = self._spawn(tmp_path)
        on_verdict({"success": True, "finding": True, "summary": "same thing"})
        on_verdict({"success": True, "finding": True, "summary": "same thing"})
        on_verdict({"success": True, "finding": True, "summary": "other thing"})
        assert len(published) == 2

    def test_details_id_works_as_key(self, tmp_path):
        m, sched, published, on_verdict = self._spawn(tmp_path)
        on_verdict({"success": True, "finding": True, "summary": "a",
                    "details": {"id": "PR-7"}})
        on_verdict({"success": True, "finding": True, "summary": "reworded",
                    "details": {"id": "PR-7"}})
        assert len(published) == 1


def _capture_run(monkeypatch, monitor_name):
    """A run tracker whose closed record lands in a list, not on disk."""
    from bobi.monitors import run_records

    recorded = []
    monkeypatch.setattr(run_records, "record", recorded.append)
    return recorded, run_records.RunTracker(monitor_name, flavor="command")


class TestPublishRetry:
    """A condition is recorded active only after its event actually
    publishes - a failed publish (event server down) is parked with its
    payload and retried by the tick drain instead of being silently lost."""

    def test_failed_publish_is_parked_and_the_drain_retries_it(self, tmp_path):
        outcomes = iter([False, True])
        calls = []

        def flaky_publish(event, data):
            ok = next(outcomes)
            calls.append(ok)
            return ok

        m = Monitor(name="x", event="monitor/x")
        sched, _ = _scheduler(tmp_path, [m], publish=flaky_publish)

        sched._reconcile(m, [Condition(key="k", data={})])
        assert sched.state["x"]["active"] == []  # not active until published
        assert sched.state["x"]["pending_publish"] == {"k": {}}

        # The detector still reports it, and reconcile must NOT fire it again:
        # the park owns the key now, and one outage delivering the same
        # finding twice is its own bug.
        sched._reconcile(m, [Condition(key="k", data={})])
        assert calls == [False]

        sched._drain_parked()
        assert calls == [False, True]
        assert sched.state["x"]["active"] == ["k"]
        assert "pending_publish" not in sched.state["x"]

    def test_the_drain_runs_for_a_monitor_that_is_not_due(self, tmp_path):
        """The whole of #1006: a scheduled monitor's "next interval" is a day
        away, so a retry that waits for the monitor to be due never runs."""
        outcomes = iter([False, True])

        def flaky_publish(event, data):
            return next(outcomes)

        m = Monitor(name="x", event="monitor/x", interval="1d")
        sched, _ = _scheduler(tmp_path, [m], publish=flaky_publish)

        sched._reconcile(m, [Condition(key="k", data={})])
        sched.state["x"]["last_run"] = _fixed_now().isoformat()
        assert not sched._due(m, _fixed_now())  # nothing will re-detect today

        sched.tick()
        assert sched.state["x"]["active"] == ["k"]

    def test_a_parked_finding_the_detector_drops_is_given_up_on(self, tmp_path):
        """The park waits for the transport, not forever. Its bound is the
        monitor's own next firing: a key that firing no longer produces has
        been retired at the source."""
        m = Monitor(name="x", event="monitor/x")
        sched, published = _scheduler(tmp_path, [m],
                                      publish=lambda event, data: False)
        sched._reconcile(m, [Condition(key="k", data={})])
        assert sched.state["x"]["pending_publish"] == {"k": {}}

        sched._reconcile(m, [])  # all clear - the condition no longer holds
        assert "pending_publish" not in sched.state["x"]
        assert sched.state["x"]["active"] == []

    def test_the_park_is_bounded_and_the_waiters_keep_their_place(self, tmp_path):
        from bobi.monitors.scheduler import PARK_MAX_ITEMS

        m = Monitor(name="x", event="monitor/x")
        sched, _ = _scheduler(tmp_path, [m], publish=lambda event, data: False)
        sched._reconcile(m, [Condition(key=f"k{i}", data={})
                             for i in range(PARK_MAX_ITEMS + 5)])
        # Which payloads survive is the point, not just how many: evicting the
        # longest waiter to admit a newcomer would invert the bound.
        assert list(sched.state["x"]["pending_publish"]) == [
            f"k{i}" for i in range(PARK_MAX_ITEMS)]

    def test_a_payload_a_full_park_cannot_hold_is_reported_as_dropped(
            self, tmp_path, monkeypatch):
        """Nothing retries an overflowed payload, so the run record must not
        say it is queued for retry. Saying "retrying" over a finding nothing
        retries is the whole of #1006 - it must not come back at the park
        boundary."""
        from bobi.monitors.scheduler import PARK_MAX_ITEMS

        m = Monitor(name="x", event="monitor/x")
        sched, _ = _scheduler(tmp_path, [m], publish=lambda event, data: False)
        recorded, tracker = _capture_run(monkeypatch, "x")
        sched._reconcile(m, [Condition(key=f"k{i}", data={})
                             for i in range(PARK_MAX_ITEMS + 5)], tracker)
        tracker.close()

        assert "5 finding(s) failed to publish and were DROPPED" \
            in recorded[0].reason
        assert "parked" not in recorded[0].reason

    def test_a_retired_parked_finding_is_named_on_the_run_record(self, tmp_path,
                                                                 monkeypatch):
        m = Monitor(name="x", event="monitor/x")
        sched, _ = _scheduler(tmp_path, [m], publish=lambda event, data: False)
        sched._reconcile(m, [Condition(key="k", data={})])

        recorded, tracker = _capture_run(monkeypatch, "x")
        sched._reconcile(m, [], tracker)
        tracker.close()
        assert recorded[0].reason == (
            "1 finding(s) parked by an earlier firing were never published "
            "and are no longer detected - dropped")
        assert recorded[0].outcome == "failed"

    def test_the_park_survives_a_manager_restart(self, tmp_path):
        """Durability is the premise: an event-server outage and a manager
        restart co-occur routinely, and that is exactly when the park has to
        work."""
        m = Monitor(name="x", event="monitor/x")
        sched, _ = _scheduler(tmp_path, [m], publish=lambda event, data: False)
        sched._reconcile(m, [Condition(key="k", data={"n": 1})])

        # A whole new scheduler over the same state file - nothing in memory.
        revived, published = _scheduler(tmp_path, [m])
        assert revived.state["x"]["pending_publish"] == {"k": {"n": 1}}
        revived._drain_parked()
        assert [p["data"]["finding_key"] for p in published] == ["k"]
        assert "pending_publish" not in revived.state["x"]

    def test_a_late_delivery_writes_its_own_run_record(self, tmp_path, monkeypatch):
        """The run ledger is where a lost firing is diagnosed from, so the
        delivery that eventually follows it has to show up there too."""
        from bobi.monitors import run_records

        recorded = []
        monkeypatch.setattr(run_records, "record", recorded.append)

        m = Monitor(name="x", event="monitor/x")
        sched, _ = _scheduler(tmp_path, [m])
        sched.state["x"] = {"active": [], "pending_publish": {"k": {}}}
        sched._drain_parked()

        assert len(recorded) == 1
        assert recorded[0].monitor == "x"
        assert recorded[0].outcome == run_records.NOTIFIED
        assert recorded[0].published == 1
        assert "delivered 1 parked finding(s)" in recorded[0].reason

    def test_one_failed_post_ends_the_drain_for_this_tick(self, tmp_path):
        """A dead transport costs one 10s timeout per tick, not one per
        parked payload."""
        m = Monitor(name="x", event="monitor/x")
        attempts = []

        def publish(event, data):
            attempts.append(data["finding_key"])
            return False

        sched, _ = _scheduler(tmp_path, [m], publish=publish)
        sched.state["x"] = {"active": [],
                            "pending_publish": {"a": {}, "b": {}, "c": {}}}
        sched._drain_parked()
        assert attempts == ["a"]
        assert set(sched.state["x"]["pending_publish"]) == {"a", "b", "c"}

    def test_a_rejected_payload_does_not_starve_its_park_siblings(self, tmp_path):
        """The batch stops at its first failure, so a payload the server
        rejects outright (a 4xx it will never accept) must not sit at the head
        of the park blackholing its siblings on a healthy bus."""
        m = Monitor(name="x", event="monitor/x")
        delivered = []

        def publish(event, data):
            if data["finding_key"] == "poison":
                return False
            delivered.append(data["finding_key"])
            return True

        sched, _ = _scheduler(tmp_path, [m], publish=publish)
        sched.state["x"] = {"active": [],
                            "pending_publish": {"poison": {}, "good": {}}}
        for _ in range(4):
            sched._drain_parked()
        assert delivered == ["good"]
        assert list(sched.state["x"]["pending_publish"]) == ["poison"]

    def test_a_payload_the_wire_cannot_carry_is_not_parked(self, tmp_path, monkeypatch):
        """NaN parses in Python and not on the wire, so a retry can never
        land it - and parking it would make monitor_state.json unreadable to
        every non-Python reader."""
        m = Monitor(name="x", event="monitor/x")
        sched, _ = _scheduler(tmp_path, [m], publish=lambda e, d: False)
        recorded, tracker = _capture_run(monkeypatch, "x")
        sched._reconcile(m, [Condition(key="k", data={"n": float("nan")})],
                         tracker)
        tracker.close()

        assert "pending_publish" not in sched.state["x"]
        assert "DROPPED" in recorded[0].reason

    def test_a_firing_holding_an_undelivered_park_is_not_quiet(self, tmp_path,
                                                               monkeypatch):
        """A monitor that cannot deliver must not read as healthy. The keys
        this firing skips because the park owns them are findings nobody has
        heard."""
        m = Monitor(name="x", event="monitor/x")
        sched, _ = _scheduler(tmp_path, [m], publish=lambda e, d: False)
        sched._reconcile(m, [Condition(key="k", data={})])

        recorded, tracker = _capture_run(monkeypatch, "x")
        sched._reconcile(m, [Condition(key="k", data={})], tracker)
        tracker.close()
        assert recorded[0].outcome == "failed"
        assert recorded[0].reason == ("1 finding(s) parked by an earlier "
                                      "firing are still undelivered")

    def test_a_park_orphaned_by_pausing_the_monitor_is_given_up_on(self, tmp_path):
        """A paused monitor is never drained and never retired, so without a
        prune its park is replayed as a live finding on re-enable."""
        m = Monitor(name="x", event="monitor/x")
        other = Monitor(name="y", event="monitor/y")
        sched, published = _scheduler(tmp_path, [other])
        sched.state["x"] = {"active": [], "pending_publish": {"stale": {}}}

        sched._drain_parked()  # registry no longer offers "x"
        assert published == []
        assert "pending_publish" not in sched.state["x"]

    def test_an_empty_registry_never_prunes_a_park(self, tmp_path):
        """A registry that failed to load must not be read as "every monitor
        was deleted" and wipe every park at once."""
        sched, _ = _scheduler(tmp_path, [])
        sched.state["x"] = {"active": [], "pending_publish": {"k": {}}}
        sched._drain_parked()
        assert sched.state["x"]["pending_publish"] == {"k": {}}

    def test_the_drain_does_not_resurrect_a_key_retired_mid_post(self, tmp_path):
        """The drain publishes outside the lock, so a firing on a waiter
        thread can retire a parked key while the post is in flight. The post
        that was already on its way must not put it back - it would then be
        retried until the transport recovered and deliver the finding the
        scheduler decided must not be delivered."""
        m = Monitor(name="x", event="monitor/x")
        box = {}

        def publish(event, data):
            # Stands in for the waiter thread: the detector stops reporting
            # the key while this post is in flight.
            box["sched"]._reconcile(m, [])
            return False

        sched, _ = _scheduler(tmp_path, [m], publish=publish)
        box["sched"] = sched
        sched.state["x"] = {"active": [], "pending_publish": {"k": {}}}
        sched._drain_parked()
        assert "pending_publish" not in sched.state["x"]

    def test_the_drain_runs_after_the_due_loop(self, tmp_path):
        """The park's only staleness bound runs inside a firing, so a drain
        that went first would publish the stale payload the firing right
        behind it was about to give up on."""
        m = Monitor(name="x", event="monitor/x", interval="5m",
                    command="echo '[]'")
        sched, published = _scheduler(tmp_path, [m])
        sched.state["x"] = {"active": [], "pending_publish": {"stale": {}}}

        sched.tick()  # the firing reports nothing: "stale" is retired first
        assert published == []
        assert "pending_publish" not in sched.state["x"]

    def test_a_rejected_payload_does_not_starve_the_monitors_behind_it(self, tmp_path):
        """The first failure ends the drain, so a payload the server will
        never accept must not sit permanently in front of everyone else."""
        poison = Monitor(name="poison", event="monitor/poison")
        ok_monitor = Monitor(name="fine", event="monitor/fine")
        attempts = []

        def publish(event, data):
            attempts.append(data["monitor"])
            return data["monitor"] != "poison"

        sched, _ = _scheduler(tmp_path, [poison, ok_monitor], publish=publish)
        sched.state["poison"] = {"active": [], "pending_publish": {"p": {}}}
        sched.state["fine"] = {"active": [], "pending_publish": {"f": {}}}

        sched._drain_parked()
        assert attempts == ["poison"]  # stopped, "fine" never reached

        sched._drain_parked()  # next tick starts one monitor further along
        assert attempts == ["poison", "fine", "poison"]
        assert "pending_publish" not in sched.state["fine"]

    def test_successful_publish_marks_active(self, tmp_path):
        m = Monitor(name="x", event="monitor/x")
        sched, published = _scheduler(tmp_path, [m])
        sched._reconcile(m, [Condition(key="k", data={})])
        assert sched.state["x"]["active"] == ["k"]
        assert len(published) == 1


class TestParseVerdict:
    def test_extracts_trailing_verdict_line(self):
        from bobi.monitors.scheduler import _parse_verdict
        out = ('Launching check...\n'
               '{"success": true, "finding": true, "summary": "s", "details": {}}\n')
        v = _parse_verdict(out)
        assert v == {"success": True, "finding": True, "summary": "s",
                     "details": {}}

    def test_ignores_non_verdict_json(self):
        from bobi.monitors.scheduler import _parse_verdict
        assert _parse_verdict('{"unrelated": 1}\n') is None

    def test_no_output_is_none(self):
        from bobi.monitors.scheduler import _parse_verdict
        assert _parse_verdict("") is None
        assert _parse_verdict(None) is None

    def test_last_verdict_wins(self):
        from bobi.monitors.scheduler import _parse_verdict
        out = ('{"finding": false}\n'
               '{"success": true, "finding": true, "summary": "s"}\n')
        assert _parse_verdict(out)["finding"] is True


# === Relevance gate (two-tier semantic gate, #630) ===

class TestRelevanceSchema:
    def test_relevance_parses_and_roundtrips(self):
        m = Monitor.from_dict({"name": "x", "check": "venn_poll",
                               "relevance": "about billing"})
        assert m.relevance == "about billing"
        assert "relevance" not in m.extra
        assert m.to_dict()["relevance"] == "about billing"

    def test_absent_relevance_not_serialized(self):
        m = Monitor.from_dict({"name": "x", "command": "echo"})
        assert m.relevance == ""
        assert "relevance" not in m.to_dict()

    def test_gated_predicate_single_source_of_truth(self):
        """Monitor.gated is the one predicate both the scheduler routing and
        validate consult - it must gate exactly the mechanical detectors."""
        gated = dict(relevance="about x")
        assert Monitor(name="a", command="echo", **gated).gated
        assert Monitor(name="b", check="venn_poll", **gated).gated
        # run_monitor's elif chain hits command/check before curator, so a
        # command+curator combo IS gated at runtime - gated must agree.
        assert Monitor(name="c", command="echo", curator=True, **gated).gated
        assert not Monitor(name="d", **gated).gated              # description-only
        assert not Monitor(name="e", notify=True, command="echo", **gated).gated
        assert not Monitor(name="f", curator=True, **gated).gated
        assert not Monitor(name="g", command="echo").gated       # no criterion


def _gated_monitor(**overrides):
    """A command monitor with a relevance criterion (the gated shape)."""
    fields = dict(name="billing", command="echo '[]'",
                  relevance="emails about billing",
                  event="monitor/email.billing")
    fields.update(overrides)
    return Monitor(**fields)


class TestRelevanceGateScheduling:
    def test_new_conditions_go_to_gate_not_publish(self, tmp_path):
        m = _gated_monitor()
        gates = []
        sched, published = _scheduler(tmp_path, [m], gates=gates)
        sched._reconcile_gated(m, [Condition(key="m1", data={"subject": "refund"})],
                               [Path("/repo")])
        assert published == []          # nothing publishes before the verdict
        assert len(gates) == 1
        mon, cwd, items, _cb = gates[0]
        assert mon is m
        assert cwd == "/repo"
        assert [c.key for c in items] == ["m1"]

    def test_no_new_items_spawns_no_gate(self, tmp_path):
        m = _gated_monitor()
        gates = []
        sched, published = _scheduler(tmp_path, [m], gates=gates)
        sched.state["billing"] = {"active": ["m1"]}
        sched._reconcile_gated(m, [Condition(key="m1", data={})], [])
        assert gates == []              # still-active item: zero LLM calls
        assert published == []
        assert sched.state["billing"]["active"] == ["m1"]

    def test_only_new_keys_reach_the_gate(self, tmp_path):
        m = _gated_monitor()
        gates = []
        sched, _ = _scheduler(tmp_path, [m], gates=gates)
        sched.state["billing"] = {"active": ["m1"]}
        sched._reconcile_gated(m, [Condition(key="m1", data={}),
                                   Condition(key="m2", data={})], [])
        assert [c.key for c in gates[0][2]] == ["m2"]

    def test_relevant_publishes_and_records_irrelevant_records_silently(self, tmp_path):
        m = _gated_monitor()
        gates = []
        sched, published = _scheduler(tmp_path, [m], gates=gates)
        conditions = [Condition(key="m1", data={"subject": "refund"}),
                      Condition(key="m2", data={"subject": "lunch"})]
        sched._reconcile_gated(m, conditions, [])
        _, _, items, on_verdict = gates[0]
        on_verdict({"success": True, "relevant": ["m1"]})

        assert len(published) == 1
        ev = published[0]
        assert ev["event"] == "monitor/email.billing"
        assert ev["data"]["subject"] == "refund"
        assert ev["data"]["finding_key"] == "m1"
        # Both keys recorded: neither is ever re-judged.
        assert set(sched.state["billing"]["active"]) == {"m1", "m2"}

        # Next tick with the same items: no gate, no publish.
        sched._reconcile_gated(m, conditions, [])
        assert len(gates) == 1
        assert len(published) == 1

    def test_indeterminate_gate_records_nothing_and_retries(self, tmp_path):
        m = _gated_monitor()
        gates = []
        sched, published = _scheduler(tmp_path, [m], gates=gates)
        conditions = [Condition(key="m1", data={})]
        sched._reconcile_gated(m, conditions, [])
        gates[0][3](None)               # gate process died / no verdict

        assert published == []
        assert sched.state["billing"]["active"] == []
        # The item is still new - the next tick re-gates it.
        sched._reconcile_gated(m, conditions, [])
        assert len(gates) == 2

    def test_gate_success_false_is_indeterminate(self, tmp_path):
        m = _gated_monitor()
        gates = []
        sched, published = _scheduler(tmp_path, [m], gates=gates)
        sched._reconcile_gated(m, [Condition(key="m1", data={})], [])
        gates[0][3]({"success": False, "relevant": []})
        assert published == []
        assert sched.state["billing"]["active"] == []

    def test_failed_publish_retries_mechanically_without_regating(self, tmp_path):
        """A judged-relevant item whose publish failed must NOT go back to
        the model (a second opinion on a borderline item could flip and
        silently drop the finding). It parks in pending_publish and the tick
        drain retries only the publish, at $0."""
        m = _gated_monitor()
        gates = []
        published = []
        ok = {"value": False}

        def publish(event, data):
            published.append({"event": event, "data": data})
            return ok["value"]

        sched, _ = _scheduler(tmp_path, [m], gates=gates, publish=publish)
        conditions = [Condition(key="m1", data={"subject": "refund"})]
        sched._reconcile_gated(m, conditions, [])
        gates[0][3]({"success": True, "relevant": ["m1"]})

        # Publish failed: parked as pending, not active, judged exactly once.
        assert sched.state["billing"]["active"] == []
        assert sched.state["billing"]["pending_publish"] == {
            "m1": {"subject": "refund"}}

        # Next tick: the drain retries the publish. No second gate call.
        ok["value"] = True
        sched._drain_parked()
        assert len(gates) == 1
        assert [p["data"]["finding_key"] for p in published] == ["m1", "m1"]
        assert sched.state["billing"]["active"] == ["m1"]
        assert "pending_publish" not in sched.state["billing"]

        # And the detection that follows re-gates nothing and re-sends
        # nothing - the item was judged once and has now landed.
        sched._reconcile_gated(m, conditions, [])
        assert len(gates) == 1
        assert len(published) == 2

    def test_a_gated_park_is_held_even_when_the_detector_drops_the_key(self, tmp_path):
        """The one place the two parks differ, deliberately. `_reconcile`
        retires a parked key its detector stops reporting; the gated path
        must NOT, because re-detection here costs a model call - dropping it
        means paying to judge the same item twice, or losing a
        judged-relevant finding outright when a window-scoped detector's item
        ages out."""
        ok = {"value": False}
        m = _gated_monitor()
        gates, published = [], []

        def publish(event, data):
            if not ok["value"]:
                return False
            published.append(data["finding_key"])
            return True

        sched, _ = _scheduler(tmp_path, [m], gates=gates, publish=publish)
        sched._reconcile_gated(m, [Condition(key="m1", data={})], [])
        gates[0][3]({"success": True, "relevant": ["m1"]})
        assert sched.state["billing"]["pending_publish"] == {"m1": {}}

        # The detector's window moves on and no longer reports m1.
        sched._reconcile_gated(m, [Condition(key="m2", data={})], [])
        assert sched.state["billing"]["pending_publish"] == {"m1": {}}

        # It still lands when the transport comes back, judged exactly once.
        ok["value"] = True
        sched._drain_parked()
        assert published == ["m1"]
        assert len(gates) == 2  # m1's gate and m2's, never m1 a second time

    def test_in_flight_guard_prevents_concurrent_gates(self, tmp_path):
        m = _gated_monitor()
        gates = []
        sched, _ = _scheduler(tmp_path, [m], gates=gates)
        conditions = [Condition(key="m1", data={})]
        sched._reconcile_gated(m, conditions, [])
        sched._reconcile_gated(m, conditions, [])   # verdict still pending
        assert len(gates) == 1
        # After the verdict lands the guard lifts.
        gates[0][3](None)
        sched._reconcile_gated(m, conditions, [])
        assert len(gates) == 2

    def test_batch_capped_at_gate_max_items(self, tmp_path):
        from bobi.monitors.scheduler import GATE_MAX_ITEMS
        m = _gated_monitor()
        gates = []
        sched, _ = _scheduler(tmp_path, [m], gates=gates)
        conditions = [Condition(key=f"k{i}", data={}) for i in range(GATE_MAX_ITEMS + 5)]
        sched._reconcile_gated(m, conditions, [])
        assert len(gates[0][2]) == GATE_MAX_ITEMS
        # Overflow items were not recorded - they stay new for the next tick.
        gates[0][3]({"success": True, "relevant": []})
        assert len(sched.state["billing"]["active"]) == GATE_MAX_ITEMS

    def test_disappeared_keys_clear_in_gated_path(self, tmp_path):
        m = _gated_monitor()
        gates = []
        sched, _ = _scheduler(tmp_path, [m], gates=gates)
        sched.state["billing"] = {"active": ["gone"]}
        sched._reconcile_gated(m, [], [])
        assert sched.state["billing"]["active"] == []
        assert gates == []

    def test_hallucinated_verdict_keys_ignored(self, tmp_path):
        m = _gated_monitor()
        gates = []
        sched, published = _scheduler(tmp_path, [m], gates=gates)
        sched._reconcile_gated(m, [Condition(key="m1", data={})], [])
        gates[0][3]({"success": True, "relevant": ["made-up"]})
        assert published == []                       # unknown key never fires
        assert sched.state["billing"]["active"] == ["m1"]  # judged irrelevant

    def test_run_monitor_routes_gated_command_monitor(self, tmp_path):
        m = _gated_monitor(command="echo '[{\"id\": \"m1\", \"subject\": \"hi\"}]'")
        gates = []
        sched, published = _scheduler(tmp_path, [m], gates=gates)
        sched.run_monitor(m, sched._registry_loader(), _fixed_now())
        assert published == []
        assert len(gates) == 1
        assert [c.key for c in gates[0][2]] == ["m1"]

    def test_run_monitor_notify_with_relevance_stays_ungated(self, tmp_path):
        m = Monitor(name="roundup", notify=True, relevance="whatever",
                    event="monitor/roundup", description="ping")
        gates = []
        sched, published = _scheduler(tmp_path, [m], gates=gates)
        sched.run_monitor(m, sched._registry_loader(), _fixed_now())
        assert gates == []
        assert len(published) == 1


class TestParseGateOutput:
    def test_extracts_trailing_gate_verdict(self):
        from bobi.monitors.scheduler import _parse_gate_output
        out = 'Launching gate...\n{"success": true, "relevant": ["m1"]}\n'
        assert _parse_gate_output(out) == {"success": True, "relevant": ["m1"]}

    def test_ignores_non_gate_json(self):
        from bobi.monitors.scheduler import _parse_gate_output
        assert _parse_gate_output('{"finding": true}\n') is None

    def test_no_output_is_none(self):
        from bobi.monitors.scheduler import _parse_gate_output
        assert _parse_gate_output("") is None
        assert _parse_gate_output(None) is None


class TestDefaultSpawnGate:
    def test_request_file_and_command_shape(self, tmp_path, monkeypatch):
        """The gate subprocess gets criterion + items via a request file and
        runs the `monitors gate` plumbing command."""
        import json as json_mod
        import bobi.sdk as sdk_mod
        from bobi.monitors.scheduler import _default_spawn_gate

        project = tmp_path / "proj"
        paths.state_dir(project)
        paths.package_dir(project).mkdir(parents=True)
        paths.agent_yaml_path(project).write_text("agent: test-pack\n")
        monkeypatch.setattr(sdk_mod, "get_project_root", lambda: project)
        paths.bind_root(project)

        captured = {}

        class FakePopen:
            def __init__(self, cmd, **kwargs):
                captured["cmd"] = cmd
                req_idx = cmd.index("--request")
                captured["request"] = json_mod.loads(
                    Path(cmd[req_idx + 1]).read_text())

            def communicate(self, timeout=None):
                return ('{"success": true, "relevant": ["m1"]}\n', "")

            def kill(self):
                pass

        monkeypatch.setattr("subprocess.Popen", FakePopen)

        import threading
        got = {}
        done = threading.Event()

        def on_verdict(v):
            got["verdict"] = v
            done.set()

        m = _gated_monitor()
        items = [Condition(key="m1", data={"subject": "refund"})]
        _default_spawn_gate(m, str(project), items, on_verdict)

        assert done.wait(timeout=5), "waiter thread never delivered a verdict"
        cmd = captured["cmd"]
        assert "monitors" in cmd and "gate" in cmd
        assert captured["request"]["criterion"] == "emails about billing"
        assert captured["request"]["items"] == [
            {"key": "m1", "data": {"subject": "refund"}}]
        assert got["verdict"] == {"success": True, "relevant": ["m1"]}

    def test_spawn_failure_reports_indeterminate(self, tmp_path, monkeypatch):
        import bobi.sdk as sdk_mod
        from bobi.monitors.scheduler import _default_spawn_gate

        project = tmp_path / "proj"
        paths.state_dir(project)
        paths.package_dir(project).mkdir(parents=True)
        paths.agent_yaml_path(project).write_text("agent: test-pack\n")
        monkeypatch.setattr(sdk_mod, "get_project_root", lambda: project)
        paths.bind_root(project)

        def _boom(*a, **kw):
            raise OSError("no exec")

        monkeypatch.setattr("subprocess.Popen", _boom)

        got = {}
        m = _gated_monitor()
        _default_spawn_gate(m, str(project),
                            [Condition(key="m1", data={})],
                            lambda v: got.setdefault("verdict", v))
        # Synchronous failure path: indeterminate, never silently dropped.
        assert got["verdict"] is None
        # The request file must not leak when the spawn failed.
        assert list((paths.state_dir(project) / "gates").glob("*.json")) == []


class TestWriteGateRequest:
    def _bind(self, tmp_path, monkeypatch):
        import bobi.sdk as sdk_mod
        project = tmp_path / "proj"
        paths.state_dir(project)
        paths.package_dir(project).mkdir(parents=True)
        paths.agent_yaml_path(project).write_text("agent: test-pack\n")
        monkeypatch.setattr(sdk_mod, "get_project_root", lambda: project)
        paths.bind_root(project)
        return project

    def test_non_json_safe_payload_is_stringified(self, tmp_path, monkeypatch):
        """A check plugin may return datetimes/Decimals in Condition.data;
        the request writer must stringify, not raise every tick."""
        import json as json_mod
        from datetime import datetime as dt
        from bobi.monitors.scheduler import _write_gate_request

        project = self._bind(tmp_path, monkeypatch)
        m = _gated_monitor()
        path = _write_gate_request(
            m, [Condition(key="m1", data={"at": dt(2026, 7, 4)})])
        assert path is not None
        request = json_mod.loads(Path(path).read_text())
        assert "2026-07-04" in request["items"][0]["data"]["at"]

    def test_oversized_payload_truncated_at_write_time(self, tmp_path, monkeypatch):
        """The gate prompt truncates per item anyway - large payloads must
        not be written and re-parsed in full per gate call."""
        import json as json_mod
        from bobi.monitors.scheduler import _write_gate_request
        from bobi.subagent import GATE_ITEM_CHARS

        project = self._bind(tmp_path, monkeypatch)
        m = _gated_monitor()
        path = _write_gate_request(
            m, [Condition(key="m1", data={"body": "x" * (GATE_ITEM_CHARS * 3)})])
        request = json_mod.loads(Path(path).read_text())
        data = request["items"][0]["data"]
        assert len(data["truncated_payload"]) == GATE_ITEM_CHARS

    def test_orphaned_request_files_swept(self, tmp_path, monkeypatch):
        """A manager that died mid-gate leaves its request file (raw item
        payloads) behind; the next gate write sweeps stale ones."""
        import os
        import time as time_mod
        from bobi.monitors.scheduler import (_GATE_REQUEST_MAX_AGE,
                                             _write_gate_request)

        project = self._bind(tmp_path, monkeypatch)
        gates_dir = paths.state_dir(project) / "gates"
        gates_dir.mkdir(parents=True, exist_ok=True)
        orphan = gates_dir / "dead-manager.json"
        orphan.write_text("{}")
        stale = time_mod.time() - _GATE_REQUEST_MAX_AGE - 60
        os.utime(orphan, (stale, stale))

        m = _gated_monitor()
        _write_gate_request(m, [Condition(key="m1", data={})])
        assert not orphan.exists()
