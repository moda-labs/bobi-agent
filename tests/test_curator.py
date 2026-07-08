"""Tests for the policy-curator deterministic harness and dispatch (#456).

The curator's *judgment* (what is durable, Facts vs Decisions) lives in the
agent prompt and is exercised by the integration test. Everything here is the
deterministic half — the cursor / input-cap / no-silent-skip invariants and the
scheduler's dispatch + publish + seed wiring — kept in plain, unit-testable
Python (the #454 lesson: never let a mocked model bypass the gate).
"""

import queue
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from bobi import history, paths
from bobi.monitors import curator as curator_mod
from bobi.monitors.schema import Monitor
from bobi.monitors.scheduler import MonitorScheduler


def _row(id: int, content: str = "", session_id: str = "s1",
         tool_name: str = "", tool_input: str = "", role: str = "assistant",
         type: str = "assistant", timestamp: str = "2026-06-24T00:00:00") -> dict:
    """A messages_since() row shape (SELECT * FROM messages)."""
    return {
        "id": id, "session_id": session_id, "type": type, "role": role,
        "content": content, "tool_name": tool_name, "tool_input": tool_input,
        "timestamp": timestamp, "line_number": id,
    }


# ---------------------------------------------------------------------------
# cursor IO — the success-advanced consumption watermark
# ---------------------------------------------------------------------------

class TestCursor:
    def test_missing_file_reads_zero(self, tmp_path):
        assert curator_mod.read_cursor(tmp_path / "policy_cursor") == 0

    def test_unparseable_reads_zero(self, tmp_path):
        p = tmp_path / "policy_cursor"
        p.write_text("not-an-int")
        assert curator_mod.read_cursor(p) == 0

    def test_round_trip(self, tmp_path):
        p = tmp_path / "sub" / "policy_cursor"
        curator_mod.write_cursor(p, 42)
        assert curator_mod.read_cursor(p) == 42


# ---------------------------------------------------------------------------
# select_messages — per-run input cap, oldest-first by id (tests 9 / 9a)
# ---------------------------------------------------------------------------

class TestSelectMessages:
    def test_ingests_all_when_under_budget(self):
        rows = [_row(1, "a"), _row(2, "bb"), _row(3, "ccc")]
        ingested, highest, flags = curator_mod.select_messages(rows, 1000)
        assert [r["id"] for r in ingested] == [1, 2, 3]
        assert highest == 3
        assert flags["input_truncated"] is False
        assert flags["oversized_truncated"] == 0

    def test_oldest_first_and_defers_overflow(self):
        # Each message is 10 chars; budget 25 fits ids 1 and 2 (20), defers 3.
        rows = [_row(3, "z" * 10), _row(1, "a" * 10), _row(2, "b" * 10)]
        ingested, highest, flags = curator_mod.select_messages(rows, 25)
        assert [r["id"] for r in ingested] == [1, 2]      # oldest-first by id
        assert highest == 2                                # cursor stops at top of block
        assert flags["input_truncated"] is True
        assert flags["deferred_id_range"] == (3, 3)

    def test_deferred_overflow_reread_next_run(self):
        # Run 1 defers id 3; a second run over the deferred tail re-reads it —
        # the silent-skip the R2 newest-first-drop+advance-to-max would cause.
        rows = [_row(1, "a" * 10), _row(2, "b" * 10), _row(3, "c" * 10)]
        ingested1, highest1, _ = curator_mod.select_messages(rows, 25)
        assert highest1 == 2
        remaining = [r for r in rows if r["id"] > highest1]
        ingested2, highest2, flags2 = curator_mod.select_messages(remaining, 25)
        assert [r["id"] for r in ingested2] == [3]
        assert highest2 == 3
        assert flags2["input_truncated"] is False

    def test_tie_sibling_straddling_budget_is_reread(self):
        # Sibling rows at the same timestamp but distinct ids; the budget cuts
        # between them. The deferred sibling must be re-read next run (impossible
        # to guarantee under a timestamp cursor — the R4 fix).
        T = "2026-06-24T00:00:00"
        rows = [_row(1, "x" * 10, timestamp=T), _row(2, "y" * 10, timestamp=T)]
        ingested, highest, flags = curator_mod.select_messages(rows, 12)
        assert [r["id"] for r in ingested] == [1]
        assert highest == 1
        assert flags["deferred_id_range"] == (2, 2)
        remaining = [r for r in rows if r["id"] > highest]
        ingested2, highest2, _ = curator_mod.select_messages(remaining, 12)
        assert [r["id"] for r in ingested2] == [2]   # the tie-sibling is not lost

    def test_oversized_oldest_message_truncated_not_stalled(self):
        # A single oldest message larger than the budget must be truncated (so
        # the cursor advances past it), never ingested-as-zero (a permanent
        # stall behind the oldest unread row).
        big = "Q" * 500
        rows = [_row(7, big), _row(8, "small")]
        ingested, highest, flags = curator_mod.select_messages(rows, 100)
        assert len(ingested) == 1
        assert ingested[0]["id"] == 7
        assert len(ingested[0]["content"]) <= 100
        assert "[truncated" in ingested[0]["content"]
        assert highest == 7                            # cursor moved past it
        assert flags["oversized_truncated"] == 1
        assert flags["oversized_ids"] == [7]

    def test_oversized_message_not_rehit_next_run(self):
        big = "Q" * 500
        rows = [_row(7, big), _row(8, "small")]
        _, highest, _ = curator_mod.select_messages(rows, 100)
        remaining = [r for r in rows if r["id"] > highest]
        ingested2, _, flags2 = curator_mod.select_messages(remaining, 100)
        assert [r["id"] for r in ingested2] == [8]     # oversized row not re-hit
        assert flags2["oversized_truncated"] == 0

    def test_nothing_ingestable_returns_none_highest(self):
        ingested, highest, _ = curator_mod.select_messages([], 100)
        assert ingested == []
        assert highest is None


