"""Tests for the unified web app server — security, the dashboard snapshot,
and the per-agent lifecycle endpoints. Most classes monkeypatch service-core
calls and read a real (temp) BOBI_HOME via the bobi_install fixture;
TestMultiAgentRealService runs the real service layer on purpose (#706)."""

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from bobi import service
from bobi.webapp import server
from tests.metrics.helpers import seed_dashboard

TOKEN = "test-token-123"


def _testclient():
    # The Host guard only allows loopback; TestClient defaults to "testserver".
    app = server.build_app(token=TOKEN)
    return TestClient(app, base_url="http://127.0.0.1")


def _client():
    c = _testclient()
    c.headers.update({"x-bobi-webui-token": TOKEN})
    return c


class TestMetrics:
    RANGE = {"from": "1970-01-01T00:00:00Z", "to": "1970-01-01T01:00:00Z"}

    def test_metrics_endpoints_and_collector_privacy(self, bobi_install):
        seed_dashboard(bobi_install.repo_path, 1_000_000)
        health = bobi_install.state_dir / "metrics/collector.state.json"
        health.write_text(json.dumps({"status": "running", "db_ready": True,
                                     "reconciliation_errors": 1, "last_error": "private-task"}))
        base = f"/api/agents/{bobi_install.agent_name}/metrics"
        summary = _client().get(base + "/summary", params=self.RANGE)
        assert summary.status_code == 200
        assert summary.headers["cache-control"] == "no-store"
        assert summary.json()["totals"]["input_tokens"] == 720
        assert summary.json()["collector"]["reconciliation_errors"] == 1
        assert "private-task" not in summary.text
        summary_s1 = _client().get(base + "/summary", params={**self.RANGE, "session_id": "s1"})
        assert summary_s1.status_code == 200
        for endpoint in ("/turns", "/turns/t-pro", "/sessions/s1"):
            result = _client().get(base + endpoint, params=self.RANGE)
            assert result.status_code == 200
            assert "private-task" not in result.text
        assert _client().get(base + "/turns/t-pro").json()["router_decisions"][0]["confidence"] == 0.96
        assert _client().get(base + "/turns/absent").status_code == 404
        assert _client().get(base + "/sessions/absent").status_code == 404
        assert _client().get("/api/agents/absent/metrics/summary", params=self.RANGE).status_code == 404

    def test_metrics_unavailable_validation_and_security(self, bobi_install):
        base = f"/api/agents/{bobi_install.agent_name}/metrics"
        response = _client().get(base + "/summary", params=self.RANGE)
        assert response.status_code == 503
        assert response.json()["code"] == "metrics_not_ready"
        assert response.headers["retry-after"] == "1"
        assert not (bobi_install.state_dir / "metrics/metrics.db").exists()
        assert _testclient().get(base + "/summary", params=self.RANGE).status_code == 403
        hostile = TestClient(server.build_app(token=TOKEN), base_url="http://evil.example")
        hostile.headers.update({"x-bobi-webui-token": TOKEN})
        assert hostile.get(base + "/summary", params=self.RANGE).status_code == 403
        assert _client().get(base + "/summary").status_code == 422
        assert _client().get(base + "/summary", params={**self.RANGE, "from": "bad"}).status_code == 400
        assert _client().get(base + "/turns", params={**self.RANGE, "limit": 201}).status_code == 422

    def test_metrics_reader_saturation_release_and_unsupported_runtime(self, bobi_install):
        from bobi.metrics.query import MetricsQueryError
        from bobi.webapp.runtime import LocalRuntime, TeamRuntime

        runtime = LocalRuntime()
        client = TestClient(server.build_app(token=TOKEN, runtime=runtime), base_url="http://127.0.0.1")
        client.headers.update({"x-bobi-webui-token": TOKEN})
        url = f"/api/agents/{bobi_install.agent_name}/metrics/summary"
        for _ in range(4):
            assert runtime._metrics_reads.acquire(blocking=False)
        assert client.get(url, params=self.RANGE).json()["code"] == "metrics_busy"
        for _ in range(4):
            runtime._metrics_reads.release()
        for _ in range(5):
            assert client.get(url, params=self.RANGE).json()["code"] == "metrics_not_ready"
        with pytest.raises(MetricsQueryError) as error:
            TeamRuntime.metrics(runtime, bobi_install.agent_name, "summary", self.RANGE)
        assert error.value.code == "metrics_unsupported"

    def test_metrics_turn_io_is_lifecycle_scoped_and_web_only(self, bobi_install, monkeypatch, tmp_path):
        from bobi.metrics.query import MetricsQueries
        from bobi.metrics.store import connect

        seed_dashboard(bobi_install.repo_path, 1_000_000)
        conn = connect(bobi_install.state_dir / "metrics/metrics.db")
        conn.execute("UPDATE sessions SET provider_session_id='old-provider' WHERE session_id='s1'")
        conn.execute("UPDATE turns SET ended_at_us=started_at_us+900000")
        conn.commit()
        conn.close()
        prompt = "Event: slack/message\n  Please explain **WAL**.\n  conversation: slack:T123:channel:C123:thread:1791360056.16\n  channel_name: eng-team"
        transcript = tmp_path / "old-provider.jsonl"
        transcript.write_text("\n".join(json.dumps({"type": kind, "timestamp": at,
            "message": {"content": text}}) for kind, at, text in [
                ("user", "1970-01-01T00:00:01.100Z", prompt),
                ("assistant", "1970-01-01T00:00:01.800Z", "A **write-ahead log**."),
                ("user", "1970-01-01T00:00:02.100Z", "Different turn"),
                ("assistant", "1970-01-01T00:00:02.800Z", "Different answer"),
        ]))
        monkeypatch.setattr("bobi.chat_history.find_claude_transcript",
                            lambda session_id: transcript if session_id == "old-provider" else None)
        base = f"/api/agents/{bobi_install.agent_name}/metrics"
        result = _client().get(base + "/turns/t-routine")
        assert result.status_code == 200
        io = result.json()["conversation"]
        assert io["input"] == prompt
        assert io["response"] == "A **write-ahead log**."
        assert io["status"] == "matched"
        assert io["origin"] == {"source": "slack", "channel_id": "C123", "channel_name": "eng-team",
                                "thread_id": "1791360056.16", "url": "https://app.slack.com/client/T123/C123/thread/C123-1791360056.16"}
        assert "Different" not in json.dumps(io)
        turns = _client().get(base + "/turns", params=self.RANGE).json()["turns"]
        routine = next(turn for turn in turns if turn["turn_id"] == "t-routine")
        assert routine["conversation"]["prompt_snippet"] == "Please explain **WAL**."
        assert routine["semantic_title"] == io["semantic_title"] == "Please explain **WAL**."
        assert routine["conversation"]["response_snippet"] == "A **write-ahead log**."
        assert "entries" not in routine["conversation"]
        assert "conversation" not in MetricsQueries(bobi_install.repo_path).turn({"turn_id": "t-routine"})
        assert _client().get(base + "/turns/t-unknown").json()["conversation"]["status"] == "unavailable"
        sessions = _client().get(base + "/sessions", params=self.RANGE)
        assert sessions.status_code == 200
        assert {session["session_id"] for session in sessions.json()["sessions"]} == {"s1", "s2"}

    @pytest.mark.parametrize("brain", ["claude", "codex"])
    def test_metrics_old_turn_uses_timestamp_window_before_tail_limit(self, bobi_install, monkeypatch, tmp_path, brain):
        from bobi.metrics.store import connect

        seed_dashboard(bobi_install.repo_path, 1_000_000)
        conn = connect(bobi_install.state_dir / "metrics/metrics.db")
        conn.execute("UPDATE sessions SET provider_session_id='old-provider',brain=? WHERE session_id='s1'", (brain,))
        conn.execute("UPDATE turns SET ended_at_us=started_at_us+900000")
        conn.commit()
        conn.close()
        transcript = tmp_path / "old-provider.jsonl"
        entries = [("user", "1970-01-01T00:00:01.100Z", "Original prompt"),
                   ("assistant", "1970-01-01T00:00:01.900Z", "Original answer")]
        entries.extend(("assistant", "1970-01-01T00:00:03.000Z", "Later answer") for _ in range(250))
        entries.append(("user", "", "Missing timestamp must not match"))
        if brain == "claude":
            rows = [{"type": role, "timestamp": at, "message": {"content": text}} for role, at, text in entries]
        else:
            rows = [{"type": "response_item", "timestamp": at, "payload": {"type": "message", "role": role,
                     "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}
                    for role, at, text in entries]
        transcript.write_text("\n".join(map(json.dumps, rows)))
        monkeypatch.setattr(f"bobi.chat_history.find_{brain}_{'transcript' if brain == 'claude' else 'rollout'}",
                            lambda session_id: transcript if session_id == "old-provider" else None)
        io = _client().get(f"/api/agents/{bobi_install.agent_name}/metrics/turns/t-routine").json()["conversation"]
        assert io["input"] == "Original prompt"
        assert io["response"] == "Original answer"
        assert io["origin"] == {"source": "user"}
        assert "Later answer" not in json.dumps(io)
        assert "Missing timestamp" not in json.dumps(io)


def _add_design_slot(agents_dir, name, description="An idea, not installed."):
    src = agents_dir / name / "src"
    src.mkdir(parents=True)
    (src / "agent.yaml").write_text(yaml.dump({"agent": name}))
    (src / "agent.md").write_text(f"# {name}\n\n{description}\n")


# --- security ------------------------------------------------------------

class TestSecurity:
    def test_api_requires_token(self, bobi_install):
        c = _testclient()
        assert c.get("/api/dashboard").status_code == 403
        c.headers.update({"x-bobi-webui-token": "wrong"})
        assert c.get("/api/dashboard").status_code == 403
        c.headers.update({"x-bobi-webui-token": TOKEN})
        assert c.get("/api/dashboard").status_code == 200

    def test_host_guard(self, bobi_install):
        app = server.build_app(token=TOKEN)
        c = TestClient(app, base_url="http://evil.example")
        c.headers.update({"x-bobi-webui-token": TOKEN})
        assert c.get("/api/dashboard").status_code == 403

    def test_page_is_open_and_embeds_token(self, bobi_install):
        r = _testclient().get("/")   # no token header
        assert r.status_code == 200
        assert TOKEN in r.text


# --- dashboard -----------------------------------------------------------

class TestDashboard:
    def test_lists_installed_agent(self, bobi_install):
        r = _client().get("/api/dashboard")
        assert r.status_code == 200
        agents = r.json()["agents"]
        names = [a["name"] for a in agents]
        assert bobi_install.agent_name in names
        card = agents[names.index(bobi_install.agent_name)]
        assert card["installed"] is True
        assert card["running"] is False
        assert card["pid"] == 0

    def test_lists_design_only_slot(self, bobi_install):
        _add_design_slot(bobi_install.agents_dir, "ideas")
        agents = _client().get("/api/dashboard").json()["agents"]
        card = next(a for a in agents if a["name"] == "ideas")
        assert card["installed"] is False
        assert card["running"] is False
        assert "not installed" in card["description"]

    def test_running_agent_shows_pid(self, bobi_install):
        import os

        from bobi import paths
        pid_path = paths.manager_pid_path(bobi_install.repo_path)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()))   # a live pid: this test process
        agents = _client().get("/api/dashboard").json()["agents"]
        card = next(a for a in agents if a["name"] == bobi_install.agent_name)
        assert card["running"] is True
        assert card["pid"] == os.getpid()

    def test_stale_pid_reads_stopped(self, bobi_install):
        from bobi import paths
        pid_path = paths.manager_pid_path(bobi_install.repo_path)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text("999999999")
        agents = _client().get("/api/dashboard").json()["agents"]
        card = next(a for a in agents if a["name"] == bobi_install.agent_name)
        assert card["running"] is False


