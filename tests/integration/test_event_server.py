"""Integration tests for the event server - the protocol suite.

Parametrized over TWO backends:
1. **local** — the Node.js local.ts server started via ``ensure_running``;
   the backend this public repo runs in CI.
2. **wrangler** — a Cloudflare Worker via ``wrangler dev`` (local mode, no
   CF credentials needed). The worker adapter lives in the private deploy
   repo (repo split); point ``BOBI_TEST_ES_DIR`` at a checkout of its
   ``event-server/`` to run this same suite against it - that run is the
   protocol-compatibility gate between the two repos. Without a worker dir
   (no wrangler.jsonc at the target) the backend is skipped.

Starts the event server, sends webhook payloads, and verifies events are
delivered via the WebSocket subscription API.  All state is isolated to
the bobi_env temp install.
"""

import hashlib
import hmac
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import websocket
import yaml

from bobi.runtime_guard import with_mutable_runtime_package

PACKAGE_ROOT = Path(__file__).parent.parent.parent
TEST_GRANTS_SECRET = "bobi-integration-test-grants"


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


def _seed_resource_grants(
    base_url: str,
    bubble_id: str,
    bubble_key: str,
    grants: list[dict],
) -> None:
    from bobi.events.signing import serialize_body, sign_headers

    body = serialize_body({"grants": grants})
    headers = {"Content-Type": "application/json", "x-moda-test-secret": TEST_GRANTS_SECRET}
    headers.update(sign_headers(bubble_id, bubble_key, "POST", "/__test/resource-grants", body))
    req = urllib.request.Request(
        f"{base_url}/__test/resource-grants",
        data=body.encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        json.loads(resp.read())


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _post_event_signed(base_url: str, topic: str, body_dict: dict,
                       bubble_id: str, bubble_key: str) -> dict:
    """Publish to /events/{topic} signed with a bubble key (generic publishes
    now require a bubble signature — namespacing is not authentication)."""
    from bobi.events.signing import serialize_body, sign_headers

    body = serialize_body(body_dict)
    path = f"/events/{topic}"
    headers = {"Content-Type": "application/json"}
    headers.update(sign_headers(bubble_id, bubble_key, "POST", path, body))
    req = urllib.request.Request(base_url + path, data=body.encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _wrangler_es_dir() -> Path:
    """Worker-adapter directory for the wrangler backend.

    Defaults to the in-repo event-server/. The private deploy repo owns the
    Cloudflare Worker adapter (repo split) and points BOBI_TEST_ES_DIR at its
    own event-server/ to run this same suite as the protocol-compatibility
    gate between the pinned public bobi and its worker.
    """
    override = os.environ.get("BOBI_TEST_ES_DIR")
    return Path(override) if override else PACKAGE_ROOT / "event-server"


def _has_wrangler() -> bool:
    """Check whether wrangler is installed and runnable for this platform."""
    wrangler = _wrangler_es_dir() / "node_modules" / ".bin" / "wrangler"
    if not wrangler.exists():
        return False
    try:
        subprocess.run(
            [str(wrangler), "--version"],
            cwd=str(_wrangler_es_dir()),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _node_major() -> int:
    try:
        proc = subprocess.run(
            ["node", "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return 0
    raw = proc.stdout.strip().lstrip("v")
    try:
        return int(raw.split(".", 1)[0])
    except (ValueError, IndexError):
        return 0


def _event_server_backends():
    """Return the list of backend IDs to parametrize over."""
    backends = []
    if _node_major() >= 20:
        backends.append("local")
    # wrangler backend is opt-in via the BOBI_TEST_WRANGLER env var or when
    # wrangler is already installed in the target dir - but only where a
    # worker actually lives (wrangler.jsonc). The public repo has none post
    # repo-split, so without BOBI_TEST_ES_DIR this skips instead of launching
    # `wrangler dev` in a configless directory (stale pre-split node_modules
    # would otherwise hard-fail every wrangler-parametrized test).
    wants_wrangler = os.environ.get("BOBI_TEST_WRANGLER") == "1" or _has_wrangler()
    if wants_wrangler and (_wrangler_es_dir() / "wrangler.jsonc").exists():
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


def _start_local_server(bobi_env):
    """Start the local Node.js event server, return (base_url, port, cleanup)."""
    from bobi.events.server import ensure_running

    for attempt in range(3):
        port = _free_port()
        base_url = f"http://localhost:{port}"
        ensure_running(
            port,
            project_path=bobi_env.project_path,
            extra_env={"BOBI_ES_TEST_GRANTS_SECRET": TEST_GRANTS_SECRET},
        )

        if _wait_healthy(base_url, timeout=10):
            break

        pid_file = bobi_env.state_dir / "event-server.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
            pid_file.unlink(missing_ok=True)
    else:
        raise RuntimeError("Local event server failed to start after 3 attempts")

    def _cleanup():
        pid_file = bobi_env.state_dir / "event-server.pid"
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

    es_dir = _wrangler_es_dir()

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
    test_secrets = {
        "INTERNAL_DO_SECRET": "test-internal-secret",
        "TEST_GRANTS_SECRET": TEST_GRANTS_SECRET,
    }
    for key, value in test_secrets.items():
        secret_line = f"{key}={value}"
        for idx, line in enumerate(dev_vars_lines):
            if line.startswith(f"{key}="):
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
def event_server(request, bobi_env):
    """Start the event server on a free port, yield (base_url, port, backend), then stop it."""
    backend = request.param

    if backend == "local":
        base_url, port, cleanup = _start_local_server(bobi_env)
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
    bootstrap = _register(base_url, "test-deploy-bootstrap", ["_bootstrap"])
    _seed_resource_grants(
        base_url,
        bootstrap["bubble_id"],
        bootstrap["bubble_key"],
        [
            {"service": "github", "resource": "test-org/test-repo"},
            {"service": "linear", "resource": "TEST"},
            {"service": "slack", "resource": "T_TEST"},
        ],
    )
    result = _register(base_url, "test-deploy", [
        "github:test-org/test-repo",
        "linear:TEST",
        "slack:T_TEST",
    ], bootstrap["bubble_id"], bootstrap["bubble_key"])
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
        bootstrap = _register(base_url, "lifecycle-bootstrap", ["_bootstrap"])
        _seed_resource_grants(
            base_url,
            bootstrap["bubble_id"],
            bootstrap["bubble_key"],
            [{"service": "github", "resource": "test-org/test-repo"}],
        )
        result = _register(
            base_url,
            "lifecycle-test",
            ["github:test-org/test-repo"],
            bootstrap["bubble_id"],
            bootstrap["bubble_key"],
        )
        assert "deployment_id" in result
        assert "api_key" in result
        assert result["api_key"].startswith("moda_")

    @pytest.mark.local_only
    def test_health_shows_deployment_count(self, event_server):
        base_url, *_ = event_server
        bootstrap = _register(base_url, "count-bootstrap", ["_bootstrap"])
        _seed_resource_grants(
            base_url,
            bootstrap["bubble_id"],
            bootstrap["bubble_key"],
            [{"service": "github", "resource": "some-org/some-repo"}],
        )
        _register(
            base_url,
            "count-test",
            ["github:some-org/some-repo"],
            bootstrap["bubble_id"],
            bootstrap["bubble_key"],
        )
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
        bootstrap = _register(base_url, "self-loop-bootstrap", ["_bootstrap"])
        _seed_resource_grants(
            base_url,
            bootstrap["bubble_id"],
            bootstrap["bubble_key"],
            [{"service": "slack", "resource": "T_SELF"}],
        )
        dep = _register(
            base_url,
            "self-loop-test",
            ["slack:T_SELF"],
            bootstrap["bubble_id"],
            bootstrap["bubble_key"],
        )
        dep_id, api_key = dep["deployment_id"], dep["api_key"]
        # Register workspace with explicit bot_id (skips auth.test)
        ws = _post_json(f"{base_url}/slack/workspaces", {
            "workspace_id": "T_SELF",
            "bot_token": "xoxb-fake-for-test",
            "bot_id": "B_SELF",
            "app_id": "A_SELF",
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
        from bobi.events.subscriptions import monitor_subscription_keys

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
        from bobi.events.subscriptions import monitor_subscription_keys

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
        """The local server replays buffered events from the zero cursor."""
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

        # Reconnect as a process with no persisted cursor. Zero means nothing
        # has been processed, so every buffered event must be replayed.
        first_seq = events[0].get("seq", 1)
        replayed = _drain_ws(base_url, dep_id, api_key, timeout=2, last_seen=0)
        assert any(event.get("seq") == first_seq for event in replayed)

    @pytest.mark.local_only
    def test_restart_replays_first_unacked_event_from_zero(
        self, event_server, tmp_path
    ):
        """A fresh client must replay buffered seq=1 with last_seen=0.

        This is the manager-restart half of #799. The Slack mention is
        delivered live and left in the server buffer, while the cursor is
        deliberately absent because the prior process never processed or ACKed
        seq=1. A real EventServerClient restart therefore reconnects with
        last_seen=0 and must receive that first event from the real local server.
        """
        from bobi.events.client import EventServerClient, _load_cursor

        base_url, _port, _backend = event_server
        workspace = "T_REPLAY_ZERO"
        bootstrap = _register(
            base_url, "replay-zero-bootstrap", ["_bootstrap"]
        )
        _seed_resource_grants(
            base_url,
            bootstrap["bubble_id"],
            bootstrap["bubble_key"],
            [{"service": "slack", "resource": workspace}],
        )
        deployment = _register(
            base_url,
            "replay-zero-manager",
            [f"slack:{workspace}"],
            bootstrap["bubble_id"],
            bootstrap["bubble_key"],
        )

        cursor_path = tmp_path / "cursor.json"
        assert _load_cursor(cursor_path) == 0
        original_queue = queue.SimpleQueue()
        original = EventServerClient(
            server_url=base_url,
            deployment_id=deployment["deployment_id"],
            api_key=deployment["api_key"],
            cursor_path=cursor_path,
            queue=original_queue,
            state_dir=tmp_path,
        )

        # Deliver live to the original process. This becomes the first event
        # in its queue and the server buffer, exactly matching the incident
        # (batch seq<=1 reached the inbox, but the hung session never processed
        # the mention and therefore never invoked its ACK callback).
        slack_payload = {
            "type": "event_callback",
            "team_id": workspace,
            "event": {
                "type": "app_mention",
                "user": "U_HUMAN",
                "channel": "C_ENG",
                "channel_type": "channel",
                "text": "<@U_BOT> replay me after restart",
                "ts": "1700000099.000001",
            },
        }
        slack_headers = {}
        slack_secret = os.environ.get("SLACK_SIGNING_SECRET", "")
        if slack_secret:
            body = json.dumps(slack_payload).encode()
            timestamp = str(int(time.time()))
            signature = "v0=" + hmac.new(
                slack_secret.encode(),
                f"v0:{timestamp}:".encode() + body,
                hashlib.sha256,
            ).hexdigest()
            slack_headers = {
                "x-slack-request-timestamp": timestamp,
                "x-slack-signature": signature,
            }
        original.start()
        try:
            assert original.wait_connected(timeout=2.0), (
                "original client never completed its subscription"
            )
            _post_json(
                f"{base_url}/webhooks/slack",
                slack_payload,
                headers=slack_headers,
            )
            delivered = original_queue.get(timeout=1.0)
        finally:
            original.stop()
            if original._thread is not None:
                original._thread.join(timeout=2.0)

        assert delivered["seq"] == 1
        assert delivered["type"] == "slack.mention"
        # Merely enqueuing an event does not persist the processed cursor.
        assert _load_cursor(cursor_path) == 0

        # A new process starts with an empty in-memory enqueue floor and the
        # same unadvanced cursor, so its wire request is last_seen=0.
        replay_queue = queue.SimpleQueue()
        client = EventServerClient(
            server_url=base_url,
            deployment_id=deployment["deployment_id"],
            api_key=deployment["api_key"],
            cursor_path=cursor_path,
            queue=replay_queue,
            state_dir=tmp_path,
        )
        client.start()
        try:
            assert client.wait_connected(timeout=2.0), (
                "restarted client never completed its last_seen=0 subscription"
            )
            try:
                replayed = replay_queue.get(timeout=0.5)
            except queue.Empty:
                pytest.fail(
                    "local event server did not replay unacked seq=1 from "
                    "last_seen=0"
                )
        finally:
            client.stop()
            if client._thread is not None:
                client._thread.join(timeout=2.0)

        assert replayed["seq"] == 1
        assert replayed["type"] == "slack.mention"
        assert "replay me after restart" in replayed["text"]
        # Receipt alone is not an ACK. Until the manager completes the turn,
        # the persisted cursor remains pinned so another restart can replay it.
        assert _load_cursor(cursor_path) == 0


class TestHeartbeatLiveness:
    """The real server must answer the app-level heartbeat so the client can
    tell a live receive path from a deaf-but-connected one (#425)."""

    def test_real_client_becomes_and_stays_live(self, deployment):
        from bobi.events.client import EventServerClient

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

    def test_stop_and_restart(self, bobi_env):
        from bobi.events.server import ensure_running

        port = _free_port()
        base_url = f"http://localhost:{port}"
        ensure_running(port, project_path=bobi_env.project_path)

        data = _get_json(f"{base_url}/health")
        assert data["status"] == "ok"

        pid_file = bobi_env.state_dir / "event-server.pid"
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        time.sleep(1)

        with pytest.raises(Exception):
            _get_json(f"{base_url}/health")

        ensure_running(port, project_path=bobi_env.project_path)
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
    from bobi.events.signing import serialize_body, sign_headers

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
    """Bubbles minted per named start must NOT overlap on a shared event
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

    def test_cli_events_publish_delivers_custom_topic(self, event_server, bobi_env):
        """`bobi agent <name> events publish source/type` uses the same signed
        publish path as library callers and reaches a source/type subscriber."""
        from bobi.config import bubble_state_path, save_bubble_state

        base_url, *_ = event_server
        dep = _register(base_url, "custom-topic-cli", ["alert/firing"])
        bubble_path = bubble_state_path(bobi_env.project_path)
        original_bubble = bubble_path.read_text() if bubble_path.exists() else None

        cfg_path = bobi_env.package_dir / "agent.yaml"
        original_cfg = cfg_path.read_text()
        cfg = yaml.safe_load(original_cfg) or {}
        cfg["event_server"] = {"url": base_url}
        cfg.pop("event_server_url", None)

        events, ready, thread = _live_subscriber(
            base_url,
            dep["deployment_id"],
            dep["api_key"],
        )
        assert ready.wait(5)

        try:
            save_bubble_state(
                bobi_env.project_path,
                dep["bubble_id"],
                dep["bubble_key"],
            )
            with with_mutable_runtime_package(bobi_env.project_path):
                cfg_path.write_text(yaml.dump(cfg, sort_keys=False))
            result = subprocess.run(
                [
                    sys.executable, "-m", "bobi.cli",
                    "agent", bobi_env.agent_name,
                    "events", "publish", "alert/firing",
                ],
                input='{"title":"x"}',
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(bobi_env.project_path),
                env={
                    **os.environ,
                    "BOBI_HOME": str(bobi_env.home_dir),
                    "BOBI_ROOT": str(bobi_env.project_path),
                },
            )
        finally:
            with with_mutable_runtime_package(bobi_env.project_path):
                cfg_path.write_text(original_cfg)
            if original_bubble is None:
                bubble_path.unlink(missing_ok=True)
            else:
                bubble_path.write_text(original_bubble)
                bubble_path.chmod(0o600)

        assert result.returncode == 0, result.stderr + result.stdout
        thread.join(timeout=5)
        matching = [
            e for e in events
            if e.get("source") == "alert"
            and e.get("type") == "firing"
            and e.get("payload", {}).get("title") == "x"
        ]
        assert matching, f"custom topic publish was not delivered: {events}"

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

    def test_signed_generic_publish_rejects_global_topics_without_delivery(
            self, event_server):
        """Global webhook topics must stay webhook-only through the real HTTP
        route, including URL path parsing and signature verification."""
        base_url, *_ = event_server

        for name, subscriptions, topic, body in [
            (
                "global-source-guard",
                ["repo"],
                "repo",
                {"source": "github:org", "payload": {"text": "fake"}},
            ),
            (
                "global-path-guard",
                ["ci/github:org"],
                "github:org",
                {"source": "ci", "payload": {"text": "fake"}},
            ),
        ]:
            dep = _register(base_url, name, subscriptions)
            events, ready, thread = _live_subscriber(
                base_url,
                dep["deployment_id"],
                dep["api_key"],
            )
            assert ready.wait(5)

            with pytest.raises(urllib.error.HTTPError) as ei:
                _post_event_signed(
                    base_url,
                    topic,
                    body,
                    dep["bubble_id"],
                    dep["bubble_key"],
                )
            assert ei.value.code == 400
            thread.join(timeout=5)
            assert events == [], (
                "global-topic generic publish was rejected but still delivered: "
                f"{events}"
            )

    def test_webhook_fans_out_across_bubbles(self, event_server):
        """Inbound webhooks remain GLOBAL, but #488 now admits only bubbles with
        an explicit resource grant for the topic."""
        base_url, *_ = event_server
        a_boot = _register(base_url, "a-bootstrap", ["_bootstrap"])
        b_boot = _register(base_url, "b-bootstrap", ["_bootstrap"])
        _seed_resource_grants(
            base_url,
            a_boot["bubble_id"],
            a_boot["bubble_key"],
            [{"service": "github", "resource": "shared/repo"}],
        )
        _seed_resource_grants(
            base_url,
            b_boot["bubble_id"],
            b_boot["bubble_key"],
            [{"service": "github", "resource": "shared/repo"}],
        )
        a = _register(
            base_url,
            "a",
            ["github:shared/repo"],
            a_boot["bubble_id"],
            a_boot["bubble_key"],
        )
        b = _register(
            base_url,
            "b",
            ["github:shared/repo"],
            b_boot["bubble_id"],
            b_boot["bubble_key"],
        )
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
            self, event_server, bobi_env):
        from bobi.config import bubble_state_path
        from bobi.events import publish as publish_mod
        from bobi.events.server import ensure_bubble
        from bobi.events.subscriptions import monitor_subscription_keys
        from bobi.monitors.schema import Condition, Monitor
        from bobi.monitors.scheduler import MonitorScheduler

        base_url, *_ = event_server

        # Point the (session-scoped) project at this test server; restore
        # after, and drop publish's runtime URL cache both ways.
        agent_yaml = bobi_env.package_dir / "agent.yaml"
        original = agent_yaml.read_text()
        with with_mutable_runtime_package(bobi_env.project_path):
            agent_yaml.write_text(original + f"\nevent_server_url: {base_url}\n")
        publish_mod._es_url_cache.clear()
        # The session-scoped project may carry a bubble minted against the
        # session event server. ensure_bubble returns any on-disk bubble
        # as-is, and THIS test's server has never seen it, so the signed
        # JOIN would 403 (production recovers via force_remint_of; the test
        # helper does not). Isolate bubble state both ways too.
        bubble_path = bubble_state_path(bobi_env.project_path)
        original_bubble = (bubble_path.read_bytes()
                          if bubble_path.exists() else None)
        bubble_path.unlink(missing_ok=True)
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
                state_path=bobi_env.state_dir / "e2e_monitor_state.json",
                registry_loader=lambda: FakeRegistry(),
                project_path=bobi_env.project_path,
            )
            sched._checks["conflicts"] = lambda mon, repos: [
                Condition(key="repo#7", data={"pr_number": 7, "repo": "org/repo"})
            ]

            # Mint the project's bubble; the subscriber JOINs it and the
            # scheduler's post_event signs with the same bubble.json — both must
            # share a bubble for the finding to be delivered.
            bubble = ensure_bubble(base_url, bobi_env.project_path)
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
            with with_mutable_runtime_package(bobi_env.project_path):
                agent_yaml.write_text(original)
            if original_bubble is None:
                bubble_path.unlink(missing_ok=True)
            else:
                bubble_path.write_bytes(original_bubble)
            publish_mod._es_url_cache.clear()


@pytest.mark.local_only
class TestBindAddress:
    """The event server must default to loopback (127.0.0.1) and only widen
    the listen address when BOBI_ES_BIND is set explicitly.  Bind
    address must be independent of any auth setting (#241)."""

    @staticmethod
    def _start_server(bobi_env, port: int, extra_env: dict | None = None):
        """Start an event server with explicit env control, return (proc, log_path)."""
        from bobi.events.server import (
            _build_local,
            _find_event_server_dir,
            _needs_build,
            _needs_install,
            _run_npm,
        )

        es_dir = _find_event_server_dir()
        if _needs_install(es_dir):
            _run_npm(["npm", "install", "--omit=dev", "--no-audit", "--no-fund"], es_dir)
        if _needs_build(es_dir):
            _build_local(es_dir)

        log_path = bobi_env.state_dir / f"event-server-bind-{port}.log"

        # Start from a clean env without any inherited BOBI_ES_BIND
        env = {k: v for k, v in os.environ.items() if k != "BOBI_ES_BIND"}
        env["BOBI_ES_PORT"] = str(port)
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

    def test_default_binds_loopback(self, bobi_env):
        """Without BOBI_ES_BIND, the server binds 127.0.0.1 and logs the
        loopback-only advisory message."""
        port = _free_port()
        proc, log_path, lf = self._start_server(bobi_env, port)
        try:
            assert self._wait_healthy(f"http://127.0.0.1:{port}")

            log_text = log_path.read_text()
            assert f"127.0.0.1:{port}" in log_text
            assert "loopback-only" in log_text
            assert "BOBI_ES_BIND" in log_text
        finally:
            self._stop(proc, lf)

    def test_explicit_bind_all_interfaces(self, bobi_env):
        """BOBI_ES_BIND=0.0.0.0 widens the listener; the log omits the
        loopback advisory."""
        port = _free_port()
        proc, log_path, lf = self._start_server(
            bobi_env, port, {"BOBI_ES_BIND": "0.0.0.0"})
        try:
            assert self._wait_healthy(f"http://127.0.0.1:{port}")

            log_text = log_path.read_text()
            assert f"0.0.0.0:{port}" in log_text
            assert "loopback-only" not in log_text
        finally:
            self._stop(proc, lf)

    def test_bind_decoupled_from_webhook_secret(self, bobi_env):
        """Setting BOBI_ES_WEBHOOK_SECRET must not change the bind address.
        Regression guard: an earlier draft coupled auth and bind."""
        port = _free_port()
        proc, log_path, lf = self._start_server(
            bobi_env, port, {"BOBI_ES_WEBHOOK_SECRET": "s3cret"})
        try:
            assert self._wait_healthy(f"http://127.0.0.1:{port}")

            log_text = log_path.read_text()
            assert f"127.0.0.1:{port}" in log_text
            assert "loopback-only" in log_text
        finally:
            self._stop(proc, lf)


@pytest.mark.local_only
class TestWebhookSignatureVerification:
    """#639: every /webhooks/<source> route verifies through the unified
    pipeline's structural verify slot. This drives the REAL local server
    process with per-provider secrets set and proves bad/missing signatures
    are rejected while valid ones deliver - including linear, whose route
    previously had no signature check at all."""

    GITHUB_SECRET = "gh-int-secret"
    SLACK_SECRET = "sl-int-secret"
    LINEAR_SECRET = "ln-int-secret"

    @pytest.fixture()
    def secured_server(self, bobi_env):
        port = _free_port()
        proc, _log_path, lf = TestBindAddress._start_server(bobi_env, port, {
            "BOBI_ES_WEBHOOK_SECRET": self.GITHUB_SECRET,
            "BOBI_ES_SLACK_SIGNING_SECRET": self.SLACK_SECRET,
            "BOBI_ES_LINEAR_WEBHOOK_SECRET": self.LINEAR_SECRET,
        })
        try:
            assert TestBindAddress._wait_healthy(f"http://127.0.0.1:{port}")
            yield f"http://127.0.0.1:{port}"
        finally:
            TestBindAddress._stop(proc, lf)

    @staticmethod
    def _post(url: str, body: bytes, headers: dict) -> int:
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status
        except urllib.error.HTTPError as err:
            return err.code

    def test_github_signature_enforced(self, secured_server):
        body = json.dumps({"action": "opened",
                           "repository": {"full_name": "org/repo"}}).encode()
        url = f"{secured_server}/webhooks/github"
        headers = {"x-github-event": "issues", "x-github-delivery": "d1"}

        assert self._post(url, body, headers) == 401
        assert self._post(
            url, body, {**headers, "x-hub-signature-256": "sha256=bad"}) == 401

        sig = "sha256=" + hmac.new(
            self.GITHUB_SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert self._post(
            url, body, {**headers, "x-hub-signature-256": sig}) == 200

    def test_linear_signature_enforced(self, secured_server):
        body = json.dumps({"action": "update", "type": "Issue",
                           "data": {"title": "t", "team": {"key": "ENG"}},
                           "webhookTimestamp": int(time.time() * 1000)}).encode()
        url = f"{secured_server}/webhooks/linear"

        assert self._post(url, body, {}) == 401
        assert self._post(url, body, {"linear-signature": "deadbeef"}) == 401

        sig = hmac.new(self.LINEAR_SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert self._post(url, body, {"linear-signature": sig}) == 200

    def test_linear_replay_rejected(self, secured_server):
        stale = json.dumps({"action": "update", "type": "Issue",
                            "data": {"title": "t", "team": {"key": "ENG"}},
                            "webhookTimestamp": int(time.time() * 1000) - 3_600_000}).encode()
        sig = hmac.new(self.LINEAR_SECRET.encode(), stale, hashlib.sha256).hexdigest()
        assert self._post(
            f"{secured_server}/webhooks/linear", stale, {"linear-signature": sig}) == 401

    def test_slack_signature_enforced(self, secured_server):
        body = json.dumps({"type": "event_callback",
                           "event": {"type": "message", "user": "U1", "channel": "D1",
                                     "channel_type": "im", "text": "hi",
                                     "ts": "1700000000.000001"}}).encode()
        url = f"{secured_server}/webhooks/slack"

        assert self._post(url, body, {}) == 401

        ts = str(int(time.time()))
        sig = "v0=" + hmac.new(
            self.SLACK_SECRET.encode(), f"v0:{ts}:".encode() + body,
            hashlib.sha256).hexdigest()
        assert self._post(url, body, {"x-slack-request-timestamp": ts,
                                      "x-slack-signature": sig}) == 200

    def test_slack_url_verification_passes_unsigned(self, secured_server):
        """The handshake carries no signing headers; the pipeline's preVerify
        stage must short-circuit it before the signature check."""
        body = json.dumps({"type": "url_verification", "challenge": "c1"}).encode()
        assert self._post(f"{secured_server}/webhooks/slack", body, {}) == 200


class TestIngestTokens:
    """#640: scoped ingest tokens end to end on BOTH backends. Mints a token
    bound to (bubble, alert/firing) over the signed management API, then
    drives the acceptance matrix: a plain curl-style POST with the bearer
    token delivers to a bubble subscriber; missing/wrong/revoked tokens and
    cross-topic use are opaque 403s; list never exposes token material."""

    @staticmethod
    def _signed(base_url: str, method: str, path: str, body: str,
                bubble_id: str, bubble_key: str) -> tuple[int, dict]:
        from bobi.events.signing import sign_headers

        headers = {"Content-Type": "application/json"} if body else {}
        headers.update(sign_headers(bubble_id, bubble_key, method, path, body))
        req = urllib.request.Request(
            base_url + path, data=body.encode() if body else None,
            headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            try:
                return err.code, json.loads(err.read())
            except ValueError:
                return err.code, {}

    @staticmethod
    def _ingest(base_url: str, topic: str, payload: dict,
                token: str | None = None) -> int:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"{base_url}/webhooks/ingest/{topic}",
            data=json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                json.loads(resp.read())
                return resp.status
        except urllib.error.HTTPError as err:
            return err.code

    def test_unsigned_token_mint_rejected(self, event_server):
        base_url, *_ = event_server
        req = urllib.request.Request(
            f"{base_url}/ingest-tokens",
            data=json.dumps({"topic": "alert/firing"}).encode(),
            headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 403

    def test_ingest_token_lifecycle(self, event_server):
        base_url, *_ = event_server
        from bobi.events.signing import serialize_body

        boot = _register(base_url, "ingest-bootstrap", ["_bootstrap"])
        bub, key = boot["bubble_id"], boot["bubble_key"]
        dep = _register(base_url, "ingest-subscriber", ["alert/firing"], bub, key)

        # Mint - the token appears here and only here.
        status, minted = self._signed(
            base_url, "POST", "/ingest-tokens",
            serialize_body({"name": "oncall", "topic": "alert/firing"}), bub, key)
        assert status == 201
        token = minted["token"]
        assert token.startswith("ingt_")

        # The exact acceptance flow: a static-header POST of plain JSON
        # reaches a subscriber of the bound topic in the bound bubble.
        events = _send_and_drain(
            base_url, dep["deployment_id"], dep["api_key"],
            lambda: self._ingest(base_url, "alert/firing",
                                 {"title": "disk full", "severity": "critical"}, token))
        matching = [e for e in events
                    if e.get("source") == "ingest" and e.get("type") == "alert/firing"]
        assert len(matching) == 1
        assert matching[0]["fields"]["title"] == "disk full"
        assert matching[0]["payload"] == {"title": "disk full", "severity": "critical"}

        # Missing, wrong, and cross-topic tokens: opaque 403.
        assert self._ingest(base_url, "alert/firing", {"t": 1}) == 403
        assert self._ingest(base_url, "alert/firing", {"t": 1}, "ingt_wrong") == 403
        assert self._ingest(base_url, "alert/resolved", {"t": 1}, token) == 403

        # List shows metadata, never token material.
        status, listed = self._signed(base_url, "GET", "/ingest-tokens", "", bub, key)
        assert status == 200
        assert [t["id"] for t in listed["tokens"]] == [minted["id"]]
        assert all("token" not in t and "token_hash" not in t for t in listed["tokens"])

        # Revoke takes effect immediately.
        status, _ = self._signed(
            base_url, "DELETE", f"/ingest-tokens/{minted['id']}", "", bub, key)
        assert status == 200
        assert self._ingest(base_url, "alert/firing", {"t": 1}, token) == 403

    def test_token_is_bubble_scoped(self, event_server):
        """A second bubble cannot see or revoke the first bubble's tokens, and
        the ingested event stays inside the minting bubble."""
        base_url, *_ = event_server
        from bobi.events.signing import serialize_body

        owner = _register(base_url, "ingest-owner", ["_bootstrap"])
        other = _register(base_url, "ingest-other", ["_bootstrap"])
        # A subscriber to the SAME topic in the OTHER bubble must not receive.
        other_dep = _register(base_url, "ingest-other-sub", ["alert/firing"],
                              other["bubble_id"], other["bubble_key"])

        status, minted = self._signed(
            base_url, "POST", "/ingest-tokens",
            serialize_body({"topic": "alert/firing"}),
            owner["bubble_id"], owner["bubble_key"])
        assert status == 201

        events = _send_and_drain(
            base_url, other_dep["deployment_id"], other_dep["api_key"],
            lambda: self._ingest(base_url, "alert/firing", {"title": "x"},
                                 minted["token"]),
            timeout=3)
        assert [e for e in events if e.get("source") == "ingest"] == []

        status, listed = self._signed(
            base_url, "GET", "/ingest-tokens", "",
            other["bubble_id"], other["bubble_key"])
        assert status == 200 and listed["tokens"] == []
        status, _ = self._signed(
            base_url, "DELETE", f"/ingest-tokens/{minted['id']}", "",
            other["bubble_id"], other["bubble_key"])
        assert status == 404

    @pytest.mark.local_only
    def test_env_seeded_token_survives_local_restart(self, bobi_env):
        """#661: local in-memory token state self-heals from env on restart."""
        first_token = "ingt_static_restart_secret"
        rotated_token = "ingt_rotated_restart_secret"
        procs = []

        def start(port: int, token: str):
            proc, _log_path, lf = TestBindAddress._start_server(
                bobi_env,
                port,
                {"BOBI_ES_INGEST_TOKENS": f"alert/firing={token}"},
            )
            procs.append((proc, lf))
            base = f"http://127.0.0.1:{port}"
            assert TestBindAddress._wait_healthy(base, timeout=30)
            return base

        def assert_delivers(base_url: str, suffix: str, token: str):
            boot = _register(base_url, f"env-ingest-bootstrap-{suffix}", ["_bootstrap"])
            bub, key = boot["bubble_id"], boot["bubble_key"]
            dep = _register(base_url, f"env-ingest-subscriber-{suffix}",
                            ["alert/firing"], bub, key)

            status, listed = self._signed(base_url, "GET", "/ingest-tokens", "", bub, key)
            assert status == 200
            assert listed["tokens"][0]["env_managed"] is True
            assert listed["tokens"][0]["name"] == "BOBI_ES_INGEST_TOKENS"

            status, body = self._signed(
                base_url, "DELETE", f"/ingest-tokens/{listed['tokens'][0]['id']}",
                "", bub, key)
            assert status == 400
            assert "BOBI_ES_INGEST_TOKENS" in body["error"]

            events = _send_and_drain(
                base_url, dep["deployment_id"], dep["api_key"],
                lambda: self._ingest(base_url, "alert/firing", {"title": suffix}, token),
            )
            matching = [e for e in events
                        if e.get("source") == "ingest" and e.get("type") == "alert/firing"]
            assert len(matching) == 1
            assert matching[0]["fields"]["title"] == suffix

            # Cross-topic behavior remains the same opaque 403 as minted tokens.
            assert self._ingest(base_url, "alert/resolved", {"title": suffix}, token) == 403

        try:
            first = start(_free_port(), first_token)
            assert_delivers(first, "before-restart", first_token)

            proc, lf = procs.pop()
            TestBindAddress._stop(proc, lf)

            second = start(_free_port(), first_token)
            assert_delivers(second, "after-restart", first_token)

            proc, lf = procs.pop()
            TestBindAddress._stop(proc, lf)

            rotated = start(_free_port(), rotated_token)
            assert self._ingest(rotated, "alert/firing", {"title": "old"}, first_token) == 403
            assert_delivers(rotated, "after-rotation", rotated_token)
        finally:
            while procs:
                proc, lf = procs.pop()
                TestBindAddress._stop(proc, lf)
