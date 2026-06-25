"""Tests for the #488 client side — resource-grant authorization.

`authorize_resources()` verifies the upstream credential for each global
github:/linear: subscription with the event server (POST /resources/authorize)
BEFORE the deployment registers, so the server's grant check passes. A topic
whose credential is missing or rejected is dropped (never blocking startup), and
a `400 unauthorized_topics` from register() is retried once (KV propagation lag)
before surfacing as a configuration error.
"""

import json
from unittest.mock import patch

import httpx
import pytest

from modastack import http as pooled
from modastack.config import Config, ServiceConfig
from modastack.events.server import (
    authorize_resources,
    register,
    UnauthorizedTopics,
)
from modastack.subagent import _start_event_subscription


def _cfg(github_token="", linear_key=""):
    services = []
    if github_token:
        services.append(ServiceConfig(name="github", credentials={"token": github_token}))
    if linear_key:
        services.append(ServiceConfig(name="linear", credentials={"api_key": linear_key}))
    return Config(services=services)


# --- authorize_resources (test 10) ------------------------------------------


def test_authorize_resources_signs_and_posts_per_global_resource():
    """Each github:/linear: topic is verified with a signed POST; the returned
    set keeps every authorized topic plus non-global / slack pass-throughs."""
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/resources/authorize")
        captured.append(json.loads(request.content.decode()))
        # Mandatory bubble signature present on every authorize call.
        assert request.headers.get("x-moda-signature")
        assert request.headers.get("x-moda-bubble") == "bub_test"
        return httpx.Response(200, json={"ok": True})

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    with patch.object(pooled, "_client", mock_http):
        kept = authorize_resources(
            "https://es.invalid", _cfg(github_token="ghp_x", linear_key="lin_y"),
            ["inbox/self", "github:Org/Repo", "linear:ENG", "slack:T1"],
            "bub_test", "bkey_test",
        )

    # Non-global + slack pass through; both global topics authorized + kept.
    assert kept == ["inbox/self", "github:Org/Repo", "linear:ENG", "slack:T1"]
    services = sorted(c["service"] for c in captured)
    assert services == ["github", "linear"]
    # The credential travels in the body (verified once) — that's expected; the
    # SERVER is responsible for never persisting it.
    gh = next(c for c in captured if c["service"] == "github")
    assert gh["resource"] == "Org/Repo" and gh["credential"] == "ghp_x"


def test_authorize_resources_drops_topic_with_missing_credential():
    """A github:/linear: topic with no configured credential is logged + dropped
    (never sent to register, so register is not hard-rejected)."""
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True})

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    with patch.object(pooled, "_client", mock_http):
        kept = authorize_resources(
            "https://es.invalid", _cfg(github_token="ghp_x"),  # no linear key
            ["github:o/r", "linear:ENG"],
            "bub_test", "bkey_test",
        )

    assert kept == ["github:o/r"]              # linear dropped — no credential
    assert [c["service"] for c in captured] == ["github"]  # only github posted


def test_authorize_resources_drops_topic_on_server_denial():
    """A 403 from the server (credential can't read the resource) drops the topic."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    with patch.object(pooled, "_client", mock_http):
        kept = authorize_resources(
            "https://es.invalid", _cfg(github_token="ghp_x"),
            ["github:o/r"], "bub_test", "bkey_test",
        )
    assert kept == []  # denied → dropped


def test_authorize_resources_never_calls_for_slack_or_nonglobal():
    """Slack (converges via /slack/workspaces) and non-global topics make no
    /resources/authorize call and always pass through."""
    transport = httpx.MockTransport(lambda req: (_ for _ in ()).throw(
        AssertionError(f"unexpected authorize call: {req.url}")))
    mock_http = httpx.Client(transport=transport)
    with patch.object(pooled, "_client", mock_http):
        kept = authorize_resources(
            "https://es.invalid", _cfg(),
            ["slack:T1", "slack:T1:C9", "inbox/x", "monitor/y"],
            "bub_test", "bkey_test",
        )
    assert kept == ["slack:T1", "slack:T1:C9", "inbox/x", "monitor/y"]


def test_authorize_resources_noop_without_bubble_credential():
    """No bubble key (can't sign) → return subscribe unchanged, no calls."""
    transport = httpx.MockTransport(lambda req: (_ for _ in ()).throw(
        AssertionError("must not call without a bubble key")))
    mock_http = httpx.Client(transport=transport)
    with patch.object(pooled, "_client", mock_http):
        kept = authorize_resources("https://es.invalid", _cfg(github_token="x"),
                                   ["github:o/r"], "", "")
    assert kept == ["github:o/r"]


# --- register() retry on 400 unauthorized_topics (test 10) -------------------


@patch("time.sleep")
def test_register_retries_once_on_unauthorized_topics(_sleep):
    """A 400 unauthorized_topics (KV-propagation lag for a just-authorized grant)
    is retried ONCE; a 201 on the retry succeeds."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(400, json={"error": "unauthorized_topics",
                                             "topics": ["github:o/r"]})
        return httpx.Response(201, json={"deployment_id": "dep-1", "api_key": "moda_k"})

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    with patch.object(pooled, "_client", mock_http):
        dep, key = register("https://es.invalid", "sess", ["github:o/r"],
                            bubble_id="bub", bubble_key="bkey")

    assert (dep, key) == ("dep-1", "moda_k")
    assert len(calls) == 2          # initial + one retry
    _sleep.assert_called_once()     # waited for KV propagation


@patch("time.sleep")
def test_register_surfaces_unauthorized_after_retry(_sleep):
    """A persistent 400 unauthorized_topics surfaces as UnauthorizedTopics after
    the single retry (a genuine misconfiguration, not propagation lag)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "unauthorized_topics",
                                         "topics": ["github:o/r"]})

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    with patch.object(pooled, "_client", mock_http):
        with pytest.raises(UnauthorizedTopics) as exc:
            register("https://es.invalid", "sess", ["github:o/r"],
                     bubble_id="bub", bubble_key="bkey")
    assert exc.value.topics == ["github:o/r"]


# --- startup order: authorize BEFORE register (test 11) ---------------------


def test_startup_authorizes_resources_before_register(tmp_path):
    """_start_event_subscription must authorize resource grants before it
    registers the deployment, so a github:/linear: topic has its grant by the
    time the server checks it."""
    ms = tmp_path / ".modastack"
    ms.mkdir()
    (ms / "agent.yaml").write_text(
        "agent: test\nentry_point: manager\nevent_server: https://es.invalid\n"
        "services:\n  - name: github\n    credentials:\n      token: ghp_x\n"
    )

    order = []

    def fake_authorize(url, cfg, subscribe, bubble_id, bubble_key):
        order.append("authorize")
        return list(subscribe)

    def fake_register(url, name, subscriptions, bubble_id="", bubble_key="", **kw):
        order.append("register")
        return ("dep-1", "key-1")

    # The names are imported INTO subagent at call time via
    # `from modastack.events.server import ...`, so patch them on the source
    # module (the same convention the other subscription tests use).
    with patch("modastack.events.server.ensure_bubble",
               return_value={"bubble_id": "bub", "bubble_key": "bkey"}), \
         patch("modastack.events.server.register_slack_workspaces", return_value=[]), \
         patch("modastack.events.server.authorize_resources", side_effect=fake_authorize), \
         patch("modastack.events.server.register", side_effect=fake_register), \
         patch("modastack.events.client.EventServerClient"), \
         patch("modastack.events.drain.drain_loop"):
        _start_event_subscription("sess", ["github:o/r"], tmp_path)

    assert order.index("authorize") < order.index("register")