# --- per-agent status ------------------------------------------------------

class TestStatus:
    def test_unknown_agent_404(self, bobi_install):
        assert _client().get("/api/agents/nope/status").status_code == 404

    def test_known_agent(self, bobi_install):
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/status")
        assert r.status_code == 200
        assert r.json()["installed"] is True


# --- spend (observability #733) --------------------------------------------

def _seed_session(sessions_dir, name, *, cost, role="engineer",
                  model_usage=None):
    d = sessions_dir / name
    d.mkdir(parents=True, exist_ok=True)
    data = {"name": name, "role": role, "total_cost_usd": cost}
    if model_usage is not None:
        data["model_usage"] = model_usage
    (d / "state.json").write_text(json.dumps(data))


class TestSpend:
    def test_team_spend_folds_sessions(self, bobi_install):
        sd = bobi_install.sessions_dir
        _seed_session(sd, "director", cost=0.60, role="director",
                      model_usage={"anthropic:opus": {"cost_usd": 0.60}})
        _seed_session(sd, "eng", cost=0.40, role="engineer",
                      model_usage={"anthropic:sonnet": {"cost_usd": 0.40}})
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/spend")
        assert r.status_code == 200
        body = r.json()
        assert body["total_cost_usd"] == 1.0
        assert body["sessions_counted"] == 2
        assert body["by_role"] == {"director": 0.6, "engineer": 0.4}
        # by_model ranked highest-first
        assert list(body["by_model"]) == ["anthropic:opus", "anthropic:sonnet"]

    def test_team_spend_empty(self, bobi_install):
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/spend")
        assert r.status_code == 200
        assert r.json()["total_cost_usd"] == 0.0
        assert r.json()["sessions_counted"] == 0

    def test_team_spend_unknown_agent_404(self, bobi_install):
        assert _client().get("/api/agents/nope/spend").status_code == 404

    def test_team_spend_estimates_codex_tokens(self, bobi_install):
        # A codex-brained session records tokens with the cached split but
        # cost 0 (#760): the wire carries a fold-time estimate + raw token
        # volume on the additive fields, and recorded dollars stay 0.
        _seed_session(bobi_install.sessions_dir, "dev", role="dev", cost=0.0,
                      model_usage={"openai:gpt-5.6": {
                          "cost_usd": 0.0, "input_tokens": 1_000_000,
                          "cached_input_tokens": 900_000,
                          "output_tokens": 10_000}})
        body = _client().get(
            f"/api/agents/{bobi_install.agent_name}/spend").json()
        assert body["total_cost_usd"] == 0.0
        assert body["estimated_cost_usd"] == 1.25
        assert body["estimated_by_model"] == {"openai:gpt-5.6": 1.25}
        assert body["tokens_by_model"]["openai:gpt-5.6"] == {
            "input_tokens": 1_000_000, "cached_input_tokens": 900_000,
            "output_tokens": 10_000}

    def test_fleet_spend_totals(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, "director", cost=1.25)
        _seed_session(bobi_install.sessions_dir, "dev", role="dev", cost=0.0,
                      model_usage={"openai:gpt-5.6": {
                          "cost_usd": 0.0, "input_tokens": 1_000_000,
                          "cached_input_tokens": 900_000,
                          "output_tokens": 10_000}})
        r = _client().get("/api/fleet/spend")
        assert r.status_code == 200
        body = r.json()
        assert body["total_cost_usd"] == 1.25
        assert body["estimated_cost_usd"] == 1.25
        assert body["sessions_counted"] == 2
        team = next(t for t in body["teams"]
                    if t["name"] == bobi_install.agent_name)
        assert team["total_cost_usd"] == 1.25
        assert team["estimated_cost_usd"] == 1.25
        assert team["sessions_counted"] == 2

    def test_spend_read_does_not_create_sessions_dir(self, bobi_install):
        import shutil
        # A read endpoint must not mutate disk: remove the sessions dir the
        # fixture pre-creates and confirm a spend GET leaves it absent.
        shutil.rmtree(bobi_install.sessions_dir)
        c = _client()
        assert c.get(f"/api/agents/{bobi_install.agent_name}/spend").status_code == 200
        assert c.get("/api/fleet/spend").status_code == 200
        assert not bobi_install.sessions_dir.exists()

    def test_null_cost_session_does_not_500(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, "broken", cost=None,
                      model_usage={"anthropic:opus": {"cost_usd": None}})
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/spend")
        assert r.status_code == 200
        assert r.json()["total_cost_usd"] == 0.0

    def test_fleet_spend_empty_install(self, bobi_install):
        body = _client().get("/api/fleet/spend").json()
        assert body["total_cost_usd"] == 0.0
        # the installed test agent is listed even with no spend
        names = [t["name"] for t in body["teams"]]
        assert bobi_install.agent_name in names


# --- system health (observability #733) --------------------------------------

def _seed_active_session(sessions_dir, name, *, status, role="engineer"):
    """A registry entry list_active() keeps: an active status and pid 0
    (no liveness check), so the health fold sees it."""
    d = sessions_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps(
        {"name": name, "role": role, "status": status, "pid": 0}))


class TestHealth:
    # manager_session_name: bobi-<agent>-<entry_role>; the fixture's
    # agent.yaml declares entry_point "director".
    MGR = "bobi-test-agent-director"

    def _get(self, bobi_install):
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/health")
        assert r.status_code == 200
        return r.json()

    def test_stopped_team(self, bobi_install):
        body = self._get(bobi_install)
        # Local teams share the webapp's host: live by construction, no
        # heartbeats, no supervisor trail.
        assert body["reachability"] == "live"
        assert body["last_heartbeat_at"] is None
        assert body["lifecycle"] == []
        mgr = body["manager"]
        assert mgr["status"] == "stopped"
        assert mgr["running"] is False
        assert mgr["healthy"] is False
        assert mgr["pid"] == 0
        assert mgr["restart_count"] is None
        assert body["sessions"] == []

    def test_running_manager_reports_registry_status(self, bobi_install):
        import os

        from bobi import paths
        pid_path = paths.manager_pid_path(bobi_install.repo_path)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()))   # a live pid: this process
        _seed_active_session(bobi_install.sessions_dir, self.MGR,
                             status="idle", role="director")
        _seed_active_session(bobi_install.sessions_dir, "eng",
                             status="running")
        body = self._get(bobi_install)
        mgr = body["manager"]
        assert mgr["running"] is True
        assert mgr["pid"] == os.getpid()
        assert mgr["status"] == "idle"     # the registry's word, not just "up"
        assert mgr["healthy"] is True
        # manager-first ordering, roles and statuses carried through
        sessions = body["sessions"]
        assert sessions[0]["name"] == self.MGR
        assert {"name": "eng", "role": "engineer",
                "status": "running"} in sessions

    def test_boot_window_reads_starting(self, bobi_install):
        import os

        from bobi import paths
        pid_path = paths.manager_pid_path(bobi_install.repo_path)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()))
        # pid alive but no registered manager session yet: fail open to
        # "starting" (the same verdict the hosted sidecar reports pre-spawn).
        body = self._get(bobi_install)
        assert body["manager"]["status"] == "starting"
        assert body["manager"]["running"] is True

    def test_stale_pid_reads_stopped(self, bobi_install):
        from bobi import paths
        pid_path = paths.manager_pid_path(bobi_install.repo_path)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text("999999999")
        _seed_active_session(bobi_install.sessions_dir, self.MGR,
                             status="idle", role="director")
        body = self._get(bobi_install)
        assert body["manager"]["status"] == "stopped"
        assert body["manager"]["running"] is False

    def test_terminal_sessions_not_listed(self, bobi_install):
        d = bobi_install.sessions_dir / "done-run"
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(json.dumps(
            {"name": "done-run", "role": "engineer", "status": "completed"}))
        body = self._get(bobi_install)
        assert body["sessions"] == []

    def test_unknown_agent_404(self, bobi_install):
        assert _client().get("/api/agents/nope/health").status_code == 404


# --- session logs (observability #733 vertical 3) ---------------------------

def _seed_history_session(sessions_dir, name, *, status, role="engineer",
                          error="", terminal_at=0.0, last_activity=0.0,
                          pid=0, cost=0.0):
    d = sessions_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({
        "name": name, "role": role, "status": status, "pid": pid,
        "error": error, "terminal_at": terminal_at,
        "last_activity": last_activity, "total_cost_usd": cost,
        "session_id": f"sid-{name}",
    }))


class TestSessionLog:
    MGR = "bobi-test-agent-director"

    def _get(self, bobi_install):
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/sessions")
        assert r.status_code == 200
        return r.json()

    def test_empty_history(self, bobi_install):
        assert self._get(bobi_install) == {
            "sessions": [],
            "counts": {"active": 0, "completed": 0, "failed": 0, "crashed": 0},
            "truncated": False,
        }

    def test_outcomes_listed_newest_first(self, bobi_install):
        import os

        sd = bobi_install.sessions_dir
        _seed_history_session(sd, "old-done", status="completed",
                              terminal_at=100.0, last_activity=100.0)
        _seed_history_session(sd, "boom", status="failed",
                              error="turn errored", terminal_at=200.0,
                              last_activity=200.0, cost=0.25)
        _seed_history_session(sd, "live", status="running", pid=os.getpid(),
                              last_activity=300.0)
        body = self._get(bobi_install)
        assert [s["name"] for s in body["sessions"]] == \
            ["live", "boom", "old-done"]
        boom = body["sessions"][1]
        assert boom["status"] == "failed"
        assert boom["error"] == "turn errored"
        assert boom["terminal_at"] == 200.0
        assert boom["session_id"] == "sid-boom"
        assert boom["total_cost_usd"] == 0.25
        assert boom["ended"] is True
        live = body["sessions"][0]
        assert live["error"] == ""
        assert live["terminal_at"] is None   # null, never omitted
        assert live["ended"] is False
        assert body["counts"] == {"active": 1, "completed": 1,
                                  "failed": 1, "crashed": 0}
        assert body["truncated"] is False

    def test_dead_pid_reads_crashed_not_running(self, bobi_install):
        _seed_history_session(bobi_install.sessions_dir, "zombie",
                              status="running", pid=999999999,
                              last_activity=100.0)
        body = self._get(bobi_install)
        [z] = body["sessions"]
        assert z["status"] == "crashed"
        assert z["error"]                     # the honest-status message
        assert z["terminal_at"] is not None
        assert z["ended"] is True
        assert body["counts"] == {"active": 0, "completed": 0,
                                  "failed": 0, "crashed": 1}

    def test_manager_flagged(self, bobi_install):
        _seed_history_session(bobi_install.sessions_dir, self.MGR,
                              status="completed", role="director",
                              terminal_at=1.0, last_activity=1.0)
        [row] = self._get(bobi_install)["sessions"]
        assert row["is_manager"] is True

    def test_legacy_done_counts_completed(self, bobi_install):
        _seed_history_session(bobi_install.sessions_dir, "old",
                              status="done", last_activity=1.0)
        assert self._get(bobi_install)["counts"]["completed"] == 1

    def test_error_status_counts_failed(self, bobi_install):
        # "error" is still written for turn-level failures (rotation death,
        # monitor timeouts) - the log counts it as a failure.
        _seed_history_session(bobi_install.sessions_dir, "curator",
                              status="error", last_activity=1.0)
        assert self._get(bobi_install)["counts"]["failed"] == 1

    def test_unknown_agent_404(self, bobi_install):
        assert _client().get("/api/agents/nope/sessions").status_code == 404


