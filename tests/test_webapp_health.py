"""Agent state: the health tri-state and the strip's telemetry (U3).

Two layers: the state fold over a seeded home — including a REAL manager
health server for the running case and a real silent socket for the wedged
one, because "the probe answered" is the whole claim and a mocked probe would
not test it — and the endpoint that serves it.
"""

import json
import os
import socket
import time

import pytest
from fastapi.testclient import TestClient

from bobi import paths
from bobi.webapp import health, server
from bobi.webapp.health import NOT_RESPONDING, RUNNING, STOPPED

TOKEN = "health-token-123"
MANAGER = "bobi-test-agent-director"
NOW = 1_800_000_000.0


def _seed_session(sessions_dir, name, *, status, role="engineer", pid=0,
                  started_at=NOW - 3600, last_activity=NOW, terminal_at=0.0,
                  error=""):
    d = sessions_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({
        "name": name, "role": role, "status": status, "pid": pid,
        "started_at": started_at, "last_activity": last_activity,
        "terminal_at": terminal_at, "error": error,
    }))


def _seed_pidfile(root, pid):
    p = paths.manager_pid_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(pid))


def _seed_port_file(root, port):
    (paths.state_path(root) / "manager-health.port").write_text(str(port))


def _segments(body):
    return {s["key"]: s for s in body["segments"]}


@pytest.fixture
def silent_port():
    """A port that accepts connections and never answers — a wedged manager.

    Listening without accepting is what SIGSTOP looks like from outside: the
    kernel completes the handshake from the backlog, so the probe does not get
    a refusal, it gets silence. That is the case the timeout exists for.
    """
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    yield sock.getsockname()[1]
    sock.close()


@pytest.fixture
def closed_port():
    """A port with nothing on it: connection refused, the fast failure."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def live_health_server(bobi_install):
    """A REAL manager health server, the way the manager starts one."""
    from bobi import manager_health

    port = manager_health.start(
        paths.state_path(bobi_install.repo_path), "test-agent",
        session_status_fn=lambda: [],
        manager_status_fn=lambda: {"session": MANAGER, "status": "idle",
                                   "last_activity": NOW,
                                   "idle_seconds": 42.0})
    yield port
    manager_health.stop()


def _state(bobi_install, **kw):
    """The fold as LocalRuntime calls it, with the seeded home's real files."""
    from bobi import service
    from bobi.sdk import SessionRegistry

    root = bobi_install.repo_path
    status = service.team_status(root)
    entries = SessionRegistry(root).list_all(reap_dead=True)
    active = [e for e in entries if e.status in ("starting", "running", "idle")]
    mgr = next((e for e in entries if e.name == MANAGER), None)
    kw.setdefault("running", status.manager_running)
    kw.setdefault("pid", status.manager_pid)
    kw.setdefault("root", root)
    kw.setdefault("manager_entry", mgr)
    kw.setdefault("live_runs", sum(1 for e in active if e.name != MANAGER))
    kw.setdefault("last_activity",
                  max((e.last_activity or 0.0 for e in active), default=0.0))
    kw.setdefault("now", NOW)
    return health.build_state(**kw)


# === The tri-state ===

class TestState:
    def test_no_pid_is_stopped(self, bobi_install):
        body = _state(bobi_install)
        assert body["state"] == STOPPED
        assert body["probe_ok"] is None

    def test_live_pid_and_answering_probe_is_running(self, bobi_install,
                                                     live_health_server):
        _seed_pidfile(bobi_install.repo_path, os.getpid())
        body = _state(bobi_install)
        assert body["state"] == RUNNING
        assert body["probe_ok"] is True
        # The probe's own server-derived idle time, not a re-derivation here.
        assert body["idle_seconds"] == 42.0

    def test_live_pid_and_silent_probe_is_not_responding(
            self, bobi_install, silent_port, monkeypatch):
        # The wedged manager: process alive, endpoint accepts, nothing comes
        # back. Before this, that rendered as healthy — the #887 defect.
        monkeypatch.setattr(health, "PROBE_TIMEOUT_SECONDS", 0.25)
        _seed_pidfile(bobi_install.repo_path, os.getpid())
        _seed_port_file(bobi_install.repo_path, silent_port)
        body = _state(bobi_install)
        assert body["state"] == NOT_RESPONDING
        assert body["probe_ok"] is False
        assert str(silent_port) in body["detail"]

    def test_live_pid_and_refused_probe_is_not_responding(self, bobi_install,
                                                          closed_port):
        _seed_pidfile(bobi_install.repo_path, os.getpid())
        _seed_port_file(bobi_install.repo_path, closed_port)
        assert _state(bobi_install)["state"] == NOT_RESPONDING

    def test_no_port_file_falls_back_to_the_pidfile_view(self, bobi_install):
        # A manager that publishes no health endpoint must not read as wedged:
        # "never asked" is not "asked and got nothing".
        _seed_pidfile(bobi_install.repo_path, os.getpid())
        body = _state(bobi_install)
        assert body["state"] == RUNNING
        assert body["probe_ok"] is None
        assert "pidfile view" in body["detail"]

    def test_unreadable_port_file_falls_back_too(self, bobi_install):
        _seed_pidfile(bobi_install.repo_path, os.getpid())
        (paths.state_path(bobi_install.repo_path)
         / "manager-health.port").write_text("not-a-port")
        body = _state(bobi_install)
        assert body["state"] == RUNNING
        assert body["probe_ok"] is None

    def test_dead_pid_is_stopped_even_with_a_port_file(self, bobi_install,
                                                       live_health_server):
        # The port file outlives an unclean exit. A stale one must not
        # resurrect a stopped agent — liveness is decided before the probe.
        _seed_pidfile(bobi_install.repo_path, 999999999)
        assert _state(bobi_install)["state"] == STOPPED


