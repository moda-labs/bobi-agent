import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest

from bobi.brain.stub import StubBrain
from bobi.inbox import Message
from bobi.metrics.collector import MetricsCollectorService
from bobi.metrics.policies.typesafe import TypeSafePolicy
from bobi.metrics.runtime import MetricsRuntime
from bobi.metrics.store import connect
from bobi.sdk import load_session_route
from bobi.session import Session
from bobi.subagent import _run_agent_supervised
from bobi.workflow.orchestrator import run_workflow
from bobi.workflow.schema import StepDef, Workflow
from tests.metrics.test_routing import configured, context


@pytest.fixture
def fault_server():
    state = {"fault": None, "calls": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["calls"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if state["fault"] == "timeout":
                time.sleep(0.3)
                return
            status = {"5xx": 503, "401": 401, "403": 403, "429": 429}.get(state["fault"], 200)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if state["fault"] == "malformed":
                self.wfile.write(b"invalid JSON")
                return
            self.wfile.write(json.dumps({
                "model": "changed" if state["fault"] == "version" else "jev-1.13.0",
                "answers": {"route": {"type": "choice",
                    "choice": "unknown" if state["fault"] == "invalid_model" else "cheap",
                    "confidence": 0.84 if state["fault"] == "low_confidence" else 1,
                    "probabilities": {"cheap": 1, "control": 0}}},
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("entry_point,mode,fault,reason", [
    ("session_start", "shadow", None, None),
    ("session_start", "enforce", None, None),
    ("subagent_phase", "shadow", None, None),
    ("subagent_phase", "enforce", None, None),
    ("subagent_persistent", "shadow", None, None),
    ("subagent_persistent", "enforce", None, None),
    ("subagent_supervised", "shadow", None, None),
    ("subagent_supervised", "enforce", None, None),
    ("workflow_start", "shadow", None, None),
    ("workflow_start", "enforce", None, None),
    ("session_start", "shadow", "typesafe_ok", None),
    ("session_start", "enforce", "typesafe_ok", None),
    ("session_start", "enforce", "timeout", "policy_timeout"),
    ("session_start", "enforce", "5xx", "policy_unavailable"),
    ("session_start", "enforce", "invalid_model", "policy_invalid_response"),
    ("session_start", "enforce", "version", "policy_version_drift"),
    ("session_start", "enforce", "401", "policy_unavailable"),
    ("session_start", "enforce", "403", "policy_unavailable"),
    ("session_start", "enforce", "429", "policy_unavailable"),
    ("session_start", "enforce", "malformed", "policy_invalid_response"),
    ("session_start", "enforce", "low_confidence", "policy_low_confidence"),
    ("workflow_start", "shadow", "timeout", "policy_timeout"),
    ("subagent_supervised", "enforce", "low_confidence", "policy_low_confidence"),
    ("subagent_persistent", "shadow", "malformed", "policy_invalid_response"),
])
def test_route_executes_and_projects_a_scripted_turn(bobi_install, monkeypatch, entry_point,
                                                    mode, fault, reason, fault_server):
    monkeypatch.setattr("bobi.metrics.policy._breakers", {})
    raw = configured()
    raw["policy"].update(mode=mode, scope={
        "roles": ["engineer"], "entry_points": [entry_point]})
    failed_policy = reason is not None
    executed_model = "control" if failed_policy or mode == "shadow" else "cheap"
    endpoint, server_state = fault_server
    if fault:
        raw["policy"].update(name="typesafe-jev", version="jev-1.13.0", deadline_ms=200,
            options={"instructions": "route", "criteria": {"control": "full", "cheap": "small"}})
        raw["variants"][1]["policy"] = "typesafe-jev"
        server_state["fault"] = fault
        monkeypatch.setenv("ROUTING_TEST_KEY", "test-only-key")

        def local_policy(config):
            policy = TypeSafePolicy(config)
            policy.endpoint = endpoint
            return policy

        monkeypatch.setattr("bobi.metrics.routing.load_policy", local_policy)
    ctx = replace(context(raw, "treatment"), entry_point=entry_point)
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_SUBJECT", ctx.experiment_subject)
    monkeypatch.setenv("BOBI_STUB_BRAIN", "1")
    monkeypatch.setenv("BOBI_BRAIN", "codex")
    monkeypatch.setattr("bobi.brain.session_brain_label", lambda: "codex")
    monkeypatch.setattr("bobi.subagent.session_brain_label", lambda: "codex")
    brain = StubBrain()
    monkeypatch.setattr("bobi.brain.get_brain", lambda *args: brain)
    monkeypatch.setattr("bobi.session.get_brain", lambda *args: brain)
    monkeypatch.setattr("bobi.events.publish.post_event", lambda *args, **kwargs: True)
    root = bobi_install.repo_path
    runtime = MetricsRuntime(root, mode="enabled")
    monkeypatch.setattr("bobi.metrics.runtime._runtime", runtime)
    session = None
    try:
        if entry_point == "workflow_start":
            output = {}
            assert run_workflow(Workflow(name="routed", steps=[
                StepDef(name="work", prompt="__stub__:options", agent="engineer")]),
                task="test", repo="test", cwd=str(root), run_key="routing", collect=output)
            text = output["final_text"]
        elif entry_point == "subagent_supervised":
            result = asyncio.run(_run_agent_supervised("__stub__:options", str(root),
                "routing", "work", 5, role="engineer", fresh=True))
            assert not result.error
            text = result.final_text
        else:
            session = Session("agent", str(root), fresh=True, role="engineer",
                extra_options={"model": "configured"}, routing=ctx)
            assert session.start("__stub__:options", timeout=5)
            text = session._last_response
        assert json.loads(text)["model"] == executed_model
        if session is not None and not failed_policy:
            session.stop()
            assert runtime.close(timeout=2)
            runtime = MetricsRuntime(root, mode="enabled")
            monkeypatch.setattr("bobi.metrics.runtime._runtime", runtime)

            def no_policy(config):
                raise AssertionError("resume must not load a policy")

            monkeypatch.setattr("bobi.metrics.routing.load_policy", no_policy)
            original_make_session = brain.make_session
            resume_attempts = []

            def recovering_session(**kwargs):
                client = original_make_session(**kwargs)
                resume_attempts.append(kwargs.get("resume"))
                if kwargs.get("resume"):
                    async def stale_connect():
                        raise RuntimeError("test-only stale provider token")
                    client.connect = stale_connect
                else:
                    assert load_session_route("agent", root=root) is not None
                return client

            monkeypatch.setattr(brain, "make_session", recovering_session)
            session = Session("agent", str(root), fresh=False, role="engineer",
                extra_options={"model": "configured"}, routing=ctx)
            assert session.start("__stub__:options", timeout=5)
            assert json.loads(session._last_response)["model"] == executed_model
            assert resume_attempts == ["stub-session", None]
            candidate = original_make_session(options={"model": executed_model})
            asyncio.run_coroutine_threadsafe(candidate.connect(), session._loop).result(timeout=5)
            asyncio.run_coroutine_threadsafe(session._commit_rotation(candidate, "test"),
                                            session._loop).result(timeout=5)
            assert load_session_route("agent", root=root) is not None
            asyncio.run_coroutine_threadsafe(session._process_message(
                Message(id="rotation", sender="test", text="__stub__:options")),
                session._loop).result(timeout=5)
            assert json.loads(session._last_response)["model"] == executed_model
    finally:
        if session is not None:
            session.stop()
        assert runtime.close(timeout=2)
    MetricsCollectorService(root).collect_once()
    with connect(root / "state/metrics/metrics.db", readonly=True) as connection:
        row = connection.execute("SELECT * FROM router_decisions").fetchone()
        assert row["model_selected"] == executed_model
        assert row["variant_id"] == "treatment" and row["fallback_reason"] == reason
        metadata = json.loads(row["metadata_json"])
        assert metadata["entry_point"] == entry_point
        assert metadata["policy"]["status"] == ("failed" if failed_policy else "decided")
        assert metadata["policy"]["call_id"]
        if fault is None:
            assert metadata["policy"]["cost_usd"] == 0.0
        assert connection.execute("SELECT status FROM turns").fetchone()[0] == "completed"
        if session is not None and not failed_policy:
            decisions = connection.execute("SELECT metadata_json FROM router_decisions ORDER BY decided_at_us").fetchall()
            assert [json.loads(row[0])["policy"]["status"] for row in decisions] == ["decided", "reused", "reused"]
    if entry_point != "workflow_start":
        name = session.name if session is not None else "engineer-routing-work"
        assert load_session_route(name, root=root)["decision"]["model_selected"] == executed_model
    assert len(server_state["calls"]) == (1 if fault else 0)
    assert all("task" not in call["state"] for call in server_state["calls"])
