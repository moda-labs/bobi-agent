"""Tests for _start_event_subscription — registration retry and persistence.

Regression coverage for the EC2 director crash: a transient TimeoutError
during event server registration propagated uncaught and killed the
manager daemon (register() had no retry, and the deployment was never
persisted, so every start re-registered from scratch via a guaranteed-400
PUT fallback).
"""

import json
from unittest.mock import patch

import httpx
import pytest

from bobi import paths
from bobi import http as pooled
from bobi.subagent import _start_event_subscription


REMOTE_URL = "https://events.example.invalid"


@pytest.fixture
def project(tmp_path):
    """Project dir with a remote event server configured."""
    paths.package_dir(tmp_path).mkdir(parents=True)
    paths.agent_yaml_path(tmp_path).write_text(
        f"agent: test\nentry_point: manager\nevent_server: {REMOTE_URL}\n"
    )
    return tmp_path


@pytest.fixture(autouse=True)
def _stub_bubble():
    """Every registration JOINs the instance bubble via ensure_bubble; stub it
    so these unit tests don't make a real mint HTTP call."""
    with patch("bobi.events.server.ensure_bubble",
               return_value={"bubble_id": "bub_test", "bubble_key": "bkey_test"}):
        yield


def _state_file(project, session="sess"):
    # Deployment state is per-session — sharing one deployment across
    # sessions is the bug that broadcast the user's DMs to every agent.
    return paths.state_path(project) / "deployments" / f"{session}.json"


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
@patch("bobi.events.server.register")
def test_fresh_state_registers_without_put(mock_register,
                                           mock_client, _drain, project):
    """With no saved deployment, go straight to register — no empty-id PUT."""
    mock_register.return_value = ("dep-1", "key-1")

    transport = httpx.MockTransport(lambda req: (_ for _ in ()).throw(
        AssertionError(f"unexpected HTTP call: {req.method} {req.url}")))
    mock_http = httpx.Client(transport=transport)

    with patch.object(pooled, '_client', mock_http):
        _start_event_subscription("sess", ["github:o/r"], project)

    mock_register.assert_called_once()
    saved = json.loads(_state_file(project).read_text())
    assert saved == {"deployment_id": "dep-1", "api_key": "key-1"}
    assert mock_client.call_args.kwargs["deployment_id"] == "dep-1"


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
@patch("bobi.events.server.register")
def test_deaf_reconnect_uses_filtered_registered_subscriptions(
        mock_register, mock_client, _drain, project):
    """If fresh registration drops an unbacked global topic, the deaf reconnect
    resubscribe must not later PUT the raw unfiltered subscribe list."""
    mock_register.return_value = ("dep-1", "key-1")
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    mock_http = httpx.Client(transport=transport)

    with patch.object(pooled, '_client', mock_http):
        _start_event_subscription("sess", ["github:o/r", "inbox/self"], project)
        on_deaf = mock_client.call_args.kwargs["on_deaf_reconnect"]
        on_deaf()

    mock_register.assert_called_once()
    assert mock_register.call_args.args[2] == ["inbox/self"]
    put_reqs = [r for r in captured if r.method == "PUT"]
    assert len(put_reqs) == 1
    assert json.loads(put_reqs[0].content) == {"replace": ["inbox/self"]}


@patch("time.sleep")
@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
@patch("bobi.events.server.register")
def test_transient_register_failure_retries(mock_register, _client, _drain,
                                            _sleep, project):
    """A transient timeout must not propagate — retry and succeed."""
    mock_register.side_effect = [
        TimeoutError("The read operation timed out"),
        ("dep-2", "key-2"),
    ]

    _start_event_subscription("sess", ["github:o/r"], project)

    assert mock_register.call_count == 2
    saved = json.loads(_state_file(project).read_text())
    assert saved["deployment_id"] == "dep-2"


@patch("time.sleep")
@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
@patch("bobi.events.server.register")
def test_register_exhausted_raises_clean_error(mock_register, _client, _drain,
                                               _sleep, project):
    """Persistent failure raises RuntimeError with context, not a raw socket error."""
    mock_register.side_effect = TimeoutError("The read operation timed out")

    with pytest.raises(RuntimeError, match="after 3 attempts"):
        _start_event_subscription("sess", ["github:o/r"], project)

    assert mock_register.call_count == 3
    assert not _state_file(project).exists()


