"""Integration tests for inter-agent messaging over the event server.

[comms-v1 / #268, #269] These start a real local event server and drive the
full transport — ``deliver()`` publishes an ``inbox/<target>`` event, the
server fans it to the target's WebSocket subscription, and the drain loop
pushes it into the target's in-process inbox queue. For ``wait=True`` (#269),
``deliver()`` opens a transient ``reply/<uuid>`` subscription and matches the
target's reply on its correlation id. No Claude session is needed: the Session
run loop's ``recv()``/``respond()`` is simulated, so these exercise the
transport itself without the cost of real LLM turns.

Requires Node (the local event server). Skips cleanly if it can't start.
"""

import json
import os
import signal
import socket
import threading
import time
import urllib.request

import pytest
import yaml


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def inbox_event_server(modastack_env):
    """Start a local event server on a free port and point config at it."""
    from modastack.events.server import ensure_running
    from modastack.events import publish as _pub

    port = _free_port()
    url = f"http://localhost:{port}"

    agent_yaml = modastack_env.project_path / ".modastack" / "agent.yaml"
    original = agent_yaml.read_text()
    data = yaml.safe_load(original)
    data["event_server_url"] = url
    agent_yaml.write_text(yaml.dump(data))
    _pub._es_url_cache.clear()  # resolved URL is cached per project root

    ensure_running(port, project_path=modastack_env.project_path)

    deadline = time.monotonic() + 15
    healthy = False
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
                if json.loads(r.read()).get("status") == "ok":
                    healthy = True
                    break
        except Exception:
            time.sleep(0.3)
    if not healthy:
        pytest.skip("local event server (Node) unavailable")

    yield url

    pid_file = modastack_env.state_dir / "event-server.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
        except (ProcessLookupError, ValueError, OSError):
            pass
    agent_yaml.write_text(original)
    _pub._es_url_cache.clear()


def _make_addressable(name, root):
    """Register a session + start its inbox subscription, as Session.start does."""
    from modastack.inbox import Inbox
    from modastack.subagent import _start_event_subscription
    from modastack.sdk import get_registry, SessionEntry

    inbox = Inbox(name)
    inbox.start()
    get_registry().register(SessionEntry(name=name, cwd=str(root), pid=os.getpid()))
    _start_event_subscription(name, [f"inbox/{name}"], root)
    return inbox


def test_inbox_message_round_trips_over_event_server(inbox_event_server, modastack_env):
    """A non-wait deliver() reaches the target's inbox via the event server."""
    from modastack.inbox import deliver, get_local_inbox

    root = modastack_env.project_path
    sender_inbox = _make_addressable("agent-x", root)
    target_inbox = _make_addressable("agent-y", root)

    time.sleep(2)  # let the WebSocket subscriptions connect

    try:
        ok, _ = deliver("agent-y", "hello from x", sender="agent-x", wait=False)
        assert ok

        inbox_y = get_local_inbox("agent-y")
        deadline = time.monotonic() + 15
        msg = None
        while time.monotonic() < deadline and msg is None:
            msg = inbox_y.recv(timeout=1)

        assert msg is not None, "message did not round-trip over the event server"
        assert "hello from x" in msg.text
        assert msg.sender == "agent-x"
    finally:
        sender_inbox.close()
        target_inbox.close()


def _echo_responder(target_name, stop):
    """Stand in for the target session's run loop.

    Drains the target's inbox and replies to wait-mode messages by publishing
    to the message's reply_to topic (``Inbox.respond``) — the real target-side
    path exercised by ``Session._process_message``.
    """
    from modastack.inbox import get_local_inbox

    def run():
        inbox = get_local_inbox(target_name)
        while not stop.is_set():
            m = inbox.recv(timeout=0.5)
            if m is None:
                continue
            if m.wait:
                inbox.respond(m, f"echo:{m.text}")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_blocking_ask_round_trips_over_event_server(inbox_event_server, modastack_env):
    """A wait=True deliver() round-trips via a transient reply/<uuid> topic.

    The sender (deliver) opens its own throwaway reply subscription, publishes
    the request carrying it as reply_to, and matches the reply on corr_id — the
    full #269 request/reply path over the live event server, no files.
    """
    from modastack.inbox import deliver

    root = modastack_env.project_path
    sender_inbox = _make_addressable("ask-x", root)
    target_inbox = _make_addressable("ask-y", root)

    time.sleep(2)  # let the target's subscription connect

    stop = threading.Event()
    t = _echo_responder("ask-y", stop)
    try:
        ok, response = deliver("ask-y", "ping", sender="ask-x", wait=True, timeout=30)
        assert ok, response
        assert response == "echo:ping"
    finally:
        stop.set()
        t.join(timeout=2)
        sender_inbox.close()
        target_inbox.close()


def _deployment_count(es_url: str) -> int:
    """Read the deployment count from the event server's /health endpoint."""
    with urllib.request.urlopen(f"{es_url}/health", timeout=2) as r:
        return json.loads(r.read()).get("deployments", -1)


def test_ask_teardown_leaves_zero_residual_reply_deployments(inbox_event_server, modastack_env):
    """A completed ask round-trip deregisters its transient reply deployment.

    After deliver(wait=True) completes, the reply/<uuid> deployment must be
    gone from the event server — no leak (#277).
    """
    from modastack.inbox import deliver

    root = modastack_env.project_path
    sender_inbox = _make_addressable("leak-x", root)
    target_inbox = _make_addressable("leak-y", root)

    time.sleep(2)

    # Baseline: only the two standing deployments (sender + target).
    baseline = _deployment_count(inbox_event_server)

    stop = threading.Event()
    t = _echo_responder("leak-y", stop)
    try:
        ok, response = deliver("leak-y", "ping", sender="leak-x", wait=True, timeout=30)
        assert ok, response
        assert response == "echo:ping"

        # After the ask completes, the transient reply deployment should be gone.
        after = _deployment_count(inbox_event_server)
        assert after == baseline, (
            f"residual reply deployments: {after - baseline} "
            f"(baseline={baseline}, after={after})"
        )
    finally:
        stop.set()
        t.join(timeout=2)
        sender_inbox.close()
        target_inbox.close()


def test_concurrent_asks_do_not_cross_replies(inbox_event_server, modastack_env):
    """Multiple in-flight wait=True asks each get their OWN reply (corr_id).

    Each deliver() opens a distinct reply/<uuid> topic, so even with several
    asks to the same target outstanding at once, no reply lands on the wrong
    sender. Guards the correlation contract (epic #267 acceptance).
    """
    from modastack.inbox import deliver

    root = modastack_env.project_path
    sender_inbox = _make_addressable("c-x", root)
    target_inbox = _make_addressable("c-y", root)

    time.sleep(2)

    stop = threading.Event()
    t = _echo_responder("c-y", stop)

    results: dict[str, tuple[bool, str]] = {}

    def ask(tag):
        results[tag] = deliver("c-y", tag, sender="c-x", wait=True, timeout=30)

    try:
        tags = [f"q{i}" for i in range(3)]
        askers = [threading.Thread(target=ask, args=(tag,)) for tag in tags]
        for th in askers:
            th.start()
        for th in askers:
            th.join(timeout=35)

        for tag in tags:
            assert tag in results, f"ask {tag} never returned"
            ok, resp = results[tag]
            assert ok, resp
            assert resp == f"echo:{tag}", f"crossed reply for {tag}: {resp!r}"
    finally:
        stop.set()
        t.join(timeout=2)
        sender_inbox.close()
        target_inbox.close()
