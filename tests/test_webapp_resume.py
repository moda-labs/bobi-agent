"""The runs table's one write action (U6): resuming a suspended workflow run.

Resume is a SPAWN, so the tests assert on what gets spawned rather than on a
workflow actually running: the endpoint's whole job is to validate, hand off to
a dedicated process, and return without holding the request open.
"""

import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bobi import paths
from bobi.webapp import runtime as runtime_mod
from bobi.webapp import server
from bobi.webapp.runtime import LocalRuntime, TeamLifecycleError, UnknownRun
from bobi.workflow.state import WorkflowRun
from bobi.workflow.schema import StepDef, Workflow

TOKEN = "resume-token-123"


def _seed_run(run_id="wf-1", *, status="waiting", name="await-review",
              await_event="pr.merged", suspended_at_step=3,
              session_name="worker", variable_scopes=None, cwd=""):
    run = WorkflowRun(
        run_id=run_id, workflow_name=name,
        trigger_event={"type": "issue.assigned", "data": {}},
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        status=status, suspended_at_step=suspended_at_step,
        await_event=await_event, session_name=session_name,
        variable_scopes=variable_scopes or {}, cwd=cwd,
    )
    run.save()
    return run


@pytest.fixture
def spawned(monkeypatch):
    """Capture Popen instead of launching a real resume process."""
    calls = []

    class _Proc:
        pid = 4242

    def _popen(cmd, **kw):
        calls.append({"cmd": cmd, **kw})
        return _Proc()

    monkeypatch.setattr("subprocess.Popen", _popen)
    return calls


def _client():
    app = server.build_app(token=TOKEN)
    c = TestClient(app, base_url="http://127.0.0.1")
    c.headers.update({"x-bobi-webui-token": TOKEN})
    return c


def _install_workflow(install, name="await-review"):
    directory = paths.workflows_dir(install.repo_path)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yaml").write_text(
        f"name: {name}\nsteps:\n"
        "  - name: notify_review\n"
        "    notify: slack\n"
        "    message: Review this\n"
        "  - name: await_review\n"
        "    await: approval\n"
        "  - name: finish\n"
        "    prompt: finish\n")


class TestResumeAction:
    def test_spawns_the_cli_resume_for_this_team(self, bobi_install, spawned):
        _seed_run()
        body = LocalRuntime().resume_run(bobi_install.agent_name, "wf-1")
        assert body == {"ok": True, "accepted": True, "run_id": "wf-1",
                        "workflow": "await-review",
                        "await_event": "pr.merged"}
        [call] = spawned
        assert call["cmd"][1:] == [
            "-m", "bobi.cli", "agent", bobi_install.agent_name,
            "workflows", "resume", "wf-1"]
        assert call["cwd"] == str(bobi_install.repo_path)

    def test_the_resume_runs_in_its_own_session(self, bobi_install, spawned):
        # A dedicated, detached process — not a thread, and not a child that
        # dies with the web app. resume_workflow stamps the registry entry
        # with os.getpid(), so the pid it stamps must be the run's own.
        _seed_run()
        LocalRuntime().resume_run(bobi_install.agent_name, "wf-1")
        assert spawned[0]["start_new_session"] is True

    def test_returns_before_the_workflow_finishes(self, bobi_install, spawned):
        # "accepted", not "resumed": no request is ever held open for a
        # workflow run.
        _seed_run()
        body = LocalRuntime().resume_run(bobi_install.agent_name, "wf-1")
        assert body["accepted"] is True

    def test_unknown_run(self, bobi_install, spawned):
        with pytest.raises(UnknownRun):
            LocalRuntime().resume_run(bobi_install.agent_name, "nope")
        assert spawned == []

    @pytest.mark.parametrize("status", ["completed", "running", "failed",
                                        "resuming"])
    def test_only_a_waiting_run_resumes(self, bobi_install, spawned, status):
        _seed_run(status=status)
        with pytest.raises(TeamLifecycleError):
            LocalRuntime().resume_run(bobi_install.agent_name, "wf-1")
        assert spawned == []

    def test_the_claim_is_not_taken_by_the_caller(self, bobi_install, spawned):
        # A claim held by a caller that then fails to spawn strands the run,
        # so the claim belongs to the process doing the work. The run file
        # must still be here, unclaimed, after the endpoint returns.
        _seed_run()
        LocalRuntime().resume_run(bobi_install.agent_name, "wf-1")
        runs_dir = paths.state_path(bobi_install.repo_path) / "workflow" / "runs"
        assert (runs_dir / "wf-1.json").exists()
        assert not (runs_dir / "wf-1.resuming.json").exists()


