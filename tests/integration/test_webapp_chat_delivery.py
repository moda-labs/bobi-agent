"""The run slab's reply box, end to end against real sessions (#987).

The risk this feature carries is a box that accepts typing and delivers
nothing, so these tests refuse to stop at "the POST returned 200". Each one
drives the real `POST /api/agents/{name}/chat` route on a real
`$BOBI_HOME`, polls the real job, and then reads the target session's own
transcript to see whether the words actually arrived.

Three facts are pinned, and they are the three the design rests on:

1. **A live session receives it.** The webapp's chat lands on `inbox.deliver`,
   the same function `bobi agent <name> message` reaches from a terminal, so a
   reply typed in the modal is the terminal's transport one call deeper.
2. **A suspended gate refuses it, out loud.** Its process exited when the
   workflow suspended, and `deliver` will not publish to a dead pid. That is
   not a bug to route around - the terminal cannot reach those rows either -
   and the composer's other branch exists because of it. A future change that
   points the reply branch at gate rows fails here.
3. **The continuation reaches the manager, and the parked run is untouched.**
   An empty `subagent` means the team manager on both runtimes now, so the
   relay is delivered rather than dropped. The run it names is then read back
   byte for byte: nothing in this feature may advance a workflow, because a
   suspended run resumes at the step AFTER its gate.

One mechanism, two brains: the stub leg always runs, and the claude leg runs
when the CLI is there. A turn taken by a live session through the inbox is
exactly the brain-path case CLAUDE.md names, so the real leg is not optional
dressing - it is the acceptance bar.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.request

import pytest
import yaml
from fastapi.testclient import TestClient

from bobi import service
from bobi.runtime_guard import with_mutable_runtime_package
from bobi.session import Session
from bobi.webapp import server

TOKEN = "chat-delivery-token"


@pytest.fixture
def bobi_env(dual_brain_env):
    return dual_brain_env


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def chat_event_server(bobi_env):
    """A real local event server, because the inbox has no other transport.

    `deliver` publishes an ``inbox/<session>`` event and, for a blocking send,
    waits on a transient reply subscription. Both are the event server. Stub
    it and this suite would prove that a fake bus carries a fake message; the
    whole point here is that the operator's words reach a real turn.
    """
    from bobi.events import publish as _pub
    from bobi.events.server import ensure_running

    port = _free_port()
    url = f"http://localhost:{port}"

    agent_yaml = bobi_env.package_dir / "agent.yaml"
    original = agent_yaml.read_text()
    data = yaml.safe_load(original)
    data["event_server_url"] = url
    with with_mutable_runtime_package(bobi_env.project_path):
        agent_yaml.write_text(yaml.dump(data))
    _pub._es_url_cache.clear()   # the resolved URL is cached per project root

    try:
        ensure_running(port, project_path=bobi_env.project_path)
    except RuntimeError as e:      # no Node, or its deps cannot be installed
        pytest.skip(f"local event server unavailable: {e}")

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
                if json.loads(r.read()).get("status") == "ok":
                    break
        except Exception:
            time.sleep(0.25)
    else:
        pytest.skip("local event server never became healthy")

    yield url

    pid_file = bobi_env.state_dir / "event-server.pid"
    if pid_file.exists():
        import os
        import signal
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass
        pid_file.unlink(missing_ok=True)
    with with_mutable_runtime_package(bobi_env.project_path):
        agent_yaml.write_text(original)
    _pub._es_url_cache.clear()


def _client():
    c = TestClient(server.build_app(token=TOKEN), base_url="http://127.0.0.1")
    c.headers.update({"x-bobi-webui-token": TOKEN})
    return c


def _submit(client, agent, subagent, text):
    r = client.post(f"/api/agents/{agent}/chat",
                    json={"subagent": subagent, "text": text})
    assert r.status_code == 200, r.text
    return r.json()["message_id"]


def _await_job(client, agent, message_id, timeout=180):
    """Poll the real job the way the page does, and never longer."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/api/agents/{agent}/chat/{message_id}")
        assert r.status_code == 200
        job = r.json()
        if job["status"] != "pending":
            return job
        time.sleep(0.25)
    raise AssertionError("chat job never resolved")


def _reply_directive(env, marker):
    """A turn that answers with `marker`, on whichever brain is selected."""
    if env.env.get("BOBI_BRAIN") == "stub":
        return f"__stub__:reply:{marker}"
    return (f"Reply with exactly this one word and nothing else: {marker}")


def _messages(root, session):
    """Every turn recorded for a session, transcript and chat log alike."""
    from bobi.chat_history import read_chat, read_transcript_messages
    from bobi.sdk import load_session_brain, load_session_id

    messages = read_transcript_messages(load_session_id(session, root=root),
                                        brain=load_session_brain(session,
                                                                 root=root))
    return messages + read_chat(root, session)


def _said_by(root, session, role):
    """What one side of the conversation said.

    Split by role on purpose. Asserting a marker against the whole transcript
    would pass on the echo of the prompt that carried it, which proves the
    message was written down and nothing about it having been answered.
    """
    return "\n".join(m.get("text", "") for m in _messages(root, session)
                     if m.get("role") == role)


