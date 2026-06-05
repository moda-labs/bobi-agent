"""Integration tests for the local event server.

Starts the event server via ensure_running, sends webhook payloads, and
verifies events are delivered via the WebSocket subscription API.
All state is isolated to the modastack_env temp install.
"""

import json
import os
import signal
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
import websocket


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _post_json(url: str, data: dict, headers: dict | None = None) -> dict:
    payload = json.dumps(data).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=payload, headers=hdrs)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


@pytest.fixture(scope="module")
def event_server(modastack_env):
    """Start the event server on a free port, yield the base URL, then stop it."""
    from modastack.manager.events.event_server import ensure_running

    for attempt in range(3):
        port = _free_port()
        base_url = f"http://localhost:{port}"
        ensure_running(port, repo_path=modastack_env.repo_path)

        deadline = time.monotonic() + 10
        started = False
        while time.monotonic() < deadline:
            try:
                data = _get_json(f"{base_url}/health")
                if data.get("status") == "ok":
                    started = True
                    break
            except Exception:
                time.sleep(0.3)

        if started:
            break

        pid_file = modastack_env.state_dir / "event-server.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
            pid_file.unlink(missing_ok=True)
    else:
        raise RuntimeError("Event server failed to start after 3 attempts")

    yield base_url, port

    pid_file = modastack_env.state_dir / "event-server.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass
        pid_file.unlink(missing_ok=True)


@pytest.fixture
def deployment(event_server):
    """Register a deployment and return (base_url, deployment_id, api_key)."""
    base_url, _ = event_server
    result = _post_json(f"{base_url}/deployments", {
        "name": "test-deploy",
        "subscriptions": ["test-org/test-repo", "linear:TEST", "slack:T_TEST"],
    })
    return base_url, result["deployment_id"], result["api_key"]


class TestEventServerLifecycle:

    def test_health_check(self, event_server):
        base_url, _ = event_server
        data = _get_json(f"{base_url}/health")
        assert data["status"] == "ok"
        assert data["mode"] == "local"

    def test_register_deployment(self, event_server):
        base_url, _ = event_server
        result = _post_json(f"{base_url}/deployments", {
            "name": "lifecycle-test",
            "subscriptions": ["test-org/test-repo"],
        })
        assert "deployment_id" in result
        assert "api_key" in result
        assert result["api_key"].startswith("moda_")

    def test_health_shows_deployment_count(self, event_server):
        base_url, _ = event_server
        _post_json(f"{base_url}/deployments", {
            "name": "count-test",
            "subscriptions": ["some-org/some-repo"],
        })
        data = _get_json(f"{base_url}/health")
        assert data["deployments"] >= 1


class TestGitHubWebhook:

    def test_github_issue_event_delivered(self, deployment):
        base_url, dep_id, api_key = deployment
        events = _send_and_drain(base_url, dep_id, api_key, lambda: _post_json(
            f"{base_url}/webhooks/github",
            {"action": "opened",
             "issue": {"number": 42, "title": "Test issue", "state": "open",
                       "user": {"login": "testuser"}},
             "repository": {"full_name": "test-org/test-repo"}},
            headers={"x-github-event": "issues", "x-github-delivery": "test-001"},
        ))
        github_events = [e for e in events if e.get("source") == "github"]
        assert len(github_events) >= 1
        assert github_events[0]["type"] == "github.issues"
        assert github_events[0]["repo"] == "test-org/test-repo"

    def test_github_pr_event_delivered(self, deployment):
        base_url, dep_id, api_key = deployment
        events = _send_and_drain(base_url, dep_id, api_key, lambda: _post_json(
            f"{base_url}/webhooks/github",
            {"action": "closed",
             "pull_request": {"number": 10, "title": "Fix auth", "state": "closed",
                              "merged": True, "user": {"login": "testuser"}},
             "repository": {"full_name": "test-org/test-repo"}},
            headers={"x-github-event": "pull_request", "x-github-delivery": "test-002"},
        ))
        pr_events = [e for e in events if e.get("type") == "github.pull_request"]
        assert len(pr_events) >= 1


class TestLinearWebhook:

    def test_linear_issue_event_delivered(self, deployment):
        base_url, dep_id, api_key = deployment
        events = _send_and_drain(base_url, dep_id, api_key, lambda: _post_json(
            f"{base_url}/webhooks/linear",
            {"action": "update", "type": "Issue",
             "data": {"id": "abc-123", "title": "Add caching layer",
                      "state": {"name": "In Progress"},
                      "team": {"key": "TEST"}}},
        ))
        linear_events = [e for e in events if e.get("source") == "linear"]
        assert len(linear_events) >= 1
        assert linear_events[0]["type"] == "linear.Issue.update"
        assert linear_events[0]["team_key"] == "TEST"


