"""Drill-downs (U5): the debugging transcript, and Details for runs without one.

Two readers. `read_transcript_detail` turns a Claude transcript into
timestamped lines with tool calls — the view `/messages` deliberately does not
produce — and `build_details` answers for the rows that have no transcript at
all: a $0 cached monitor tick, a firing that died before its agent started.
"""

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from bobi import chat_history, paths
from bobi.chat_history import (
    KIND_MESSAGE,
    KIND_TOOL,
    KIND_TOOL_RESULT,
    TOOL_RESULT_PREVIEW,
    read_transcript_detail,
    read_transcript_messages,
)
from bobi.monitors import run_records
from bobi.monitors.run_records import FAILED, NOTIFIED, QUIET, MonitorRun
from bobi.webapp import server
from bobi.webapp.details import UnknownRun, build_details

TOKEN = "details-token-123"
AT = "2026-08-01T12:00:00.000Z"


def _line(kind, content, *, at=AT):
    return json.dumps({"type": kind, "timestamp": at,
                       "message": {"content": content}})


def _seed_transcript(tmp_path, monkeypatch, lines):
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(chat_history, "_transcript_path", lambda sid: path)
    return path


def _seed_monitor_run(monitor="inbox-watch", **kw):
    base = dict(run_id=run_records.new_run_id(monitor), monitor=monitor,
                started_at="2026-08-01T12:00:00+00:00",
                ended_at="2026-08-01T12:00:04+00:00", outcome=QUIET,
                flavor="check:script_cache")
    base.update(kw)
    run = MonitorRun(**base)
    run_records.record(run)
    return run


# === The debugging transcript ===