# --- lifecycle actions -----------------------------------------------------

class _FakeStartup:
    pid = 4242


class _FakeSpawn:
    startup = _FakeStartup()


# --- hosted onboarding (the setup app mounted under /setup) ----------------

class TestClaudeAvailable:
    def test_absolute_resolution_counts(self, tmp_path, monkeypatch):
        cli = tmp_path / "claude"
        cli.write_text("#!/bin/sh\n")
        monkeypatch.setattr("bobi.brain.claude.shutil.which",
                            lambda name: str(cli))
        assert server._claude_available() is True

    def test_cwd_relative_fallback_is_not_availability(self, tmp_path,
                                                       monkeypatch):
        # On Linux with no CLI on PATH the resolver falls back to the bare
        # name "claude" (correct for exec). A stray file of that name in the
        # server's CWD must not read as an installed CLI.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "claude").mkdir()
        monkeypatch.setattr("bobi.brain.claude.shutil.which", lambda name: None)
        monkeypatch.setattr("bobi.brain.claude.platform.system", lambda: "Linux")
        assert server._claude_available() is False


class TestSetupHosting:
    def _open(self, client, monkeypatch, name="new-team"):
        monkeypatch.setattr(server, "_claude_available", lambda: True)
        return client.post("/api/setup/open", json={"name": name})

    def test_no_session_redirects_to_shell(self, bobi_install):
        c = _client()
        r = c.get("/setup/", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/#/setup"

    def test_open_creates_session(self, bobi_install, monkeypatch):
        c = _client()
        r = self._open(c, monkeypatch)
        assert r.status_code == 200
        assert r.json() == {"url": "/setup/new-team/", "name": "new-team",
                            "resumed": False}
        cur = c.get("/api/setup/current").json()
        assert cur == {"active": True, "name": "new-team",
                       "sessions": ["new-team"]}
        # setup state persisted under the slot's run root
        from bobi.setup.state import SetupState
        from bobi import paths
        state = SetupState.load(paths.agent_run_root("new-team"))
        assert state is not None
        assert state.team_name == "new-team"

    def test_open_resumes_unfinished_session(self, bobi_install, monkeypatch):
        c = _client()
        assert self._open(c, monkeypatch).json()["resumed"] is False
        assert self._open(c, monkeypatch).json()["resumed"] is True

    def test_open_passes_model_to_hosted_setup_app(self, bobi_install,
                                                   monkeypatch):
        from fastapi import FastAPI

        seen = {}

        def fake_build_app(*args, **kwargs):
            seen["model"] = kwargs.get("model")
            return FastAPI()

        monkeypatch.setattr(server, "_claude_available", lambda: True)
        monkeypatch.setattr("bobi.setup.webui.server.build_app", fake_build_app)

        c = _client()
        r = c.post("/api/setup/open",
                   json={"name": "new-team", "model": "sonnet"})

        assert r.status_code == 200
        assert seen["model"] == "sonnet"

    def test_hosted_page_serves_with_base_and_token(self, bobi_install,
                                                    monkeypatch):
        c = _client()
        self._open(c, monkeypatch)
        r = c.get("/setup/new-team/")
        assert r.status_code == 200
        assert TOKEN in r.text
        assert '"/setup/new-team/static/app.js"' in r.text

    def test_hosted_setup_supports_concurrent_sessions(
            self, bobi_install, monkeypatch):
        c = _client()
        assert self._open(c, monkeypatch, name="alpha").json()["url"] \
            == "/setup/alpha/"
        assert self._open(c, monkeypatch, name="beta").json()["url"] \
            == "/setup/beta/"
        cur = c.get("/api/setup/current").json()
        assert cur == {"active": True, "name": "alpha",
                       "sessions": ["alpha", "beta"]}
        assert c.get("/setup/alpha/api/state").json()["team_name"] == "alpha"
        assert c.get("/setup/beta/api/state").json()["team_name"] == "beta"

    def test_hosted_api_requires_token(self, bobi_install, monkeypatch):
        # The mounted sub-app must still enforce its /api token check even
        # though its url.path carries the /setup prefix (the root_path fix
        # in webui_common.security). Both clients share one app so they see
        # the same active onboarding session.
        app = server.build_app(token=TOKEN)
        c = TestClient(app, base_url="http://127.0.0.1")
        c.headers.update({"x-bobi-webui-token": TOKEN})
        monkeypatch.setattr(server, "_claude_available", lambda: True)
        assert c.post("/api/setup/open",
                      json={"name": "new-team"}).status_code == 200
        bare = TestClient(app, base_url="http://127.0.0.1")
        assert bare.get("/setup/new-team/api/state").status_code == 403
        assert c.get("/setup/new-team/api/state").status_code == 200

    def test_hosted_finish_returns_home_without_launching(self, bobi_install,
                                                          monkeypatch):
        # Finish returns to the dashboard; launching stays a deliberate
        # action on the agent's card/dashboard (never automatic).
        def fail_start(root, **kw):
            raise AssertionError("finish must not launch")

        monkeypatch.setattr(service, "start_team", fail_start)
        monkeypatch.setattr(service, "spawn_team", fail_start)
        c = _client()
        self._open(c, monkeypatch)
        body = c.post("/setup/new-team/api/finish").json()
        assert body["finished"] is True
        assert body["redirect"] == "/#/"
        # The onboarding slot is released: current reports inactive and
        # /setup/ redirects back to the shell.
        assert c.get("/api/setup/current").json()["active"] is False
        assert c.get("/setup/",
                     follow_redirects=False).status_code == 307

    def test_open_mode_deep_links_the_editor(self, bobi_install, monkeypatch):
        import yaml as _yaml

        from bobi import paths
        src = paths.agent_source_dir(bobi_install.agent_name)
        src.mkdir(parents=True, exist_ok=True)
        (src / "agent.yaml").write_text(_yaml.dump(
            {"agent": bobi_install.agent_name}))
        monkeypatch.setattr(server, "_claude_available", lambda: True)
        r = _client().post("/api/setup/open",
                           json={"name": bobi_install.agent_name,
                                 "mode": "open"})
        assert r.status_code == 200
        assert r.json()["url"].startswith(
            f"/setup/{bobi_install.agent_name}/?open=")
        assert str(src) in r.json()["url"]

    def test_open_mode_requires_source(self, bobi_install, monkeypatch):
        monkeypatch.setattr(server, "_claude_available", lambda: True)
        r = _client().post("/api/setup/open",
                           json={"name": "no-src-slot", "mode": "open"})
        assert r.status_code == 404

    def test_open_mode_resolves_nested_source(self, bobi_install, monkeypatch):
        # An older flow could land a template in a src/ SUBFOLDER
        # (src/eng-team/); a single team child still resolves as the source.
        import yaml as _yaml

        from bobi import paths
        nested = paths.agent_source_dir("legacy-slot") / "eng-team"
        nested.mkdir(parents=True)
        (nested / "agent.yaml").write_text(_yaml.dump({"agent": "eng-team"}))
        monkeypatch.setattr(server, "_claude_available", lambda: True)
        r = _client().post("/api/setup/open",
                           json={"name": "legacy-slot", "mode": "open"})
        assert r.status_code == 200
        assert str(nested) in r.json()["url"]

    def test_finish_renames_slot_to_team_name(self, bobi_install, monkeypatch):
        # The slot opens under a placeholder name; the team gets its real
        # name during setup (template pick / auto-name). Finish moves the
        # whole slot dir to match (#526: a slot IS its team).
        from bobi import paths

        c = _client()
        self._open(c, monkeypatch)   # slot "new-team"
        # Name the team through the real flow (mutates the parked state).
        r = c.post("/setup/new-team/api/rename", json={"name": "eng-team"})
        assert r.status_code == 200
        body = c.post("/setup/new-team/api/finish").json()
        assert body["redirect"] == "/#/"
        assert not paths.agent_dir("new-team").exists()
        assert paths.agent_run_root("eng-team").is_dir()

    def test_create_rename_moves_placeholder_source_and_run_slot(
            self, bobi_install, monkeypatch):
        # The local web app starts a from-scratch team in a placeholder slot
        # (usually new-agent). Renaming during setup should move the editable
        # source out of that slot immediately, and Finish should move the
        # run/ state beside it so the final folder is the chosen name.
        from bobi import paths

        c = _client()
        self._open(c, monkeypatch, name="new-agent")
        old_src = paths.agent_source_dir("new-agent")
        r = c.post("/setup/new-agent/api/start",
                   json={"mode": "create", "location": str(old_src)})
        assert r.status_code == 200
        old_src.mkdir(parents=True, exist_ok=True)
        (old_src / "agent.yaml").write_text("agent: new-agent\n")
        r = c.post("/setup/new-agent/api/rename",
                   json={"name": "Field Ops"})
        assert r.status_code == 200
        assert not old_src.exists()
        assert paths.agent_source_dir("field-ops").is_dir()

        body = c.post("/setup/new-agent/api/finish").json()
        assert body["redirect"] == "/#/"
        assert not paths.agent_dir("new-agent").exists()
        assert paths.agent_source_dir("field-ops").is_dir()
        assert paths.agent_run_root("field-ops").is_dir()

    def test_finish_does_not_merge_custom_source_into_existing_slot(
            self, bobi_install, monkeypatch, tmp_path):
        from bobi import paths

        c = _client()
        self._open(c, monkeypatch, name="new-agent")
        custom_src = tmp_path / "field-ops-src"
        existing_src = paths.agent_source_dir("field-ops")
        existing_src.mkdir(parents=True)
        (existing_src / "agent.yaml").write_text("agent: field-ops\n")
        r = c.post("/setup/new-agent/api/start",
                   json={"mode": "create", "location": str(custom_src)})
        assert r.status_code == 200
        r = c.post("/setup/new-agent/api/rename",
                   json={"name": "Field Ops"})
        assert r.status_code == 200

        body = c.post("/setup/new-agent/api/finish").json()
        assert body["redirect"] == "/#/"
        assert paths.agent_run_root("new-agent").is_dir()
        assert not paths.agent_run_root("field-ops").exists()

    def test_open_requires_claude(self, bobi_install, monkeypatch):
        monkeypatch.setattr(server, "_claude_available", lambda: False)
        r = _client().post("/api/setup/open", json={"name": "x"})
        assert r.status_code == 409

    def test_open_rejects_bad_name(self, bobi_install, monkeypatch):
        monkeypatch.setattr(server, "_claude_available", lambda: True)
        r = _client().post("/api/setup/open", json={"name": "../evil"})
        assert r.status_code == 400


# --- subagents + chat ------------------------------------------------------

def _entry(name, role="engineer", **kw):
    from bobi.sdk import SessionEntry
    return SessionEntry(name=name, role=role, **kw)


class TestSubagents:
    def test_unknown_agent_404(self, bobi_install):
        assert _client().get("/api/agents/nope/subagents").status_code == 404

    def test_roster_with_manager_badge(self, bobi_install, monkeypatch):
        mgr = f"bobi-{bobi_install.agent_name}-director"
        monkeypatch.setattr(
            service, "list_agents",
            lambda root: [_entry("bobi-worker-1"), _entry(mgr, role="director")])
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/subagents")
        assert r.status_code == 200
        subs = r.json()["subagents"]
        # The manager orders first and carries the badge.
        assert subs[0]["name"] == mgr
        assert subs[0]["is_manager"] is True
        assert subs[1]["is_manager"] is False

    def test_messages_from_chat_log(self, bobi_install):
        from bobi.chat_history import append_chat
        append_chat(bobi_install.repo_path, "bobi-worker-1", "user", "hi")
        append_chat(bobi_install.repo_path, "bobi-worker-1", "agent", "hello!")
        r = _client().get(
            f"/api/agents/{bobi_install.agent_name}"
            "/subagents/bobi-worker-1/messages")
        assert r.status_code == 200
        assert r.json()["messages"] == [
            {"role": "user", "text": "hi"},
            {"role": "agent", "text": "hello!"},
        ]

    def test_messages_bad_session_name_404(self, bobi_install):
        r = _client().get(
            f"/api/agents/{bobi_install.agent_name}"
            "/subagents/..%2Fetc/messages")
        assert r.status_code == 404

    def test_messages_from_codex_rollout(self, bobi_install, monkeypatch,
                                         tmp_path):
        """A codex-brained session renders its rollout, not a blank panel —
        the fleet-UI regression this fixes. The runtime dispatches on the
        recorded ``.brain`` to the Codex reader instead of the Claude one."""
        import json

        from bobi import paths

        sessions = paths.sessions_dir(bobi_install.repo_path)
        (sessions / "bobi-worker-1.id").write_text("codex-thread-42")
        (sessions / "bobi-worker-1.brain").write_text("codex")

        codex_home = tmp_path / "codex"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        rollout = (codex_home / "sessions" / "2026" / "07" / "09"
                   / "rollout-2026-07-09T05-12-25-codex-thread-42.jsonl")
        rollout.parent.mkdir(parents=True)
        rollout.write_text("\n".join(json.dumps(r) for r in [
            {"type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "are you alive?"}]}},
            {"type": "response_item", "payload": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "Yes, standing by."}]}},
        ]) + "\n")

        r = _client().get(
            f"/api/agents/{bobi_install.agent_name}"
            "/subagents/bobi-worker-1/messages")
        assert r.status_code == 200
        assert r.json()["messages"] == [
            {"role": "user", "text": "are you alive?"},
            {"role": "agent", "text": "Yes, standing by."},
        ]


