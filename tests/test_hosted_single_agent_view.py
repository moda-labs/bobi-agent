"""The single-agent view on the hosted console - gates B3 through B6.

The hosted page answers from the supervisor's admin commands; the local page
answers from ``LocalRuntime``. The design bet is that neither re-implements a
read: both call the same pure builder over the same filesystem root, so they
CANNOT drift. These tests are what makes that a property rather than an
intention.

- **B3** runs both surfaces against ONE root and asserts the payloads are
  identical. A future edit that touches one path and not the other fails here,
  which is the whole reason the delegate design was chosen over two readers.
- **B4/B5** drive the three writes against real run files on disk - a real
  suspended run, a real claim contention, a real close - not a mocked
  ``WorkflowRun``. The one seam held back is ``subprocess.Popen``: resume is
  fire-and-forget by design, so what is asserted here is the argv it spawns
  and the claim that arbitrates two racing resumes. Whether the spawned CLI
  then completes a workflow is the workflow engine's own test surface.
- **B6** pins the mid-roll story: an instance whose sidecar predates these
  commands must read as unavailable AND be named, never as a timeout.
"""

import json

import httpx
import pytest

from bobi.monitors import run_records
from bobi.monitors.run_records import QUIET, MonitorRun
from bobi.supervisor.admin import AdminListener, AdminCommandError
from bobi.supervisor.snapshot import SUPERVISOR_VERSION
from bobi.webapp import run_actions
from bobi.webapp.event_bus import (
    MIN_VIEW_SUPERVISOR_VERSION,
    EventBusRuntime,
    encode_name,
)
from bobi.webapp.runtime import LocalRuntime, TeamLifecycleError, UnknownRun
from bobi.workflow.state import WorkflowRun

OPERATOR = "view-op-token"


class _Telemetry:
    def __init__(self):
        self.identity = {"fleet": "acme", "instance": "prod-1"}
        self.last_snapshot = None


class _Supervisor:
    pass


def _listener(root, published=None):
    """An AdminListener bound to a real BOBI_HOME, as it is on the box."""
    published = published if published is not None else []

    def publish(topic, source, data, project_root):
        published.append(data)
        return True

    listener = AdminListener(supervisor=_Supervisor(), telemetry=_Telemetry(),
                             project_root=root, publish_fn=publish)
    listener._published = published
    return listener


def _dispatch(listener, command, **args):
    """Run one command through the real dispatch and return its result
    envelope - the same path a broker message takes."""
    listener._published.clear()
    listener._dispatch({"payload": {"command_id": "c1", "command": command,
                                    "args": args}})
    return listener._published[0]


def _waiting_run(install, run_id="run-1", *, workflow="triage",
                 session_name="", status="waiting"):
    run = WorkflowRun(
        run_id=run_id, workflow_name=workflow,
        trigger_event={"kind": "slack"}, started_at="2026-08-05T00:00:00",
        status=status, suspended_at_step=2, await_event="approval",
        session_name=session_name,
    )
    run.save(root=install.repo_path)
    return run


def _monitor_run(install, monitor="inbox-sweep"):
    """A real monitor run record, written through the real store - a `$0`
    cached tick, which is exactly the row that has no transcript and so is
    what `run_details` exists to answer for."""
    run = MonitorRun(
        run_id=run_records.new_run_id(monitor), monitor=monitor,
        started_at="2026-08-05T00:00:00+00:00",
        ended_at="2026-08-05T00:00:04+00:00", outcome=QUIET,
        flavor="command", script_cache_mode="cached")
    run_records.record(run)
    return run


# --- B3: the two surfaces cannot drift ------------------------------------

def test_runs_payload_is_identical_on_both_surfaces(bobi_install):
    """The anti-drift gate. One root, two readers, byte-identical payloads."""
    _waiting_run(bobi_install)
    _monitor_run(bobi_install)

    local = LocalRuntime().runs(bobi_install.agent_name)
    hosted = _dispatch(_listener(bobi_install.repo_path), "runs")["result"]

    assert hosted == local
    # Not vacuous: the fold actually found the runs written above.
    assert local["total"] >= 2
    assert any(r["kind"] == "workflow" for r in local["runs"])


def test_runs_filters_and_pagination_match(bobi_install):
    for i in range(4):
        _waiting_run(bobi_install, run_id=f"run-{i}", workflow=f"wf-{i}")

    listener = _listener(bobi_install.repo_path)
    for kwargs in ({"status": "awaiting_action"}, {"query": "wf-2"},
                   {"offset": 1, "limit": 2}, {"limit": 1}):
        local = LocalRuntime().runs(bobi_install.agent_name, **kwargs)
        hosted = _dispatch(listener, "runs", **kwargs)["result"]
        assert hosted == local, kwargs