class TestTranscriptDetail:
    def test_messages_carry_their_timestamps(self, tmp_path, monkeypatch):
        _seed_transcript(tmp_path, monkeypatch, [
            _line("user", "check the inbox"),
            _line("assistant", [{"type": "text", "text": "on it"}],
                  at="2026-08-01T12:00:05.000Z"),
        ])
        entries = read_transcript_detail("sid")
        assert [(e["role"], e["text"], e["at"]) for e in entries] == [
            ("user", "check the inbox", AT),
            ("agent", "on it", "2026-08-01T12:00:05.000Z"),
        ]
        assert {e["kind"] for e in entries} == {KIND_MESSAGE}

    def test_tool_calls_become_their_own_lines(self, tmp_path, monkeypatch):
        # The thing /messages throws away: what the agent DID between saying
        # things.
        _seed_transcript(tmp_path, monkeypatch, [
            _line("assistant", [
                {"type": "text", "text": "looking"},
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "ls -la /tmp"}},
            ]),
        ])
        entries = read_transcript_detail("sid")
        assert [e["kind"] for e in entries] == [KIND_MESSAGE, KIND_TOOL]
        tool = entries[1]
        assert tool["tool"] == "Bash"
        assert tool["text"] == "ls -la /tmp"
        assert tool["role"] == "agent"
        assert tool["at"] == AT

    def test_tool_results_are_previewed_and_flagged(self, tmp_path, monkeypatch):
        _seed_transcript(tmp_path, monkeypatch, [
            _line("user", [{"type": "tool_result",
                            "content": [{"type": "text", "text": "x" * 900}]}]),
        ])
        [entry] = read_transcript_detail("sid")
        assert entry["kind"] == KIND_TOOL_RESULT
        assert len(entry["text"]) == TOOL_RESULT_PREVIEW
        assert entry["truncated"] is True
        assert entry["is_error"] is False

    def test_error_results_are_marked(self, tmp_path, monkeypatch):
        _seed_transcript(tmp_path, monkeypatch, [
            _line("user", [{"type": "tool_result", "is_error": True,
                            "content": [{"type": "text", "text": "boom"}]}]),
        ])
        [entry] = read_transcript_detail("sid")
        assert entry["is_error"] is True
        assert entry["truncated"] is False

    def test_tool_input_summary_prefers_the_telling_key(self, tmp_path,
                                                        monkeypatch):
        _seed_transcript(tmp_path, monkeypatch, [
            _line("assistant", [
                {"type": "tool_use", "name": "Read",
                 "input": {"file_path": "/etc/hosts", "limit": 20}},
                {"type": "tool_use", "name": "Odd",
                 "input": {"alpha": 1, "beta": 2}},
            ]),
        ])
        entries = read_transcript_detail("sid")
        assert entries[0]["text"] == "/etc/hosts"
        # No telling key: say what shape the call had rather than nothing.
        assert entries[1]["text"] == "alpha, beta"

    def test_every_entry_carries_every_key(self, tmp_path, monkeypatch):
        _seed_transcript(tmp_path, monkeypatch, [
            _line("user", "hi"),
            _line("assistant", [
                {"type": "text", "text": "sure"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                {"type": "tool_result", "content": [{"type": "text",
                                                     "text": "ok"}]},
            ]),
        ])
        expected = {"kind", "role", "text", "at", "tool", "truncated",
                    "is_error"}
        for entry in read_transcript_detail("sid"):
            assert set(entry) == expected

    def test_the_tail_is_what_survives_the_cap(self, tmp_path, monkeypatch):
        _seed_transcript(tmp_path, monkeypatch,
                         [_line("user", f"m{i}") for i in range(10)])
        entries = read_transcript_detail("sid", limit=3)
        assert [e["text"] for e in entries] == ["m7", "m8", "m9"]

    def test_chat_view_is_unchanged_by_tool_lines(self, tmp_path, monkeypatch):
        # /messages must keep its shape: prose only, no tool noise.
        _seed_transcript(tmp_path, monkeypatch, [
            _line("assistant", [
                {"type": "text", "text": "on it"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            ]),
        ])
        assert read_transcript_messages("sid") == [
            {"role": "agent", "text": "on it"}]

    def test_missing_transcript_is_empty_not_an_error(self, tmp_path,
                                                      monkeypatch):
        monkeypatch.setattr(chat_history, "_transcript_path", lambda sid: None)
        assert read_transcript_detail("sid", brain="claude") == []

    def test_unparseable_lines_are_skipped(self, tmp_path, monkeypatch):
        _seed_transcript(tmp_path, monkeypatch,
                         ["{ not json", _line("user", "still here")])
        assert [e["text"] for e in read_transcript_detail("sid")] == \
            ["still here"]

    def test_entries_without_a_timestamp_report_empty_not_now(
            self, tmp_path, monkeypatch):
        # A missing timestamp is missing. Substituting the read time would
        # date every old line to whenever the page was opened.
        path = tmp_path / "t.jsonl"
        path.write_text(json.dumps(
            {"type": "user", "message": {"content": "no clock"}}) + "\n")
        monkeypatch.setattr(chat_history, "_transcript_path", lambda sid: path)
        [entry] = read_transcript_detail("sid")
        assert entry["at"] == ""


# === Details for runs with no transcript ===

class TestRunDetails:
    def test_cached_tick_gets_its_record_and_definition(self, bobi_install):
        (paths.package_dir(bobi_install.repo_path) / "monitors.yaml").write_text(
            yaml.dump({"monitors": [{
                "name": "inbox-watch", "interval": "15m",
                "description": "watch the inbox", "command": "true",
                "event": "monitor/inbox", "id_field": "id",
            }]}))
        run = _seed_monitor_run(script_cache_mode="cached")
        body = build_details(bobi_install.repo_path, run.run_id)
        assert body["kind"] == "monitor"
        assert body["run"]["run_id"] == run.run_id
        assert body["run"]["script_cache_mode"] == "cached"
        # The record says what happened; the definition says what it was
        # asked to do. Debugging a monitor needs both.
        assert body["definition"]["description"] == "watch the inbox"
        assert body["definition"]["interval"] == "15m"
        assert body["session_id"] == ""       # nothing spawned, nothing to open

    def test_failed_firing_carries_its_reason(self, bobi_install):
        run = _seed_monitor_run(outcome=FAILED, flavor="description",
                                reason="spawn-failed: claude CLI exited")
        body = build_details(bobi_install.repo_path, run.run_id)
        assert body["run"]["outcome"] == FAILED
        assert body["run"]["reason"] == "spawn-failed: claude CLI exited"

    def test_a_firing_that_did_spawn_points_at_its_transcript(self,
                                                              bobi_install):
        run = _seed_monitor_run(outcome=NOTIFIED, session_ref="check-agent-1")
        body = build_details(bobi_install.repo_path, run.run_id)
        assert body["session_id"] == "check-agent-1"

    def test_definition_survives_the_monitor_being_gone(self, bobi_install):
        # A record outlives its monitor. Losing the definition costs half the
        # slab, never the response.
        run = _seed_monitor_run(monitor="deleted-monitor")
        body = build_details(bobi_install.repo_path, run.run_id)
        assert body["definition"] == {}
        assert body["run"]["monitor"] == "deleted-monitor"

    def test_definition_omits_the_command(self, bobi_install):
        # A command can carry interpolated credentials; the slab is a
        # debugging view, not a config dump.
        (paths.package_dir(bobi_install.repo_path) / "monitors.yaml").write_text(
            yaml.dump({"monitors": [{
                "name": "inbox-watch", "interval": "15m",
                "description": "watch", "command": "curl -H 'Auth: sk-secret'",
            }]}))
        run = _seed_monitor_run()
        body = build_details(bobi_install.repo_path, run.run_id)
        assert "command" not in body["definition"]
        assert "sk-secret" not in json.dumps(body)

    def test_clock_scheduled_monitor_omits_the_unused_interval(
            self, bobi_install):
        # The registry fills `interval` with its default even for an `at:`
        # monitor, which never uses it. Printed alongside `at`, the slab
        # contradicts itself AND the run row, whose origin says "at 09:00".
        (paths.package_dir(bobi_install.repo_path) / "monitors.yaml").write_text(
            yaml.dump({"monitors": [{
                "name": "inbox-watch", "at": ["09:00"],
                "description": "watch", "command": "true",
            }]}))
        run = _seed_monitor_run()
        body = build_details(bobi_install.repo_path, run.run_id)
        assert "interval" not in body["definition"]
        assert body["definition"]["at"] == ["09:00"]

    def test_unknown_run_id(self, bobi_install):
        with pytest.raises(UnknownRun):
            build_details(bobi_install.repo_path, "no-such-run")


# === The endpoints ===

def _client():
    app = server.build_app(token=TOKEN)
    c = TestClient(app, base_url="http://127.0.0.1")
    c.headers.update({"x-bobi-webui-token": TOKEN})
    return c


class TestEndpoints:
    def test_transcript_shape(self, bobi_install, tmp_path, monkeypatch):
        _seed_transcript(tmp_path, monkeypatch, [
            _line("assistant", [
                {"type": "text", "text": "on it"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            ]),
        ])
        d = bobi_install.sessions_dir / "worker"
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps({
            "name": "worker", "session_id": "sid", "status": "completed",
            "started_at": 100.0, "terminal_at": 160.0, "total_cost_usd": 0.25,
            "model_usage": {"anthropic:claude-sonnet-4-20250514": {
                "input_tokens": 1000, "output_tokens": 500}},
        }))
        r = _client().get(
            f"/api/agents/{bobi_install.agent_name}/subagents/worker/transcript")
        assert r.status_code == 200
        body = r.json()
        assert body["session"] == "worker"
        assert [e["kind"] for e in body["entries"]] == [KIND_MESSAGE, KIND_TOOL]
        assert body["usage"] == {"started_at": 100.0, "ended_at": 160.0,
                                 "tokens": 1500, "cost_usd": 0.25,
                                 "status": "completed"}

    def test_transcript_for_an_unknown_session_is_empty(self, bobi_install,
                                                        monkeypatch):
        monkeypatch.setattr(chat_history, "_transcript_path", lambda sid: None)
        r = _client().get(
            f"/api/agents/{bobi_install.agent_name}/subagents/ghost/transcript")
        assert r.status_code == 200
        assert r.json()["entries"] == []
        assert r.json()["usage"]["status"] == ""

    def test_transcript_rejects_a_traversing_session_name(self, bobi_install):
        r = _client().get(
            f"/api/agents/{bobi_install.agent_name}/subagents/..%2Fetc"
            "/transcript")
        assert r.status_code == 404

    def test_details_endpoint(self, bobi_install):
        run = _seed_monitor_run(script_cache_mode="cached")
        r = _client().get(
            f"/api/agents/{bobi_install.agent_name}/runs/{run.run_id}/details")
        assert r.status_code == 200
        assert r.json()["run"]["run_id"] == run.run_id

    def test_details_unknown_run_404s(self, bobi_install):
        r = _client().get(
            f"/api/agents/{bobi_install.agent_name}/runs/nope/details")
        assert r.status_code == 404
        assert r.json() == {"error": "unknown run"}

    def test_details_unknown_agent_404s(self, bobi_install):
        assert _client().get(
            "/api/agents/nope/runs/whatever/details").status_code == 404