class TestChat:
    """Chat is submit-then-poll: POST returns a message id right away; the
    deliver runs in a background thread and its outcome lands on the job
    status endpoint (the reply itself reaches the transcript)."""

    def _await_job(self, client, agent, message_id, tries=100):
        import time
        for _ in range(tries):
            r = client.get(f"/api/agents/{agent}/chat/{message_id}")
            assert r.status_code == 200
            job = r.json()
            if job["status"] != "pending":
                return job
            time.sleep(0.02)
        raise AssertionError("chat job never resolved")

    def test_chat_submits_and_resolves(self, bobi_install, monkeypatch):
        seen = {}

        def fake_ask(root, agent, text, **kw):
            seen.update(root=root, agent=agent, text=text)
            return service.MessageResult(address=agent, response="done!")

        monkeypatch.setattr(service, "ask", fake_ask)
        c = _client()
        r = c.post(
            f"/api/agents/{bobi_install.agent_name}/chat",
            json={"subagent": "bobi-worker-1", "text": "go"})
        assert r.status_code == 200
        mid = r.json()["message_id"]
        assert mid
        job = self._await_job(c, bobi_install.agent_name, mid)
        assert job == {"status": "done"}
        assert seen["root"] == bobi_install.repo_path
        assert seen["agent"] == "bobi-worker-1"

    def test_an_empty_subagent_means_the_manager_on_this_runtime_too(
            self, bobi_install, monkeypatch):
        """#987 - one route, two runtimes, one meaning.

        The route documents `subagent` as optional (only `text` is required),
        and the hosted runtime has always read an empty one as "the team
        manager" (`supervisor/admin.py`). `LocalRuntime` passed the empty
        string straight to `service.ask`, which rejected it on its membership
        guard as `unknown agent ''`, so the same request behaved differently
        on `bobi app` than on the fleet.
        """
        seen = {}

        def fake_ask(root, agent, text, **kw):
            seen.update(root=root, agent=agent, text=text)
            return service.MessageResult(address=agent, response="ack")

        monkeypatch.setattr(service, "ask", fake_ask)
        c = _client()
        r = c.post(f"/api/agents/{bobi_install.agent_name}/chat",
                   json={"subagent": "", "text": "continue this"})
        assert r.status_code == 200
        job = self._await_job(c, bobi_install.agent_name, r.json()["message_id"])
        assert job == {"status": "done"}
        # The same name the supervisor resolves, not a hand-built string.
        assert seen["agent"] == service.manager_session_name(
            bobi_install.repo_path)

    def test_an_absent_subagent_key_resolves_the_same_way(self, bobi_install,
                                                          monkeypatch):
        seen = {}
        monkeypatch.setattr(service, "ask", lambda root, agent, text, **kw: (
            seen.update(agent=agent),
            service.MessageResult(address=agent, response="ack"))[1])
        c = _client()
        r = c.post(f"/api/agents/{bobi_install.agent_name}/chat",
                   json={"text": "continue this"})
        assert r.status_code == 200
        assert self._await_job(c, bobi_install.agent_name,
                               r.json()["message_id"]) == {"status": "done"}
        assert seen["agent"] == service.manager_session_name(
            bobi_install.repo_path)

    def test_chat_empty_message_400(self, bobi_install):
        r = _client().post(
            f"/api/agents/{bobi_install.agent_name}/chat",
            json={"subagent": "x", "text": "  "})
        assert r.status_code == 400

    def test_chat_delivery_failure_lands_on_job(self, bobi_install,
                                                monkeypatch):
        def fake_ask(root, agent, text, **kw):
            raise service.MessageDeliveryError("session 'x' process is dead")

        monkeypatch.setattr(service, "ask", fake_ask)
        c = _client()
        r = c.post(
            f"/api/agents/{bobi_install.agent_name}/chat",
            json={"subagent": "x", "text": "hi"})
        assert r.status_code == 200
        job = self._await_job(c, bobi_install.agent_name, r.json()["message_id"])
        assert job["status"] == "error"
        assert "dead" in job["error"]

    def test_chat_unknown_agent_404(self, bobi_install):
        r = _client().post("/api/agents/nope/chat",
                           json={"subagent": "x", "text": "hi"})
        assert r.status_code == 404

    def test_chat_status_unknown_message_404(self, bobi_install):
        r = _client().get(
            f"/api/agents/{bobi_install.agent_name}/chat/deadbeef")
        assert r.status_code == 404


class TestLifecycle:
    def test_start_spawns(self, bobi_install, monkeypatch):
        seen = {}

        def fake_spawn(root, **kw):
            seen["root"] = root
            return _FakeSpawn()

        monkeypatch.setattr(service, "spawn_team", fake_spawn)
        r = _client().post(f"/api/agents/{bobi_install.agent_name}/start")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "pid": 4242}
        assert seen["root"] == bobi_install.repo_path

    def test_start_unknown_404(self, bobi_install):
        assert _client().post("/api/agents/nope/start").status_code == 404

    def test_start_already_running(self, bobi_install, monkeypatch):
        def fake_spawn(root, **kw):
            raise service.AlreadyRunning(77)

        monkeypatch.setattr(service, "spawn_team", fake_spawn)
        r = _client().post(f"/api/agents/{bobi_install.agent_name}/start")
        assert r.status_code == 409
        assert r.json()["pid"] == 77

    def test_start_preflight_failed(self, bobi_install, monkeypatch):
        class FakeValidation:
            def format(self):
                return "missing SLACK_BOT_TOKEN"

        def fake_spawn(root, **kw):
            raise service.PreflightFailed(FakeValidation())

        monkeypatch.setattr(service, "spawn_team", fake_spawn)
        r = _client().post(f"/api/agents/{bobi_install.agent_name}/start")
        assert r.status_code == 409
        assert "SLACK_BOT_TOKEN" in r.json()["report"]

    def test_stop(self, bobi_install, monkeypatch):
        monkeypatch.setattr(
            service, "stop_team",
            lambda root, **kw: service.StopResult(pid=42, stopped=True))
        r = _client().post(f"/api/agents/{bobi_install.agent_name}/stop")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["stopped"] is True

    def test_stop_not_running_is_ok(self, bobi_install, monkeypatch):
        monkeypatch.setattr(
            service, "stop_team", lambda root, **kw: service.StopResult(pid=0))
        r = _client().post(f"/api/agents/{bobi_install.agent_name}/stop")
        assert r.json()["ok"] is True

    def test_restart(self, bobi_install, monkeypatch):
        calls = []
        monkeypatch.setattr(
            service, "stop_team",
            lambda root, **kw: calls.append("stop")
            or service.StopResult(pid=42, stopped=True))
        monkeypatch.setattr(
            service, "spawn_team",
            lambda root, **kw: calls.append("spawn") or _FakeSpawn())
        r = _client().post(f"/api/agents/{bobi_install.agent_name}/restart")
        assert r.status_code == 200
        assert calls == ["stop", "spawn"]


class TestMultiAgentRealService:
    """Regression tests for #706: one webapp process serves service-backed
    endpoints for MULTIPLE agents.

    No service monkeypatching here on purpose. The bug was the process-global
    runtime bind (`service._bind` -> `paths.bind_root`, which refuses to rebind
    to a second root), and only real service calls exercise it - the mocked
    tests above never see the bind."""

    @pytest.fixture
    def two_agents(self, tmp_path, monkeypatch):
        from bobi import paths

        home = tmp_path / "home"
        monkeypatch.setenv("BOBI_HOME", str(home))
        # The webapp daemon process never binds a runtime root; start unbound.
        paths.bind_root(None)
        names = ["alpha-team", "beta-team"]
        for name in names:
            pkg = home / "agents" / name / "run" / "package"
            pkg.mkdir(parents=True)
            (pkg / "agent.yaml").write_text(yaml.dump({
                "version": "0.0.1",
                "agent": name,
                "entry_point": "director",
            }))
        yield names
        paths.bind_root(None)

    def test_roster_serves_both_agents(self, two_agents):
        c = _client()
        for name in two_agents:
            r = c.get(f"/api/agents/{name}/subagents")
            assert r.status_code == 200, f"{name}: {r.text}"
            assert r.json()["subagents"] == []

    def test_stop_serves_both_agents(self, two_agents):
        c = _client()
        for name in two_agents:
            r = c.post(f"/api/agents/{name}/stop")
            assert r.status_code == 200, f"{name}: {r.text}"
            assert r.json()["ok"] is True

    def test_webapp_process_stays_unbound(self, two_agents):
        from bobi import paths

        c = _client()
        for name in two_agents:
            assert c.get(f"/api/agents/{name}/subagents").status_code == 200
        assert paths.bound_root() is None


