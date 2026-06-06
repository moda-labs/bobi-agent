"""Tests for the dashboard data layer and API endpoints."""

import json

import pytest
from fastapi.testclient import TestClient

from dashboard.app import app
from dashboard import data
from modastack import sdk


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _project_root(tmp_path, monkeypatch):
    """Point all state to a temp dir for test isolation."""
    state_dir = tmp_path / ".modastack" / "state"
    state_dir.mkdir(parents=True)
    sessions_dir = tmp_path / ".modastack" / "sessions"
    sessions_dir.mkdir(parents=True)
    monkeypatch.setattr(sdk, "_project_root", tmp_path)


@pytest.fixture
def events_file(tmp_path):
    return tmp_path / ".modastack" / "state" / "events.jsonl"


@pytest.fixture
def decisions_file(tmp_path):
    return tmp_path / ".modastack" / "state" / "decisions.jsonl"


def _write_events(path, events):
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def test_read_events_empty(events_file):
    events, total = data.read_events()
    assert events == []
    assert total == 0


def test_read_events_with_data(events_file):
    sample = [
        {"type": "slack.dm", "source": "slack", "timestamp": "2025-01-15T14:30:00",
         "data": {"text": "hello", "from": "zach"}},
        {"type": "task.opened", "source": "github-issues", "timestamp": "2025-01-15T14:31:00",
         "data": {"title": "Fix bug", "issue_id": "#42"}},
        {"type": "worker.waiting_input", "source": "worker", "timestamp": "2025-01-15T14:32:00",
         "data": {"issue_id": "ENG-10", "session_state": "waiting_input"}},
    ]
    _write_events(events_file, sample)

    events, total = data.read_events()
    assert total == 3
    assert len(events) == 3
    assert events[0]["type"] == "worker.waiting_input"


def test_read_events_filtering(events_file):
    sample = [
        {"type": "slack.dm", "source": "slack", "timestamp": "T1", "data": {}},
        {"type": "task.opened", "source": "github-issues", "timestamp": "T2", "data": {}},
        {"type": "slack.mention", "source": "slack", "timestamp": "T3", "data": {}},
    ]
    _write_events(events_file, sample)

    events, total = data.read_events(source="slack")
    assert total == 2
    assert all(e["source"] == "slack" for e in events)

    events, total = data.read_events(type_filter="task")
    assert total == 1
    assert events[0]["type"] == "task.opened"


def test_read_events_pagination(events_file):
    sample = [{"type": f"event.{i}", "source": "test", "timestamp": f"T{i}", "data": {}}
              for i in range(10)]
    _write_events(events_file, sample)

    events, total = data.read_events(limit=3, offset=0)
    assert total == 10
    assert len(events) == 3
    assert events[0]["type"] == "event.9"

    events, total = data.read_events(limit=3, offset=3)
    assert len(events) == 3
    assert events[0]["type"] == "event.6"


def test_read_decisions_empty(decisions_file):
    decisions = data.read_decisions()
    assert decisions == []


def test_read_decisions_with_data(decisions_file):
    sample = [
        {"timestamp": "2025-01-15 14:28:00", "events": 2, "event_types": ["slack.dm"]},
        {"timestamp": "2025-01-15 14:30:00", "events": 1, "event_types": ["task.opened"]},
    ]
    _write_events(decisions_file, sample)

    decisions = data.read_decisions(limit=5)
    assert len(decisions) == 2
    assert decisions[0]["events"] == 1


def test_status_endpoint(client, monkeypatch):
    monkeypatch.setattr(data, "get_manager_status", lambda: {"alive": False, "state": "exited"})
    monkeypatch.setattr(data, "get_sessions", lambda: [])

    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["manager"]["alive"] is False
    assert body["engineers"] == []


def test_events_endpoint(client, events_file):
    sample = [
        {"type": "slack.dm", "source": "slack", "timestamp": "T1", "data": {"text": "hi"}},
    ]
    _write_events(events_file, sample)

    resp = client.get("/api/events?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["events"][0]["type"] == "slack.dm"


def test_events_endpoint_pagination(client, events_file):
    sample = [{"type": f"e.{i}", "source": "test", "timestamp": f"T{i}", "data": {}}
              for i in range(20)]
    _write_events(events_file, sample)

    resp = client.get("/api/events?limit=5&offset=0")
    body = resp.json()
    assert body["total"] == 20
    assert len(body["events"]) == 5

    resp2 = client.get("/api/events?limit=5&offset=5")
    body2 = resp2.json()
    assert len(body2["events"]) == 5
    assert body["events"][0] != body2["events"][0]


def test_decisions_endpoint(client, decisions_file):
    sample = [
        {"timestamp": "T1", "events": 3, "event_types": ["a", "b"]},
    ]
    _write_events(decisions_file, sample)

    resp = client.get("/api/decisions")
    assert resp.status_code == 200
    assert len(resp.json()["decisions"]) == 1


def test_index_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "modastack" in resp.text


def test_read_modastack_log_empty(tmp_path):
    assert data.read_modastack_log() == []


def test_read_modastack_log_tail(tmp_path):
    path = tmp_path / ".modastack" / "state" / "manager.log"
    path.write_text("\n".join(f"line {i}" for i in range(10)) + "\n")
    lines = data.read_modastack_log(limit=3)
    assert lines == ["line 7", "line 8", "line 9"]


def test_tail_lines_spans_multiple_blocks(tmp_path):
    path = tmp_path / "big.log"
    path.write_text("\n".join(f"line {i:05d}" for i in range(20000)) + "\n")
    lines = data._tail_lines(path, 4)
    assert lines == ["line 19996", "line 19997", "line 19998", "line 19999"]


def test_logs_endpoint(client, tmp_path):
    path = tmp_path / ".modastack" / "state" / "manager.log"
    path.write_text("hello\nworld\n")
    resp = client.get("/api/logs?limit=10")
    assert resp.status_code == 200
    assert resp.json()["lines"] == ["hello", "world"]


def test_activity_snippet(tmp_path):
    session_dir = tmp_path / ".modastack" / "sessions" / "eng-7-implement"
    session_dir.mkdir(parents=True)
    (session_dir / "log.jsonl").write_text(
        json.dumps({"event": "UserPromptSubmit", "text": "go"}) + "\n"
        + json.dumps({"event": "response", "text": "Working on it\nstep two"}) + "\n"
        + json.dumps({"event": "Stop"}) + "\n"
    )
    assert data._activity_snippet("eng-7-implement") == "Working on it step two"
    assert data._activity_snippet("nonexistent") == ""


def test_sources_endpoint(client, events_file):
    sample = [
        {"type": "a", "source": "slack", "timestamp": "T1", "data": {}},
        {"type": "b", "source": "github", "timestamp": "T2", "data": {}},
        {"type": "c", "source": "slack", "timestamp": "T3", "data": {}},
    ]
    _write_events(events_file, sample)

    resp = client.get("/api/sources")
    assert resp.status_code == 200
    assert set(resp.json()["sources"]) == {"slack", "github"}