class TestSlackWebhook:

    def test_slack_url_verification(self, event_server):
        base_url, _ = event_server
        payload = json.dumps({
            "type": "url_verification",
            "challenge": "test-challenge-token",
        }).encode()
        req = urllib.request.Request(
            f"{base_url}/webhooks/slack",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        assert data["challenge"] == "test-challenge-token"

    def test_slack_dm_event_delivered(self, deployment):
        base_url, dep_id, api_key = deployment
        events = _send_and_drain(base_url, dep_id, api_key, lambda: _post_json(
            f"{base_url}/webhooks/slack",
            {"type": "event_callback", "team_id": "T_TEST",
             "event": {"type": "message", "user": "U_ZACH",
                       "channel": "D_DM", "channel_type": "im",
                       "text": "What's the deploy status?",
                       "ts": "1700000000.000001"}},
        ))
        slack_events = [e for e in events if e.get("source") == "slack"]
        assert len(slack_events) >= 1
        assert slack_events[0]["type"] == "slack.dm"
        assert slack_events[0]["workspace"] == "T_TEST"

    def test_slack_mention_event_delivered(self, deployment):
        base_url, dep_id, api_key = deployment
        events = _send_and_drain(base_url, dep_id, api_key, lambda: _post_json(
            f"{base_url}/webhooks/slack",
            {"type": "event_callback", "team_id": "T_TEST",
             "event": {"type": "app_mention", "user": "U_ZACH",
                       "channel": "C_ENG", "channel_type": "channel",
                       "text": "<@U_BOT> check deploy",
                       "ts": "1700000001.000001"}},
        ))
        mention_events = [e for e in events if e.get("type") == "slack.mention"]
        assert len(mention_events) >= 1


class TestWebSocketDrain:

    def test_multiple_events_ordered(self, deployment):
        base_url, dep_id, api_key = deployment

        def _send_batch():
            for i in range(3):
                _post_json(f"{base_url}/webhooks/github",
                    {"action": "opened",
                     "issue": {"number": 200 + i, "title": f"Issue {i}",
                               "state": "open", "user": {"login": "test"}},
                     "repository": {"full_name": "test-org/test-repo"}},
                    headers={"x-github-event": "issues",
                             "x-github-delivery": f"order-{i}"},
                )

        events = _send_and_drain(base_url, dep_id, api_key, _send_batch)
        seqs = [e["seq"] for e in events if "seq" in e]
        assert len(seqs) >= 3
        assert seqs == sorted(seqs)

    def test_replay_after_disconnect(self, deployment):
        """Post events, connect with last_seen=0, reconnect with last_seen > 0 to get replay."""
        base_url, dep_id, api_key = deployment

        # First: send an event while WS is connected (to populate the buffer)
        events = _send_and_drain(base_url, dep_id, api_key, lambda: _post_json(
            f"{base_url}/webhooks/github",
            {"action": "opened",
             "issue": {"number": 300, "title": "Replay test", "state": "open",
                       "user": {"login": "test"}},
             "repository": {"full_name": "test-org/test-repo"}},
            headers={"x-github-event": "issues", "x-github-delivery": "replay-001"},
        ))
        assert len(events) >= 1

        # Second: reconnect with last_seen=0 and request replay
        # Server replays events with seq > last_seen when last_seen > 0
        # Since we set last_seen=0, it won't replay (server only replays when last_seen > 0)
        # So use last_seen=events[0]["seq"] - 1 to get replay
        first_seq = events[0].get("seq", 1)
        if first_seq > 1:
            replayed = _drain_ws(base_url, dep_id, api_key, timeout=2, last_seen=first_seq - 1)
            assert len(replayed) >= 1


class TestEventServerCLI:

    def test_status_shows_running(self, event_server, cli_run):
        _, port = event_server
        base_url = f"http://localhost:{port}"
        data = _get_json(f"{base_url}/health")
        assert data["status"] == "ok"

    def test_stop_and_restart(self, modastack_env):
        from modastack.manager.events.event_server import ensure_running

        port = _free_port()
        base_url = f"http://localhost:{port}"
        ensure_running(port, repo_path=modastack_env.repo_path)

        data = _get_json(f"{base_url}/health")
        assert data["status"] == "ok"

        pid_file = modastack_env.state_dir / "event-server.pid"
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        time.sleep(1)

        with pytest.raises(Exception):
            _get_json(f"{base_url}/health")

        ensure_running(port, repo_path=modastack_env.repo_path)
        deadline = time.monotonic() + 10
        running = False
        while time.monotonic() < deadline:
            try:
                data = _get_json(f"{base_url}/health")
                if data.get("status") == "ok":
                    running = True
                    break
            except Exception:
                time.sleep(0.3)
        assert running

        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
            pid_file.unlink(missing_ok=True)


def _send_and_drain(base_url: str, dep_id: str, api_key: str,
                    send_fn, timeout: float = 5) -> list[dict]:
    """Connect WS first, then send webhook, then drain events."""
    ws_url = base_url.replace("http://", "ws://")
    url = f"{ws_url}/deployments/{dep_id}/subscribe?last_seen=0"

    events = []
    ready = threading.Event()

    def _ws_thread():
        ws = websocket.create_connection(
            url,
            header=[f"Authorization: Bearer {api_key}"],
            timeout=timeout,
        )
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                ws.settimeout(max(0.1, deadline - time.monotonic()))
                try:
                    raw = ws.recv()
                    msg = json.loads(raw)
                    if msg.get("type") == "connected":
                        ready.set()
                    elif msg.get("type") in ("event", "replay"):
                        events.append(msg["data"])
                except websocket.WebSocketTimeoutException:
                    break
                except Exception:
                    break
        finally:
            ws.close()

    t = threading.Thread(target=_ws_thread, daemon=True)
    t.start()

    ready.wait(timeout=5)
    time.sleep(0.1)

    send_fn()

    t.join(timeout=timeout)
    return events


def _drain_ws(base_url: str, dep_id: str, api_key: str,
              timeout: float = 3, last_seen: int = 0) -> list[dict]:
    """Connect via WebSocket after events are buffered, collect replays."""
    ws_url = base_url.replace("http://", "ws://")
    url = f"{ws_url}/deployments/{dep_id}/subscribe?last_seen={last_seen}"

    events = []
    ws = websocket.create_connection(
        url,
        header=[f"Authorization: Bearer {api_key}"],
        timeout=timeout,
    )
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ws.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                raw = ws.recv()
                msg = json.loads(raw)
                if msg.get("type") in ("event", "replay"):
                    events.append(msg["data"])
                elif msg.get("type") == "connected":
                    pass
            except websocket.WebSocketTimeoutException:
                break
            except Exception:
                break
    finally:
        ws.close()
    return events
