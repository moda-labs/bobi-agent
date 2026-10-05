from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner

from bobi.admin_client import AdminClientError, run_admin_command
from bobi.cli import main


class Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def test_client_maps_alias_and_polls_terminal_result(monkeypatch):
    calls = []
    monkeypatch.setattr("bobi.admin_client.http.post", lambda url, **kwargs: (
        calls.append(("POST", url, kwargs)) or Response(202, {"command_id": "c1", "status": "pending"})
    ))

    def get(url, **kwargs):
        calls.append(("GET", url, kwargs))
        if url.endswith("/fleet/instances/my%20fleet/agent%2Fone"):
            return Response(200, {"supervisor": {"version": "0.4.0"}})
        return Response(200, {
            "command_id": "c1", "status": "done", "result": {"usage_turn": {"turn": {"turn_id": "t1"}}},
        })

    monkeypatch.setattr("bobi.admin_client.http.get", get)

    result = run_admin_command(
        base_url="https://events.example.com/",
        token="secret",
        fleet="my fleet",
        instance="agent/one",
        alias="metrics_turn",
        args={"turn_id": "t1"},
        wait=True,
    )

    assert result["status"] == "done"
    assert calls[0][0] == "GET"
    assert calls[0][1].endswith("/fleet/instances/my%20fleet/agent%2Fone")
    assert calls[1][1].endswith("/fleet/instances/my%20fleet/agent%2Fone/commands")
    assert calls[1][2]["json"] == {"command": "usage_turn", "args": {"turn_id": "t1"}}
    assert calls[1][2]["headers"] == {"Authorization": "Bearer secret"}


@pytest.mark.parametrize("version", ["0.3.9", None, "development"])
def test_drilldown_rejects_unsupported_supervisor_without_post(monkeypatch, version):
    calls = []
    monkeypatch.setattr("bobi.admin_client.http.get", lambda url, **kwargs: (
        calls.append(("GET", url, kwargs)) or Response(200, {"supervisor": {"version": version}})
    ))
    monkeypatch.setattr("bobi.admin_client.http.post", lambda *args, **kwargs: (
        calls.append(("POST", args, kwargs)) or pytest.fail("unsupported drill-down must not be posted")
    ))

    with pytest.raises(AdminClientError, match=r"0\.4\.0 or newer is required"):
        run_admin_command(
            base_url="https://events.example.com",
            token="secret",
            fleet="f",
            instance="i",
            alias="metrics_turn",
            args={"turn_id": "t1"},
        )

    assert [call[0] for call in calls] == ["GET"]


def test_summary_remains_available_without_version_preflight(monkeypatch):
    calls = []
    monkeypatch.setattr("bobi.admin_client.http.get", lambda *args, **kwargs: (
        calls.append(("GET", args, kwargs)) or pytest.fail("summary must not preflight")
    ))
    monkeypatch.setattr("bobi.admin_client.http.post", lambda url, **kwargs: (
        calls.append(("POST", url, kwargs)) or Response(202, {"command_id": "c1", "status": "pending"})
    ))

    result = run_admin_command(
        base_url="https://events.example.com",
        token="secret",
        fleet="f",
        instance="i",
        alias="metrics_summary",
        args={},
    )

    assert result["command_id"] == "c1"
    assert [call[0] for call in calls] == ["POST"]


def test_client_wraps_transport_failure(monkeypatch):
    monkeypatch.setattr(
        "bobi.admin_client.http.post",
        lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")),
    )

    with pytest.raises(AdminClientError, match="admin command failed: offline"):
        run_admin_command(
            base_url="https://events.example.com",
            token="secret",
            fleet="f",
            instance="i",
            alias="metrics_summary",
            args={},
        )


def test_client_rejects_non_object_response(monkeypatch):
    monkeypatch.setattr(
        "bobi.admin_client.http.post",
        lambda *args, **kwargs: Response(202, []),
    )

    with pytest.raises(AdminClientError, match="non-object"):
        run_admin_command(
            base_url="https://events.example.com",
            token="secret",
            fleet="f",
            instance="i",
            alias="metrics_summary",
            args={},
        )