# === Strip telemetry ===

class TestRunningSegments:
    def test_full_set(self, bobi_install):
        _seed_pidfile(bobi_install.repo_path, os.getpid())
        _seed_session(bobi_install.sessions_dir, MANAGER, status="idle",
                      role="director", started_at=NOW - 7200)
        _seed_session(bobi_install.sessions_dir, "eng", status="running",
                      last_activity=NOW - 30)
        segs = _segments(_state(bobi_install))
        assert segs["uptime"]["value"] == 7200.0
        assert segs["uptime"]["kind"] == "duration"
        assert segs["manager_pid"]["value"] == str(os.getpid())
        assert segs["live_runs"]["value"] == 1        # the manager is not a run
        assert segs["last_activity"]["kind"] == "time"
        assert [s["key"] for s in _state(bobi_install)["segments"]] == [
            "uptime", "manager_pid", "live_runs", "last_activity"]

    def test_uptime_omitted_in_the_boot_window(self, bobi_install):
        # Manager pid alive, no registry entry yet: no start time to report,
        # so the segment is absent rather than 0 or "unknown".
        _seed_pidfile(bobi_install.repo_path, os.getpid())
        assert "uptime" not in _segments(_state(bobi_install))

    def test_live_runs_counts_only_active_subagents(self, bobi_install):
        _seed_pidfile(bobi_install.repo_path, os.getpid())
        _seed_session(bobi_install.sessions_dir, MANAGER, status="idle",
                      role="director")
        _seed_session(bobi_install.sessions_dir, "done-one", status="completed",
                      terminal_at=NOW)
        _seed_session(bobi_install.sessions_dir, "eng", status="running")
        assert _segments(_state(bobi_install))["live_runs"]["value"] == 1


class TestStoppedSegments:
    def test_from_the_last_terminal_record(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, MANAGER, status="completed",
                      role="director", started_at=NOW - 86400,
                      terminal_at=NOW - 600)
        segs = _segments(_state(bobi_install))
        assert segs["since"]["value"] == NOW - 600
        assert segs["exit"]["value"] == "clean"
        assert segs["was_up"]["value"] == 86400 - 600

    def test_unclean_exit_reports_its_error(self, bobi_install):
        _seed_session(bobi_install.sessions_dir, MANAGER, status="crashed",
                      role="director", terminal_at=NOW,
                      error="killed by signal 9")
        assert _segments(_state(bobi_install))["exit"]["value"] == \
            "killed by signal 9"

    def test_terminal_status_without_an_error_still_names_itself(
            self, bobi_install):
        _seed_session(bobi_install.sessions_dir, MANAGER, status="crashed",
                      role="director", terminal_at=NOW)
        assert _segments(_state(bobi_install))["exit"]["value"] == "crashed"

    def test_a_gracefully_stopped_manager_reports_all_three(self,
                                                            bobi_install):
        # The shape the REAL stop path writes: `Session._run`'s teardown ends
        # with status="stopped", not "completed". Every test above seeded
        # "completed", so the whole stopped strip rendered empty in practice
        # while the suite stayed green — found by stopping a live agent from
        # the page and watching SINCE/EXIT/WAS UP not appear (#887 QA).
        _seed_session(bobi_install.sessions_dir, MANAGER, status="stopped",
                      role="director", started_at=NOW - 3600,
                      terminal_at=NOW - 30)
        segs = _segments(_state(bobi_install))
        assert segs["since"]["value"] == NOW - 30
        assert segs["exit"]["value"] == "clean"
        assert segs["was_up"]["value"] == 3600 - 30

    def test_unclosed_record_reports_no_exit(self, bobi_install):
        # The manager is gone but its record was never closed, so it still
        # reads `idle`. "Exit: idle" would be a live-sounding word for a dead
        # process — the segment is dropped instead. (Caught in QA, not by a
        # unit test: the seeded home's record had pid 0, so nothing reaped it.)
        _seed_session(bobi_install.sessions_dir, MANAGER, status="idle",
                      role="director", started_at=NOW - 7200)
        segs = _segments(_state(bobi_install))
        assert "exit" not in segs
        assert "since" not in segs        # never terminal, so no stop time
        assert "was_up" not in segs

    def test_never_run_agent_has_no_segments(self, bobi_install):
        # A fresh agent's strip says STOPPED and nothing else. That is the
        # whole truth about it — inventing SINCE/EXIT/WAS UP would not be.
        body = _state(bobi_install)
        assert body["state"] == STOPPED
        assert body["segments"] == []