class TestSystemLogs:
    def test_logs_empty_when_no_file(self, bobi_install):
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/logs")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["logs"] == []

    def test_logs_tail_and_error_detection(self, bobi_install):
        log_path = bobi_install.state_dir / "manager.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "[INFO] Starting server\n"
            "[WARNING] High latency\n"
            "[ERROR] API Error: 521 Web server is down\n"
        )
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/logs")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert len(data["logs"]) == 3
        assert "521" in data["recent_error"]


class TestRoutingConfig:
    @pytest.fixture(autouse=True)
    def _gateway_agent(self, monkeypatch):
        # The roster uses gateway routing ids (ds/, cx/); a native brain rejects them.
        monkeypatch.setenv("BOBI_GATEWAY_BASE_URL", "https://gateway.invalid/v1")

    @pytest.mark.parametrize("route", ["metrics/jev-config", "routing/config"])
    def test_jev_config_without_experiment_is_disabled_not_missing(self, bobi_install, route):
        response = _client().get(f"/api/agents/{bobi_install.agent_name}/{route}")
        assert response.status_code == 200
        assert response.json()["enabled"] is False and response.json()["mode"] == "off"
        assert response.headers["cache-control"] == "no-store"
        assert _client().get(f"/api/agents/missing-agent/{route}").status_code == 404

    def test_jev_config_offers_priced_models_and_names_unusable_policy(self, bobi_install):
        from bobi import paths
        from bobi.costs import model_prices
        from tests.metrics.test_routing import concise_config

        config = _client().get(f"/api/agents/{bobi_install.agent_name}/metrics/jev-config").json()
        # The picker lists the agent's own brain models, unprefixed; every one has a list price.
        assert config["agent_brain"] == "claude"
        assert {"opus", "claude-haiku-4-5", "claude-3-7-sonnet"} <= set(config["native_models"])
        assert not any("/" in model or model.startswith("gpt-") for model in config["native_models"])
        assert {"cx/gpt-5.6-luna", "ds/deepseek-flash"} <= set(config["priced_models"])
        assert all(model_prices(model) for model in config["native_models"] + config["priced_models"])
        assert config["unpriced_models"] == [] and config["config_error"] == ""
        broken = {**concise_config(), "min_confidence": 7}
        paths.env_path(bobi_install.repo_path).write_text("# BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(broken) + "\n")
        config = _client().get(f"/api/agents/{bobi_install.agent_name}/metrics/jev-config").json()
        # A saved policy that fails validation is named instead of silently shown as defaults.
        assert config["enabled"] is False and "min_confidence" in config["config_error"]
        paths.env_path(bobi_install.repo_path).write_text("# BOBI_METRICS_EXPERIMENT_JSON={not json\n")
        config = _client().get(f"/api/agents/{bobi_install.agent_name}/metrics/jev-config").json()
        assert config["config_error"] == "BOBI_METRICS_EXPERIMENT_JSON is not valid JSON"

    def test_jev_config_does_not_hide_runtime_failure(self, bobi_install, monkeypatch):
        def unavailable(env):
            raise OSError("configuration unavailable")

        monkeypatch.setattr("bobi.metrics.router.load_experiment", unavailable)
        with pytest.raises(OSError, match="configuration unavailable"):
            _client().get(f"/api/agents/{bobi_install.agent_name}/metrics/jev-config")

    def test_jev_config_bounds_security_and_hosted_refusal(self, bobi_install):
        from bobi.metrics.query import MetricsQueryError
        from bobi.webapp.runtime import LocalRuntime, TeamRuntime, UnknownTeam

        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        client = _client()
        for content in ("[]", "{", '{"instructions":"' + "x" * 131072 + '"}', '{"min_confidence":NaN}'):
            response = client.post(url, content=content, headers={"content-type": "application/json"})
            assert response.status_code == 400 and response.json()["code"] == "invalid_jev_config"
        for method in ("get", "post", "delete"):
            assert getattr(_testclient(), method)(url).status_code == 403
        hostile = TestClient(server.build_app(token=TOKEN), base_url="http://evil.example")
        assert hostile.delete(url, headers={"x-bobi-webui-token": TOKEN}).status_code == 403
        runtime = LocalRuntime()
        for call in (TeamRuntime.get_routing_config, TeamRuntime.update_routing_config):
            with pytest.raises(MetricsQueryError) as error:
                if call == TeamRuntime.get_routing_config:
                    call(runtime, bobi_install.agent_name)
                else:
                    call(runtime, bobi_install.agent_name, {"mode": "off"})
            assert error.value.code == "metrics_unsupported"
        with pytest.raises(UnknownTeam):
            runtime.get_routing_config("../outside")
        with pytest.raises(UnknownTeam):
            runtime.update_routing_config("../outside", {"mode": "off"})

    @pytest.mark.parametrize("stored", [{"policy": {"scope": "invalid"}}, {"api_key": "do-not-expose"}, []])
    def test_invalid_saved_config_is_safe_to_read(self, bobi_install, stored):
        from bobi import paths

        paths.env_path(bobi_install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(stored) + "\n")
        response = _client().get(f"/api/agents/{bobi_install.agent_name}/metrics/jev-config")
        assert response.status_code == 200
        assert response.json()["enabled"] is False and response.json()["mode"] == "off"
        assert response.json()["raw_json"] == "" and "do-not-expose" not in response.text

    def test_jev_config_roundtrip_reset_and_runtime_environment(self, bobi_install):
        import os
        from bobi import paths
        from bobi.config import parse_env_file, project_env
        from bobi.metrics.router import load_experiment
        from bobi.env import child_agent_env

        env_file = paths.env_path(bobi_install.repo_path)
        env_file.write_text("# keep this comment\nOTHER_TOKEN=synthetic-private\n")
        before = dict(os.environ)
        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        payload = {"mode": "shadow", "control_model": "model-a", "candidate_models": ["model-a", "model-b"],
                   "roles": ["engineer"], "instructions": "Choose B for complex work.\nOtherwise choose A."}
        client = _client()
        saved = client.post(url, json=payload)
        assert saved.status_code == 200, saved.text
        assert saved.headers["cache-control"] == "no-store"
        assert saved.json()["enabled"] is True
        assert saved.json()["instructions"] == payload["instructions"]
        assert saved.json()["restart_required"] is True and saved.json()["applies_on"] == "agent_restart"
        assert env_file.stat().st_mode & 0o777 == 0o600
        assert env_file.read_text().startswith("# keep this comment\nOTHER_TOKEN=synthetic-private\n")
        assert "synthetic-private" not in saved.text
        assert dict(os.environ) == before
        config, secret = load_experiment(project_env(bobi_install.repo_path))
        restarted_config, restarted_secret = load_experiment(child_agent_env(bobi_install.repo_path, base={}))
        assert restarted_config == config and restarted_secret == secret
        assert secret and config.policy.mode == "shadow"
        assert json.loads(config.policy.options_json)["instructions"] == payload["instructions"]
        assert client.get(url).json() == saved.json()
        assert client.get(f"/api/agents/{bobi_install.agent_name}/routing/config").json() == saved.json()
        deleted = client.delete(url)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["enabled"] is False and deleted.json()["mode"] == "off"
        assert deleted.json()["raw_json"] == ""
        assert parse_env_file(env_file)["BOBI_METRICS_EXPERIMENT_JSON"] == ""
        assert parse_env_file(env_file)["BOBI_METRICS_ASSIGNMENT_SECRET"] == ""
        assert "# BOBI_METRICS_EXPERIMENT_JSON=" not in env_file.read_text()
        assert load_experiment(project_env(bobi_install.repo_path)) is None
        assert dict(os.environ) == before
        assert not (bobi_install.state_dir / "metrics/metrics.db").exists()
        assert client.post(url, json={"mode": "off"}).json()["mode"] == "off"

    @pytest.mark.parametrize("invalid,field", [
        ({"mode": "invalid"}, "mode"), ({"mode": True}, "mode"), ({"enabled": "false"}, "enabled"),
        ({"enabled": True, "mode": "off"}, "mode"), ({"instructions": ""}, "instructions"),
        ({"instructions": 5}, "instructions"), ({"candidate_models": []}, "candidate_models"),
        ({"candidate_models": "model-a"}, "candidate_models"),
        ({"candidate_models": ["model-a", "model-a"]}, "candidate_models"),
        ({"candidate_models": [None]}, "candidate_models"), ({"control_model": "not-a-candidate"}, "control_model"),
        ({"roles": []}, "roles"), ({"roles": ["unknown-role"]}, "roles"), ({"roles": ["engineer", "engineer"]}, "roles"),
        ({"roles": "engineer"}, "roles"), ({"min_confidence": True}, "min_confidence"),
        ({"min_confidence": 1.5}, "min_confidence"), ({"api_key": "never-echo-this"}, "api_key"),
        ({"candidate_models": ["ds/deepseek-flash", "opus", "claude-opus-5-5"]}, "candidate_models"),
    ])
    def test_jev_config_rejects_invalid_input_without_mutation(self, bobi_install, invalid, field):
        from bobi import paths
        from tests.metrics.test_routing import concise_config

        env_file = paths.env_path(bobi_install.repo_path)
        before = "BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(concise_config()) + "\nBOBI_METRICS_ASSIGNMENT_SECRET=synthetic\n"
        env_file.write_text(before)
        response = _client().post(f"/api/agents/{bobi_install.agent_name}/metrics/jev-config", json=invalid)
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "invalid_jev_config"
        assert "never-echo-this" not in response.text
        assert env_file.read_text() == before
        # The message names the offending field so the editor can say what to fix.
        assert field in response.json()["error"]

    @pytest.mark.parametrize("brain,candidates,problem", [
        ("claude", ["haiku", "claude-opus-5-5"], None),
        ("claude", ["haiku", "ds/deepseek-v4-pro"], "ds/deepseek-v4-pro is a gateway routing id"),
        ("claude", ["haiku", "gpt-5.4"], "gpt-5.4 is an OpenAI model"),
        ("codex", ["gpt-5.6-luna", "gpt-5.6-sol"], None),
        ("codex", ["gpt-5.6-luna", "opus"], "opus is an Anthropic model"),
    ])
    def test_jev_candidates_must_run_on_a_native_brain(self, bobi_install, monkeypatch, brain, candidates, problem):
        from bobi import paths
        from bobi.config import parse_env_file

        monkeypatch.delenv("BOBI_GATEWAY_BASE_URL")
        monkeypatch.setenv("BOBI_BRAIN", brain)
        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        config = _client().get(url).json()
        assert config["agent_brain"] == brain and config["gateway"] is False
        assert config["brain"] == "auto" and config["prompt_egress"] == "redacted"
        response = _client().post(url, json={"mode": "shadow", "control_model": candidates[0],
            "candidate_models": candidates, "roles": ["engineer"], "instructions": "Route by difficulty."})
        if problem:
            assert response.status_code == 400 and problem in response.json()["error"]
            assert "BOBI_GATEWAY_BASE_URL" in response.json()["error"]
            return
        assert response.status_code == 200, response.text
        saved = json.loads(parse_env_file(paths.env_path(bobi_install.repo_path))["BOBI_METRICS_EXPERIMENT_JSON"])
        # The policy follows whichever brain the agent runs and keeps the chosen egress.
        assert saved["brain"] == "auto" and saved["prompt_egress"] == "redacted"
        assert response.json()["config_warnings"] == []

    def test_jev_pinned_brain_must_match_the_agent(self, bobi_install, monkeypatch):
        from bobi import paths
        from tests.metrics.test_routing import concise_config

        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        rejected = _client().post(url, json={**{k: concise_config()[k] for k in ("control_model", "candidate_models", "roles", "instructions")},
                                             "mode": "enforce", "brain": "codex", "prompt_egress": "none"})
        assert rejected.status_code == 400 and "does not match this agent's claude brain" in rejected.json()["error"]
        paths.env_path(bobi_install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(
            {**concise_config(), "brain": "codex"}) + "\nBOBI_METRICS_ASSIGNMENT_SECRET=synthetic\n")
        # A saved policy that can never route this agent is flagged on read.
        assert "does not match" in _client().get(url).json()["config_warnings"][0]

    def test_jev_mode_off_keeps_the_policy_for_re_enabling(self, bobi_install):
        from bobi import paths
        from bobi.config import parse_env_file
        from tests.metrics.test_routing import concise_config

        env_file = paths.env_path(bobi_install.repo_path)
        env_file.write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(concise_config()) + "\nBOBI_METRICS_ASSIGNMENT_SECRET=synthetic\n")
        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        off = _client().post(url, json={"mode": "off"}).json()
        assert off["enabled"] is False and off["configured_mode"] == "enforce"
        assert off["instructions"] == concise_config()["instructions"]
        assert parse_env_file(env_file)["BOBI_METRICS_EXPERIMENT_JSON"] == ""
        restored = _client().post(url, json={"mode": "enforce"}).json()
        assert restored["enabled"] is True and restored["instructions"] == concise_config()["instructions"]

    @pytest.mark.parametrize("legacy", [False, True])
    def test_jev_edit_preserves_advanced_policy_and_updates_criteria(self, bobi_install, legacy, monkeypatch):
        monkeypatch.setenv("BOBI_BRAIN", "codex")
        from bobi import paths
        from bobi.config import parse_env_file
        from bobi.metrics.router import ExperimentConfig, public_config
        from tests.metrics.test_routing import concise_config

        raw = {**concise_config(), "brain": "codex", "deadline_ms": 1750,
               "endpoint": "https://example.invalid/systemone", "prompt_egress": "redacted",
               "max_prompt_bytes": 4096, "entry_points": ["session_start"],
               "criteria": {"ds/deepseek-flash": "Retain this criterion", "ds/deepseek-v4-pro": "Removed criterion"}}
        if legacy:
            raw = public_config(ExperimentConfig.from_mapping(raw))
            raw["policy"]["options"]["endpoint"] = "https://example.invalid/systemone"
            raw["policy"]["credential_env"] = "CUSTOM_JEV_KEY"
            raw["policy"]["max_in_flight"] = 3
        env_file = paths.env_path(bobi_install.repo_path)
        env_file.write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(raw) + "\nBOBI_METRICS_ASSIGNMENT_SECRET=synthetic\n")
        response = _client().post(f"/api/agents/{bobi_install.agent_name}/metrics/jev-config", json={
            "candidate_models": ["ds/deepseek-flash", "new-model"], "instructions": "Edited instructions"})
        assert response.status_code == 200, response.text
        saved = json.loads(parse_env_file(env_file)["BOBI_METRICS_EXPERIMENT_JSON"])
        policy = ExperimentConfig.from_mapping(saved).policy
        assert policy.brain == "codex" and policy.deadline_ms == 1750
        assert policy.prompt_egress == "redacted" and policy.max_prompt_bytes == 4096
        assert policy.entry_points == ("session_start",)
        options = json.loads(policy.options_json)
        assert options["endpoint"] == "https://example.invalid/systemone"
        assert options["instructions"] == "Edited instructions"
        assert options["criteria"]["ds/deepseek-flash"] == "Retain this criterion"
        assert set(options["criteria"]) == {"ds/deepseek-flash", "new-model"}
        if legacy:
            assert policy.credential_env == "CUSTOM_JEV_KEY" and policy.max_in_flight == 3

    def test_jev_config_preserves_file_on_read_and_write_failure(self, bobi_install, monkeypatch):
        from pathlib import Path
        from bobi import paths

        env_file = paths.env_path(bobi_install.repo_path)
        env_file.write_text("OTHER_TOKEN=synthetic-private\n")
        read_text = Path.read_text
        def fail_read(target, *args, **kwargs):
            if target == env_file:
                raise OSError("private-error-do-not-echo")
            return read_text(target, *args, **kwargs)
        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        with monkeypatch.context() as context:
            context.setattr(Path, "read_text", fail_read)
            response = _client().post(url, json={"mode": "off"})
        assert response.status_code == 500 and response.json()["code"] == "config_write_failed"
        assert "private-error" not in response.text
        assert env_file.read_text() == "OTHER_TOKEN=synthetic-private\n"
        def fail_write(*args, **kwargs):
            raise OSError("private-error-do-not-echo")
        monkeypatch.setattr("bobi.fsutil.atomic_write_text", fail_write)
        response = _client().delete(url)
        assert response.status_code == 500 and response.json()["code"] == "config_write_failed"
        assert env_file.read_text() == "OTHER_TOKEN=synthetic-private\n"

    def test_jev_config_process_override_rejects_false_success(self, bobi_install, monkeypatch):
        from bobi import paths

        env_file = paths.env_path(bobi_install.repo_path)
        env_file.write_text("OTHER_TOKEN=synthetic-private\n")
        monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", "{}")
        url = f"/api/agents/{bobi_install.agent_name}/metrics/jev-config"
        response = _client().post(url, json={"mode": "off"})
        assert response.status_code == 409 and response.json()["code"] == "config_environment_override"
        assert env_file.read_text() == "OTHER_TOKEN=synthetic-private\n"

    def test_jev_concurrent_edits_keep_both_changes(self, bobi_install):
        from concurrent.futures import ThreadPoolExecutor
        from bobi.webapp.runtime import LocalRuntime
        from tests.metrics.test_routing import concise_config

        runtime = LocalRuntime()
        runtime.update_routing_config(bobi_install.agent_name, concise_config())
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(runtime.update_routing_config, bobi_install.agent_name, payload) for payload in (
                {"instructions": "Concurrent instructions"}, {"roles": ["engineer"]})]
            for future in futures:
                assert future.result()["enabled"] is True
        saved = runtime.get_routing_config(bobi_install.agent_name)
        assert saved["instructions"] == "Concurrent instructions" and saved["roles"] == ["engineer"]

    def test_jev_edit_does_not_leak_into_another_agent(self, bobi_install, monkeypatch):
        import os
        import shutil
        from bobi import paths
        from bobi.config import _DOTENV_LOADED, project_env
        from bobi.env import child_agent_env
        from bobi.metrics.router import load_experiment
        from bobi.webapp.runtime import LocalRuntime
        from tests.metrics.test_routing import concise_config

        other_root = bobi_install.agents_dir / "second-agent" / "run"
        shutil.copytree(bobi_install.repo_path / "package", other_root / "package")
        raw = json.dumps(concise_config())
        paths.env_path(other_root).write_text("BOBI_METRICS_EXPERIMENT_JSON=" + raw + "\nBOBI_METRICS_ASSIGNMENT_SECRET=other-agent-synthetic\n")
        monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", raw)
        monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "other-agent-synthetic")
        monkeypatch.setitem(_DOTENV_LOADED, "BOBI_METRICS_EXPERIMENT_JSON", raw)
        monkeypatch.setitem(_DOTENV_LOADED, "BOBI_METRICS_ASSIGNMENT_SECRET", "other-agent-synthetic")
        process_before = dict(os.environ)
        other_before = paths.env_path(other_root).read_text()
        runtime = LocalRuntime()
        runtime.update_routing_config(bobi_install.agent_name, {**concise_config(), "instructions": "Changed for first agent"})
        assert runtime.get_routing_config("second-agent")["instructions"] == concise_config()["instructions"]
        assert paths.env_path(other_root).read_text() == other_before and dict(os.environ) == process_before
        config, secret = load_experiment(child_agent_env(bobi_install.repo_path))
        assert json.loads(config.policy.options_json)["instructions"] == "Changed for first agent"
        assert secret != b"other-agent-synthetic"
        assert load_experiment(project_env(other_root))[1] == b"other-agent-synthetic"

    @pytest.mark.parametrize("trigger,prompt,title", [
        ("startup", "You are a bobi director.\n# Bobi Agent\nHow you receive events", "Agent Startup & Initialization"),
        ("inbox", "You are the **sleep cycle** for this agent team. Internal transcript delta", "Sleep Cycle & Memory Compaction"),
        ("inbox", "Compaction required: reduce the session context.", "Sleep Cycle & Memory Compaction"),
        ("inbox", "No new events. Wait for delivery.", "Idle Standby (No new events)"),
        ("inbox", "Process pending inbox messages.", "Inbox Event Processing"),
        ("direct", "Fix sleep cycle regression", "Fix sleep cycle regression"),
        ("chat", "Process inbox", "Process inbox"),
        ("user", "Explain the # Bobi Agent header", "Explain the # Bobi Agent header"),
    ])
    def test_semantic_titles_do_not_show_internal_triggers(self, trigger, prompt, title):
        from bobi.webapp.runtime import _turn_conversation

        turn = {"started_at_us": 1_000_000, "ended_at_us": 2_000_000, "trigger_kind": trigger}
        entries = [{"kind": "message", "role": "user", "text": prompt, "at": "1970-01-01T00:00:01.1Z"}]
        data = _turn_conversation(turn, entries)
        assert data["semantic_title"] == title
        if title != prompt:
            assert data["user_message"] is None and data["prompt_snippet"] is None
        else:
            assert data["user_message"] == prompt

    def test_semantic_title_user_first_and_curator_session_fallback(self):
        from bobi.webapp.runtime import _turn_conversation

        turn = {"started_at_us": 1_000_000, "ended_at_us": 2_000_000, "trigger_kind": "inbox"}
        entries = [{"kind": "message", "role": "user", "text": text, "at": "1970-01-01T00:00:01.1Z"} for text in (
            "You are the **sleep cycle** for this agent team. Internal machinery",
            "Event: slack/message\nFix sleep cycle regression\nconversation: slack:T123:channel:C123:thread:1791360056.16")]
        session = {"role": "curator", "session_name": "curator-1"}
        data = _turn_conversation(turn, entries, session=session)
        assert data["semantic_title"] == "Fix sleep cycle regression"
        assert data["user_message"] == "Fix sleep cycle regression"
        assert data["origin"]["source"] == "slack"
        assert _turn_conversation(turn, [], session=session)["semantic_title"] == "Sleep Cycle & Memory Compaction"
        wrapped = [{"kind": "message", "role": "user", "at": "1970-01-01T00:00:01.1Z",
                    "text": "You are an agent in a bobi deployment. You are the **sleep cycle** for this agent team."}]
        assert _turn_conversation(turn, wrapped, session=session)["semantic_title"] == "Sleep Cycle & Memory Compaction"
        startup = {**turn, "trigger_kind": "startup"}
        idle = [{"kind": "message", "role": "agent", "text": "No new events.", "at": "1970-01-01T00:00:01.1Z"}]
        assert _turn_conversation(startup, idle)["semantic_title"] == "Idle Standby (No new events)"
        assert _turn_conversation(turn, idle, session=session)["semantic_title"] == "Idle Standby (No new events)"
        inbox = [{"kind": "message", "role": "user", "text": "Process pending inbox messages.", "at": "1970-01-01T00:00:01.1Z"}]
        assert _turn_conversation(turn, inbox, session=session)["semantic_title"] == "Inbox Event Processing"

    def test_runtime_preserves_tool_preview_bytes_and_marks_additional_clipping(self):
        from bobi.webapp.runtime import _turn_conversation

        turn = {"started_at_us": 1_000_000, "ended_at_us": 2_000_000, "trigger_kind": "inbox"}
        entries = [{"kind": "tool_result", "role": "user", "text": "é" * 4000,
                    "total_bytes": 12000, "truncated": True, "at": "1970-01-01T00:00:01.1Z"},
                   {"kind": "message", "role": "agent", "text": "é" * 5000,
                    "truncated": False, "at": "1970-01-01T00:00:01.2Z"}]
        data = _turn_conversation(turn, entries)
        assert data["entries"][0]["total_bytes"] == 12000 and data["entries"][0]["truncated"] is True
        assert len(data["entries"][0]["text"]) == 4000
        assert data["entries"][1]["total_bytes"] == 10000 and data["entries"][1]["truncated"] is False
        assert data["entries"][1]["text"] == entries[1]["text"]
        assert data["truncated"] is True

    def test_runtime_tool_preview_has_no_secondary_cap_and_preserves_metadata(self):
        from bobi.webapp.runtime import _turn_conversation

        text = "hello " * 1000
        turn = {"started_at_us": 1_000_000, "ended_at_us": 2_000_000, "trigger_kind": "user"}
        entry = {"kind": "tool_result", "role": "user", "text": text, "at": "1970-01-01T00:00:01.1Z"}
        data = _turn_conversation(turn, [entry])
        assert data["entries"][0]["text"] == "hello " * 666
        assert data["entries"][0]["total_bytes"] == len(text.encode("utf-8"))
        assert data["entries"][0]["truncated"] is True
        assert entry["text"] == text

    def test_runtime_preserves_complete_messages_until_aggregate_budget(self):
        from bobi.webapp.runtime import _turn_conversation

        turn = {"started_at_us": 1_000_000, "ended_at_us": 2_000_000, "trigger_kind": "user"}
        entry = {"kind": "message", "role": "user", "text": "é" * 10000,
                 "truncated": False, "at": "1970-01-01T00:00:01.1Z"}
        data = _turn_conversation(turn, [entry])
        assert data["entries"][0]["text"] == data["input"] == data["user_message"] == entry["text"]
        assert data["truncated"] is False
        oversized = {**entry, "text": "界" * 100000}
        bounded = _turn_conversation(turn, [oversized])
        assert len(bounded["input"].encode("utf-8")) <= 32768
        assert len(bounded["user_message"].encode("utf-8")) <= 32768
        assert len(bounded["entries"][0]["text"].encode("utf-8")) <= 32768
        assert bounded["entries"][0]["total_bytes"] == 300000
        assert bounded["entries"][0]["truncated"] is True and bounded["truncated"] is True

    def test_sandbox_classifies_without_writing_state_or_changing_credentials(self, bobi_install, monkeypatch):
        import httpx
        import os
        from bobi import paths
        from bobi.metrics.store import connect
        from tests.metrics.test_routing import concise_config

        seed_dashboard(bobi_install.repo_path, 1_000_000)
        env_file = paths.env_path(bobi_install.repo_path)
        minimal = {field: concise_config()[field] for field in ("control_model", "candidate_models", "instructions")}
        minimal["prompt_egress"] = "redacted"
        env_file.write_text("# BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(minimal) +
                            "\nTYPESAFE_API_KEY=configured-test-key\n")
        database = bobi_install.state_dir / "metrics/metrics.db"
        connection = connect(database)
        before = "\n".join(connection.iterdump())
        connection.close()
        files_before = {path: path.read_bytes() for path in bobi_install.state_dir.rglob("*") if path.is_file()}
        env_before = dict(os.environ)
        wire_calls = []
        client_class = httpx.AsyncClient

        def handler(request):
            wire_calls.append(request)
            payload = json.loads(request.content)
            assert payload["state"]["task"] == "Write Raft. API_KEY=[redacted] [redacted]"
            assert payload["state"]["role"] == "curator"
            assert payload["state"]["entry_point"] == "subagent_phase"
            assert request.headers["authorization"] == "Bearer override-test-key"
            return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"route": {
                "type": "choice", "choice": "ds/deepseek-v4-pro", "confidence": 0.94,
                "probabilities": {"ds/deepseek-flash": 0.03, "ds/deepseek-v4-pro": 0.97}}},
                "usage": {"input_tokens": 12, "output_tokens": 4}, "debug": "override-test-key",
                "saved": "configured-test-key", "access_token": "opaque-vendor-token"})

        monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
            lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs))
        payload = {"prompt": "Write Raft. API_KEY=private configured-test-key", "api_key": "override-test-key",
                   "features": {"role": "curator", "entry_point": "subagent_phase", "brain": "claude"}}
        url = f"/api/agents/{bobi_install.agent_name}/metrics/test-route"
        result = _client().post(url, json=payload)
        assert result.status_code == 200, result.text
        data = result.json()
        assert data["selected_model"] == "ds/deepseek-v4-pro" and data["confidence"] == 0.94
        assert data["probabilities"]["ds/deepseek-v4-pro"] == 0.97 and data["latency_ms"] >= 0
        assert data["outgoing_payload"]["state"] == data["features"]
        assert data["raw_response"]["debug"] == "[redacted]"
        assert data["raw_response"]["saved"] == "[redacted]"
        assert data["raw_response"]["access_token"] == "[redacted]"
        assert data["raw_response"]["usage"] == {"input_tokens": 12, "output_tokens": 4}
        assert data["features"]["prompt_bytes"] == len(payload["prompt"].encode())
        assert result.headers["cache-control"] == "no-store"
        assert "override-test-key" not in result.text and "private" not in result.text
        assert len(wire_calls) == 1 and dict(os.environ) == env_before
        connection = connect(database)
        assert "\n".join(connection.iterdump()) == before
        connection.close()
        assert {path: path.read_bytes() for path in files_before} == files_before
        assert env_file.read_text().startswith("# BOBI_METRICS_EXPERIMENT_JSON=")

    @pytest.mark.parametrize("body", [b'{"api_key":"private-canary",', b'[]',
        b'{"prompt":"test","api_key":"private-canary","extra":"' + b'x' * 131072 + b'"}',
        b'[' * 2000 + b'0' + b']' * 2000])
    def test_sandbox_rejects_bad_bodies_without_echoing_secrets(self, bobi_install, monkeypatch, body):
        monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
                            lambda **kwargs: pytest.fail("invalid body reached network"))
        response = _client().post(f"/api/agents/{bobi_install.agent_name}/metrics/test-route",
                                  content=body, headers={"content-type": "application/json"})
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_simulation"
        assert "private-canary" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert not (bobi_install.state_dir / "metrics/metrics.db").exists()

    def test_sandbox_concurrent_keys_remain_request_local(self, bobi_install, monkeypatch):
        import asyncio
        import os
        from bobi import paths
        from bobi.metrics.policy import PolicyResult
        from bobi.metrics.query import MetricsQueryError
        from bobi.webapp.runtime import LocalRuntime
        from tests.metrics.test_routing import concise_config

        paths.env_path(bobi_install.repo_path).write_text(
            "BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(concise_config()) + "\n")
        before = dict(os.environ)
        calls = []
        async def decide(policy, request, *, timeout_s):
            key = policy.api_key
            calls.append(key)
            await asyncio.sleep(0.01)
            assert policy.api_key == key
            return PolicyResult("ds/deepseek-flash", 0.94, None, "jev-1.13.0", None, None,
                {"ds/deepseek-flash": 0.97, "ds/deepseek-v4-pro": 0.03}, {"debug": key}, {})
        monkeypatch.setattr("bobi.metrics.policies.typesafe.TypeSafePolicy.decide", decide)
        runtime = LocalRuntime()
        async def exercise():
            return await asyncio.gather(*(runtime.test_route(bobi_install.agent_name,
                {"prompt": "test", "api_key": f"request-{index}-key"}) for index in range(6)),
                return_exceptions=True)
        results = asyncio.run(exercise())
        assert calls == [f"request-{index}-key" for index in range(4)]
        assert all(result["raw_response"]["debug"] == "[redacted]" for result in results[:4])
        assert all(isinstance(result, MetricsQueryError) and result.code == "metrics_busy" for result in results[4:])
        assert dict(os.environ) == before
        for _ in range(4):
            assert runtime._routing_tests.acquire(blocking=False)
        for _ in range(4):
            runtime._routing_tests.release()
        assert not (bobi_install.state_dir / "metrics/metrics.db").exists()

    @pytest.mark.parametrize("payload", [
        {}, {"prompt": " "}, {"prompt": "é" * 32769}, {"prompt": "test", "api_key": ["bad"]},
        {"prompt": "test", "api_key": False}, {"prompt": "test", "api_key": None},
        {"prompt": "test", "endpoint": False}, {"prompt": "test", "endpoint": None},
        {"prompt": "test", "features": {"task": "injected"}}, {"prompt": "test", "features": None},
        {"prompt": "test", "features": {"role": "unknown"}},
        {"prompt": "test", "features": {"entry_point": "unknown"}},
        {"prompt": "test", "endpoint": "https://127.0.0.1/private"},
        {"prompt": "test", "endpoint": "https://evil.example"},
        {"prompt": "test", "endpoint": "https://api.typesafe.ai:444/v1/systemone"},
        {"prompt": "test", "endpoint": "https://api.typesafe.ai:bad/v1/systemone"},
        {"prompt": "test", "endpoint": "http://api.typesafe.ai/v1/systemone"},
        {"prompt": "test", "endpoint": "https://user:pass@api.typesafe.ai/v1/systemone"},
        {"prompt": "test", "endpoint": "https://api.typesafe.ai:0/v1/systemone"},
        {"prompt": "test", "endpoint": "https://api.typesafe.ai/v1/systemone\n"},
        {"prompt": "test", "api_key": "bad\nkey"}, {"prompt": "test", "api_key": "x" * 4097},
    ])
    def test_sandbox_rejects_invalid_inputs_without_network_or_database(self, bobi_install, monkeypatch, payload):
        from bobi import paths
        from tests.metrics.test_routing import concise_config

        paths.env_path(bobi_install.repo_path).write_text(
            "BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(concise_config()) + "\n")
        monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
                            lambda **kwargs: pytest.fail("invalid sandbox input reached network"))
        response = _client().post(f"/api/agents/{bobi_install.agent_name}/metrics/test-route", json=payload)
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_simulation"
        assert not (bobi_install.state_dir / "metrics/metrics.db").exists()

    def test_sandbox_security_and_missing_configuration(self, bobi_install):
        url = f"/api/agents/{bobi_install.agent_name}/metrics/test-route"
        assert _testclient().post(url, json={"prompt": "test"}).status_code == 403
        hostile = TestClient(server.build_app(token=TOKEN), base_url="http://evil.example")
        hostile.headers.update({"x-bobi-webui-token": TOKEN})
        assert hostile.post(url, json={"prompt": "test"}).status_code == 403
        assert _client().post("/api/agents/absent/metrics/test-route", json={"prompt": "test"}).status_code == 404
        assert _client().post(url, json={"prompt": "test"}).status_code == 400

    @pytest.mark.parametrize("fault", ["unexpected", "nested", "expanded", "trusted_override"])
    def test_sandbox_bounds_and_sanitizes_vendor_results(self, bobi_install, monkeypatch, fault):
        import httpx
        from bobi import paths
        from tests.metrics.test_routing import concise_config

        config = {**concise_config(), "max_prompt_bytes": 65536, "prompt_egress": "redacted",
                  "endpoint": "https://router.example:8443/v1/systemone"}
        paths.env_path(bobi_install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(config) +
                                                       "\nTYPESAFE_API_KEY=private-canary\n")
        client_class = httpx.AsyncClient
        def handler(request):
            assert str(request.url) == "https://router.example:8443/alternate"
            if fault == "unexpected":
                raise OSError("private-canary sensitive-vendor-error")
            raw = {"model": "jev-1.13.0", "answers": {"route": {
                "type": "choice", "choice": "ds/deepseek-flash", "confidence": 0.94,
                "probabilities": {"ds/deepseek-flash": 0.97, "ds/deepseek-v4-pro": 0.03}}},
                "usage": {"input_tokens": 12, "output_tokens": 4}}
            if fault == "nested":
                nested = {}
                for _ in range(40):
                    nested = {"child": nested}
                raw["extra"] = nested
            if fault == "expanded":
                raw["extra"] = "é" * 30000
            return httpx.Response(200, content=json.dumps(raw, ensure_ascii=False).encode())
        monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
            lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs))
        response = _client().post(f"/api/agents/{bobi_install.agent_name}/metrics/test-route",
            json={"prompt": "é" * 30000 if fault == "expanded" else "test",
                  "endpoint": "https://router.example:8443/alternate"})
        assert response.status_code == (200 if fault == "trusted_override" else 502)
        assert "private-canary" not in response.text and "sensitive-vendor-error" not in response.text
        assert not (bobi_install.state_dir / "metrics/metrics.db").exists()

    def test_sandbox_deadline_and_saturation_release_without_affecting_production(self, bobi_install, monkeypatch):
        import asyncio
        from bobi import paths
        from bobi.webapp.runtime import LocalRuntime
        from tests.metrics.test_routing import concise_config

        paths.env_path(bobi_install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_JSON=" +
            json.dumps({**concise_config(), "deadline_ms": 50}) + "\nTYPESAFE_API_KEY=test-only-key\n")
        async def slow_policy(self, request, *, timeout_s):
            await asyncio.sleep(1)
        monkeypatch.setattr("bobi.metrics.policies.typesafe.TypeSafePolicy.decide", slow_policy)
        runtime = LocalRuntime()
        client = TestClient(server.build_app(token=TOKEN, runtime=runtime), base_url="http://127.0.0.1")
        client.headers.update({"x-bobi-webui-token": TOKEN})
        url = f"/api/agents/{bobi_install.agent_name}/metrics/test-route"
        for _ in range(4):
            assert runtime._routing_tests.acquire(blocking=False)
        response = client.post(url, json={"prompt": "test"})
        assert response.status_code == 503 and response.json()["code"] == "metrics_busy"
        for _ in range(4):
            runtime._routing_tests.release()
        for _ in range(2):
            response = client.post(url, json={"prompt": "test"})
            assert response.status_code == 504 and response.json()["code"] == "policy_timeout"
        for _ in range(4):
            assert runtime._routing_tests.acquire(blocking=False)
        for _ in range(4):
            runtime._routing_tests.release()
        assert not (bobi_install.state_dir / "metrics/metrics.db").exists()

    def test_sandbox_accepts_legacy_policies_and_custom_secret_names(self, bobi_install, monkeypatch):
        import httpx
        from bobi import paths
        from bobi.metrics.router import ExperimentConfig, public_config
        from tests.metrics.test_routing import concise_config

        config = public_config(ExperimentConfig.from_mapping(concise_config()))
        config["policy"]["credential_env"] = "CUSTOM_TYPESAFE_KEY"
        config["policy"]["brain"] = "codex"
        paths.env_path(bobi_install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(config) +
                                                       "\nCUSTOM_TYPESAFE_KEY=legacy-test-key\n")
        client_class = httpx.AsyncClient
        def handler(request):
            assert request.headers["authorization"] == "Bearer legacy-test-key"
            assert json.loads(request.content)["state"]["brain"] == "codex"
            return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"route": {
                "type": "choice", "choice": "ds/deepseek-v4-pro", "confidence": 0.94,
                "probabilities": {"ds/deepseek-flash": 0.03, "ds/deepseek-v4-pro": 0.97}}},
                "usage": {"input_tokens": 12, "output_tokens": 4}})
        monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
            lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs))
        response = _client().post(f"/api/agents/{bobi_install.agent_name}/metrics/test-route", json={"prompt": "test"})
        assert response.status_code == 200, response.text
        assert response.json()["selected_model"] == "ds/deepseek-v4-pro"
        assert not (bobi_install.state_dir / "metrics/metrics.db").exists()

    @pytest.mark.parametrize("mode,confidence,status,version", [
        ("shadow", 0.94, 200, "jev-1.13.0"), ("enforce", 0.5, 200, "jev-1.13.0"),
        ("enforce", 0.94, 401, "jev-1.13.0"), ("enforce", 0.94, 200, "jev-other"),
        ("enforce", 0.94, 200, "jev-1.13.4"), ("enforce", 0.94, 200, "jev-1.14.0"),
    ])
    def test_sandbox_uses_saved_key_and_previews_guarded_decisions(self, bobi_install, monkeypatch, mode, confidence, status, version):
        import httpx
        from bobi import paths
        from tests.metrics.test_routing import concise_config

        config = {**concise_config(), "mode": mode}
        paths.env_path(bobi_install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(config) +
                                                       "\nTYPESAFE_API_KEY=saved-test-key\n")
        client_class = httpx.AsyncClient
        def handler(request):
            assert request.headers["authorization"] == "Bearer saved-test-key"
            return httpx.Response(status, json={"model": version, "answers": {"route": {
                "type": "choice", "choice": "ds/deepseek-v4-pro", "confidence": confidence,
                "probabilities": {"ds/deepseek-flash": 0.03, "ds/deepseek-v4-pro": 0.97}}},
                "usage": {"input_tokens": 12, "output_tokens": 4}})
        monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
            lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs))
        response = _client().post(f"/api/agents/{bobi_install.agent_name}/metrics/test-route", json={"prompt": "test"})
        if status != 200:
            assert response.status_code == 502
            assert response.json()["code"] == "policy_unauthenticated"
            assert "saved-test-key" not in response.text
            return
        assert response.status_code == 200, response.text
        data = response.json()
        # A patch bump is the pinned model; another release line previews as a fallback, not an error.
        drift = version in {"jev-other", "jev-1.14.0"}
        assert data["model_version"] == version and data["pinned_version"] == "jev-1.13.0"
        assert data["fallback_reason"] == ("policy_version_drift" if drift else "policy_low_confidence" if confidence < 0.85 else None)
        assert data["effective_model"] == ("ds/deepseek-flash" if drift or confidence < 0.85 or mode == "shadow" else "ds/deepseek-v4-pro")

    @pytest.mark.parametrize("egress", ["none", "redacted"])
    def test_sandbox_sends_task_text_only_when_production_would(self, bobi_install, monkeypatch, egress):
        import httpx
        from bobi import paths
        from tests.metrics.test_routing import concise_config

        paths.env_path(bobi_install.repo_path).write_text("BOBI_METRICS_EXPERIMENT_JSON=" + json.dumps(
            {**concise_config(), "prompt_egress": egress}) + "\nTYPESAFE_API_KEY=saved-test-key\n")
        client_class = httpx.AsyncClient
        sent = []
        def handler(request):
            sent.append(json.loads(request.content)["state"])
            return httpx.Response(200, json={"model": "jev-1.13.0", "answers": {"route": {
                "type": "choice", "choice": "ds/deepseek-flash", "confidence": 0.9,
                "probabilities": {"ds/deepseek-flash": 0.9, "ds/deepseek-v4-pro": 0.1}}},
                "usage": {"input_tokens": 1, "output_tokens": 1}})
        monkeypatch.setattr("bobi.metrics.policies.typesafe.httpx.AsyncClient",
            lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs))
        response = _client().post(f"/api/agents/{bobi_install.agent_name}/metrics/test-route",
                                  json={"prompt": "Refactor the billing module"})
        assert response.status_code == 200, response.text
        assert response.json()["prompt_egress"] == egress
        assert ("task" in sent[0]) is (egress == "redacted")
        assert sent[0]["prompt_bytes"] == len("Refactor the billing module")

    def test_routing_config_lifecycle(self, bobi_install):
        c = _client()
        name = bobi_install.agent_name

        # Initial state should be disabled
        r = c.get(f"/api/agents/{name}/routing/config")
        assert r.status_code == 200
        data = r.json()
        assert "enabled" in data

        # Enable JEV in shadow mode
        r_update = c.post(f"/api/agents/{name}/routing/config", json={
            "enabled": True,
            "mode": "shadow",
            "control_model": "ds/deepseek-flash",
            "candidate_models": ["ds/deepseek-flash", "ds/deepseek-v4-pro"],
            "roles": ["engineer"],
            "min_confidence": 0.85
        })
        assert r_update.status_code == 200
        updated = r_update.json()
        assert updated["enabled"] is True
        assert updated["mode"] == "shadow"
        assert updated["control_model"] == "ds/deepseek-flash"
        from bobi import paths
        from bobi.config import parse_env_file
        saved = json.loads(parse_env_file(paths.env_path(bobi_install.repo_path))["BOBI_METRICS_EXPERIMENT_JSON"])
        assert saved["candidate_models"] == ["ds/deepseek-flash", "ds/deepseek-v4-pro"]
        assert "variants" not in saved and "policy" not in saved and "router_name" not in saved

        # Update to enforce mode
        r_enforce = c.post(f"/api/agents/{name}/routing/config", json={
            "enabled": True,
            "mode": "enforce",
            "control_model": "ds/deepseek-flash",
            "candidate_models": ["ds/deepseek-flash", "ds/deepseek-v4-pro"],
            "roles": ["engineer"]
        })
        assert r_enforce.status_code == 200
        assert r_enforce.json()["mode"] == "enforce"

        # Disable JEV
        r_disable = c.post(f"/api/agents/{name}/routing/config", json={"enabled": False})
        assert r_disable.status_code == 200
        assert r_disable.json()["enabled"] is False
