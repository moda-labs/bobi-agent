"""The runs table's one write action (U6): resuming a suspended workflow run.

Resume is a SPAWN, so the tests assert on what gets spawned rather than on a
workflow actually running: the endpoint's whole job is to validate, hand off to
a dedicated process, and return without holding the request open.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from bobi import paths
from bobi.webapp import runtime as runtime_mod
from bobi.webapp import server
from bobi.webapp.runtime import LocalRuntime, TeamLifecycleError, UnknownRun
from bobi.workflow.state import WorkflowRun

TOKEN = "resume-token-123"


def _seed_run(run_id="wf-1", *, status="waiting", name="await-review",
              await_event="pr.merged", suspended_at_step=3):
    run = WorkflowRun(
        run_id=run_id, workflow_name=name,
        trigger_event={"type": "issue.assigned", "data": {}},
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        status=status, suspended_at_step=suspended_at_step,
        await_event=await_event, session_name="worker",
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
