"""Saved replay identity survives unsuccessful subscription synchronization."""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from bobi import http as pooled, paths
from bobi.events.state import bubble_state_path, session_cursor_path
from bobi.subagent import _start_event_subscription


@pytest.mark.parametrize("bubble_contents", ["valid", None, "invalid-json", '{"bubble_id": "bub_test"}'])
@pytest.mark.parametrize("endpoint", ["https://events.example.invalid", "http://localhost:8099", ""])
@pytest.mark.parametrize("failure", [200, 400, 401, 403, 404, 429, 500, 502, 503, 504, "timeout", "connect", "malformed", "invalid-json", "unexpected"])
def test_saved_identity_survives_sync_and_recovers(tmp_path, caplog, endpoint, failure, bubble_contents):
    paths.package_dir(tmp_path).mkdir(parents=True)
    paths.agent_yaml_path(tmp_path).write_text(
        "agent: test\nentry_point: manager\n" + (f"event_server: {endpoint}\n" if endpoint else "")
    )
    state = paths.state_path(tmp_path) / "deployments/sess.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"deployment_id": "dep-old", "api_key": "secret-key"}))
    bubble = bubble_state_path(tmp_path)
    if bubble_contents == "valid":
        bubble.write_text(json.dumps({"bubble_id": "bub_test", "bubble_key": "secret-bubble"}))
    elif bubble_contents is not None:
        bubble.write_text(bubble_contents)
    cursor = session_cursor_path(tmp_path, "sess")
    cursor.parent.mkdir(parents=True)
    cursor.write_text('{"last_seen": 4}\n')
    before = {p: p.read_bytes() for p in (state, bubble, cursor) if p.exists()}
    captured = []
    response = failure

    def handler(request):
        captured.append(request)
        if response == "timeout":
            raise httpx.ReadTimeout("secret-body", request=request)
        if response == "connect":
            raise httpx.ConnectError("secret-body", request=request)
        if response == "malformed":
            return httpx.Response(503, text="secret-body")
        if response == "invalid-json":
            return httpx.Response(200, text="secret-body")
        if response == "unexpected":
            return httpx.Response(200, json=["secret-body"])
        if response == 200:
            return httpx.Response(200, json={"subscriptions": ["inbox/sess"]})
        return httpx.Response(response, json={
            "error": "unauthorized_topics" if response == 400 else "unauthorized",
            "detail": "secret-body",
        })

    with (
        patch("bobi.events.server.ensure_running", return_value="connected") as ensure,
        patch("bobi.events.server.ensure_bubble", return_value={"bubble_id": "bub_test", "bubble_key": "secret-bubble"}) as mint,
        patch("bobi.events.server.authorize_resources", side_effect=lambda url, cfg, topics, *args, **kwargs: topics) as authorize,
        patch("bobi.events.server.register", return_value=("dep-new", "key-new")) as register,
        patch("bobi.events.client.EventServerClient") as client,
        patch("bobi.events.drain.drain_loop"),
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        patch.object(pooled, "_client", http),
    ):
        if failure == 200:
            _start_event_subscription("sess", ["inbox/sess"], tmp_path)
        else:
            with pytest.raises(RuntimeError, match="saved deployment and completion cursor retained") as exc:
                _start_event_subscription("sess", ["inbox/sess"], tmp_path)
            assert "secret" not in str(exc.value)
            client.assert_not_called()
            assert {p: p.read_bytes() for p in before} == before
            if failure in (401, 403):
                assert "missing/expired or credentials may be invalid" in caplog.text
            response = 200
            _start_event_subscription("sess", ["inbox/sess"], tmp_path)
        register.assert_not_called()
        mint.assert_not_called()
        if bubble_contents == "valid":
            assert authorize.call_args.kwargs["filter_unauthorized"] is False
        else:
            authorize.assert_not_called()
        assert client.call_args.kwargs["deployment_id"] == "dep-old"
        assert client.call_args.kwargs["cursor_path"] == cursor
        if not endpoint:
            assert ensure.call_args.args[0] == 8080
    assert {p: p.read_bytes() for p in before} == before
    assert bubble.exists() == (bubble_contents is not None)
    assert all(r.method == "PUT" and r.url.path == "/deployments/dep-old/subscriptions" for r in captured)
    assert all(json.loads(r.content) == {"replace": ["inbox/sess"]} for r in captured)
    assert "secret" not in caplog.text


def test_stop_during_initial_subscription_discards_late_client(bobi_install):
    """A completed network call cannot publish a client after stop has won."""
    from bobi.session import Session

    session = Session(name="late-subscription", cwd=str(bobi_install.repo_path))
    subscription = MagicMock()

    def stop_during_registration(*args, **kwargs):
        session.stop()
        return subscription

    with patch("bobi.subagent._start_event_subscription", side_effect=stop_during_registration):
        session._start_subscription()
    assert session._subscription is None
    subscription.stop.assert_called_once()


def test_subscription_retry_caps_backoff_and_honors_stop(bobi_install, monkeypatch):
    from bobi.session import Session

    session = Session(name="retry-backoff", cwd=str(bobi_install.repo_path))
    delays = []

    def wait_or_stop(delay):
        delays.append(delay)
        return len(delays) == 8

    monkeypatch.setattr(session._sub_retry_stop, "wait", wait_or_stop)
    with patch("bobi.subagent._start_event_subscription", side_effect=TimeoutError) as start:
        session._retry_subscription_in_background(["inbox/retry-backoff"])
        session._sub_retry_thread.join(timeout=2)
    assert not session._sub_retry_thread.is_alive()
    assert delays == [2, 4, 8, 16, 32, 60, 60, 60]
    assert start.call_count == 7
    assert all(call.kwargs["register_attempts"] == 1 for call in start.call_args_list)
    assert session._subscription is None