def _create_bubble(project):
    """Create a bubble.json so the PUT path is taken (post-bubble state)."""
    from bobi.config import bubble_state_path
    bp = bubble_state_path(project)
    bp.parent.mkdir(parents=True, exist_ok=True)
    bp.write_text(json.dumps({"bubble_id": "bub_test", "bubble_key": "bkey_test"}))


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
@patch("bobi.events.server.register")
def test_saved_state_uses_put_not_register(mock_register,
                                           mock_client, _drain, project):
    """With a saved deployment, update subscriptions via PUT — no re-register."""
    state = _state_file(project)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"deployment_id": "dep-3", "api_key": "key-3"}))
    _create_bubble(project)

    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    mock_http = httpx.Client(transport=transport)

    with patch.object(pooled, '_client', mock_http):
        _start_event_subscription("sess", ["github:o/r"], project)

    mock_register.assert_not_called()
    assert len(captured) >= 1
    put_reqs = [r for r in captured if r.method == "PUT"]
    assert len(put_reqs) == 1
    assert str(put_reqs[0].url) == f"{REMOTE_URL}/deployments/dep-3/subscriptions"
    assert json.loads(put_reqs[0].content) == {"replace": ["github:o/r"]}
    assert mock_client.call_args.kwargs["deployment_id"] == "dep-3"


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
@patch("bobi.events.server.register")
def test_saved_state_keeps_unbacked_global_topics_and_resubscribes_same(
        mock_register, mock_client, _drain, project):
    """Saved deployments keep global topics when pre-PUT authorization fails,
    because the server may already hold a no-expiry grant. Deaf reconnect must
    reassert that same authorized/kept list, not the raw subscribe input."""
    from bobi import paths

    paths.agent_yaml_path(project).write_text(
        f"agent: test\nentry_point: manager\nevent_server: {REMOTE_URL}\n"
        "services:\n  - name: github\n    credentials:\n      token: ghp_secret\n"
    )
    state = _state_file(project)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"deployment_id": "dep-3", "api_key": "key-3"}))
    _create_bubble(project)

    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "POST" and str(request.url).endswith("/resources/authorize"):
            return httpx.Response(403, json={"error": "forbidden"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    mock_http = httpx.Client(transport=transport)

    with patch.object(pooled, '_client', mock_http):
        _start_event_subscription("sess", ["github:o/r", "inbox/self"], project)
        on_deaf = mock_client.call_args.kwargs["on_deaf_reconnect"]
        on_deaf()

    mock_register.assert_not_called()
    put_reqs = [r for r in captured if r.method == "PUT"]
    assert len(put_reqs) == 2
    assert json.loads(put_reqs[0].content) == {"replace": ["github:o/r", "inbox/self"]}
    assert json.loads(put_reqs[1].content) == {"replace": ["github:o/r", "inbox/self"]}


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
@patch("bobi.events.server.register")
def test_failed_put_falls_back_to_register(mock_register,
                                           mock_client, _drain, project):
    """A dead saved deployment (PUT fails) re-registers and persists fresh creds."""
    state = _state_file(project)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"deployment_id": "dep-old", "api_key": "key-old"}))
    _create_bubble(project)
    mock_register.return_value = ("dep-new", "key-new")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            raise httpx.HTTPStatusError(
                "401 Unauthorized",
                request=request,
                response=httpx.Response(401),
            )
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    mock_http = httpx.Client(transport=transport)

    with patch.object(pooled, '_client', mock_http):
        _start_event_subscription("sess", ["github:o/r"], project)

    mock_register.assert_called_once()
    saved = json.loads(_state_file(project).read_text())
    assert saved["deployment_id"] == "dep-new"


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
@patch("bobi.events.server.register")
def test_forbidden_put_response_falls_back_to_register(mock_register,
                                                       mock_client, _drain,
                                                       project):
    """A 403 subscription update response means the saved api_key is stale."""
    state = _state_file(project)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"deployment_id": "dep-old", "api_key": "key-old"}))
    _create_bubble(project)
    mock_register.return_value = ("dep-new", "key-new")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(403, text="Invalid API key", request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    transport = httpx.MockTransport(handler)
    mock_http = httpx.Client(transport=transport)

    with patch.object(pooled, '_client', mock_http):
        _start_event_subscription("sess", ["github:o/r"], project)

    mock_register.assert_called_once()
    saved = json.loads(_state_file(project).read_text())
    assert saved == {"deployment_id": "dep-new", "api_key": "key-new"}
    assert mock_client.call_args.kwargs["deployment_id"] == "dep-new"


# --- Pre-bubble upgrade (stale deployment_state, no bubble.json) -------------


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
@patch("bobi.events.server.register")
def test_pre_bubble_upgrade_reregisters(mock_register,
                                        mock_client, _drain, project):
    """Saved deployment_state but no bubble.json → pre-bubble upgrade.

    The old api_key predates auth bubbles and can't sign publishes against
    a v0.21+ server (403). The client must drop the stale state + cursor
    and re-register through ensure_bubble instead of the PUT path.

    Regression for #314: Cloudflare upgrade leaves client unable to publish.
    """
    state = _state_file(project)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"deployment_id": "dep-old", "api_key": "key-old"}))
    # Intentionally NO _create_bubble(project) — simulates pre-bubble state.

    # Plant a stale cursor that should be cleared on re-register.
    from bobi.config import session_cursor_path
    cursor = session_cursor_path(project, "sess")
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text(json.dumps({"last_seen": 42}))

    mock_register.return_value = ("dep-fresh", "key-fresh")

    transport = httpx.MockTransport(lambda req: (_ for _ in ()).throw(
        AssertionError(f"unexpected HTTP call: {req.method} {req.url}")))
    mock_http = httpx.Client(transport=transport)

    with patch.object(pooled, '_client', mock_http):
        _start_event_subscription("sess", ["github:o/r"], project)

    # Must re-register, NOT PUT to the stale deployment.
    mock_register.assert_called_once()
    saved = json.loads(_state_file(project).read_text())
    assert saved == {"deployment_id": "dep-fresh", "api_key": "key-fresh"}
    assert mock_client.call_args.kwargs["deployment_id"] == "dep-fresh"
    # Stale cursor cleared (register_with_retry also clears it, but the
    # pre-bubble guard clears it before the call for determinism).
    assert not cursor.exists()


