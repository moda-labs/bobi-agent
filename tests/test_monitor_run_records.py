"""Monitor run records — the per-firing outcome ledger (U1).

Two layers: the store itself (round-trip, ordering, retention, corruption)
and the scheduler wiring that produces exactly one record per firing with an
honest outcome.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from bobi.monitors import run_records
from bobi.monitors.run_records import FAILED, NOTIFIED, QUIET, MonitorRun, RunTracker
from bobi.monitors.scheduler import MonitorScheduler
from bobi.monitors.schema import Condition, Monitor


def _fixed_now():
    return datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)


def _run(monitor="m", outcome=QUIET, started_at="2026-07-31T12:00:00+00:00", **kw):
    return MonitorRun(
        run_id=run_records.new_run_id(monitor), monitor=monitor,
        started_at=started_at, ended_at=started_at, outcome=outcome, **kw)


# === Store ===

class TestStore:
    def test_round_trip(self, bobi_install):
        run_records.record(_run(outcome=NOTIFIED, reason="posted digest",
                                published=2, flavor="command"))
        loaded = run_records.load("m")
        assert len(loaded) == 1
        assert loaded[0].outcome == NOTIFIED
        assert loaded[0].reason == "posted digest"
        assert loaded[0].published == 2
        assert loaded[0].flavor == "command"

    def test_newest_first(self, bobi_install):
        for i in range(3):
            run_records.record(_run(started_at=f"2026-07-31T12:0{i}:00+00:00"))
        assert [r.started_at for r in run_records.load("m")] == [
            "2026-07-31T12:02:00+00:00",
            "2026-07-31T12:01:00+00:00",
            "2026-07-31T12:00:00+00:00",
        ]

    def test_retention_cap_drops_the_oldest(self, bobi_install):
        for i in range(run_records.RETENTION_PER_MONITOR + 5):
            run_records.record(_run(reason=f"run-{i}"))
        loaded = run_records.load("m")
        assert len(loaded) == run_records.RETENTION_PER_MONITOR
        assert loaded[0].reason == f"run-{run_records.RETENTION_PER_MONITOR + 4}"
        assert loaded[-1].reason == "run-5"

    def test_retention_is_per_monitor(self, bobi_install):
        for i in range(run_records.RETENTION_PER_MONITOR + 5):
            run_records.record(_run(monitor="loud", reason=f"r{i}"))
        run_records.record(_run(monitor="quiet-one", reason="only"))
        assert len(run_records.load("quiet-one")) == 1

    def test_monitor_names_with_path_characters_stay_in_the_runs_dir(self, bobi_install):
        run_records.record(_run(monitor="../../etc/passwd", reason="nice try"))
        written = list(run_records._runs_dir().glob("*.json"))
        assert len(written) == 1
        assert written[0].parent == run_records._runs_dir()
        assert run_records.load("../../etc/passwd")[0].reason == "nice try"

    def test_missing_and_corrupt_files_read_as_empty(self, bobi_install):
        assert run_records.load("never-ran") == []
        run_records.record(_run())
        run_records.record_path("m").write_text("{not json")
        assert run_records.load("m") == []
        assert run_records.load_all() == []

    def test_load_all_merges_monitors_newest_first(self, bobi_install):
        run_records.record(_run(monitor="a", started_at="2026-07-31T12:00:00+00:00"))
        run_records.record(_run(monitor="b", started_at="2026-07-31T13:00:00+00:00"))
        run_records.record(_run(monitor="a", started_at="2026-07-31T14:00:00+00:00"))
        assert [r.monitor for r in run_records.load_all()] == ["a", "b", "a"]

    def test_a_broken_store_never_breaks_a_firing(self, bobi_install, monkeypatch):
        monkeypatch.setattr(run_records, "_runs_dir",
                            lambda: (_ for _ in ()).throw(OSError("disk gone")))
        run_records.record(_run())  # must not raise


# === Tracker ===

class TestTracker:
    def test_nothing_published_is_quiet(self, bobi_install):
        RunTracker("m").close()
        assert run_records.load("m")[0].outcome == QUIET

    def test_a_publish_makes_it_notified(self, bobi_install):
        t = RunTracker("m")
        t.add_published(2)
        t.close()
        record = run_records.load("m")[0]
        assert record.outcome == NOTIFIED
        assert record.published == 2

    def test_a_noted_failure_beats_a_publish(self, bobi_install):
        # Two of three findings delivered and then it broke: not a clean run.
        t = RunTracker("m")
        t.add_published(2)
        t.note_failure("1 finding(s) failed to publish")
        t.close()
        record = run_records.load("m")[0]
        assert record.outcome == FAILED
        assert record.reason == "1 finding(s) failed to publish"
        assert record.published == 2

    def test_first_failure_reason_wins(self, bobi_install):
        t = RunTracker("m")
        t.note_failure("spawn-failed: no such file")
        t.note_failure("check agent returned no usable verdict")
        t.close()
        assert run_records.load("m")[0].reason == "spawn-failed: no such file"

    def test_close_is_idempotent(self, bobi_install):
        t = RunTracker("m")
        t.close()
        t.close(FAILED, "second thoughts")
        assert len(run_records.load("m")) == 1
        assert run_records.load("m")[0].outcome == QUIET

    def test_session_and_script_cache_mode_are_optional(self, bobi_install):
        t = RunTracker("m", flavor="check:script_cache")
        t.note_session("")
        t.note_script_cache_mode("")
        t.note_session("monitor-x-check")
        t.note_script_cache_mode("cached")
        t.close()
        record = run_records.load("m")[0]
        assert record.session_ref == "monitor-x-check"
        assert record.script_cache_mode == "cached"
        assert record.flavor == "check:script_cache"


# === Scheduler wiring ===

def _scheduler(tmp_path, monitors, *, publish=None, spawned=None, gates=None,
               real_spawn=False):
    """A scheduler over a hand-built registry, capturing published events.

    `spawned` / `gates`, when lists, capture the spawn callbacks instead of
    launching subprocesses. `real_spawn` keeps the production spawn hooks —
    the only way to exercise the spawn helpers' own failure reporting.
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

    kw = {}
    if not real_spawn:
        kw["spawn_check"] = (
            (lambda mon, cwd, cb: spawned.append((mon, cwd, cb)))
            if spawned is not None else (lambda mon, cwd, cb: None))
        kw["spawn_gate"] = (
            (lambda mon, cwd, items, cb: gates.append((mon, cwd, items, cb)))
            if gates is not None else (lambda mon, cwd, items, cb: None))

    sched = MonitorScheduler(
        publish=publish or _record_publish,
        state_path=tmp_path / "state.json",
        now=_fixed_now,
        registry_loader=lambda: FakeRegistry(),
        **kw,
    )
    return sched, published