def test_overview_payload_is_identical_on_both_surfaces(bobi_install):
    local = LocalRuntime().overview(bobi_install.agent_name)
    hosted = _dispatch(_listener(bobi_install.repo_path), "overview")["result"]

    assert hosted == {"overview": local}
    assert local["name"] == bobi_install.agent_name


def test_run_details_payload_is_identical_on_both_surfaces(bobi_install):
    run = _monitor_run(bobi_install)

    local = LocalRuntime().run_details(bobi_install.agent_name, run.run_id)
    hosted = _dispatch(_listener(bobi_install.repo_path),
                       "run_details", run_id=run.run_id)["result"]

    assert hosted == {"details": local}
    assert local["run"]["run_id"] == run.run_id


def test_spend_carries_the_savings_block_hosted_too(bobi_install):
    """`script_cache` is part of the spend payload the page renders, so a
    hosted box that omitted it answered 200 with the savings block blank."""
    local = LocalRuntime().spend_summary(bobi_install.agent_name)
    hosted = _dispatch(_listener(bobi_install.repo_path), "spend")["result"]

    assert "script_cache" in hosted["spend"]
    assert hosted["spend"] == local


# --- B4: the three writes, against real runs on disk ----------------------

def test_resume_spawns_the_cli_and_returns_accepted(bobi_install, monkeypatch):
    _waiting_run(bobi_install, run_id="run-r")
    # The process boundary is the seam: resume is fire-and-forget by design,
    # so what this pins is the argv handed to the OS. `run_actions` imports
    # subprocess inside the call, so patching the module attribute catches it.
    spawned = []
    monkeypatch.setattr("subprocess.Popen",
                        lambda *a, **k: spawned.append(a[0]))

    out = run_actions.resume_run(bobi_install.repo_path, "run-r")

    assert out == {"ok": True, "accepted": True, "run_id": "run-r",
                   "workflow": "triage", "await_event": "approval",
                   "verdict": ""}
    argv = spawned[0]
    assert argv[1:] == ["-m", "bobi.cli", "agent", bobi_install.agent_name,
                        "workflows", "resume", "run-r"]


def test_resume_carries_the_gate_verdict_to_the_cli(bobi_install, monkeypatch):
    """The hosted surface answers a gate through the same seam.

    Both runtimes delegate to this module, so the verdict has to survive the
    argv boundary here or the hosted console's Approve resumes with no answer -
    which the workflow reads as "not an approval" and quietly reworks.
    """
    _waiting_run(bobi_install, run_id="run-r")
    spawned = []
    monkeypatch.setattr("subprocess.Popen",
                        lambda *a, **k: spawned.append(a[0]))

    out = run_actions.resume_run(bobi_install.repo_path, "run-r",
                                 verdict="approve", reply="ship it")

    assert out["verdict"] == "approve"
    assert spawned[0][-4:] == ["--verdict", "approve", "--reply", "ship it"]


def test_two_concurrent_resumes_do_not_double_run(bobi_install):
    """Single-winner, arbitrated where the claim actually lives.

    ``resume_run`` deliberately does NOT hold the claim - a claim held by a
    caller that then fails to spawn strands the run - so both callers spawn
    and the arbitration happens in ``WorkflowRun.claim``. Exactly one wins,
    which is what stops the workflow from advancing twice.
    """
    run = _waiting_run(bobi_install, run_id="run-race")
    a = WorkflowRun.from_dict(json.loads(
        (bobi_install.state_dir / "workflow" / "runs" /
         "run-race.json").read_text()))
    b = WorkflowRun.from_dict(json.loads(
        (bobi_install.state_dir / "workflow" / "runs" /
         "run-race.json").read_text()))

    won = [a.claim(root=bobi_install.repo_path),
           b.claim(root=bobi_install.repo_path)]

    assert won.count(True) == 1, "exactly one resume may claim the run"
    assert run.run_id == "run-race"


def test_close_really_closes_the_run_and_cancels_its_session(bobi_install):
    _waiting_run(bobi_install, run_id="run-c", session_name="worker-1")
    (bobi_install.sessions_dir / "worker-1").mkdir(parents=True, exist_ok=True)
    (bobi_install.sessions_dir / "worker-1" / "state.json").write_text(
        json.dumps({"name": "worker-1", "session_id": "sid-1",
                    "role": "engineer", "status": "running", "pid": 0}))

    out = run_actions.close_run(bobi_install.repo_path, "run-c")

    assert out == {"ok": True, "closed": True, "run_id": "run-c",
                   "workflow": "triage"}
    remaining = WorkflowRun.list_runs(root=bobi_install.repo_path)
    closed = next(r for r in remaining if r.run_id == "run-c")
    assert closed.status == "cancelled"