# --- Per-session deployment isolation (DM broadcast incident) ----------------


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
@patch("bobi.events.server.register")
def test_each_session_registers_its_own_deployment(mock_register,
                                                   mock_client, _drain, project):
    """Two sessions from one project root must NOT share a deployment.

    Regression for the prod incident: the second session PUT-added its
    repo-scoped subscriptions onto the first session's deployment, so the
    event server fanned the director's Slack DMs out to every project lead.
    """
    mock_register.side_effect = [("dep-director", "key-d"), ("dep-lead", "key-l")]

    transport = httpx.MockTransport(lambda req: (_ for _ in ()).throw(
        AssertionError(f"unexpected HTTP call: {req.method} {req.url}")))
    mock_http = httpx.Client(transport=transport)

    with patch.object(pooled, '_client', mock_http):
        _start_event_subscription("director", ["slack:T1"], project)
        _start_event_subscription("lead", ["github:o/r"], project)

    # No cross-session PUT: the lead registers fresh, never touching the
    # director's deployment.
    assert mock_register.call_count == 2
    assert json.loads(_state_file(project, "director").read_text())[
        "deployment_id"] == "dep-director"
    assert json.loads(_state_file(project, "lead").read_text())[
        "deployment_id"] == "dep-lead"

    # Each client connects to its own deployment with its own cursor.
    dep_ids = [c.kwargs["deployment_id"] for c in mock_client.call_args_list]
    assert dep_ids == ["dep-director", "dep-lead"]
    cursors = [c.kwargs["cursor_path"] for c in mock_client.call_args_list]
    assert cursors[0] != cursors[1]


@patch("bobi.events.drain.drain_loop")
@patch("bobi.events.client.EventServerClient")
@patch("bobi.events.server.register")
def test_fresh_register_resets_session_cursor(mock_register,
                                              _client, _drain, project):
    """A new deployment starts a new seq space — a leftover cursor from a
    previous deployment must not survive registration."""
    from bobi.config import session_cursor_path
    cursor = session_cursor_path(project, "sess")
    cursor.parent.mkdir(parents=True)
    cursor.write_text(json.dumps({"last_seen": 37}))
    mock_register.return_value = ("dep-1", "key-1")

    _start_event_subscription("sess", ["github:o/r"], project)

    assert not cursor.exists()