@pytest.mark.timeout(300)
def test_a_reply_typed_on_a_live_session_reaches_that_session(bobi_env):
    """The test that proves the feature.

    Not "the endpoint answered" but "the agent got the message": the assertion
    is on the target session's own recorded turns.
    """
    root = bobi_env.project_path
    name = "chat-live-target"
    session = Session(name=name, cwd=str(root),
                      system_prompt={"type": "preset", "preset": "claude_code"})
    try:
        assert session.start(startup_prompt=None, timeout=120), \
            "session failed to start"

        client = _client()
        typed = _reply_directive(bobi_env, "PONG-987")
        job = _await_job(client, bobi_env.agent_name,
                         _submit(client, bobi_env.agent_name, name, typed))
        assert job["status"] == "done", job

        assert typed in _said_by(root, name, "user"), \
            "the operator's message never reached the session"
        # The answer, from the agent's own side of the transcript: proof a
        # turn was taken, not just that the request was written down.
        assert "PONG-987" in _said_by(root, name, "agent"), \
            "the session never answered"
    finally:
        session.stop()


@pytest.mark.timeout(300)
def test_a_suspended_gates_session_refuses_delivery_rather_than_swallowing_it(
        bobi_env):
    """The limitation, pinned as a regression guard.

    A workflow parked on an approval gate has no process: the orchestrator
    disconnects its client and returns at the await step. The registry entry
    stays `waiting` with a stale pid, and `deliver` checks the pid before it
    publishes. So the honest outcome is a refusal the page can show, not a
    silent accept - and the composer must keep taking its other branch here.
    """
    from bobi.sdk import SessionEntry, get_registry

    root = bobi_env.project_path
    gate = "wf-issue-lifecycle-test-repo-987"
    registry = get_registry()
    # What a suspended gate leaves on disk: a `waiting` entry, never corrected
    # (the reaper only inspects live-looking statuses), and a pid that is gone.
    registry.register(SessionEntry(name=gate, role="engineer",
                                   status="waiting", pid=999_999_999))

    client = _client()
    job = _await_job(client, bobi_env.agent_name,
                     _submit(client, bobi_env.agent_name, gate, "are you there?"))
    assert job["status"] == "error", job
    assert gate in job["error"]

    # And nothing was written to it: a refusal is not a half-delivery.
    assert "are you there?" not in _said_by(root, gate, "user")


@pytest.mark.timeout(300)
def test_the_continuation_relay_reaches_the_manager(bobi_env):
    """The continuation branch, with the manager really listening.

    An empty `subagent` resolves to the manager on this runtime now, matching
    what the hosted one has always done. The test asserts DELIVERY, not the
    spawn: starting a fresh session is the manager's judgement, and a spec
    should not describe a delegated request as a guaranteed process.
    """
    root = bobi_env.project_path
    manager = service.manager_session_name(root)
    session = Session(name=manager, cwd=str(root),
                      system_prompt={"type": "preset", "preset": "claude_code"})
    try:
        assert session.start(startup_prompt=None, timeout=120), \
            "manager session failed to start"

        client = _client()
        relay = ("[bobi console] The operator typed this on a run whose "
                 "session has ended.\n\n" + _reply_directive(bobi_env, "RELAY-OK"))
        # An empty subagent, exactly as the page sends it.
        job = _await_job(client, bobi_env.agent_name,
                         _submit(client, bobi_env.agent_name, "", relay))
        assert job["status"] == "done", job
        assert "session has ended" in _said_by(root, manager, "user"), \
            "the relay never reached the manager"
        assert "RELAY-OK" in _said_by(root, manager, "agent"), \
            "the manager never took the turn"
    finally:
        session.stop()


@pytest.mark.timeout(300)
def test_the_parked_run_is_byte_identical_afterwards(bobi_env):
    """The anti-regression that would fail if this feature ever resumed.

    `run.suspended_at_step` is the step AFTER the gate, so a resume starts the
    next step - for `issue-lifecycle`, writing code against an unapproved
    spec. Nothing in the composer's path may touch the run record, so the
    record is read as raw bytes before and after a full send.
    """
    root = bobi_env.project_path
    runs_dir = root / "state" / "workflow" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    record = runs_dir / "11d31ce5.json"
    record.write_text(json.dumps({
        "run_id": "11d31ce5", "workflow_name": "issue-lifecycle",
        "trigger_event": {"type": "github/issue.opened", "data": {}},
        "started_at": "2026-08-12T14:00:00+00:00", "completed_at": "",
        "status": "waiting", "suspended_at_step": 7,
        "await_event": "approval",
        "session_name": "wf-issue-lifecycle-test-repo-987",
        "variable_scopes": {}, "repo": "bobi-agent", "cwd": "",
        "run_key": "987", "resumed_at": "",
    }))
    before = record.read_bytes()

    manager = service.manager_session_name(root)
    session = Session(name=manager, cwd=str(root),
                      system_prompt={"type": "preset", "preset": "claude_code"})
    try:
        assert session.start(startup_prompt=None, timeout=120), \
            "manager session failed to start"
        client = _client()
        job = _await_job(client, bobi_env.agent_name,
                         _submit(client, bobi_env.agent_name, "",
                                 _reply_directive(bobi_env, "ACK")))
        assert job["status"] == "done", job
    finally:
        session.stop()

    assert record.read_bytes() == before, \
        "the parked run record changed: something reached the resume path"