class TestTruncateHeadTail:
    def test_keeps_under_budget_with_marker(self):
        out = curator_mod._truncate_head_tail("z" * 1000, 100)
        assert len(out) <= 100
        assert "[truncated" in out

    def test_short_text_untouched(self):
        assert curator_mod._truncate_head_tail("abc", 100) == "abc"


# ---------------------------------------------------------------------------
# parse_result / render_transcript / build_curator_task
# ---------------------------------------------------------------------------

class TestParseResult:
    def test_extracts_trailing_json(self):
        out = "blah blah\nthinking...\n{\"success\": true, \"updated\": false}"
        assert curator_mod.parse_result(out) == {"success": True, "updated": False}

    def test_ignores_prose_and_non_success_json(self):
        out = "{\"foo\": 1}\nsome prose"
        assert curator_mod.parse_result(out) is None

    def test_none_when_empty(self):
        assert curator_mod.parse_result("") is None


class TestRenderTranscript:
    def test_groups_by_session_with_ids(self):
        rows = [_row(1, "hello", session_id="A"),
                _row(2, "world", session_id="B")]
        out = curator_mod.render_transcript(rows)
        assert "### session: A" in out
        assert "### session: B" in out
        assert "#1" in out and "#2" in out

    def test_renders_tool_rows(self):
        rows = [_row(3, "", tool_name="Bash", tool_input="ls")]
        out = curator_mod.render_transcript(rows)
        assert "[tool:Bash]" in out


class TestBuildCuratorTask:
    def test_includes_seed_block_when_seeding(self):
        task = curator_mod.build_curator_task(
            "PROMPT", "transcript", "", {}, seed="LEGACY JOURNAL TEXT")
        assert "ONE-TIME SEED" in task
        assert "LEGACY JOURNAL TEXT" in task

    def test_no_seed_block_without_seed(self):
        task = curator_mod.build_curator_task("PROMPT", "transcript", "", {})
        assert "ONE-TIME SEED" not in task

    def test_surfaces_ingest_notes(self):
        flags = {"input_truncated": True, "deferred_id_range": (5, 9),
                 "oversized_truncated": 1, "oversized_ids": [2]}
        task = curator_mod.build_curator_task("P", "t", "", flags)
        assert "DEFERRED" in task and "5" in task and "9" in task
        assert "oversized" in task.lower()


# ---------------------------------------------------------------------------
# scheduler dispatch: cursor advance / publish / seed (tests 3, 4, 8)
# ---------------------------------------------------------------------------

