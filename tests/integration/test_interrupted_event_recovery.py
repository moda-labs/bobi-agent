"""MOD-289: kill a manager after a side effect, then recover via server replay."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from bobi import paths
from bobi.events.client import _load_cursor
from bobi.events.signing import serialize_body, sign_headers
from bobi.events.state import save_bubble_state, save_deployment_state, session_cursor_path
from bobi.subagent import _start_event_subscription

from .conftest import BRAIN_PARAMS, _free_port
from .test_event_server import event_server, _register


def _wait(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for recovery boundary")


def _kill_owned(proc):
    table = subprocess.check_output(["ps", "-axo", "pid=,ppid="], text=True)
    parents = [tuple(map(int, line.split())) for line in table.splitlines()]
    owned = {proc.pid}
    while True:
        descendants = {pid for pid, parent in parents if parent in owned}
        if descendants <= owned:
            break
        owned.update(descendants)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=10)
    for pid in owned - {proc.pid}:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _root(tmp_path, base):
    root = tmp_path / "home/agents/recovery/run"
    paths.package_dir(root).mkdir(parents=True)
    paths.agent_yaml_path(root).write_text(
        f"agent: recovery\nentry_point: manager\nevent_server: {base}\n"
    )
    return root


def test_owned_local_server_restart_recovers_saved_subscription(tmp_path):
    from bobi.events.server import ensure_running, health
    from bobi.events.signing import signed_request
    from bobi.events.state import load_bubble_state, load_deployment_state

    base = f"http://localhost:{_free_port()}"
    root = _root(tmp_path, base)
    session = "recovery-manager"
    subscription = None
    try:
        subscription = _start_event_subscription(session, [f"inbox/{session}"], root)
        assert subscription.client.wait_connected(10)
        original = load_deployment_state(root, session)
        bubble = load_bubble_state(root)
        cursor = session_cursor_path(root, session)
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text('{"last_seen":4}')
        assert ensure_running(int(base.rsplit(":", 1)[1]), project_path=root) == "connected"
        assert load_deployment_state(root, session) == original
        assert cursor.read_text() == '{"last_seen":4}'
        subscription.stop()
        subscription = None
        pid = int((paths.state_path(root) / "event-server.pid").read_text())
        os.kill(pid, signal.SIGTERM)
        _wait(lambda: health(base) is None)

        subscription = _start_event_subscription(session, [f"inbox/{session}"], root)
        assert subscription.client.wait_connected(10)
        assert load_deployment_state(root, session)["deployment_id"] != original["deployment_id"]
        current_bubble = load_bubble_state(root)
        assert current_bubble["bubble_id"] != bubble["bubble_id"]
        assert _load_cursor(cursor) == 0
        backups = list((paths.state_path(root) / "event-transport-backups").iterdir())
        assert len(backups) == 1
        assert json.loads((backups[0] / "bubble.json").read_text()) == bubble
        assert json.loads((backups[0] / "deployments" / f"{session}.json").read_text()) == original
        assert (backups[0] / "cursors" / f"{session}.json").read_text() == '{"last_seen":4}'
        received = []
        subscription.client.state_dir = paths.state_path(root)
        subscription.client.on_event = received.append
        response = signed_request(base, "POST", f"/events/inbox/{session}", {"text": "after restart"},
                                  current_bubble["bubble_id"], current_bubble["bubble_key"], timeout=5)
        assert response.status_code == 200
        _wait(lambda: received)
        assert received[0]["seq"] == 1
        assert received[0]["text"] == "after restart"
    finally:
        if subscription is not None:
            subscription.stop()
        pid_file = paths.state_path(root) / "event-server.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("brain", BRAIN_PARAMS)
@pytest.mark.timeout(300)
def test_interrupted_event_completes_in_successor(event_server, tmp_path, brain):
    base, _, _ = event_server
    root = _root(tmp_path, base)
    markers = tmp_path / "markers"
    markers.mkdir()
    env = {
        **os.environ, "BOBI_HOME": str(tmp_path / "home"), "BOBI_ROOT": str(root),
        "BOBI_BRAIN": brain, "BOBI_STUB_BRAIN": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }
    cursor = session_cursor_path(root, "recovery-manager")
    state = paths.state_path(root) / "deployments/recovery-manager.json"
    processes, logs = [], []

    def wait_for_turn(predicate, timeout=90):
        _wait(lambda: predicate() or processes[-1].poll() is not None, timeout)
        assert processes[-1].poll() is None, (
            tmp_path / f"manager-{len(processes)}.log"
        ).read_text()[-6000:]

    def boot(number):
        log = (tmp_path / f"manager-{number}.log").open("w")
        logs.append(log)
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("recovery_manager.py")),
             str(root), str(markers), brain, str(number)],
            env=env, cwd=root, stdout=log, stderr=log, start_new_session=True,
        )
        processes.append(proc)
        _wait(lambda: (markers / f"ready-{number}").exists() or proc.poll() is not None, 140)
        assert proc.poll() is None, (tmp_path / f"manager-{number}.log").read_text()[-6000:]
        return proc

    def publish(text):
        bubble = json.loads((paths.state_path(root) / "bubble.json").read_text())
        route = "/events/inbox/recovery-manager"
        body = serialize_body({"text": text, "sender": "test"})
        headers = sign_headers(bubble["bubble_id"], bubble["bubble_key"], "POST", route, body)
        with httpx.Client() as client:
            response = client.post(base + route, content=body, headers={
                **headers, "Content-Type": "application/json"})
            response.raise_for_status()

    try:
        first = boot(1)
        original = state.read_bytes()
        publish(f"PREFIX_EVENT: Use Bash to append exactly one line 'done' to {markers}/prefix. Then reply OK.")
        wait_for_turn(lambda: _load_cursor(cursor) >= 1)
        prefix = _load_cursor(cursor)
        assert (markers / "prefix").read_text().splitlines() == ["done"]
        publish(
            f"PENDING_EVENT: Use Bash to touch {markers}/first, then wait until "
            f"{markers}/release exists (while [ ! -f {markers}/release ]; do sleep 1; done), "
            f"then touch {markers}/second. Do all steps before replying OK. "
            "If this instruction is replayed, run these steps again."
        )
        wait_for_turn(lambda: (markers / "first").exists())
        assert not (markers / "second").exists()
        assert _load_cursor(cursor) == prefix
        _kill_owned(first)
        assert _load_cursor(cursor) == prefix
        (markers / "release").touch()
        boot(2)
        wait_for_turn(lambda: (markers / "second").exists() and _load_cursor(cursor) > prefix)
        assert state.read_bytes() == original
        assert (markers / "prefix").read_text().splitlines() == ["done"]
        deliveries = [json.loads(line) for line in (markers / "deliveries-2.jsonl").read_text().splitlines()]
        assert len(deliveries) == 1 and "PENDING_EVENT" in deliveries[0]
        if brain == "stub":
            assert (markers / "attempt-1").exists()
            assert (markers / "attempt-2").exists()
    finally:
        for proc in processes:
            _kill_owned(proc)
        for log in logs:
            log.close()


def test_missing_deployment_and_bad_key_preserve_recovery_state(event_server, tmp_path, caplog):
    base, _, _ = event_server
    root = _root(tmp_path, base)
    deployment = _register(base, "recovery-manager", ["inbox/recovery-manager"])
    dep, key = deployment["deployment_id"], deployment["api_key"]
    save_bubble_state(root, deployment["bubble_id"], deployment["bubble_key"])
    save_deployment_state(root, "recovery-manager", dep, key)
    cursor = session_cursor_path(root, "recovery-manager")
    cursor.parent.mkdir(parents=True)
    cursor.write_text('{"last_seen":4}')
    state = paths.state_path(root) / "deployments/recovery-manager.json"
    original = state.read_bytes(), cursor.read_bytes()
    route = f"{base}/deployments/{dep}"
    # A temporary client outage followed by a real backend PUT must reuse the
    # deployment. Keep the server alive and finish well before eviction.
    with patch("bobi.http.put", side_effect=httpx.ReadTimeout("test outage")):
        with pytest.raises(RuntimeError, match="temporary transport failure"):
            _start_event_subscription("recovery-manager", ["inbox/recovery-manager"], root)
    assert (state.read_bytes(), cursor.read_bytes()) == original
    subscription = _start_event_subscription("recovery-manager", ["inbox/recovery-manager"], root)
    try:
        assert subscription.client.wait_connected(10)
        assert (state.read_bytes(), cursor.read_bytes()) == original
    finally:
        subscription.stop()
    with httpx.Client() as client:
        bad = client.put(route + "/subscriptions", headers={"Authorization": "Bearer wrong"}, json={"replace": ["inbox/recovery-manager"]})
        assert bad.status_code == 403 and bad.json() == {"error": "unauthorized"}
        client.delete(route, headers={"Authorization": f"Bearer {key}"}).raise_for_status()
        missing = client.put(route + "/subscriptions", headers={"Authorization": f"Bearer {key}"}, json={"replace": ["inbox/recovery-manager"]})
        assert missing.status_code == bad.status_code and missing.json() == bad.json()
    with patch("bobi.events.server.register") as register:
        with pytest.raises(RuntimeError, match="missing/expired or credentials may be invalid"):
            _start_event_subscription("recovery-manager", ["inbox/recovery-manager"], root)
        register.assert_not_called()
    assert (state.read_bytes(), cursor.read_bytes()) == original
    assert key not in caplog.text
    assert "replay continuity is unverified" in caplog.text
