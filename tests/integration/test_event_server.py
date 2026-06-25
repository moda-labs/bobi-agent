"""Integration tests for the event server.

Parametrized over TWO backends:
1. **local** — the Node.js local.ts server started via ``ensure_running``
2. **wrangler** — the Cloudflare Worker via ``wrangler dev`` (local mode,
   no CF credentials needed)

Starts the event server, sends webhook payloads, and verifies events are
delivered via the WebSocket subscription API.  All state is isolated to
the modastack_env temp install.
"""

import json
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import websocket

PACKAGE_ROOT = Path(__file__).parent.parent.parent


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


def _post_event_signed(base_url: str, topic: str, body_dict: dict,
                       bubble_id: str, bubble_key: str) -> dict:
    """Publish to /events/{topic} signed with a bubble key (generic publishes
    now require a bubble signature — namespacing is not authentication)."""
    from modastack.events.signing import serialize_body, sign_headers

    body = serialize_body(body_dict)
    path = f"/events/{topic}"
    headers = {"Content-Type": "application/json"}
    headers.update(sign_headers(bubble_id, bubble_key, "POST", path, body))
    req = urllib.request.Request(base_url + path, data=body.encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _has_wrangler() -> bool:
    """Check whether wrangler is available (npm ci'd in event-server/)."""
    wrangler = PACKAGE_ROOT / "event-server" / "node_modules" / ".bin" / "wrangler"
    return wrangler.exists()


def _event_server_backends():
    """Return the list of backend IDs to parametrize over."""
    backends = ["local"]
    # wrangler backend is opt-in via the MODASTACK_TEST_WRANGLER env var or
    # when wrangler is already installed in event-server/node_modules.
    if os.environ.get("MODASTACK_TEST_WRANGLER") == "1" or _has_wrangler():
        backends.append("wrangler")
    return backends


def _wait_healthy(base_url: str, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = _get_json(f"{base_url}/health")
            if data.get("status") == "ok":
                return True
        except Exception:
            time.sleep(0.3)
    return False


def _start_local_server(modastack_env):
    """Start the local Node.js event server, return (base_url, port, cleanup)."""
    from modastack.events.server import ensure_running

    for attempt in range(3):
        port = _free_port()
        base_url = f"http://localhost:{port}"
        ensure_running(port, project_path=modastack_env.project_path)

        if _wait_healthy(base_url, timeout=10):
            break

        pid_file = modastack_env.state_dir / "event-server.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
            pid_file.unlink(missing_ok=True)
    else:
        raise RuntimeError("Local event server failed to start after 3 attempts")

    def _cleanup():
        pid_file = modastack_env.state_dir / "event-server.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
            pid_file.unlink(missing_ok=True)

    return base_url, port, _cleanup


def _start_wrangler_server():
    """Start wrangler dev on a free port, return (base_url, port, cleanup)."""
    import fcntl

    es_dir = PACKAGE_ROOT / "event-server"

    # Ensure node_modules exist
    if not (es_dir / "node_modules").exists():
        subprocess.run(
            ["npm", "ci", "--no-audit", "--no-fund"],
            cwd=str(es_dir), check=True, capture_output=True, timeout=120,
        )

    lock_path = es_dir / ".dev.vars.test.lock"
    lock_file = open(lock_path, "w")
    lock_deadline = time.monotonic() + 180
    while True:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= lock_deadline:
                lock_file.close()
                raise RuntimeError("timed out waiting for wrangler .dev.vars test lock")
            time.sleep(0.2)

    dev_vars_path = es_dir / ".dev.vars"
    original_dev_vars = dev_vars_path.read_text() if dev_vars_path.exists() else None
    dev_vars_lines = (original_dev_vars or "").splitlines()
    secret_line = "INTERNAL_DO_SECRET=test-internal-secret"
    for idx, line in enumerate(dev_vars_lines):
        if line.startswith("INTERNAL_DO_SECRET="):
            dev_vars_lines[idx] = secret_line
            break
    else:
        dev_vars_lines.append(secret_line)
    dev_vars_path.write_text("\n".join(dev_vars_lines) + "\n")

    restored_dev_vars = False

    def _restore_dev_vars():
        nonlocal restored_dev_vars
        if restored_dev_vars:
            return
        if original_dev_vars is None:
            dev_vars_path.unlink(missing_ok=True)
        else:
            dev_vars_path.write_text(original_dev_vars)
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
        lock_path.unlink(missing_ok=True)
        restored_dev_vars = True

    log_file = None
    try:
        port = _free_port()
        base_url = f"http://localhost:{port}"

        log_path = es_dir / f".wrangler-test-{port}.log"
        log_file = open(log_path, "w")

        proc = subprocess.Popen(
            [
                str(es_dir / "node_modules" / ".bin" / "wrangler"),
                "dev",
                f"--port={port}",
            ],
            cwd=str(es_dir),
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )

        if not _wait_healthy(base_url, timeout=30):
            log_file.close()
            try:
                log_text = log_path.read_text()
            except Exception:
                log_text = "(unreadable)"
            proc.kill()
            proc.wait()
            _restore_dev_vars()
            raise RuntimeError(
                f"wrangler dev failed to start on port {port}.\nLog:\n{log_text}"
            )
    except Exception:
        if log_file and not log_file.closed:
            log_file.close()
        _restore_dev_vars()
        raise

    def _cleanup():
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
            proc.kill()
            proc.wait()
        log_file.close()
        log_path.unlink(missing_ok=True)
        _restore_dev_vars()

    return base_url, port, _cleanup


@pytest.fixture(scope="module", params=_event_server_backends())
def event_server(request, modastack_env):
    """Start the event server on a free port, yield (base_url, port, backend), then stop it."""
    backend = request.param

    if backend == "local":
        base_url, port, cleanup = _start_local_server(modastack_env)
    else:
        base_url, port, cleanup = _start_wrangler_server()

    yield base_url, port, backend

    cleanup()


@pytest.fixture(autouse=True)
def _skip_local_only_on_wrangler(request, event_server):
    """Skip tests marked ``local_only`` when running against the wrangler backend."""
    _base, _port, backend = event_server
    if backend != "local" and request.node.get_closest_marker("local_only"):
        pytest.skip(f"local-only test (backend={backend})")


@pytest.fixture
def deployment(event_server):
    """Register a deployment and return (base_url, deployment_id, api_key)."""
    base_url, _port, _backend = event_server
    result = _post_json(f"{base_url}/deployments", {
        "name": "test-deploy",
        "subscriptions": ["github:test-org/test-repo", "linear:TEST", "slack:T_TEST"],
    })
    return base_url, result["deployment_id"], result["api_key"]


class TestEventServerLifecycle:

    def test_health_check(self, event_server):
        base_url, _port, backend = event_server
        data = _get_json(f"{base_url}/health")
        assert data["status"] == "ok"
        if backend == "local":
            assert data["mode"] == "local"

    def test_register_deployment(self, event_server):
        base_url, *_ = event_server
        result = _post_json(f"{base_url}/deployments", {
            "name": "lifecycle-test",
            "subscriptions": ["github:test-org/test-repo"],
        })
        assert "deployment_id" in result
        assert "api_key" in result
        assert result["api_key"].startswith("moda_")

    @pytest.mark.local_only
    def test_health_shows_deployment_count(self, event_server):
        base_url, *_ = event_server
        _post_json(f"{base_url}/deployments", {
            "name": "count-test",
            "subscriptions": ["github:some-org/some-repo"],
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
        assert github_events[0]["v"] == 2
        assert github_events[0]["topics"] == ["github:test-org/test-repo"]
        assert github_events[0]["delivery"] == "bulk"
        assert "test-org/test-repo" in github_events[0]["text"]

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
        assert linear_events[0]["v"] == 2
        assert linear_events[0]["topics"] == ["linear:TEST"]


class TestSlackWebhook:

    def test_slack_url_verification(self, event_server):
        base_url, *_ = event_server
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
        assert slack_events[0]["v"] == 2
        assert slack_events[0]["topics"] == ["slack:T_TEST"]
        assert slack_events[0]["delivery"] == "chat"

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


class TestSlackSelfReplyLoop:
    """Regression: register workspace → bot-authored payload → zero deliveries.

    PR #209 fixed a self-reply loop where the agent's own Slack messages were
    re-ingested as inbound events.  The event server filters messages whose
    ``event.bot_id`` matches the registered workspace ``bot_id``.  These tests
    verify the full HTTP path: workspace registration, webhook ingestion, and
    WebSocket delivery.
    """

    @pytest.fixture
    def deployment_with_workspace(self, event_server):
        """Register a deployment + workspace with a known bot_id."""
        base_url, *_ = event_server
        dep = _post_json(f"{base_url}/deployments", {
            "name": "self-loop-test",
            "subscriptions": ["slack:T_SELF"],
        })
        dep_id, api_key = dep["deployment_id"], dep["api_key"]
        # Register workspace with explicit bot_id (skips auth.test)
        ws = _post_json(f"{base_url}/slack/workspaces", {
            "workspace_id": "T_SELF",
            "bot_token": "xoxb-fake-for-test",
            "bot_id": "B_SELF",
        })
        assert ws["bot_id"] == "B_SELF"
        return base_url, dep_id, api_key

    def test_bot_own_message_not_delivered(self, deployment_with_workspace):
        """A message authored by the registered bot must be silently dropped."""
        base_url, dep_id, api_key = deployment_with_workspace
        events = _send_and_drain(base_url, dep_id, api_key, lambda: _post_json(
            f"{base_url}/webhooks/slack",
            {"type": "event_callback", "team_id": "T_SELF",
             "event": {"type": "app_mention", "user": "U_SELF",
                       "bot_id": "B_SELF",
                       "channel": "C_ENG", "channel_type": "channel",
                       "text": "I just replied to a thread",
                       "ts": "1700000010.000001"}},
        ), timeout=3)
        slack_events = [e for e in events if e.get("source") == "slack"]
        assert len(slack_events) == 0, (
            f"Bot's own message was delivered — self-reply loop not prevented: {slack_events}"
        )

    def test_other_bot_message_still_delivered(self, deployment_with_workspace):
        """Messages from a different bot must pass through normally."""
        base_url, dep_id, api_key = deployment_with_workspace
        events = _send_and_drain(base_url, dep_id, api_key, lambda: _post_json(
            f"{base_url}/webhooks/slack",
            {"type": "event_callback", "team_id": "T_SELF",
             "event": {"type": "app_mention", "user": "U_OTHER",
                       "bot_id": "B_OTHER",
                       "channel": "C_ENG", "channel_type": "channel",
                       "text": "message from another bot",
                       "ts": "1700000011.000001"}},
        ))
        slack_events = [e for e in events if e.get("source") == "slack"]
        assert len(slack_events) >= 1, "Other bot's message was incorrectly filtered"

    def test_human_message_still_delivered(self, deployment_with_workspace):
        """Human messages (no bot_id) must pass through normally."""
        base_url, dep_id, api_key = deployment_with_workspace
        events = _send_and_drain(base_url, dep_id, api_key, lambda: _post_json(
            f"{base_url}/webhooks/slack",
            {"type": "event_callback", "team_id": "T_SELF",
             "event": {"type": "message", "user": "U_HUMAN",
                       "channel": "D_DM", "channel_type": "im",
                       "text": "hello from a human",
                       "ts": "1700000012.000001"}},
        ))
        slack_events = [e for e in events if e.get("source") == "slack"]
        assert len(slack_events) >= 1, "Human message was incorrectly filtered"


class TestMonitorEventDelivery:
    """Monitor findings travel the unified path: the scheduler publishes every
    flavor's finding via ``events.publish.post_event`` (POST /events/<type>
    with the source in the body), and the event server routes it onto BOTH the
    bare-type topic and the source-qualified topic — so subscriptions written
    either way (``support.email`` or ``monitor/support.email``) match.

    History: #235 found that exact-string matching delivered only on the bare
    type, so the manager's raw ``monitor/<type>`` subscriptions never matched
    and findings vanished with ``delivered_to: 0``. The topic contract now
    carries the source; ``monitor_subscription_keys`` (which subscribes to
    both forms) is kept for skew tolerance against older deployed servers.
    """

    def test_monitor_finding_reaches_subscriber(self, event_server):
        from modastack.events.subscriptions import monitor_subscription_keys

        base_url, *_ = event_server
        subs = monitor_subscription_keys(["monitor/support.email"])
        dep = _post_json(f"{base_url}/deployments", {
            "name": "monitor-delivery-test",
            "subscriptions": subs,
        })
        dep_id, api_key = dep["deployment_id"], dep["api_key"]
        bub_id, bub_key = dep["bubble_id"], dep["bubble_key"]

        # The scheduler's publish path: post_event("monitor/support.email",
        # data) POSTs to /events/support.email with {"source": "monitor"},
        # signed with the instance's bubble key.
        events = _send_and_drain(base_url, dep_id, api_key, lambda: _post_event_signed(
            base_url, "support.email",
            {"source": "monitor",
             "payload": {"summary": "new real-customer support email"}},
            bub_id, bub_key,
        ))

        monitor_events = [e for e in events if e.get("source") == "monitor"]
        assert len(monitor_events) >= 1, (
            "Monitor finding was not delivered — the subscription topic does "
            "not match the topic the event server routes /events/support.email "
            f"onto. Subscriptions were {subs}."
        )
        assert monitor_events[0]["type"] == "support.email"
        assert monitor_events[0]["payload"]["summary"] == (
            "new real-customer support email"
        )

    def test_raw_event_string_subscription_matches(self, event_server):
        """The topic contract: a subscription written as the full
        ``monitor/<type>`` event string — exactly what a monitor's ``event:``
        field says — matches, because the server routes path-topic events onto
        the source-qualified topic too. (Before this contract, #235, it
        silently never matched.)"""
        base_url, *_ = event_server
        dep = _post_json(f"{base_url}/deployments", {
            "name": "raw-event-sub-test",
            "subscriptions": ["monitor/support.email"],
        })
        dep_id, api_key = dep["deployment_id"], dep["api_key"]
        bub_id, bub_key = dep["bubble_id"], dep["bubble_key"]

        events = _send_and_drain(base_url, dep_id, api_key, lambda: _post_event_signed(
            base_url, "support.email",
            {"source": "monitor", "payload": {"summary": "x"}},
            bub_id, bub_key,
        ))

        monitor_events = [e for e in events if e.get("source") == "monitor"]
        assert len(monitor_events) == 1, (
            "A 'monitor/support.email' subscription must match an event "
            "POSTed to /events/support.email with source=monitor — the "
            "source-qualified topic is part of the routing contract now."
        )

    def test_dual_subscription_delivers_exactly_once(self, event_server):
        """A deployment subscribed to BOTH forms (what
        monitor_subscription_keys produces) gets one copy, not two — deliver()
        dedupes deployments across matched topics."""
        from modastack.events.subscriptions import monitor_subscription_keys

        base_url, *_ = event_server
        subs = monitor_subscription_keys(["monitor/support.email"])
        assert len(subs) == 2  # both forms — the premise of this test
        dep = _post_json(f"{base_url}/deployments", {
            "name": "dual-sub-test",
            "subscriptions": subs,
        })
        dep_id, api_key = dep["deployment_id"], dep["api_key"]
        bub_id, bub_key = dep["bubble_id"], dep["bubble_key"]

        events = _send_and_drain(base_url, dep_id, api_key, lambda: _post_event_signed(
            base_url, "support.email",
            {"source": "monitor", "payload": {"summary": "once"}},
            bub_id, bub_key,
        ))

        monitor_events = [e for e in events if e.get("source") == "monitor"]
        assert len(monitor_events) == 1, (
            f"Expected exactly one delivery, got {len(monitor_events)} — "
            "matching both topic forms must not double-deliver."
        )


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


class TestHeartbeatLiveness:
    """The real server must answer the app-level heartbeat so the client can
    tell a live receive path from a deaf-but-connected one (#425)."""

    def test_real_client_becomes_and_stays_live(self, deployment):
        from modastack.events.client import EventServerClient

        base_url, dep_id, api_key = deployment

        client = EventServerClient(
            server_url=base_url,
            deployment_id=dep_id,
            api_key=api_key,
            cursor_path=None,
        )
        # Fast cadence so the round-trip is observable within the test budget.
        client._HEARTBEAT_INTERVAL_S = 0.2
        client._HEARTBEAT_TIMEOUT_S = 1.5
        client.start()
        try:
            assert client.wait_connected(timeout=5.0)

            # A real ping must round-trip to a real pong → liveness goes True.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not client.is_live():
                time.sleep(0.05)
            assert client.is_live(), "real server never answered the heartbeat ping"

            # And stay live across several heartbeat windows — a healthy server
            # must not be mistaken for deaf.
            time.sleep(client._HEARTBEAT_TIMEOUT_S * 2)
            assert client.is_live()
            assert client._deaf_reconnects == 0
        finally:
            client.stop()


@pytest.mark.local_only
class TestEventServerCLI:

    def test_status_shows_running(self, event_server, cli_run):
        _, port, _backend = event_server
        base_url = f"http://localhost:{port}"
        data = _get_json(f"{base_url}/health")
        assert data["status"] == "ok"

    def test_stop_and_restart(self, modastack_env):
        from modastack.events.server import ensure_running

        port = _free_port()
        base_url = f"http://localhost:{port}"
        ensure_running(port, project_path=modastack_env.project_path)

        data = _get_json(f"{base_url}/health")
        assert data["status"] == "ok"

        pid_file = modastack_env.state_dir / "event-server.pid"
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        time.sleep(1)

        with pytest.raises(Exception):
            _get_json(f"{base_url}/health")

        ensure_running(port, project_path=modastack_env.project_path)
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


def _register(base_url: str, name: str, subs: list[str],
              bubble_id: str = "", bubble_key: str = "") -> dict:
    """Register a deployment. MINT (unsigned) when no bubble_key; JOIN (signed)
    otherwise. Returns the full server response."""
    from modastack.events.signing import serialize_body, sign_headers

    body = serialize_body({"name": name, "subscriptions": subs})
    headers = {"Content-Type": "application/json"}
    if bubble_key:
        headers.update(sign_headers(bubble_id, bubble_key, "POST", "/deployments", body))
    req = urllib.request.Request(f"{base_url}/deployments", data=body.encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _live_subscriber(base_url: str, dep_id: str, api_key: str, timeout: float = 4):
    """Open a live WS subscriber. Returns (events, ready_event, thread); events
    fills as the deployment receives them. Connect several before publishing to
    observe which bubbles a single publish reaches."""
    ws_url = base_url.replace("http://", "ws://")
    url = f"{ws_url}/deployments/{dep_id}/subscribe?last_seen=0"
    events: list[dict] = []
    ready = threading.Event()

    def _thread():
        try:
            ws = websocket.create_connection(
                url, header=[f"Authorization: Bearer {api_key}"], timeout=timeout)
        except Exception:
            return
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                ws.settimeout(max(0.1, deadline - time.monotonic()))
                try:
                    msg = json.loads(ws.recv())
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

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    return events, ready, t


class TestBubbleIsolation:
    """Bubbles minted per `modastack start` must NOT overlap on a shared event
    server — whether many VMs hit one server or several local instances from
    different dirs do. Exercised end to end against the real local.ts server.
    """

    def test_mint_returns_bubble_key_once(self, event_server):
        base_url, *_ = event_server
        dep = _register(base_url, "m", ["inbox/m"])
        assert dep["bubble_id"].startswith("bub_")
        assert dep["bubble_key"].startswith("bkey_")

    def test_same_session_name_no_cross_delivery(self, event_server):
        """Two instances (two bubbles), same session name + same topic: a publish
        in bubble A reaches A's subscriber and NEVER bubble B's. This is the
        #270 Part A acceptance: no cross-delivery in a shared event server."""
        base_url, *_ = event_server
        a = _register(base_url, "manager", ["inbox/manager"])
        b = _register(base_url, "manager", ["inbox/manager"])
        assert a["bubble_id"] != b["bubble_id"]

        evA, readyA, tA = _live_subscriber(base_url, a["deployment_id"], a["api_key"])
        evB, readyB, tB = _live_subscriber(base_url, b["deployment_id"], b["api_key"])
        assert readyA.wait(5) and readyB.wait(5)
        time.sleep(0.1)

        _post_event_signed(base_url, "inbox/manager",
                           {"source": "inbox", "payload": {"text": "for A only"}},
                           a["bubble_id"], a["bubble_key"])
        tA.join(timeout=5)
        tB.join(timeout=5)

        a_texts = [e["payload"].get("text") for e in evA if e.get("source") == "inbox"]
        assert "for A only" in a_texts, f"bubble A did not receive its own message: {evA}"
        b_inbox = [e for e in evB if e.get("source") == "inbox"]
        assert b_inbox == [], f"bubble B received bubble A's message — isolation broken: {b_inbox}"

    def test_join_shares_bubble_and_delivers(self, event_server):
        """A second session JOINing a bubble (signed) shares it and receives its
        events — the within-instance round trip every agent relies on."""
        base_url, *_ = event_server
        a = _register(base_url, "worker1", ["inbox/worker1"])
        b = _register(base_url, "worker2", ["inbox/worker2"], a["bubble_id"], a["bubble_key"])
        assert b["bubble_id"] == a["bubble_id"]
        assert "bubble_key" not in b  # never returned on join

        evB, readyB, tB = _live_subscriber(base_url, b["deployment_id"], b["api_key"])
        assert readyB.wait(5)
        time.sleep(0.1)
        _post_event_signed(base_url, "inbox/worker2",
                           {"source": "inbox", "payload": {"text": "hi w2"}},
                           a["bubble_id"], a["bubble_key"])
        tB.join(timeout=5)
        texts = [e["payload"].get("text") for e in evB if e.get("source") == "inbox"]
        assert "hi w2" in texts

    def test_unsigned_publish_rejected(self, event_server):
        base_url, *_ = event_server
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post_json(f"{base_url}/events/inbox/x",
                       {"source": "inbox", "payload": {"text": "nope"}})
        assert ei.value.code == 403

    def test_publish_with_wrong_bubble_key_rejected(self, event_server):
        base_url, *_ = event_server
        a = _register(base_url, "m", ["inbox/m"])
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post_event_signed(base_url, "inbox/m",
                               {"source": "inbox", "payload": {"text": "x"}},
                               a["bubble_id"], "bkey_wrong")
        assert ei.value.code == 403

    def test_join_with_wrong_key_rejected(self, event_server):
        base_url, *_ = event_server
        a = _register(base_url, "m", ["inbox/m"])
        with pytest.raises(urllib.error.HTTPError) as ei:
            _register(base_url, "intruder", ["inbox/x"], a["bubble_id"], "bkey_wrong")
        assert ei.value.code == 403

    def test_webhook_fans_out_across_bubbles(self, event_server):
        """ACCEPTED v1 behavior (#239): inbound webhooks are GLOBAL — they reach
        subscribers in ANY bubble. This is the documented cross-tenant hole that
        keeps Slack/GitHub working pre-#239. Locked as a test so a future change
        that closes it is a conscious decision, not a silent break."""
        base_url, *_ = event_server
        a = _register(base_url, "a", ["github:shared/repo"])
        b = _register(base_url, "b", ["github:shared/repo"])
        assert a["bubble_id"] != b["bubble_id"]

        evA, rA, tA = _live_subscriber(base_url, a["deployment_id"], a["api_key"])
        evB, rB, tB = _live_subscriber(base_url, b["deployment_id"], b["api_key"])
        assert rA.wait(5) and rB.wait(5)
        time.sleep(0.1)

        _post_json(f"{base_url}/webhooks/github",
                   {"action": "opened",
                    "issue": {"number": 1, "title": "x", "state": "open",
                              "user": {"login": "u"}},
                    "repository": {"full_name": "shared/repo"}},
                   headers={"x-github-event": "issues", "x-github-delivery": "iso-1"})
        tA.join(timeout=5)
        tB.join(timeout=5)

        assert any(e.get("source") == "github" for e in evA)
        assert any(e.get("source") == "github" for e in evB), (
            "webhook did not fan out to the second bubble — the v1 global "
            "behavior changed; update #239 expectations deliberately"
        )


@pytest.mark.local_only
class TestSchedulerEndToEnd:
    """The unified monitor path, end to end with a REAL scheduler and a REAL
    event server: a native check's condition is published by the scheduler
    through events.publish.post_event and delivered to a subscriber registered
    with the manager's monitor-subscription keys. This is the wiring that the
    old in-process inject shortcut never exercised — and why #235 stayed
    hidden until a description-only monitor hit it in production.
    """

    def test_native_check_finding_delivered_to_subscriber(
            self, event_server, modastack_env):
        from modastack.events import publish as publish_mod
        from modastack.events.server import ensure_bubble
        from modastack.events.subscriptions import monitor_subscription_keys
        from modastack.monitors.schema import Condition, Monitor
        from modastack.monitors.scheduler import MonitorScheduler

        base_url, *_ = event_server

        # Point the (session-scoped) project at this test server; restore
        # after, and drop publish's per-project URL cache both ways.
        agent_yaml = modastack_env.project_path / ".modastack" / "agent.yaml"
        original = agent_yaml.read_text()
        agent_yaml.write_text(original + f"\nevent_server_url: {base_url}\n")
        publish_mod._es_url_cache.clear()
        try:
            m = Monitor(name="pr-conflict-check",
                        event="monitor/pr.conflict_detected",
                        check="conflicts", interval="1m")

            class FakeRegistry:
                def effective_monitors(self):
                    return [m]

                def projects_for(self, _m):
                    return []

            sched = MonitorScheduler(
                state_path=modastack_env.state_dir / "e2e_monitor_state.json",
                registry_loader=lambda: FakeRegistry(),
                project_path=modastack_env.project_path,
            )
            sched._checks["conflicts"] = lambda mon, repos: [
                Condition(key="repo#7", data={"pr_number": 7, "repo": "org/repo"})
            ]

            # Mint the project's bubble; the subscriber JOINs it and the
            # scheduler's post_event signs with the same bubble.json — both must
            # share a bubble for the finding to be delivered.
            bubble = ensure_bubble(base_url, modastack_env.project_path)
            subs = monitor_subscription_keys(["monitor/pr.conflict_detected"])
            dep = _register(base_url, "scheduler-e2e-test", subs,
                            bubble["bubble_id"], bubble["bubble_key"])
            dep_id, api_key = dep["deployment_id"], dep["api_key"]

            events = _send_and_drain(base_url, dep_id, api_key, sched.tick)

            monitor_events = [e for e in events if e.get("source") == "monitor"]
            assert len(monitor_events) == 1, (
                "Scheduler tick did not deliver the native-check finding "
                f"through the event server. Subscriptions: {subs}; "
                f"received: {events}"
            )
            ev = monitor_events[0]
            assert ev["type"] == "pr.conflict_detected"
            assert ev["payload"]["monitor"] == "pr-conflict-check"
            assert ev["payload"]["pr_number"] == 7
            # Transactional dedup: published -> recorded active.
            assert sched.state["pr-conflict-check"]["active"] == ["repo#7"]
        finally:
            agent_yaml.write_text(original)
            publish_mod._es_url_cache.clear()


@pytest.mark.local_only
class TestBindAddress:
    """The event server must default to loopback (127.0.0.1) and only widen
    the listen address when MODASTACK_ES_BIND is set explicitly.  Bind
    address must be independent of any auth setting (#241)."""

    @staticmethod
    def _start_server(modastack_env, port: int, extra_env: dict | None = None):
        """Start an event server with explicit env control, return (proc, log_path)."""
        from modastack.events.server import _find_event_server_dir, _needs_build, _run_npm

        es_dir = _find_event_server_dir()
        if not (es_dir / "node_modules").exists():
            _run_npm(["npm", "install", "--no-audit", "--no-fund"], es_dir)
        if _needs_build(es_dir):
            _run_npm(["npm", "run", "build:local"], es_dir)

        log_path = modastack_env.state_dir / f"event-server-bind-{port}.log"

        # Start from a clean env without any inherited MODASTACK_ES_BIND
        env = {k: v for k, v in os.environ.items() if k != "MODASTACK_ES_BIND"}
        env["MODASTACK_ES_PORT"] = str(port)
        if extra_env:
            env.update(extra_env)

        lf = open(log_path, "w")
        proc = subprocess.Popen(
            ["node", str(es_dir / "dist" / "local.js")],
            stdout=lf, stderr=lf,
            env=env, start_new_session=True,
        )
        return proc, log_path, lf

    @staticmethod
    def _wait_healthy(url: str, timeout: float = 10) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = _get_json(f"{url}/health")
                if data.get("status") == "ok":
                    return True
            except Exception:
                pass
            time.sleep(0.3)
        return False

    @staticmethod
    def _stop(proc, lf):
        try:
            os.kill(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            proc.kill()
        lf.close()

    def test_default_binds_loopback(self, modastack_env):
        """Without MODASTACK_ES_BIND, the server binds 127.0.0.1 and logs the
        loopback-only advisory message."""
        port = _free_port()
        proc, log_path, lf = self._start_server(modastack_env, port)
        try:
            assert self._wait_healthy(f"http://127.0.0.1:{port}")

            log_text = log_path.read_text()
            assert f"127.0.0.1:{port}" in log_text
            assert "loopback-only" in log_text
            assert "MODASTACK_ES_BIND" in log_text
        finally:
            self._stop(proc, lf)

    def test_explicit_bind_all_interfaces(self, modastack_env):
        """MODASTACK_ES_BIND=0.0.0.0 widens the listener; the log omits the
        loopback advisory."""
        port = _free_port()
        proc, log_path, lf = self._start_server(
            modastack_env, port, {"MODASTACK_ES_BIND": "0.0.0.0"})
        try:
            assert self._wait_healthy(f"http://127.0.0.1:{port}")

            log_text = log_path.read_text()
            assert f"0.0.0.0:{port}" in log_text
            assert "loopback-only" not in log_text
        finally:
            self._stop(proc, lf)

    def test_bind_decoupled_from_webhook_secret(self, modastack_env):
        """Setting MODASTACK_ES_WEBHOOK_SECRET must not change the bind address.
        Regression guard: an earlier draft coupled auth and bind."""
        port = _free_port()
        proc, log_path, lf = self._start_server(
            modastack_env, port, {"MODASTACK_ES_WEBHOOK_SECRET": "s3cret"})
        try:
            assert self._wait_healthy(f"http://127.0.0.1:{port}")

            log_text = log_path.read_text()
            assert f"127.0.0.1:{port}" in log_text
            assert "loopback-only" in log_text
        finally:
            self._stop(proc, lf)
