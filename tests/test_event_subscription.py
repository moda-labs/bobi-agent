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


def _state_file(project):
    return project / ".modastack" / "state" / "deployment.json"


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