class TestNotRespondingSegments:
    def test_carries_the_diagnosis(self, bobi_install, closed_port):
        _seed_pidfile(bobi_install.repo_path, os.getpid())
        _seed_port_file(bobi_install.repo_path, closed_port)
        _seed_session(bobi_install.sessions_dir, MANAGER, status="running",
                      role="director", last_activity=NOW - 720)
        segs = _segments(_state(bobi_install))
        assert segs["manager_pid"]["note"] == "alive"
        assert str(closed_port) in segs["health_probe"]["value"]
        assert segs["last_activity"]["value"] == NOW - 720

    def test_no_invented_failure_duration(self, bobi_install, closed_port):
        # The prototype's "failing 12m" needs a memory this process does not
        # have. LAST ACTIVITY carries the time signal instead.
        _seed_pidfile(bobi_install.repo_path, os.getpid())
        _seed_port_file(bobi_install.repo_path, closed_port)
        probe_seg = _segments(_state(bobi_install))["health_probe"]
        assert probe_seg["kind"] == "text"


# === Normalization for runtimes that predate the state keys ===

class TestNormalize:
    def test_fills_the_state_keys_from_a_legacy_payload(self):
        body = health.normalize({"manager": {"running": True}, "sessions": []})
        assert body["state"] == RUNNING
        assert body["detail"] == ""
        assert body["segments"] == []

    def test_stopped_when_the_legacy_manager_is_not_running(self):
        assert health.normalize({"manager": {"running": False}})["state"] == \
            STOPPED

    def test_never_overwrites_a_runtime_that_reports_state(self):
        body = health.normalize({"state": NOT_RESPONDING, "detail": "wedged",
                                 "segments": [{"key": "x"}],
                                 "manager": {"running": True}})
        assert body["state"] == NOT_RESPONDING
        assert body["detail"] == "wedged"
        assert body["segments"] == [{"key": "x"}]

    def test_survives_an_empty_payload(self):
        assert health.normalize({})["state"] == STOPPED


# === The endpoint ===

def _client():
    app = server.build_app(token=TOKEN)
    c = TestClient(app, base_url="http://127.0.0.1")
    c.headers.update({"x-bobi-webui-token": TOKEN})
    return c


class TestHealthEndpoint:
    def _get(self, bobi_install):
        r = _client().get(f"/api/agents/{bobi_install.agent_name}/health")
        assert r.status_code == 200
        return r.json()

    def test_stopped_agent_serves_the_state_keys(self, bobi_install):
        body = self._get(bobi_install)
        assert body["state"] == STOPPED
        assert body["detail"]
        assert body["segments"] == []
        # The pre-existing shape is untouched.
        assert body["reachability"] == "live"
        assert body["manager"]["running"] is False
        assert body["lifecycle"] == []

    def test_running_agent_reports_running_and_healthy(self, bobi_install,
                                                       live_health_server):
        _seed_pidfile(bobi_install.repo_path, os.getpid())
        _seed_session(bobi_install.sessions_dir, MANAGER, status="idle",
                      role="director")
        body = self._get(bobi_install)
        assert body["state"] == RUNNING
        assert body["manager"]["healthy"] is True
        assert body["manager"]["idle_seconds"] == 42.0

    def test_wedged_manager_is_not_healthy(self, bobi_install, closed_port):
        # The whole point of U3: pid alive, and the payload says so, but
        # `healthy` is False and the state names the problem.
        _seed_pidfile(bobi_install.repo_path, os.getpid())
        _seed_port_file(bobi_install.repo_path, closed_port)
        _seed_session(bobi_install.sessions_dir, MANAGER, status="running",
                      role="director")
        body = self._get(bobi_install)
        assert body["state"] == NOT_RESPONDING
        assert body["manager"]["running"] is True
        assert body["manager"]["pid"] == os.getpid()
        assert body["manager"]["healthy"] is False

    def test_manager_without_a_health_endpoint_stays_healthy(self,
                                                             bobi_install):
        _seed_pidfile(bobi_install.repo_path, os.getpid())
        _seed_session(bobi_install.sessions_dir, MANAGER, status="idle",
                      role="director")
        body = self._get(bobi_install)
        assert body["state"] == RUNNING
        assert body["manager"]["healthy"] is True

    def test_segments_are_json_safe(self, bobi_install):
        _seed_pidfile(bobi_install.repo_path, os.getpid())
        _seed_session(bobi_install.sessions_dir, MANAGER, status="idle",
                      role="director", started_at=time.time() - 60)
        for seg in self._get(bobi_install)["segments"]:
            assert set(seg) == {"key", "label", "kind", "value", "note"}
            assert seg["kind"] in ("duration", "time", "count", "text")

    def test_unknown_agent_404s(self, bobi_install):
        assert _client().get("/api/agents/nope/health").status_code == 404
