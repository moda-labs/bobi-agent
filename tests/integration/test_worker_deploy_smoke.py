"""Smoke the Cloudflare Worker as DEPLOYED, not as `wrangler dev` runs it.

`ci.yml`'s wrangler job proves the Worker's *code* against `wrangler dev` in
local mode, which needs no Cloudflare account at all. That leaves an entire
class of breakage unproven: the KV binding resolving to a real namespace, the
`v1` `new_sqlite_classes` Durable Object migration applying to a real script,
and account-side provisioning generally. The only real `wrangler deploy`
anywhere used to be moda-agents' production deploy — so production was the
first thing to ever exercise the deploy path.

This module runs against an already-deployed Worker named by
``BOBI_SMOKE_EVENT_SERVER_URL`` and is skipped without it, so it stays inert
in every ordinary suite run. `.github/workflows/worker-deploy-smoke.yml`
deploys the dedicated `ci-smoke` Worker and points this at it.

Two tests, deliberately:

* health proves the deploy that answers is *this* commit's deploy, via the
  release sha stamped into the config at render time — the one field a local
  dev server cannot produce;
* the round-trip proves the bindings, exercising KV (deployment registration
  is KV-backed) and the Durable Object (the WebSocket subscription is a DO
  session). A deploy whose migration did not apply fails here, not in
  production.

The helpers are local rather than imported from ``test_event_server`` on
purpose: that module's helpers hardcode ``http://`` → ``ws://``, which is
wrong for a real deployment served over TLS.
"""

import json
import os
import ssl
import threading
import time
import urllib.request

import pytest
import websocket

pytestmark = pytest.mark.live

SMOKE_URL_ENV = "BOBI_SMOKE_EVENT_SERVER_URL"

#: Every request below sends this explicitly, and it is load-bearing against a
#: DEPLOYED Worker in a way it never was against `wrangler dev`: Cloudflare's
#: managed bot rules answer 403 to the default `Python-urllib/x.y` User-Agent
#: at the edge. Verified 2026-08-01 against the deployed ci-smoke Worker —
#: `Python-urllib/3.13` got 403 while `python-httpx`, `curl` and this string
#: all got 200. Only this module is affected; the shipped client uses httpx.
SMOKE_USER_AGENT = "bobi-ci-smoke/1"

requires_deployment = pytest.mark.skipif(
    not os.environ.get(SMOKE_URL_ENV),
    reason=f"deployed-Worker smoke needs {SMOKE_URL_ENV} (set by worker-deploy-smoke.yml)",
)


@pytest.fixture
def smoke_url() -> str:
    """The deployed Worker's base URL.

    Named `smoke_url`, not `base_url`: pytest-base-url ships a session-scoped
    `base_url` fixture, and shadowing it with a function-scoped one makes every
    test in this module error out at setup with a ScopeMismatch.
    """
    return os.environ[SMOKE_URL_ENV].rstrip("/")