def test_wait_timeout_is_total_deadline_and_returns_pending(monkeypatch):
    clock = {"now": 10.0}
    request_timeouts = []
    monkeypatch.setattr("bobi.admin_client.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "bobi.admin_client.time.sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    monkeypatch.setattr(
        "bobi.admin_client.http.post",
        lambda *args, **kwargs: Response(202, {"command_id": "c1", "status": "pending"}),
    )

    def get(_url, **kwargs):
        request_timeouts.append(kwargs["timeout"])
        clock["now"] += 0.4
        return Response(200, {"command_id": "c1", "status": "pending"})

    monkeypatch.setattr("bobi.admin_client.http.get", get)

    result = run_admin_command(
        base_url="https://events.example.com",
        token="secret",
        fleet="f",
        instance="i",
        alias="metrics_summary",
        args={},
        wait=True,
        timeout=1.0,
        poll_interval=0.25,
    )

    assert result["status"] == "pending"
    assert clock["now"] <= 11.25
    assert request_timeouts
    assert all(0 < request_timeout <= 1.0 for request_timeout in request_timeouts)

def test_wait_timeout_includes_drilldown_preflight_and_submission(monkeypatch):
    clock = {"now": 10.0}
    request_timeouts = []
    monkeypatch.setattr("bobi.admin_client.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "bobi.admin_client.time.sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    def get(url, **kwargs):
        request_timeouts.append(("GET", kwargs["timeout"]))
        if url.endswith("/fleet/instances/f/i"):
            clock["now"] += 0.6
            return Response(200, {"supervisor": {"version": "0.4.0"}})
        clock["now"] += 0.1
        return Response(200, {"command_id": "c1", "status": "pending"})

    def post(_url, **kwargs):
        request_timeouts.append(("POST", kwargs["timeout"]))
        clock["now"] += 0.2
        return Response(202, {"command_id": "c1", "status": "pending"})

    monkeypatch.setattr("bobi.admin_client.http.get", get)
    monkeypatch.setattr("bobi.admin_client.http.post", post)

    result = run_admin_command(
        base_url="https://events.example.com",
        token="secret",
        fleet="f",
        instance="i",
        alias="metrics_turn",
        args={"turn_id": "t1"},
        wait=True,
        timeout=1.0,
        poll_interval=0.25,
    )

    assert result["status"] == "pending"
    assert clock["now"] == pytest.approx(11.0)
    assert request_timeouts[0] == ("GET", pytest.approx(1.0))
    assert request_timeouts[1] == ("POST", pytest.approx(0.4))
    assert request_timeouts[2] == ("GET", pytest.approx(0.2))

def test_timeout_exhausted_by_preflight_does_not_submit(monkeypatch):
    clock = {"now": 10.0}
    monkeypatch.setattr("bobi.admin_client.time.monotonic", lambda: clock["now"])

    def get(_url, **_kwargs):
        clock["now"] += 1.0
        return Response(200, {"supervisor": {"version": "0.4.0"}})

    monkeypatch.setattr("bobi.admin_client.http.get", get)
    monkeypatch.setattr(
        "bobi.admin_client.http.post",
        lambda *_args, **_kwargs: pytest.fail("expired command must not be submitted"),
    )

    with pytest.raises(AdminClientError, match="timed out before command submission"):
        run_admin_command(
            base_url="https://events.example.com",
            token="secret",
            fleet="f",
            instance="i",
            alias="metrics_turn",
            args={"turn_id": "t1"},
            timeout=1.0,
        )


def test_cli_rejects_non_object_args_without_network():
    result = CliRunner().invoke(main, [
        "admin", "metrics_turn", "--url", "https://events.example.com",
        "--fleet", "f", "--instance", "i", "--token", "secret",
        "--args", "[]", "--json",
    ])
    assert result.exit_code == 2
    assert "JSON object" in result.output


def test_cli_prints_wire_envelope(monkeypatch):
    monkeypatch.setattr("bobi.admin_client.run_admin_command", lambda **kwargs: {
        "command_id": "c1", "status": "done", "result": {"usage_turn": {}},
    })
    result = CliRunner().invoke(main, [
        "admin", "metrics_turn", "--url", "https://events.example.com",
        "--fleet", "f", "--instance", "i", "--token", "secret",
        "--args", '{"turn_id":"t1"}', "--wait", "--json",
    ])
    assert result.exit_code == 0, result.output
    assert '"status": "done"' in result.output
