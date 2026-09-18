"""External proof for the Terraform Worker + Kubernetes sidecar example.

The test process runs on the GitHub runner, outside the kind cluster. Its only
runtime connection is the public Worker URL, so a pass proves the documented
outbound-only topology rather than using kubectl as a control backdoor.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

pytestmark = pytest.mark.live

URL_ENV = "BOBI_SELF_HOST_EVENT_SERVER_URL"
TOKEN_ENV = "BOBI_SELF_HOST_FLEET_OPERATOR_TOKEN"
FLEET_ENV = "BOBI_SELF_HOST_FLEET"
INSTANCE_ENV = "BOBI_SELF_HOST_INSTANCE"
VERSION_ENV = "BOBI_SELF_HOST_VERSION"
USER_AGENT = "bobi-self-host-proof/1"

requires_consumer = pytest.mark.skipif(
    not os.environ.get(URL_ENV),
    reason=f"self-host proof needs {URL_ENV} (set by self-hosted-consumer.yml)",
)


def _request(path: str, *, token: str | None = None, payload: dict | None = None):
    base = os.environ[URL_ENV].rstrip("/")
    headers = {"User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    request = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


def _detail_path() -> str:
    fleet = urllib.parse.quote(os.environ[FLEET_ENV], safe="")
    instance = urllib.parse.quote(os.environ[INSTANCE_ENV], safe="")
    return f"/fleet/instances/{fleet}/{instance}"


def _await_detail(predicate, timeout: float = 180) -> dict:
    token = os.environ[TOKEN_ENV]
    deadline = time.monotonic() + timeout
    last_status = None
    last_body = None
    while time.monotonic() < deadline:
        last_status, last_body = _request(_detail_path(), token=token)
        if last_status == 200 and predicate(last_body):
            return last_body
        time.sleep(2)
    raise AssertionError(
        f"instance detail did not reach the expected state within {timeout:g}s; "
        f"last response was HTTP {last_status}: {last_body!r}"
    )


def _await_command(status_path: str, timeout: float = 60) -> dict:
    token = os.environ[TOKEN_ENV]
    deadline = time.monotonic() + timeout
    last_status = None
    last_body = None
    while time.monotonic() < deadline:
        last_status, last_body = _request(status_path, token=token)
        if last_status == 200 and last_body.get("status") in {"done", "error"}:
            return last_body
        time.sleep(0.5)
    raise AssertionError(
        f"restart command did not resolve within {timeout:g}s; "
        f"last response was HTTP {last_status}: {last_body!r}"
    )


@requires_consumer
def test_self_hosted_fleet_api_fails_closed_without_the_operator_token():
    status, body = _request("/fleet/status", token="wrong-token")

    assert status == 401
    assert body == {"error": "unauthorized"}


@requires_consumer
def test_kubernetes_sidecar_heartbeats_and_restarts_from_outside_the_cluster():
    expected_fleet = os.environ[FLEET_ENV]
    expected_instance = os.environ[INSTANCE_ENV]
    expected_version = os.environ[VERSION_ENV]
    expected_image = f"ghcr.io/moda-labs/bobi:{expected_version}"

    before = _await_detail(
        lambda value: (
            value.get("reachability") == "live"
            and value.get("manager", {}).get("healthy") is True
            and value.get("manager", {}).get("status") in {"running", "idle"}
            and int(value.get("manager", {}).get("pid") or 0) > 0
        )
    )

    deployment = before["deployment"]
    assert deployment["fleet"] == expected_fleet
    assert deployment["instance"] == expected_instance
    assert deployment["platform"] == "k8s"
    assert deployment["machine"].startswith("bobi-self-host-consumer-")
    assert deployment["node"]
    assert before["supervisor"]["version"]
    assert before["versions"]["bobi"] == expected_version
    assert before["versions"]["image"] == expected_image

    old_pid = int(before["manager"]["pid"])
    status, issued = _request(
        _detail_path() + "/commands",
        token=os.environ[TOKEN_ENV],
        payload={"command": "restart", "args": {}},
    )
    assert status == 202, issued
    assert issued["status"] == "pending"
    assert int(issued["delivered_to"]) >= 1

    command = _await_command(issued["status_url"])
    assert command["status"] == "done", command
    assert command["result"] == {"accepted": True, "action": "restart"}

    after = _await_detail(
        lambda value: (
            int(value.get("manager", {}).get("pid") or 0) > 0
            and int(value["manager"]["pid"]) != old_pid
            and value["manager"].get("last_restart_reason") == "operator"
            and value["manager"].get("healthy") is True
        )
    )
    assert after["manager"]["pid"] != old_pid