class TestClaimIsSingleWinner:
    def test_second_claim_loses(self, bobi_install):
        # The guard the CLI resume gained: exactly one process proceeds.
        run_a = _seed_run()
        run_b = WorkflowRun.load("wf-1")
        assert run_a.claim() is True
        assert run_b.claim() is False

    def test_cli_resume_refuses_a_claimed_run(self, bobi_install):
        # The guard lives in the spawned command, so prove it there: with the
        # run already claimed, a second resume must stop before doing any
        # work rather than running the workflow a second time.
        from click.testing import CliRunner

        from bobi.cli import main

        _seed_run()
        WorkflowRun.load("wf-1").claim()
        result = CliRunner().invoke(
            main, ["agent", bobi_install.agent_name, "workflows", "resume",
                   "wf-1"])
        assert result.exit_code == 1
        # Claimed runs leave no <run_id>.json, so this stops at the lookup —
        # either way it never reaches resume_workflow.
        assert "wf-1" in result.output

    def test_claim_renames_the_run_file(self, bobi_install):
        _seed_run()
        runs_dir = paths.state_path(bobi_install.repo_path) / "workflow" / "runs"
        WorkflowRun.load("wf-1").claim()
        assert not (runs_dir / "wf-1.json").exists()
        claimed = json.loads((runs_dir / "wf-1.resuming.json").read_text())
        assert claimed["status"] == "resuming"
        assert claimed["resumed_at"]


class TestCloseIsSingleWinner:
    def test_close_publishes_a_terminal_cancelled_run(self, bobi_install):
        _seed_run()
        closed = WorkflowRun.close("wf-1", root=bobi_install.repo_path)
        assert closed is not None
        assert closed.status == "cancelled"
        assert closed.completed_at
        assert closed.await_event == ""
        saved = WorkflowRun.list_runs(root=bobi_install.repo_path)[0]
        assert saved.status == "cancelled"

    def test_close_loses_after_resume_claims(self, bobi_install):
        run = _seed_run()
        assert run.claim() is True
        assert WorkflowRun.close(
            "wf-1", root=bobi_install.repo_path) is None

    def test_resume_claim_loses_after_close(self, bobi_install):
        run = _seed_run()
        assert WorkflowRun.close(
            "wf-1", root=bobi_install.repo_path) is not None
        assert run.claim() is False