def _fire(sched, monitor):
    sched.run_monitor(monitor, sched._registry_loader(), _fixed_now())


class TestSchedulerRecordsRuns:
    def test_a_notifying_command_run(self, bobi_install, tmp_path):
        m = Monitor(name="emails", event="monitor/emails",
                    command="echo '[{\"id\": \"e1\"}]'")
        sched, published = _scheduler(tmp_path, [m])
        _fire(sched, m)
        assert len(published) == 1
        record = run_records.load("emails")[0]
        assert record.outcome == NOTIFIED
        assert record.published == 1
        assert record.flavor == "command"
        assert record.started_at and record.ended_at

    def test_a_quiet_run_is_distinguishable_from_a_notifying_one(
            self, bobi_install, tmp_path):
        m = Monitor(name="emails", event="monitor/emails", command="echo '[]'")
        sched, published = _scheduler(tmp_path, [m])
        _fire(sched, m)
        assert published == []
        assert run_records.load("emails")[0].outcome == QUIET

    def test_a_repeat_of_a_known_condition_is_quiet_not_notified(
            self, bobi_install, tmp_path):
        m = Monitor(name="emails", event="monitor/emails",
                    command="echo '[{\"id\": \"e1\"}]'")
        sched, published = _scheduler(tmp_path, [m])
        _fire(sched, m)
        _fire(sched, m)
        assert len(published) == 1  # deduped
        outcomes = [r.outcome for r in run_records.load("emails")]
        assert outcomes == [QUIET, NOTIFIED]

    def test_a_failing_command_records_why(self, bobi_install, tmp_path):
        m = Monitor(name="emails", command="echo 'boom' >&2; exit 3")
        sched, _ = _scheduler(tmp_path, [m])
        _fire(sched, m)
        record = run_records.load("emails")[0]
        assert record.outcome == FAILED
        assert "exited 3" in record.reason
        assert "boom" in record.reason

    def test_non_json_command_output_records_why(self, bobi_install, tmp_path):
        m = Monitor(name="emails", command="echo not-json")
        sched, _ = _scheduler(tmp_path, [m])
        _fire(sched, m)
        record = run_records.load("emails")[0]
        assert record.outcome == FAILED
        assert "non-JSON" in record.reason

    def test_an_unpublished_finding_is_not_a_notified_run(
            self, bobi_install, tmp_path):
        # The event server was down: the finding is parked, nobody heard it.
        m = Monitor(name="emails", event="monitor/emails",
                    command="echo '[{\"id\": \"e1\"}]'")
        sched, _ = _scheduler(tmp_path, [m], publish=lambda e, d: False)
        _fire(sched, m)
        record = run_records.load("emails")[0]
        assert record.outcome == FAILED
        assert "failed to publish" in record.reason
        assert record.published == 0

    def test_unknown_check_records_why(self, bobi_install, tmp_path):
        m = Monitor(name="x", check="nonexistent")
        sched, _ = _scheduler(tmp_path, [m])
        _fire(sched, m)
        record = run_records.load("x")[0]
        assert record.outcome == FAILED
        assert "nonexistent" in record.reason

    def test_a_raising_check_records_why(self, bobi_install, tmp_path):
        m = Monitor(name="x", check="__boom")
        sched, _ = _scheduler(tmp_path, [m])

        def _boom(mon, repos):
            raise RuntimeError("check exploded")

        sched._checks["__boom"] = _boom
        _fire(sched, m)
        record = run_records.load("x")[0]
        assert record.outcome == FAILED
        assert "check exploded" in record.reason

    def test_a_notify_monitor_records_a_notified_run(self, bobi_install, tmp_path):
        m = Monitor(name="standup", notify=True, description="stand-up time",
                    event="monitor/standup")
        sched, published = _scheduler(tmp_path, [m])
        _fire(sched, m)
        assert len(published) == 1
        record = run_records.load("standup")[0]
        assert record.outcome == NOTIFIED
        assert record.flavor == "notify"

    def test_script_cache_mode_rides_on_the_record(self, bobi_install, tmp_path,
                                                   monkeypatch):
        from bobi.monitors import script_cache_checks

        m = Monitor(name="inbox", check="script_cache", event="monitor/inbox")
        sched, _ = _scheduler(tmp_path, [m])
        sched._checks["script_cache"] = lambda mon, repos: []
        monkeypatch.setattr(script_cache_checks, "_load_trusted_state",
                            lambda name: {"last_mode": "cached"})
        _fire(sched, m)
        record = run_records.load("inbox")[0]
        assert record.script_cache_mode == "cached"
        assert record.flavor == "check:script_cache"

    def test_retention_holds_across_many_firings(self, bobi_install, tmp_path):
        m = Monitor(name="emails", command="echo '[]'")
        sched, _ = _scheduler(tmp_path, [m])
        for _ in range(run_records.RETENTION_PER_MONITOR + 3):
            _fire(sched, m)
        assert len(run_records.load("emails")) == run_records.RETENTION_PER_MONITOR


