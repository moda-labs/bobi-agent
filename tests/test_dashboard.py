"""Tests for the dashboard data layer and API endpoints."""

import json

import pytest
from fastapi.testclient import TestClient

from dashboard.app import app
from dashboard import data


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def events_file(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.setattr(data, "EVENTS_PATH", path)
    return path


@pytest.fixture
def decisions_file(tmp_path, monkeypatch):
    path = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(data, "DECISIONS_PATH", path)
    return path


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
    assert events[0]["type"] == "worker.waiting_input"  # reverse chronological


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
    assert events[0]["type"] == "event.9"  # reverse order

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
    assert decisions[0]["events"] == 1  # most recent first


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
