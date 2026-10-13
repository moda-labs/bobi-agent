"""`bobi admin` exit codes: a script can tell success, a failed command, and
a wait that ran out of time apart without parsing the printed envelope."""

import json

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