def _install_root(path):
    paths.package_dir(path).mkdir(parents=True)
    paths.agent_yaml_path(path).write_text("name: test-agent\n")


def test_resolve_root_does_not_walk_up_from_cwd(tmp_path):
    """Runtime identity is selected by BOBI_ROOT or `bobi agent <name>`, not cwd."""
    from bobi.paths import resolve_root

    _install_root(tmp_path)
    checkout = tmp_path / "repos" / "some-repo" / "src"
    checkout.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="No Bobi Agent runtime selected"):
        resolve_root(checkout)
    assert resolve_root(tmp_path) == tmp_path


def test_resolve_root_honors_bobi_root_env(tmp_path, monkeypatch):
    """Child processes inherit BOBI_ROOT, so their working dir is irrelevant."""
    from bobi.paths import resolve_root

    _install_root(tmp_path)
    repo = tmp_path / "repos" / "some-repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("BOBI_ROOT", str(tmp_path))

    assert resolve_root(repo) == tmp_path


def test_resolve_root_raises_without_installation(tmp_path):
    """No explicit runtime selection is an error, not a guess."""
    from bobi.paths import resolve_root

    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RuntimeError, match="No Bobi Agent runtime selected"):
        resolve_root(plain)


# --- Slack workspace registration (self-reply loop prevention) ---------------

def test_register_slack_workspaces_posts_bot_token():
    """Regression for the Slack self-reply loop: the agent must register its
    workspace bot with the event server, else the event server can't identify
    (and skip) the bot's own messages and the agent loops on its own replies.
    """
    from bobi.events.server import register_slack_workspaces
    from bobi.config import Config, ServiceConfig

    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "auth.test" in url:
            return httpx.Response(200, json={
                "ok": True,
                "team_id": "T0952",
                "bot_id": "BSELF",
                "user_id": "USELF",
            })
        if "bots.info" in url:
            assert "bot=BSELF" in url
            return httpx.Response(200, json={"ok": True, "bot": {"app_id": "A0952"}})
        if url.endswith("/slack/workspaces"):
            captured.append({"url": url, "body": json.loads(request.content.decode())})
            return httpx.Response(200, json={"ok": True, "workspace_id": "T0952",
                                             "bot_id": "BSELF", "app_id": "A0952"})
        raise AssertionError(f"unexpected url {url}")

    transport = httpx.MockTransport(handler)
    mock_http = httpx.Client(transport=transport)

    with patch.object(pooled, '_client', mock_http):
        cfg = Config(services=[
            ServiceConfig(name="slack", credentials={
                "bot_token": "xoxb-test",
                "signing_secret": "shhh",
            }),
        ])
        result = register_slack_workspaces("http://localhost:8080", cfg)

    assert result == ["T0952"]
    assert len(captured) == 1
    assert captured[0]["url"] == "http://localhost:8080/slack/workspaces"
    # bot_id + app_id + signing_secret travel with the registration. app_id keys
    # the per-bot record so two bots can share a workspace; bot_user_id lets the
    # server drop message.* duplicates of app_mention; signing_secret lets the
    # server verify THIS app's inbound events (a second app signs with its own
    # secret, so a single global secret would 401 it).
    assert captured[0]["body"] == {"workspace_id": "T0952",
                                   "bot_token": "xoxb-test",
                                   "bot_id": "BSELF",
                                   "bot_user_id": "USELF",
                                   "app_id": "A0952",
                                   "signing_secret": "shhh"}


def test_register_slack_workspaces_noop_without_token():
    from bobi.events.server import register_slack_workspaces
    from bobi.config import Config

    transport = httpx.MockTransport(lambda req: (_ for _ in ()).throw(
        AssertionError("no network call without a slack bot token")))
    mock_http = httpx.Client(transport=transport)

    with patch.object(pooled, '_client', mock_http):
        assert register_slack_workspaces("http://localhost:8080", Config()) == []