class _CuratorHarness:
    """Drive _spawn_curator with a captured spawn + publish + stubbed history."""

    def __init__(self, tmp_path, rows):
        self.root = tmp_path
        self.rows = rows
        self.published = []
        self.captured = {}  # task, on_result, cwd
        self.cursor_seen = []

        def fake_publish(event, data):
            self.published.append((event, data))
            return True

        def fake_spawn(monitor, cwd, task, on_result):
            self.captured = {"task": task, "on_result": on_result, "cwd": cwd}

        self.sched = MonitorScheduler(
            publish=fake_publish, state_path=tmp_path / "monitor_state.json",
            project_path=tmp_path, spawn_curator=fake_spawn)

    def messages_since(self, cursor, limit=None):
        self.cursor_seen.append(cursor)
        return list(self.rows)


@pytest.fixture
def monitor():
    return Monitor(name="policy-curator", curator=True,
                   event="system/policy.updated", interval="6h")


def _patch_history(harness):
    return patch.multiple("bobi.history",
                          index=lambda: None,
                          messages_since=harness.messages_since)


class TestCuratorDispatch:
    def test_reads_cursor_not_last_run(self, tmp_path, monitor):
        h = _CuratorHarness(tmp_path, [_row(10, "a"), _row(11, "b")])
        # Pre-seed the curator cursor; the curator must read THIS, not last_run.
        from bobi import paths
        curator_mod.write_cursor(paths.policy_cursor_path(tmp_path), 9)
        with _patch_history(h):
            h.sched._spawn_curator(monitor, [tmp_path])
        assert h.cursor_seen == [9]
        assert "task" in h.captured  # dispatched

    def test_cursor_advances_to_highest_only_on_success(self, tmp_path, monitor):
        from bobi import paths
        h = _CuratorHarness(tmp_path, [_row(10, "a"), _row(11, "b"), _row(12, "c")])
        with _patch_history(h):
            h.sched._spawn_curator(monitor, [tmp_path])
        cursor_path = paths.policy_cursor_path(tmp_path)
        # Not advanced before the result lands.
        assert curator_mod.read_cursor(cursor_path) == 0
        h.captured["on_result"]({"success": True, "updated": False})
        assert curator_mod.read_cursor(cursor_path) == 12

    def test_failed_run_leaves_cursor_unmoved(self, tmp_path, monitor):
        from bobi import paths
        h = _CuratorHarness(tmp_path, [_row(10, "a"), _row(11, "b")])
        with _patch_history(h):
            h.sched._spawn_curator(monitor, [tmp_path])
        h.captured["on_result"]({"success": False})
        assert curator_mod.read_cursor(paths.policy_cursor_path(tmp_path)) == 0

    def test_publishes_policy_updated_on_success_updated(self, tmp_path, monitor):
        h = _CuratorHarness(tmp_path, [_row(10, "a")])
        with _patch_history(h):
            h.sched._spawn_curator(monitor, [tmp_path])
        h.captured["on_result"]({"success": True, "updated": True,
                                 "summary": "added a fact", "bytes": 123,
                                 "urgent": False})
        assert len(h.published) == 1
        event, data = h.published[0]
        assert event == "system/policy.updated"
        assert data["summary"] == "added a fact"
        assert data["bytes"] == 123
        assert data["urgent"] is False

    def test_no_publish_when_not_updated(self, tmp_path, monitor):
        h = _CuratorHarness(tmp_path, [_row(10, "a")])
        with _patch_history(h):
            h.sched._spawn_curator(monitor, [tmp_path])
        h.captured["on_result"]({"success": True, "updated": False})
        assert h.published == []

    def test_no_dispatch_when_no_rows_and_no_seed(self, tmp_path, monitor):
        h = _CuratorHarness(tmp_path, [])
        with _patch_history(h):
            h.sched._spawn_curator(monitor, [tmp_path])
        assert h.captured == {}  # nothing dispatched

    def test_failed_curator_result_publishes_monitor_error(self, tmp_path, monitor):
        h = _CuratorHarness(tmp_path, [_row(10, "a")])
        with _patch_history(h):
            h.sched._spawn_curator(monitor, [tmp_path])
        h.captured["on_result"]({"success": False, "summary": "bad output"})
        assert h.published == [(
            "system/monitor.error",
            {
                "monitor": "policy-curator",
                "flavor": "curator",
                "reason": "indeterminate-result",
                "detail": "bad output",
            },
        )]