def test_close_over_the_admin_command_returns_the_same_shape(bobi_install):
    _waiting_run(bobi_install, run_id="run-cc")

    envelope = _dispatch(_listener(bobi_install.repo_path),
                         "close_run", run_id="run-cc")

    assert envelope["status"] == "done"
    assert envelope["result"] == {"ok": True, "closed": True,
                                  "run_id": "run-cc", "workflow": "triage"}


# --- B5: the documented error shapes --------------------------------------

def test_unknown_run_is_reported_with_its_code(bobi_install):
    envelope = _dispatch(_listener(bobi_install.repo_path),
                         "resume_run", run_id="nope")

    assert envelope["status"] == "error"
    assert envelope["result"] == {"code": "unknown_run", "run_id": "nope"}
    assert "unknown run 'nope'" in envelope["error"]


def test_non_resumable_run_is_reported_with_its_status(bobi_install):
    _waiting_run(bobi_install, run_id="run-done", status="completed")

    envelope = _dispatch(_listener(bobi_install.repo_path),
                         "resume_run", run_id="run-done")

    assert envelope["status"] == "error"
    assert envelope["result"] == {"code": "not_waiting", "run_id": "run-done",
                                  "status": "completed"}
    assert "not 'waiting'" in envelope["error"]


def test_missing_run_id_is_a_bad_request_not_an_unknown_run(bobi_install):
    envelope = _dispatch(_listener(bobi_install.repo_path), "close_run")

    assert envelope["result"] == {"code": "bad_request"}
    assert "run_id is required" in envelope["error"]


def test_remind_reports_a_workflow_that_is_gone(bobi_install):
    _waiting_run(bobi_install, run_id="run-m", workflow="vanished")

    envelope = _dispatch(_listener(bobi_install.repo_path),
                         "remind_run", run_id="run-m")

    assert envelope["result"]["code"] == "action_failed"
    assert "no longer installed" in envelope["error"]


def test_local_runtime_maps_the_same_refusals_to_typed_errors(bobi_install):
    _waiting_run(bobi_install, run_id="run-done2", status="completed")
    rt = LocalRuntime()

    with pytest.raises(UnknownRun):
        rt.resume_run(bobi_install.agent_name, "nope")
    with pytest.raises(TeamLifecycleError, match="not 'waiting'"):
        rt.close_run(bobi_install.agent_name, "run-done2")


# --- B6: an old sidecar reads as unavailable, and is NAMED ----------------

class _FleetStub:
    """A /fleet API whose instance answers commands it knows and silently
    drops the ones it does not - exactly what an older supervisor does."""

    def __init__(self, supervisor_version, *, known=()):
        self.supervisor_version = supervisor_version
        self.known = set(known)
        self.commands: dict[str, dict] = {}
        self._n = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/commands") and request.method == "POST":
            self._n += 1
            cid = f"cmd-{self._n}"
            body = json.loads(request.content)
            self.commands[cid] = {"command": body["command"]}
            return httpx.Response(200, json={"command_id": cid})
        if "/commands/" in path:
            cid = path.rsplit("/", 1)[-1]
            command = self.commands.get(cid, {}).get("command")
            if command not in self.known:
                # Dropped without a reply: pending forever, like the real one.
                return httpx.Response(200, json={"status": "pending"})
            return httpx.Response(200, json={"status": "done", "result": {}})
        detail = {"deployment": {"fleet": "acme", "instance": "prod-1"},
                  "manager": {"status": "idle", "pid": 1},
                  "reachability": "live"}
        if self.supervisor_version is not None:
            detail["supervisor"] = {"version": self.supervisor_version}
        return httpx.Response(200, json=detail)


# A short bound so the timeout legs below stay fast. The production value
# (DEFAULT_VIEW_COMMAND_TIMEOUT) is a judgement about webapp workers, not
# about what makes a dropped command observable - a drop is a drop at any
# deadline.
TEST_TIMEOUT = 0.05


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _runtime(stub, handler=None):
    return EventBusRuntime(
        fleet_api_url="http://fleet.test", operator_token=OPERATOR,
        poll_interval=0.0, view_command_timeout=TEST_TIMEOUT,
        run_write_command_timeout=TEST_TIMEOUT,
        client=_client(handler or stub.handler))


