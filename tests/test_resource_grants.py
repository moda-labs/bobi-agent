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

from bobi import http as pooled
from bobi.config import Config, ServiceConfig
from bobi.events.server import (
    authorize_resources,
    register,
    BubbleRejected,
    UnauthorizedTopics,
)
from bobi.subagent import _start_event_subscription


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


def test_authorize_resources_can_keep_unverified_topics_for_saved_deployment():
    """Saved deployments may already have server-side no-expiry grants. The
    update path should try to authorize new resources, but must not silently
    replace existing subscriptions with a filtered list when credentials are
    unavailable locally."""
    transport = httpx.MockTransport(lambda req: (_ for _ in ()).throw(
        AssertionError(f"unexpected authorize call without credentials: {req.url}")))
    mock_http = httpx.Client(transport=transport)
    with patch.object(pooled, "_client", mock_http):
        kept = authorize_resources(
            "https://es.invalid", _cfg(),
            ["github:o/r", "linear:ENG", "inbox/self"],
            "bub_test", "bkey_test",
            filter_unauthorized=False,
        )

    assert kept == ["github:o/r", "linear:ENG", "inbox/self"]


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


def test_authorize_resources_warns_with_unbacked_global_topics(caplog):
    """A failed grant authorization must leave one loud summary listing the
    affected global subscriptions, so startup cannot look subscribed while the
    backing grant is missing."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    with patch.object(pooled, "_client", mock_http), caplog.at_level("WARNING"):
        kept = authorize_resources(
            "https://es.invalid", _cfg(github_token="ghp_x", linear_key="lin_y"),
            ["github:o/r", "linear:ENG", "inbox/self"],
            "bub_test", "bkey_test",
        )

    assert kept == ["inbox/self"]
    assert "Global event subscriptions without resource grants were dropped" in caplog.text
    assert "github:o/r" in caplog.text
    assert "linear:ENG" in caplog.text


def test_authorize_resources_transport_warning_does_not_log_credentials(caplog):
    """Transport failures should say which topic is unbacked without copying
    credential-shaped values from config or exception text into logs."""
    secret = "ghp_secret_should_not_log"

    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"upstream failed with {secret}")

    mock_http = httpx.Client(transport=httpx.MockTransport(handler))
    with patch.object(pooled, "_client", mock_http), caplog.at_level("WARNING"):
        kept = authorize_resources(
            "https://es.invalid", _cfg(github_token=secret),
            ["github:o/r"],
            "bub_test", "bkey_test",
        )

    assert kept == []
    assert "github:o/r" in caplog.text
    assert secret not in caplog.text


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


def test_authorize_resources_drops_whatsapp_topic_the_registration_did_not_back():
    """A whatsapp:<pnid> topic is grant-backed only by register_whatsapp_numbers.
    When the caller just ran it and the pnid is NOT in the returned list, the
    topic is dropped - keeping it would hard-reject the whole atomic
    register/PUT (#488) and stall delivery for every channel."""
    transport = httpx.MockTransport(lambda req: (_ for _ in ()).throw(
        AssertionError(f"unexpected authorize call: {req.url}")))
    mock_http = httpx.Client(transport=transport)
    with patch.object(pooled, "_client", mock_http):
        kept = authorize_resources(
            "https://es.invalid", _cfg(),
            ["inbox/self", "whatsapp:747556541"],
            "bub_test", "bkey_test",
            whatsapp_registered=[],
        )
    assert kept == ["inbox/self"]


def test_authorize_resources_keeps_registered_whatsapp_topic():
    """A pnid the registration DID back passes through."""
    mock_http = httpx.Client(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(
        AssertionError(f"unexpected authorize call: {req.url}"))))
    with patch.object(pooled, "_client", mock_http):
        kept = authorize_resources(
            "https://es.invalid", _cfg(),
            ["whatsapp:747556541", "inbox/self"],
            "bub_test", "bkey_test",
            whatsapp_registered=["747556541"],
        )
    assert kept == ["whatsapp:747556541", "inbox/self"]


def test_authorize_resources_drops_discord_topic_the_registration_did_not_back():
    """Same contract as WhatsApp through the generalized registered-by-service
    filter (#2): a discord:<application_id> topic is grant-backed only by
    register_discord_apps, so an unbacked one is dropped before register/PUT."""
    transport = httpx.MockTransport(lambda req: (_ for _ in ()).throw(
        AssertionError(f"unexpected authorize call: {req.url}")))
    mock_http = httpx.Client(transport=transport)
    with patch.object(pooled, "_client", mock_http):
        kept = authorize_resources(
            "https://es.invalid", _cfg(),
            ["inbox/self", "discord:111222333444555666"],
            "bub_test", "bkey_test",
            discord_registered=[],
        )
    assert kept == ["inbox/self"]


def test_authorize_resources_keeps_registered_discord_topic():
    """An application id the registration DID back passes through; None means
    no registration ran and the topic is kept."""
    mock_http = httpx.Client(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(
        AssertionError(f"unexpected authorize call: {req.url}"))))
    with patch.object(pooled, "_client", mock_http):
        kept = authorize_resources(
            "https://es.invalid", _cfg(),
            ["discord:111222333444555666", "inbox/self"],
            "bub_test", "bkey_test",
            discord_registered=["111222333444555666"],
        )
        kept_unregistered = authorize_resources(
            "https://es.invalid", _cfg(),
            ["discord:111222333444555666"],
            "bub_test", "bkey_test",
        )
    assert kept == ["discord:111222333444555666", "inbox/self"]
    assert kept_unregistered == ["discord:111222333444555666"]