class TestReminderReconstruction:
    def test_replays_the_visited_notification_without_mutating_run(
            self, bobi_install, monkeypatch):
        from bobi.workflow import orchestrator

        scopes = {
            "requested_by": {"channel": "C123", "thread_ts": "99.1"},
            "draft": {"summary": "Review this"},
            "_runtime": {"visits": {"notify_review": 1}},
        }
        run = _seed_run(
            await_event="approval", suspended_at_step=2,
            variable_scopes=scopes, cwd=str(bobi_install.repo_path))
        workflow = Workflow("await-review", [
            StepDef(name="notify_review", notify="slack",
                    message="${{draft.summary}}"),
            StepDef(name="await_review", await_event="approval"),
            StepDef(name="finish", prompt="finish"),
        ])
        calls = []

        def _notify(step, ctx, cwd, run_key, workflow_name):
            calls.append((step.name, ctx.resolve(step.message), cwd))
            return SimpleNamespace(delivered=True, error="")

        monkeypatch.setattr(orchestrator, "_execute_notify_step", _notify)
        outcome = orchestrator.remind_workflow(run, workflow)
        assert outcome.delivered is True
        assert calls == [("notify_review", "Review this",
                          str(bobi_install.repo_path))]
        assert run.status == "waiting"
        assert run.await_event == "approval"

    def test_visit_map_selects_the_branch_that_was_delivered(
            self, bobi_install, monkeypatch):
        from bobi.workflow import orchestrator

        run = _seed_run(
            await_event="approval", suspended_at_step=3,
            variable_scopes={"_runtime": {"visits": {"fallback": 1}}},
            cwd=str(bobi_install.repo_path))
        workflow = Workflow("await-review", [
            StepDef(name="fallback", notify="slack", goto="await_review"),
            StepDef(name="normal", notify="slack"),
            StepDef(name="await_review", await_event="approval"),
            StepDef(name="finish", prompt="finish"),
        ])
        seen = []
        monkeypatch.setattr(
            orchestrator, "_execute_notify_step",
            lambda step, *args: (seen.append(step.name) or
                                 SimpleNamespace(delivered=True, error="")))
        assert orchestrator.remind_workflow(run, workflow).delivered is True
        assert seen == ["fallback"]

    def test_agent_delivered_gate_gets_a_generic_thread_reminder(
            self, bobi_install, monkeypatch):
        from bobi.workflow import orchestrator

        run = _seed_run(
            name="blog-image", await_event="clarification",
            suspended_at_step=2,
            variable_scopes={"requested_by": {"channel": "C123"}},
            cwd=str(bobi_install.repo_path))
        workflow = Workflow("blog-image", [
            StepDef(name="describe", prompt="post and ask"),
            StepDef(name="await_decision", await_event="clarification"),
            StepDef(name="finish", prompt="finish"),
        ])
        messages = []

        def _notify(step, ctx, cwd, run_key, workflow_name):
            messages.append(step.message)
            return SimpleNamespace(delivered=True, error="")

        monkeypatch.setattr(orchestrator, "_execute_notify_step", _notify)
        assert orchestrator.remind_workflow(run, workflow).delivered is True
        assert "blog-image" in messages[0]
        assert "clarification" in messages[0]
        assert "Reply in this thread" in messages[0]


class TestWaitingRunActions:
    def test_runtime_reminds_without_resuming(self, bobi_install, monkeypatch):
        from bobi.workflow import orchestrator

        _install_workflow(bobi_install)
        _seed_run(await_event="approval", suspended_at_step=2,
                  variable_scopes={
                      "_runtime": {"visits": {"notify_review": 1}}},
                  cwd=str(bobi_install.repo_path))
        monkeypatch.setattr(
            orchestrator, "remind_workflow",
            lambda run, workflow: SimpleNamespace(delivered=True, error=""))
        body = LocalRuntime().remind_run(bobi_install.agent_name, "wf-1")
        assert body["delivered"] is True
        assert body["await_event"] == "approval"
        assert WorkflowRun.list_runs(root=bobi_install.repo_path)[0].status \
            == "waiting"

    def test_runtime_surfaces_undeliverable_reminder(self, bobi_install,
                                                      monkeypatch):
        from bobi.workflow import orchestrator

        _install_workflow(bobi_install)
        _seed_run(await_event="approval", suspended_at_step=2)
        monkeypatch.setattr(
            orchestrator, "remind_workflow",
            lambda run, workflow: SimpleNamespace(
                delivered=False, error="no Slack channel available"))
        with pytest.raises(TeamLifecycleError,
                           match="no Slack channel available"):
            LocalRuntime().remind_run(bobi_install.agent_name, "wf-1")

    def test_runtime_closes_waiting_run(self, bobi_install):
        from bobi.sdk import SessionEntry, SessionRegistry

        SessionRegistry(bobi_install.repo_path).register(
            SessionEntry(name="worker", status="waiting"))
        _seed_run(session_name="worker")
        body = LocalRuntime().close_run(bobi_install.agent_name, "wf-1")
        assert body == {"ok": True, "closed": True, "run_id": "wf-1",
                        "workflow": "await-review"}
        assert WorkflowRun.list_runs(root=bobi_install.repo_path)[0].status \
            == "cancelled"
        [session] = SessionRegistry(bobi_install.repo_path).list_all()
        assert session.status == "cancelled"
        assert session.error == "workflow closed by operator"
        assert session.terminal_at > 0

    @pytest.mark.parametrize("action", ["remind", "close"])
    def test_only_waiting_runs_accept_actions(self, bobi_install, action):
        _seed_run(status="completed")
        method = getattr(LocalRuntime(), f"{action}_run")
        with pytest.raises(TeamLifecycleError, match="not 'waiting'"):
            method(bobi_install.agent_name, "wf-1")


