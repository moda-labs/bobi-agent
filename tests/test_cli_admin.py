"""`bobi admin` exit codes: a script can tell success, a failed command, and
a wait that ran out of time apart without parsing the printed envelope."""

import json

import pytest
from click.testing import CliRunner

from bobi.cli import main

_BASE_ARGS = [
    "admin", "metrics_summary",
    "--url", "http://127.0.0.1:8787",
    "--fleet", "f", "--instance", "i", "--token", "secret",
]


def _invoke(monkeypatch, view, *extra):
    calls = []

    def fake_run_admin_command(**kwargs):
        calls.append(kwargs)
        return view

    monkeypatch.setattr("bobi.admin_client.run_admin_command",
                        fake_run_admin_command)
    result = CliRunner().invoke(main, [*_BASE_ARGS, *extra])
    return result, calls


def test_done_exits_zero(monkeypatch):
    view = {"command_id": "c1", "status": "done", "result": {"ok": True}}
    result, _ = _invoke(monkeypatch, view, "--wait", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == view


def test_error_exits_one_and_still_prints_the_envelope(monkeypatch):
    view = {"command_id": "c1", "status": "error",
            "result": {"code": "metrics_not_ready"}}
    result, _ = _invoke(monkeypatch, view, "--wait", "--json")
    assert result.exit_code == 1
    assert json.loads(result.output) == view


def test_error_without_wait_exits_one(monkeypatch):
    view = {"command_id": "c1", "status": "error"}
    result, _ = _invoke(monkeypatch, view)
    assert result.exit_code == 1


def test_wait_that_ends_pending_exits_two(monkeypatch):
    view = {"command_id": "c1", "status": "pending"}
    result, calls = _invoke(monkeypatch, view, "--wait", "--timeout", "0.1",
                            "--json")
    assert calls[0]["wait"] is True
    assert result.exit_code == 2
    assert json.loads(result.output) == view


def test_pending_without_wait_exits_zero(monkeypatch):
    # Submit-only is expected to come back unresolved.
    view = {"command_id": "c1", "status": "pending"}
    result, _ = _invoke(monkeypatch, view)
    assert result.exit_code == 0, result.output


def test_client_failure_exits_one(monkeypatch):
    from bobi.admin_client import AdminClientError

    def fail(**kwargs):
        raise AdminClientError("admin command rejected (401): nope")

    monkeypatch.setattr("bobi.admin_client.run_admin_command", fail)
    result = CliRunner().invoke(main, _BASE_ARGS)
    assert result.exit_code == 1
    assert "admin command rejected" in result.output


def test_help_states_the_exit_codes():
    result = CliRunner().invoke(main, ["admin", "--help"])
    assert result.exit_code == 0
    text = " ".join(result.output.split())
    assert "Exit codes: 0 success" in text
    assert "1 the command failed" in text
    assert "2 --wait timed out" in text


def _fake_unresolved_worker(monkeypatch, poll_failure):
    """A command that never resolves: every poll reports `pending` after a
    fixed latency, until the poll that `poll_failure` decides to fail."""
    import httpx

    clock = {"now": 100.0}
    monkeypatch.setattr("bobi.admin_client.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "bobi.admin_client.time.sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    request = httpx.Request("GET", "http://127.0.0.1:8787")

    class Response:
        text = ""

        def __init__(self, status_code):
            self.status_code = status_code

        def json(self):
            return {"command_id": "c1", "status": "pending"}

    def post(_url, **kwargs):
        return Response(202)

    def get(_url, **kwargs):
        failure = poll_failure(clock["now"], kwargs["timeout"], request)
        if failure is not None:
            raise failure
        clock["now"] += 0.1
        return Response(200)

    monkeypatch.setattr("bobi.admin_client.http.post", post)
    monkeypatch.setattr("bobi.admin_client.http.get", get)
    return clock


def test_wait_whose_deadline_falls_inside_a_poll_exits_two_with_the_envelope(monkeypatch):
    import httpx

    def time_out_at_the_deadline(now, timeout, request):
        # 0.1 s polls 0.25 s apart start at 0, 0.35, 0.70 and 1.05 s, so a
        # 1.1 s deadline lands inside the fourth, where httpx gives up.
        if timeout < 0.1:
            clock["now"] = now + timeout
            return httpx.ReadTimeout("timed out", request=request)
        return None

    clock = _fake_unresolved_worker(monkeypatch, time_out_at_the_deadline)
    result = CliRunner().invoke(main, [*_BASE_ARGS, "--wait", "--timeout", "1.1", "--json"])

    assert result.exit_code == 2, result.output
    assert json.loads(result.output) == {"command_id": "c1", "status": "pending"}
    assert clock["now"] == pytest.approx(101.1)


def test_wait_whose_poll_fails_before_the_deadline_exits_one(monkeypatch):
    import httpx

    def connection_refused(_now, _timeout, request):
        return httpx.ConnectError("connection refused", request=request)

    _fake_unresolved_worker(monkeypatch, connection_refused)
    result = CliRunner().invoke(main, [*_BASE_ARGS, "--wait", "--timeout", "1.0", "--json"])

    assert result.exit_code == 1
    assert "admin poll failed: connection refused" in result.output