@pytest.mark.parametrize("version", ["0.1.0", None])
def test_old_supervisor_surfaces_as_unavailable_naming_the_instance(version):
    rt = _runtime(_FleetStub(version))

    with pytest.raises(TeamLifecycleError) as e:
        rt.runs("acme~prod-1")

    message = str(e.value)
    assert "unavailable" in message
    assert encode_name("acme", "prod-1") in message, "must name the instance"
    assert "timed out" not in message, "a working-but-old box is not a timeout"
    assert ".".join(str(p) for p in MIN_VIEW_SUPERVISOR_VERSION) in message


def test_the_unavailable_check_does_not_key_off_error_wording():
    """The classification keys off a TYPE, not a phrase.

    An earlier cut matched `"timed out" in str(e)`, which would go quietly
    dead the first time someone rephrased the message - and going dead here
    means silently reverting to blaming the network for every old box. This
    asserts the timeout is its own exception, and that rewording it changes
    nothing.
    """
    from bobi.webapp import event_bus

    assert issubclass(event_bus._CommandTimeout, TeamLifecycleError)
    rt = _runtime(_FleetStub("0.1.0"))
    original = event_bus._CommandTimeout.__init__

    try:
        # Reword every timeout message; the verdict must not move.
        event_bus._CommandTimeout.__init__ = (
            lambda self, msg: original(self, "no reply from the deployment"))
        with pytest.raises(TeamLifecycleError, match="unavailable"):
            rt.runs("acme~prod-1")
    finally:
        event_bus._CommandTimeout.__init__ = original


def test_a_two_component_version_is_not_read_as_older():
    """"0.2" means unspecified patch, not "below 0.2.0"."""
    from bobi.webapp.event_bus import _version_tuple

    assert _version_tuple("0.2") >= MIN_VIEW_SUPERVISOR_VERSION
    assert _version_tuple("0.1.9") < MIN_VIEW_SUPERVISOR_VERSION
    assert _version_tuple("") is None
    assert _version_tuple(None) is None


def test_a_wedged_current_supervisor_still_reads_as_a_timeout():
    """The inverse, and the reason the version is consulted at all: a box
    NEW enough to know the command but not answering is stuck, and calling
    that 'unavailable, too old' would be the same lie inverted."""
    rt = _runtime(_FleetStub(SUPERVISOR_VERSION))

    with pytest.raises(TeamLifecycleError, match="timed out"):
        rt.overview("acme~prod-1")


def test_a_current_supervisor_answers_the_view_commands():
    """Not vacuous: the same harness succeeds once the command is known."""
    rt = _runtime(_FleetStub(SUPERVISOR_VERSION, known={"runs", "overview"}))

    assert rt.overview("acme~prod-1") == {}
    assert rt.runs("acme~prod-1")["runs"] == []


def test_transcript_without_entries_is_unavailable_not_a_silent_chat_view():
    """A supervisor that predates the `detail` arg answers the chat view.
    Rendering that as the debugging transcript would drop every tool call
    without saying so, which is the failure this check exists to prevent."""
    stub = _FleetStub(SUPERVISOR_VERSION, known={"transcript"})

    def handler(request):
        resp = stub.handler(request)
        if "/commands/" in request.url.path and resp.json().get("status") == "done":
            return httpx.Response(200, json={"status": "done",
                                             "result": {"messages": []}})
        return resp

    rt = _runtime(stub, handler)

    with pytest.raises(TeamLifecycleError, match="unavailable"):
        rt.transcript("acme~prod-1", "bobi-x-manager")


def test_unknown_run_travels_back_as_the_typed_error():
    """The code on the error result is what lets the hosted runtime raise the
    SAME exception the local one raises, so the handler answers alike."""
    stub = _FleetStub(SUPERVISOR_VERSION, known={"run_details"})

    def handler(request):
        resp = stub.handler(request)
        if "/commands/" in request.url.path and resp.json().get("status") == "done":
            return httpx.Response(200, json={
                "status": "error", "error": "unknown run 'ghost'",
                "result": {"code": "unknown_run", "run_id": "ghost"}})
        return resp

    rt = _runtime(stub, handler)

    with pytest.raises(UnknownRun) as e:
        rt.run_details("acme~prod-1", "ghost")
    assert e.value.run_id == "ghost"


# --- the protocol version is the capability signal ------------------------

def test_supervisor_version_is_at_least_what_the_runtime_requires():
    """A bump that forgets one side is a silent mid-roll failure: the box
    would answer commands the runtime still believes it cannot."""
    shipped = tuple(int(p) for p in SUPERVISOR_VERSION.split("."))
    assert shipped >= MIN_VIEW_SUPERVISOR_VERSION


def test_admin_command_error_carries_only_the_detail_it_has():
    err = AdminCommandError("boom", "not_waiting", run_id="r", status=None)
    assert err.detail == {"run_id": "r"}
