"""Tests for _start_event_subscription — registration retry and persistence.

Regression coverage for the EC2 director crash: a transient TimeoutError
during event server registration propagated uncaught and killed the
manager daemon (register() had no retry, and the deployment was never
persisted, so every start re-registered from scratch via a guaranteed-400
PUT fallback).
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from modastack.subagent import _start_event_subscription


REMOTE_URL = "https://events.example.invalid"


@pytest.fixture
def project(tmp_path):
    """Project dir with a remote event server configured."""
    ms = tmp_path / ".modastack"
    ms.mkdir()
    (ms / "agent.yaml").write_text(
        f"agent: test\nentry_point: manager\nevent_server: {REMOTE_URL}\n"
    )
    return tmp_path


def _state_file(project, session="sess"):
    # Deployment state is per-session — sharing one deployment across
    # sessions is the bug that broadcast the user's DMs to every agent.
    return project / ".modastack" / "state" / "deployments" / f"{session}.json"


@patch("modastack.events.drain.drain_loop")
@patch("modastack.events.client.EventServerClient")
@patch("urllib.request.urlopen")
@patch("modastack.events.server.register")
def test_fresh_state_registers_without_put(mock_register, mock_urlopen,
                                           mock_client, _drain, project):
    """With no saved deployment, go straight to register — no empty-id PUT."""
    mock_register.return_value = ("dep-1", "key-1")

    _start_event_subscription("sess", ["github:o/r"], project)

    mock_urlopen.assert_not_called()
    mock_register.assert_called_once()
    saved = json.loads(_state_file(project).read_text())
    assert saved == {"deployment_id": "dep-1", "api_key": "key-1"}
    assert mock_client.call_args.kwargs["deployment_id"] == "dep-1"


@patch("time.sleep")
@patch("modastack.events.drain.drain_loop")
@patch("modastack.events.client.EventServerClient")
@patch("modastack.events.server.register")
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
@patch("modastack.events.drain.drain_loop")
@patch("modastack.events.client.EventServerClient")
@patch("modastack.events.server.register")
def test_register_exhausted_raises_clean_error(mock_register, _client, _drain,
                                               _sleep, project):
    """Persistent failure raises RuntimeError with context, not a raw socket error."""
    mock_register.side_effect = TimeoutError("The read operation timed out")

    with pytest.raises(RuntimeError, match="after 3 attempts"):
        _start_event_subscription("sess", ["github:o/r"], project)

    assert mock_register.call_count == 3
    assert not _state_file(project).exists()


@patch("modastack.events.drain.drain_loop")
@patch("modastack.events.client.EventServerClient")
@patch("urllib.request.urlopen")
@patch("modastack.events.server.register")
def test_saved_state_uses_put_not_register(mock_register, mock_urlopen,
                                           mock_client, _drain, project):
    """With a saved deployment, update subscriptions via PUT — no re-register."""
    state = _state_file(project)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"deployment_id": "dep-3", "api_key": "key-3"}))

    _start_event_subscription("sess", ["github:o/r"], project)

    mock_register.assert_not_called()
    req = mock_urlopen.call_args.args[0]
    assert req.full_url == f"{REMOTE_URL}/deployments/dep-3/subscriptions"
    assert mock_client.call_args.kwargs["deployment_id"] == "dep-3"


@patch("modastack.events.drain.drain_loop")
@patch("modastack.events.client.EventServerClient")
@patch("urllib.request.urlopen")
@patch("modastack.events.server.register")
def test_failed_put_falls_back_to_register(mock_register, mock_urlopen,
                                           mock_client, _drain, project):
    """A dead saved deployment (PUT fails) re-registers and persists fresh creds."""
    state = _state_file(project)
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"deployment_id": "dep-old", "api_key": "key-old"}))
    mock_urlopen.side_effect = OSError("HTTP Error 401")
    mock_register.return_value = ("dep-new", "key-new")

    _start_event_subscription("sess", ["github:o/r"], project)

    mock_register.assert_called_once()
    saved = json.loads(_state_file(project).read_text())
    assert saved["deployment_id"] == "dep-new"


# --- Per-session deployment isolation (DM broadcast incident) ----------------


@patch("modastack.events.drain.drain_loop")
@patch("modastack.events.client.EventServerClient")
@patch("urllib.request.urlopen")
@patch("modastack.events.server.register")
def test_each_session_registers_its_own_deployment(mock_register, mock_urlopen,
                                                   mock_client, _drain, project):
    """Two sessions from one project root must NOT share a deployment.

    Regression for the prod incident: the second session PUT-added its
    repo-scoped subscriptions onto the first session's deployment, so the
    event server fanned the director's Slack DMs out to every project lead.
    """
    mock_register.side_effect = [("dep-director", "key-d"), ("dep-lead", "key-l")]

    _start_event_subscription("director", ["slack:T1"], project)
    _start_event_subscription("lead", ["github:o/r"], project)

    # No cross-session PUT: the lead registers fresh, never touching the
    # director's deployment.
    mock_urlopen.assert_not_called()
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


@patch("modastack.events.drain.drain_loop")
@patch("modastack.events.client.EventServerClient")
@patch("urllib.request.urlopen")
@patch("modastack.events.server.register")
def test_fresh_register_resets_session_cursor(mock_register, _urlopen,
                                              _client, _drain, project):
    """A new deployment starts a new seq space — a leftover cursor from a
    previous deployment must not survive registration."""
    from modastack.config import session_cursor_path
    cursor = session_cursor_path(project, "sess")
    cursor.parent.mkdir(parents=True)
    cursor.write_text(json.dumps({"last_seen": 37}))
    mock_register.return_value = ("dep-1", "key-1")

    _start_event_subscription("sess", ["github:o/r"], project)

    assert not cursor.exists()


def _install_root(path):
    (path / ".modastack").mkdir(parents=True)
    (path / ".modastack" / "agent.yaml").write_text("name: test-agent\n")


def test_resolve_root_walks_up_to_modastack(tmp_path):
    """An agent spawned from a repo checkout inside the project must bind to
    the real project root, not fork its own config/state/subscriptions."""
    from modastack.paths import resolve_root

    _install_root(tmp_path)
    checkout = tmp_path / "repos" / "some-repo" / "src"
    checkout.mkdir(parents=True)

    assert resolve_root(checkout) == tmp_path
    assert resolve_root(tmp_path) == tmp_path


def test_resolve_root_skips_state_only_modastack(tmp_path):
    """A state-only .modastack/ (sessions/state dropped into a repo checkout
    by the runtime) must not capture resolution — the walk continues to the
    installed root above it. Regression: engineer dispatch resolved a repo's
    state-only dir as project root and died with workflow-not-found."""
    from modastack.paths import resolve_root

    _install_root(tmp_path)
    repo = tmp_path / "repos" / "some-repo"
    (repo / ".modastack" / "sessions").mkdir(parents=True)
    (repo / ".modastack" / "state").mkdir()

    assert resolve_root(repo) == tmp_path
    assert resolve_root(repo / ".modastack" / "state") == tmp_path


def test_resolve_root_raises_without_installation(tmp_path):
    """No installed root above the start dir is an error, not a guess —
    resolving to the bare start dir was the fallback that let processes
    invent roots."""
    import pytest
    from modastack.paths import resolve_root

    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RuntimeError, match="no Modastack installation"):
        resolve_root(plain)


# --- Slack workspace registration (self-reply loop prevention) ---------------

def _slack_resp(payload):
    return type("Resp", (), {
        "read": lambda self: json.dumps(payload).encode(),
        "__enter__": lambda self: self,
        "__exit__": lambda self, *a: False,
    })()


def test_register_slack_workspaces_posts_bot_token(monkeypatch):
    """Regression for the Slack self-reply loop: the agent must register its
    workspace bot with the event server, else the event server can't identify
    (and skip) the bot's own messages and the agent loops on its own replies.
    """
    from modastack.events.server import register_slack_workspaces
    from modastack.config import Config, ServiceConfig

    captured = {}

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        if "auth.test" in url:
            return _slack_resp({"ok": True, "team_id": "T0952", "bot_id": "BSELF"})
        if url.endswith("/slack/workspaces"):
            captured["url"] = url
            captured["body"] = json.loads(req.data.decode())
            return _slack_resp({"ok": True, "workspace_id": "T0952", "bot_id": "BSELF"})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    cfg = Config(services=[
        ServiceConfig(name="slack", credentials={"bot_token": "xoxb-test"}),
    ])
    result = register_slack_workspaces("http://localhost:8080", cfg)

    assert result == ["T0952"]
    assert captured["url"] == "http://localhost:8080/slack/workspaces"
    # bot_id travels with the registration: relying on the server's own
    # auth.test fallback left self-reply filtering silently disabled
    # whenever that second lookup failed.
    assert captured["body"] == {"workspace_id": "T0952",
                                "bot_token": "xoxb-test",
                                "bot_id": "BSELF"}


def test_register_slack_workspaces_noop_without_token(monkeypatch):
    from modastack.events.server import register_slack_workspaces
    from modastack.config import Config

    def fake_urlopen(*a, **k):
        raise AssertionError("no network call without a slack bot token")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert register_slack_workspaces("http://localhost:8080", Config()) == []