class TestCuratorTaskTransport:
    def test_default_spawn_curator_passes_short_file_pointer_and_cleans_up(
        self, tmp_path, monkeypatch, monitor
    ):
        from bobi.monitors.scheduler import _default_spawn_curator

        paths.bind_root(tmp_path)
        full_task = "x" * 205_000
        captured = {}
        results = []

        class FakeProc:
            def communicate(self, timeout=None):
                return '{"success": true, "updated": false}\n', None

        def fake_popen(cmd, **kwargs):
            pointer = cmd[cmd.index("--task") + 1]
            captured["cmd"] = cmd
            captured["pointer"] = pointer
            task_path = Path(pointer.splitlines()[2])
            captured["task_path"] = task_path
            assert len(pointer) < 1000
            assert task_path.read_text() == full_task
            return FakeProc()

        monkeypatch.setattr("subprocess.Popen", fake_popen)
        _default_spawn_curator(monitor, None, full_task, results.append)

        for _ in range(100):
            if results:
                break
            time.sleep(0.01)

        assert results == [{"success": True, "updated": False}]
        assert "--task" in captured["cmd"]
        assert not captured["task_path"].exists()

    def test_monitor_spawn_rejects_oversized_argv_and_publishes_error(
        self, tmp_path, monkeypatch, monitor
    ):
        from bobi.monitors.scheduler import _spawn_monitor_agent

        paths.bind_root(tmp_path)
        published = []
        cleaned = []
        results = []

        monkeypatch.setattr(
            "bobi.monitors.scheduler._default_publish",
            lambda event, data: published.append((event, data)) or True,
        )
        monkeypatch.setattr(
            "subprocess.Popen",
            lambda *args, **kwargs: pytest.fail("Popen should not be called"),
        )

        _spawn_monitor_agent(
            ["bobi", "--task", "x" * 100_001],
            monitor.name,
            "curator",
            lambda out: {"success": True},
            results.append,
            cleanup=lambda: cleaned.append(True),
        )

        assert results == [None]
        assert cleaned == [True]
        assert published[0][0] == "system/monitor.error"
        assert published[0][1]["reason"] == "spawn-failed"
        assert "argv element" in published[0][1]["detail"]

    def test_monitor_spawn_uses_injected_publisher_for_errors(
        self, tmp_path, monkeypatch, monitor
    ):
        from bobi.monitors.scheduler import _spawn_monitor_agent

        paths.bind_root(tmp_path)
        default_published = []
        injected_published = []

        monkeypatch.setattr(
            "bobi.monitors.scheduler._default_publish",
            lambda event, data: default_published.append((event, data)) or True,
        )
        monkeypatch.setattr(
            "subprocess.Popen",
            lambda *args, **kwargs: pytest.fail("Popen should not be called"),
        )

        _spawn_monitor_agent(
            ["bobi", "--task", "x" * 100_001],
            monitor.name,
            "curator",
            lambda out: {"success": True},
            lambda result: None,
            publish=lambda event, data: injected_published.append((event, data)) or True,
        )

        assert default_published == []
        assert injected_published[0][0] == "system/monitor.error"


# ---------------------------------------------------------------------------
# one-time seed (in-scope item 7) — distill legacy journals into first policy.md
# ---------------------------------------------------------------------------

class TestCuratorSeed:
    def _seed_journal(self, tmp_path, session, text):
        from bobi import paths
        d = paths.state_path(tmp_path) / "memory" / session
        d.mkdir(parents=True, exist_ok=True)
        (d / "INDEX.md").write_text(text)

    def test_first_run_seeds_from_legacy_journals(self, tmp_path, monitor):
        self._seed_journal(tmp_path, "old-director",
                           "chose squash-merge over rebase for single-commit PRs")
        h = _CuratorHarness(tmp_path, [])  # no transcripts yet — seed still fires
        with _patch_history(h):
            h.sched._spawn_curator(monitor, [tmp_path])
        assert "task" in h.captured
        assert "ONE-TIME SEED" in h.captured["task"]
        assert "squash-merge" in h.captured["task"]

    def test_seed_skipped_once_policy_exists(self, tmp_path, monitor):
        from bobi import paths
        self._seed_journal(tmp_path, "old-director", "some legacy decision")
        paths.state_path(tmp_path).mkdir(parents=True, exist_ok=True)
        paths.policy_path(tmp_path).write_text("## Facts\n\n## Decisions\n")
        h = _CuratorHarness(tmp_path, [])  # policy.md exists + no rows → no dispatch
        with _patch_history(h):
            h.sched._spawn_curator(monitor, [tmp_path])
        assert h.captured == {}  # idempotent: seed does not re-fire

    def test_seed_runs_alongside_transcript_delta(self, tmp_path, monitor):
        self._seed_journal(tmp_path, "old-director", "legacy: prefer X")
        h = _CuratorHarness(tmp_path, [_row(5, "new transcript msg")])
        with _patch_history(h):
            h.sched._spawn_curator(monitor, [tmp_path])
        task = h.captured["task"]
        assert "ONE-TIME SEED" in task
        assert "new transcript msg" in task