class TestResumeEndpoint:
    def _post(self, bobi_install, run_id="wf-1"):
        return _client().post(
            f"/api/agents/{bobi_install.agent_name}"
            f"/workflows/runs/{run_id}/resume")

    def test_accepted(self, bobi_install, spawned):
        _seed_run()
        r = self._post(bobi_install)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        # The confirm dialog names the awaited event, so the payload carries it.
        assert body["await_event"] == "pr.merged"
        assert body["workflow"] == "await-review"

    def test_not_resumable_is_409(self, bobi_install, spawned):
        _seed_run(status="completed")
        r = self._post(bobi_install)
        assert r.status_code == 409
        assert "not 'waiting'" in r.json()["error"]
        assert spawned == []

    def test_unknown_run_is_404(self, bobi_install, spawned):
        r = self._post(bobi_install, run_id="nope")
        assert r.status_code == 404
        assert r.json() == {"error": "unknown run"}

    def test_unknown_agent_is_404(self, bobi_install, spawned):
        r = _client().post("/api/agents/nope/workflows/runs/wf-1/resume")
        assert r.status_code == 404

    def test_traversing_run_id_is_rejected(self, bobi_install, spawned):
        r = self._post(bobi_install, run_id="..%2F..%2Fetc")
        assert r.status_code == 404
        assert spawned == []

    def test_requires_the_token(self, bobi_install, spawned):
        app = server.build_app(token=TOKEN)
        c = TestClient(app, base_url="http://127.0.0.1")
        r = c.post(f"/api/agents/{bobi_install.agent_name}"
                   f"/workflows/runs/wf-1/resume")
        assert r.status_code == 403
        assert spawned == []


class TestWaitingActionEndpoints:
    def _post(self, install, action, run_id="wf-1"):
        return _client().post(
            f"/api/agents/{install.agent_name}/workflows/runs/"
            f"{run_id}/{action}")

    def test_remind_delivered(self, bobi_install, monkeypatch):
        monkeypatch.setattr(
            LocalRuntime, "remind_run",
            lambda self, name, run_id: {
                "ok": True, "delivered": True, "run_id": run_id})
        response = self._post(bobi_install, "remind")
        assert response.status_code == 200
        assert response.json()["delivered"] is True

    def test_close_accepted(self, bobi_install, monkeypatch):
        monkeypatch.setattr(
            LocalRuntime, "close_run",
            lambda self, name, run_id: {
                "ok": True, "closed": True, "run_id": run_id})
        response = self._post(bobi_install, "close")
        assert response.status_code == 200
        assert response.json()["closed"] is True

    @pytest.mark.parametrize("action", ["remind", "close"])
    def test_traversing_run_id_is_rejected(self, bobi_install, action):
        response = self._post(
            bobi_install, action, run_id="..%2F..%2Fetc")
        assert response.status_code == 404
