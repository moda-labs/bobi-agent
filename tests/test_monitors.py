"""Tests for the background monitoring system — schema, registry, scheduler."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from modastack.config import GlobalConfig
from modastack.monitors.schema import Monitor, parse_interval
from modastack.monitors import registry as registry_mod
from modastack.monitors.registry import MonitorRegistry
from modastack.monitors.checks import Condition
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

    def test_state_key_namespaces_repo_scoped(self):
        assert Monitor(name="dh").state_key == "dh"
        assert Monitor(name="dh", repo="/r/jobtack").state_key == "dh@/r/jobtack"

    def test_to_dict_roundtrip_disabled(self):
        m = Monitor.from_dict({"name": "x", "enabled": False, "url": "u"})
        d = m.to_dict()
        assert d["enabled"] is False
        assert d["url"] == "u"


# === Registry merge ===

def _write(path: Path, monitors: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump({"monitors": monitors}))


class TestRegistryMerge:
    def test_repo_specific_monitor_scoped(self, tmp_path):
        repo = tmp_path / "jobtack"
        _write(repo / ".modastack" / "monitors.yaml", [
            {"name": "deploy-health", "interval": "5m", "url": "https://j"},
        ])
        reg = MonitorRegistry.load(GlobalConfig(repos=[repo]), repo_path=repo)
        dh = [m for m in reg.effective_monitors() if m.name == "deploy-health"]
        assert len(dh) == 1
        assert dh[0].repo == str(repo)
        assert reg.repos_for(dh[0]) == [repo]

    def test_repo_opt_out_of_default(self, tmp_path):
        repo = tmp_path / "jobtack"
        _write(repo / ".modastack" / "monitors.yaml", [{"name": "stale-pr-check", "enabled": False}])
        reg = MonitorRegistry.load(GlobalConfig(repos=[repo]), repo_path=repo)
        stale = [m for m in reg.effective_monitors() if m.name == "stale-pr-check"]
        for s in stale:
            assert reg.repos_for(s) == []

    def test_repo_override_of_default(self, tmp_path):
        repo = tmp_path / "jobtack"
        _write(repo / ".modastack" / "monitors.yaml", [{"name": "pr-conflict-check", "interval": "5m"}])
        reg = MonitorRegistry.load(GlobalConfig(repos=[repo]), repo_path=repo)
        glob = reg.globals.get("pr-conflict-check")
        if glob:
            assert reg.repos_for(glob) == []
        scoped = [m for m in reg.repo_monitors if m.name == "pr-conflict-check"][0]
        assert reg.repos_for(scoped) == [repo]


# === Registry writes ===

class TestRegistryWrites:
    def test_add_repo_writes_monitors_file(self, tmp_path):
        repo = tmp_path / "r"
        repo.mkdir()
        MonitorRegistry.add_repo(Monitor(name="dh", extra={"url": "u"}), repo)
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

        def repos_for(self, m):
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

        import modastack.monitors.scheduler as sm
        sm.CHECKS["__test_check"] = lambda mon, repos: [Condition(key="k", data={"a": 1})]
        m.check = "__test_check"
        try:
            reg = sched._registry_loader()
            sched.run_monitor(m, reg, _fixed_now())
        finally:
            del sm.CHECKS["__test_check"]
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
        assert cwd == "/repo"  # first applicable repo
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