def _get_json(url: str, timeout: float = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": SMOKE_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        assert resp.status == 200, f"{url} -> HTTP {resp.status}"
        return json.loads(resp.read())


def _register(base_url: str, name: str, subscriptions: list[str]) -> dict:
    """MINT a bubble (unsigned registration) and return the server response."""
    from bobi.events.signing import serialize_body

    body = serialize_body({"name": name, "subscriptions": subscriptions})
    req = urllib.request.Request(
        f"{base_url}/deployments",
        data=body.encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": SMOKE_USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _publish(base_url: str, topic: str, payload: dict, bubble_id: str, bubble_key: str) -> None:
    """Publish to /events/{topic}, signed with the bubble key."""
    from bobi.events.signing import serialize_body, sign_headers

    body = serialize_body(payload)
    path = f"/events/{topic}"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": SMOKE_USER_AGENT,
    }
    headers.update(sign_headers(bubble_id, bubble_key, "POST", path, body))
    req = urllib.request.Request(base_url + path, data=body.encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.status == 200, f"publish -> HTTP {resp.status}"


def _ws_url(base_url: str, deployment_id: str) -> str:
    scheme = "wss" if base_url.startswith("https://") else "ws"
    host = base_url.split("://", 1)[1]
    return f"{scheme}://{host}/deployments/{deployment_id}/subscribe?last_seen=0"


def _subscribe_then_publish(
    base_url: str,
    deployment_id: str,
    api_key: str,
    publish_fn,
    timeout: float = 30,
) -> list[dict]:
    """Open the subscription FIRST, then publish, then drain what arrived.

    Ordering matters: subscribing after the publish would pass on replay alone
    and would not prove live fan-out through the Durable Object.
    """
    events: list[dict] = []
    ready = threading.Event()
    failure: list[BaseException] = []

    def _pump():
        try:
            ws = websocket.create_connection(
                _ws_url(base_url, deployment_id),
                header=[
                    f"Authorization: Bearer {api_key}",
                    f"User-Agent: {SMOKE_USER_AGENT}",
                ],
                timeout=timeout,
                sslopt={"cert_reqs": ssl.CERT_REQUIRED},
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test below
            failure.append(exc)
            ready.set()
            return
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                ws.settimeout(max(0.1, deadline - time.monotonic()))
                try:
                    msg = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    break
                except Exception:
                    break
                if msg.get("type") == "connected":
                    ready.set()
                elif msg.get("type") in ("event", "replay"):
                    events.append(msg["data"])
                    break
        finally:
            ws.close()

    thread = threading.Thread(target=_pump, daemon=True)
    thread.start()
    assert ready.wait(timeout=timeout), "WebSocket never reported `connected`"
    if failure:
        raise AssertionError(f"WebSocket subscribe failed: {failure[0]!r}")
    time.sleep(0.5)

    publish_fn()

    thread.join(timeout=timeout)
    return events


@requires_deployment
def test_deployed_worker_health(smoke_url: str):
    """/health answers 200, and the answer is THIS commit's deploy.

    Asserting the release stamp (not merely `status: ok`) is what makes this
    non-vacuous: a stale Worker, or the wrong Worker entirely, answers 200 too.

    The stamp is the discriminator precisely because nothing else is.
    `CF_VERSION_METADATA` looked like the obvious "this is really deployed"
    signal and is not: `wrangler dev` populates `version_id` and
    `version_timestamp` too (verified 2026-08-01 — dev answered a real uuid).
    What dev cannot fake is the release sha, because the shipped
    `wrangler.jsonc` hardcodes `"dev"` and only
    `scripts/render_worker_ci_config.py` replaces it with the commit under
    test.
    """
    data = _get_json(f"{smoke_url}/health")

    assert data["status"] == "ok"
    assert data["auth"] == "hmac"

    expected_sha = os.environ["BOBI_SMOKE_EXPECTED_SHA"]
    assert data["release"]["sha"] == expected_sha, (
        f"deployed Worker reports release sha {data['release']['sha']!r}, "
        f"expected {expected_sha!r} — the URL under test is not this deploy "
        "(a stale Worker, the shipped default config, or a dev server)"
    )
    assert data["release"]["sha"] != "dev", (
        "release sha is the shipped placeholder — the deploy did not use the "
        "rendered CI config"
    )

    # The binding resolved. Weaker than it looks (dev populates it too), but a
    # null here means CF_VERSION_METADATA is unbound on the deployed script.
    assert data["worker"]["version_id"], "no CF_VERSION_METADATA version id"


@requires_deployment
def test_deployed_worker_publish_subscribe_roundtrip(smoke_url: str):
    """A publish reaches a live subscriber through the DEPLOYED Worker.

    Registration is KV-backed and the subscription is a Durable Object
    session, so a green run proves both bindings resolved and the `v1`
    `new_sqlite_classes` migration applied to the deployed script.
    """
    topic = "ci-smoke"
    registration = _register(smoke_url, "ci-smoke-deploy-check", [topic])

    deployment_id = registration["deployment_id"]
    api_key = registration["api_key"]
    bubble_id = registration["bubble_id"]
    bubble_key = registration["bubble_key"]

    marker = f"ci-smoke-{time.time_ns()}"
    events = _subscribe_then_publish(
        smoke_url,
        deployment_id,
        api_key,
        lambda: _publish(smoke_url, topic, {"marker": marker}, bubble_id, bubble_key),
    )

    markers = [
        event.get("payload", {}).get("marker")
        for event in events
        if isinstance(event, dict)
    ]
    assert marker in markers, (
        f"published marker {marker!r} never reached the subscriber; "
        f"received {events!r}"
    )