def test_authorize_resources_keeps_whatsapp_when_no_registration_ran():
    """whatsapp_registered=None means no registration was attempted (e.g. an
    inbox-only session) - keep the topic, matching the pre-#656 behavior."""
    mock_http = httpx.Client(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(
        AssertionError(f"unexpected authorize call: {req.url}"))))
    with patch.object(pooled, "_client", mock_http):
        kept = authorize_resources(
            "https://es.invalid", _cfg(),
            ["whatsapp:747556541"],
            "bub_test", "bkey_test",
        )
    assert kept == ["whatsapp:747556541"]


def test_authorize_resources_keeps_unbacked_whatsapp_for_saved_deployment():
    """filter_unauthorized=False (saved deployment): the server may hold a
    no-expiry grant from an earlier start, so the topic is kept and the
    server stays authoritative - same contract as github/linear."""
    mock_http = httpx.Client(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(
        AssertionError(f"unexpected authorize call: {req.url}"))))
    with patch.object(pooled, "_client", mock_http):
        kept = authorize_resources(
            "https://es.invalid", _cfg(),
            ["whatsapp:747556541"],
            "bub_test", "bkey_test",
            filter_unauthorized=False,
            whatsapp_registered=[],
        )
    assert kept == ["whatsapp:747556541"]


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
    from bobi import paths

    paths.package_dir(tmp_path).mkdir(parents=True)
    paths.agent_yaml_path(tmp_path).write_text(
        "agent: test\nentry_point: manager\nevent_server: https://es.invalid\n"
        "services:\n  - name: github\n    credentials:\n      token: ghp_x\n"
    )

    order = []

    def fake_authorize(url, cfg, subscribe, bubble_id, bubble_key, **kw):
        order.append("authorize")
        return list(subscribe)

    def fake_register(url, name, subscriptions, bubble_id="", bubble_key="", **kw):
        order.append("register")
        return ("dep-1", "key-1")

    # The names are imported INTO subagent at call time via
    # `from bobi.events.server import ...`, so patch them on the source
    # module (the same convention the other subscription tests use).
    with patch("bobi.events.server.ensure_bubble",
               return_value={"bubble_id": "bub", "bubble_key": "bkey"}), \
         patch("bobi.events.server.register_slack_workspaces", return_value=[]), \
         patch("bobi.events.server.authorize_resources", side_effect=fake_authorize), \
         patch("bobi.events.server.register", side_effect=fake_register), \
         patch("bobi.events.client.EventServerClient"), \
         patch("bobi.events.drain.drain_loop"):
        _start_event_subscription("sess", ["github:o/r"], tmp_path)

    assert order.index("authorize") < order.index("register")


def test_startup_reauthorizes_resources_after_forced_bubble_remint(tmp_path):
    """If the event server rejects the on-disk bubble, startup must force a
    re-mint and authorize resource grants with the new bubble before the
    successful register. Otherwise global topics can be registered without the
    grants that delivery requires."""
    from bobi import paths

    paths.package_dir(tmp_path).mkdir(parents=True)
    paths.agent_yaml_path(tmp_path).write_text(
        "agent: test\nentry_point: manager\nevent_server: https://es.invalid\n"
        "services:\n  - name: github\n    credentials:\n      token: ghp_x\n"
    )

    auth_bubbles = []
    register_bubbles = []

    def fake_ensure_bubble(url, project_path, force_remint_of=""):
        assert url == "https://es.invalid"
        if force_remint_of:
            assert force_remint_of == "bub_old"
            return {"bubble_id": "bub_new", "bubble_key": "bkey_new"}
        return {"bubble_id": "bub_old", "bubble_key": "bkey_old"}

    def fake_authorize(url, cfg, subscribe, bubble_id, bubble_key, **kw):
        auth_bubbles.append((bubble_id, bubble_key))
        return list(subscribe)

    def fake_register(url, name, subscriptions, bubble_id="", bubble_key="", **kw):
        register_bubbles.append((bubble_id, bubble_key, list(subscriptions)))
        if len(register_bubbles) == 1:
            raise BubbleRejected("stale bubble")
        return ("dep-1", "key-1")

    with patch("bobi.events.server.ensure_bubble", side_effect=fake_ensure_bubble), \
         patch("bobi.events.server.register_slack_workspaces", return_value=[]), \
         patch("bobi.events.server.register_whatsapp_numbers", return_value=[]), \
         patch("bobi.events.server.authorize_resources", side_effect=fake_authorize), \
         patch("bobi.events.server.register", side_effect=fake_register), \
         patch("bobi.events.client.EventServerClient"), \
         patch("bobi.events.drain.drain_loop"):
        _start_event_subscription("sess", ["github:o/r"], tmp_path)

    assert auth_bubbles == [("bub_old", "bkey_old"), ("bub_new", "bkey_new")]
    assert register_bubbles == [
        ("bub_old", "bkey_old", ["github:o/r"]),
        ("bub_new", "bkey_new", ["github:o/r"]),
    ]
