"""Filtered `metrics_summary` against supervisors that cannot filter."""

import json

import pytest
from click.testing import CliRunner

from bobi.admin_client import run_admin_command
from bobi.cli import main

LEGACY_FOLD = {
    "jobs": {"total": 2, "completed": 2, "failed": 0, "closed": 0},
    "tokens": {"input": 10, "cached_input": 0, "output": 5, "total": 15},
    "cost_usd": 0.1,
    "estimated_cost_usd": 0,
}


class Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def _fake_worker(monkeypatch, version, calls):
    def get(url, **kwargs):
        calls.append(("GET", url))
        return Response(200, {"supervisor": {"version": version}})

    def post(url, **kwargs):
        calls.append(("POST", url, kwargs["json"]))
        return Response(202, {
            "command_id": "c1", "status": "done", "result": {"usage": dict(LEGACY_FOLD)},
        })

    monkeypatch.setattr("bobi.admin_client.http.get", get)
    monkeypatch.setattr("bobi.admin_client.http.post", post)


def _summary(args):
    return run_admin_command(
        base_url="https://events.example.com", token="secret", fleet="f",
        instance="i", alias="metrics_summary", args=args,
    )


@pytest.mark.parametrize("version", ["0.3.0", None, "development"])
def test_filtered_summary_from_old_supervisor_says_it_is_unfiltered(monkeypatch, version):
    calls = []
    _fake_worker(monkeypatch, version, calls)

    result = _summary({"window_seconds": 3600, "model": "haiku"})

    assert [call[0] for call in calls] == ["GET", "POST"]
    assert calls[1][2] == {"command": "usage", "args": {"window_seconds": 3600, "model": "haiku"}}
    usage = result["result"]["usage"]
    assert usage["jobs"]["total"] == 2
    assert usage["fine_grained_unavailable"] == {"code": "supervisor_too_old"}


def test_filtered_summary_from_current_supervisor_is_left_alone(monkeypatch):
    calls = []
    _fake_worker(monkeypatch, "0.4.0", calls)

    result = _summary({"group_by": ["model"]})

    assert [call[0] for call in calls] == ["GET", "POST"]
    assert "fine_grained_unavailable" not in result["result"]["usage"]


def test_unfiltered_summary_skips_the_version_check(monkeypatch):
    calls = []
    _fake_worker(monkeypatch, "0.3.0", calls)

    result = _summary({"window_seconds": 3600})

    assert [call[0] for call in calls] == ["POST"]
    assert "fine_grained_unavailable" not in result["result"]["usage"]


def test_pending_filtered_summary_carries_the_marker_on_the_envelope(monkeypatch):
    calls = []
    _fake_worker(monkeypatch, "0.3.0", calls)
    monkeypatch.setattr("bobi.admin_client.http.post", lambda url, **kwargs: Response(
        202, {"command_id": "c1", "status": "pending"}))

    result = _summary({"session_id": "s1"})

    assert result["fine_grained_unavailable"] == {"code": "supervisor_too_old"}


def test_cli_prints_the_marker_for_a_filtered_summary(monkeypatch):
    calls = []
    _fake_worker(monkeypatch, "0.3.0", calls)

    result = CliRunner().invoke(main, [
        "admin", "metrics_summary", "--url", "https://events.example.com",
        "--fleet", "f", "--instance", "i", "--token", "secret",
        "--args", json.dumps({"model": "haiku"}), "--json",
    ])

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["result"]["usage"]["fine_grained_unavailable"] == {"code": "supervisor_too_old"}