class TestSchedulerRecordsOutOfBandRuns:
    """An out-of-band firing is recorded when its verdict lands, not when it
    was dispatched — otherwise every spawned run would look finished."""

    def test_no_record_until_the_verdict_lands(self, bobi_install, tmp_path):
        m = Monitor(name="custom", description="check the thing",
                    event="monitor/custom")
        spawned = []
        sched, published = _scheduler(tmp_path, [m], spawned=spawned)
        _fire(sched, m)
        assert run_records.load("custom") == []

        _, _, on_verdict = spawned[0]
        on_verdict({"success": True, "finding": True, "summary": "found it",
                    "session": "monitor-check-abc-check"})
        record = run_records.load("custom")[0]
        assert record.outcome == NOTIFIED
        assert record.session_ref == "monitor-check-abc-check"
        assert record.flavor == "description"
        assert len(published) == 1

    def test_an_all_clear_verdict_is_quiet(self, bobi_install, tmp_path):
        m = Monitor(name="custom", description="check the thing")
        spawned = []
        sched, _ = _scheduler(tmp_path, [m], spawned=spawned)
        _fire(sched, m)
        spawned[0][2]({"success": True, "finding": False})
        assert run_records.load("custom")[0].outcome == QUIET

    def test_an_indeterminate_verdict_is_a_failed_run(self, bobi_install, tmp_path):
        m = Monitor(name="custom", description="check the thing")
        spawned = []
        sched, _ = _scheduler(tmp_path, [m], spawned=spawned)
        _fire(sched, m)
        spawned[0][2](None)
        record = run_records.load("custom")[0]
        assert record.outcome == FAILED
        assert "no usable verdict" in record.reason

    def test_a_spawn_failure_carries_its_reason_onto_the_record(
            self, bobi_install, tmp_path, monkeypatch):
        # The real spawn path: no test double, so _spawn_monitor_agent's
        # on_error hook is what has to reach the record.
        m = Monitor(name="custom", description="check the thing")
        sched, _ = _scheduler(tmp_path, [m], real_spawn=True)

        def _boom(*a, **kw):
            raise OSError("claude CLI not found")

        monkeypatch.setattr("subprocess.Popen", _boom)
        _fire(sched, m)
        record = run_records.load("custom")[0]
        assert record.outcome == FAILED
        assert record.reason.startswith("spawn-failed:")
        assert "claude CLI not found" in record.reason

    def test_a_gated_firing_is_recorded_when_the_gate_verdict_lands(
            self, bobi_install, tmp_path):
        m = Monitor(name="inbox", event="monitor/inbox", relevance="anything urgent",
                    command="echo '[{\"id\": \"e1\"}]'")
        gates = []
        sched, published = _scheduler(tmp_path, [m], gates=gates)
        _fire(sched, m)
        assert run_records.load("inbox") == []  # gate still in flight

        _, _, items, on_verdict = gates[0]
        on_verdict({"success": True, "relevant": [items[0].key]})
        record = run_records.load("inbox")[0]
        assert record.outcome == NOTIFIED
        assert record.published == 1
        assert len(published) == 1

    def test_an_irrelevant_gate_verdict_is_a_quiet_run(self, bobi_install, tmp_path):
        m = Monitor(name="inbox", event="monitor/inbox", relevance="anything urgent",
                    command="echo '[{\"id\": \"e1\"}]'")
        gates = []
        sched, published = _scheduler(tmp_path, [m], gates=gates)
        _fire(sched, m)
        gates[0][3]({"success": True, "relevant": []})
        assert published == []
        assert run_records.load("inbox")[0].outcome == QUIET

    def test_an_indeterminate_gate_is_a_failed_run(self, bobi_install, tmp_path):
        m = Monitor(name="inbox", event="monitor/inbox", relevance="anything urgent",
                    command="echo '[{\"id\": \"e1\"}]'")
        gates = []
        sched, _ = _scheduler(tmp_path, [m], gates=gates)
        _fire(sched, m)
        gates[0][3](None)
        record = run_records.load("inbox")[0]
        assert record.outcome == FAILED
        assert "no verdict" in record.reason

    def test_one_record_per_firing_even_when_a_gate_never_spawns(
            self, bobi_install, tmp_path):
        # Nothing new to judge: the firing ends in this thread, once.
        m = Monitor(name="inbox", event="monitor/inbox", relevance="anything urgent",
                    command="echo '[]'")
        gates = []
        sched, _ = _scheduler(tmp_path, [m], gates=gates)
        _fire(sched, m)
        assert gates == []
        assert len(run_records.load("inbox")) == 1
        assert run_records.load("inbox")[0].outcome == QUIET