# ---------------------------------------------------------------------------
# drain-side passive/active delivery (test 11)
# ---------------------------------------------------------------------------

class _OneShotQueue:
    def __init__(self, events):
        self._events = list(events)
        self._calls = 0

    def get(self):
        self._calls += 1
        if self._calls == 1 and self._events:
            return self._events[0]
        raise KeyboardInterrupt

    def empty(self):
        return not (self._calls == 1 and len(self._events) > 1)

    def get_nowait(self):
        if len(self._events) > 1:
            return self._events.pop(1)
        raise queue.Empty


def _run_drain_one_batch(events):
    from bobi.events.drain import drain_loop
    from bobi.inbox import register_local_inbox, unregister_local_inbox

    delivered = []

    class _CaptureInbox:
        def push(self, msg, priority=False):
            delivered.append(msg)

    register_local_inbox("test-policy-session", _CaptureInbox())
    try:
        with patch("bobi.events.drain.time.sleep"):
            try:
                drain_loop("test-policy-session", queue=_OneShotQueue(events),
                           formatter=lambda e: e.get("text", ""))
            except KeyboardInterrupt:
                pass
    finally:
        unregister_local_inbox("test-policy-session")
    return delivered


class TestPolicyUpdatedDelivery:
    def test_non_urgent_is_suppressed(self):
        delivered = _run_drain_one_batch([{
            "type": "system/policy.updated",
            "payload": {"summary": "routine distillation", "urgent": False},
        }])
        assert delivered == []  # passive — no inbox push

    def test_urgent_is_pushed_with_reread_instruction(self):
        delivered = _run_drain_one_batch([{
            "type": "system/policy.updated",
            "payload": {"summary": "reversed a decision", "urgent": True},
        }])
        assert len(delivered) == 1
        assert "Re-read run/state/policy.md" in delivered[0].text
        assert "reversed a decision" in delivered[0].text

    def test_bare_topic_also_matched(self):
        delivered = _run_drain_one_batch([{
            "type": "policy.updated",
            "payload": {"summary": "x", "urgent": False},
        }])
        assert delivered == []


class TestMonitorErrorDelivery:
    def test_monitor_error_is_pushed_actively(self):
        from bobi.events.drain import _MONITOR_ERROR_DELIVERED

        _MONITOR_ERROR_DELIVERED.clear()
        delivered = _run_drain_one_batch([{
            "type": "system/monitor.error",
            "payload": {
                "monitor": "policy-curator",
                "flavor": "curator",
                "reason": "spawn-failed",
                "detail": "argv element too large",
            },
        }])
        assert len(delivered) == 1
        assert delivered[0].sender == "monitor-error"
        assert "policy-curator" in delivered[0].text
        assert "argv element too large" in delivered[0].text

    def test_duplicate_monitor_error_is_suppressed(self):
        from bobi.events.drain import _MONITOR_ERROR_DELIVERED

        _MONITOR_ERROR_DELIVERED.clear()
        event = {
            "type": "system/monitor.error",
            "payload": {
                "monitor": "policy-curator",
                "flavor": "curator",
                "reason": "spawn-failed",
                "detail": "argv element too large",
            },
        }
        delivered = _run_drain_one_batch([event])
        assert len(delivered) == 1
        delivered = _run_drain_one_batch([event])
        assert delivered == []
        delivered = _run_drain_one_batch([event])
        assert len(delivered) == 1
        assert "Repeated 3 times" in delivered[0].text
