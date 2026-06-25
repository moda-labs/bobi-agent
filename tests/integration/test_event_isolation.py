"""Event delivery isolation between agent sessions sharing one project root.

Regression test for the prod incident (2026-06-12): the director and two
project leads all ran from the same project root. `_start_event_subscription`
reused the single shared deployment.json — each new agent PUT-added its
repo-scoped subscriptions onto the SAME event-server deployment and opened
another WebSocket on it. The server fans every matching event out to every
socket on a deployment, so every agent received the union of everyone's
subscriptions: the user's Slack DMs to the director were delivered to all
project leads, and each lead replied on Slack.

This test drives the real framework path with two separate processes (as in
prod — each agent process has its own event queue and drain loop) against a
real local event server, and asserts deliveries are scoped to what each
session actually subscribed to.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent.parent

DM_TEXT = "did the jobtack tickets stall? #39 and #97"
TEST_GRANTS_SECRET = "modastack-integration-test-grants"

DRIVER = '''\
"""One agent session's event subscription, exactly as _run_agent_entry wires it."""
import json, logging, sys, time
from pathlib import Path

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(name)s %(message)s")

project = Path(sys.argv[1])
session = sys.argv[2]
subs = json.loads(sys.argv[3])
out = Path(sys.argv[4])

from modastack.sdk import set_project_root
set_project_root(project)

# Capture the drain loop's final hop (push into the session's in-process
# inbox) to a file the test can read. Everything upstream — registration,
# WebSocket, queue, drain — is real. The drain resolves the inbox by session
# name via the process-local registry, so register a capturing stand-in.
import modastack.inbox as inbox

class _CaptureInbox:
    def push(self, msg):
        with open(out, "a") as f:
            f.write(json.dumps({"session": session, "text": msg.text}) + "\\n")
            f.flush()

inbox.register_local_inbox(session, _CaptureInbox())

from modastack.subagent import _start_event_subscription
_start_event_subscription(session, subs, project)

time.sleep(120)  # parent terminates us
'''


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


@pytest.fixture
def iso_project(tmp_path):
    """Isolated project root with its own local event server."""
    from modastack.events.server import ensure_running, health

    project = tmp_path / "iso-project"
    (project / ".modastack" / "state").mkdir(parents=True)

    port = _free_port()
    base_url = f"http://localhost:{port}"
    (project / ".modastack" / "agent.yaml").write_text(
        f"agent: iso-test\nentry_point: manager\nevent_server: {base_url}\n"
    )

    ensure_running(
        port,
        project_path=project,
        extra_env={"MODASTACK_ES_TEST_GRANTS_SECRET": TEST_GRANTS_SECRET},
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if health(base_url):
            break
        time.sleep(0.3)
    else:
        raise RuntimeError("local event server did not become healthy")

    yield project, base_url

    pid_file = project / ".modastack" / "state" / "event-server.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass


@pytest.fixture
def session_proc(tmp_path):
    """Launch agent-session driver subprocesses; clean them up afterwards."""
    driver_path = tmp_path / "driver.py"
    driver_path.write_text(DRIVER)
    procs = []

    def _launch(project: Path, session: str, subs: list[str]) -> Path:
        out = tmp_path / f"received-{session}.jsonl"
        proc = subprocess.Popen(
            [sys.executable, str(driver_path), str(project), session,
             json.dumps(subs), str(out)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env={
                **os.environ,
                "PYTHONPATH": str(PACKAGE_ROOT),
                "MODASTACK_ES_TEST_GRANTS_SECRET": TEST_GRANTS_SECRET,
            },
        )
        procs.append(proc)

        # Wait until this session's WebSocket is connected.
        deadline = time.monotonic() + 20
        connected = False
        lines = []
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            if "Event client connected" in line:
                connected = True
                break
        if not connected and proc.poll() is not None:
            rest = proc.stdout.read()
            if rest:
                lines.append(rest)
        assert connected, (
            f"session {session} never connected to the event server:\n"
            + "".join(lines[-20:])
        )
        return out

    yield _launch

    for proc in procs:
        proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _received(out: Path) -> list[dict]:
    if not out.exists():
        return []
    return [json.loads(line) for line in out.read_text().splitlines() if line]


def _wait_for(out: Path, needle: str, timeout: float = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(needle in r["text"] for r in _received(out)):
            return True
        time.sleep(0.5)
    return False


@pytest.mark.timeout(120)
def test_sessions_only_receive_their_own_subscriptions(iso_project, session_proc):
    """Two sessions, one project root: a Slack DM must reach only the
    workspace-subscribed session; a repo event only the repo-subscribed one."""
    project, base_url = iso_project

    director_out = session_proc(project, "director", ["slack:T_ISO"])
    lead_out = session_proc(project, "lead-jobtack",
                            ["github:test-org/iso-repo"])

    _post_json(f"{base_url}/webhooks/slack", {
        "type": "event_callback", "team_id": "T_ISO",
        "event": {"type": "message", "user": "U_ZACH",
                  "channel": "D_DM_WITH_ZACH", "channel_type": "im",
                  "text": DM_TEXT, "ts": "1700000100.000001"},
    })
    _post_json(f"{base_url}/webhooks/github", {
        "action": "opened",
        "issue": {"number": 39, "title": "research UI", "state": "open",
                  "user": {"login": "zach"}},
        "repository": {"full_name": "test-org/iso-repo"},
    }, headers={"x-github-event": "issues", "x-github-delivery": "iso-001"})

    assert _wait_for(director_out, DM_TEXT), (
        "director (workspace subscriber) never received the Slack DM"
    )
    assert _wait_for(lead_out, "iso-repo"), (
        "lead (repo subscriber) never received the GitHub issue event"
    )
    # Grace period for any misrouted copies still in flight.
    time.sleep(5)

    leaked_to_lead = [r for r in _received(lead_out) if DM_TEXT in r["text"]]
    assert not leaked_to_lead, (
        "Slack DM leaked to a session that never subscribed to Slack — "
        "this is the prod incident where every project lead received and "
        f"replied to the user's DMs: {leaked_to_lead}"
    )

    leaked_to_director = [
        r for r in _received(director_out) if "iso-repo" in r["text"]
    ]
    assert not leaked_to_director, (
        "GitHub repo event leaked to a session that only subscribed to "
        f"Slack: {leaked_to_director}"
    )

    dm_copies = [r for r in _received(director_out) if DM_TEXT in r["text"]]
    assert len(dm_copies) == 1, (
        f"director received the DM {len(dm_copies)} times — expected exactly once"
    )
