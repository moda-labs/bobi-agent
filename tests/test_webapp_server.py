"""Tests for the unified web app server — security, the dashboard snapshot,
and the per-agent lifecycle endpoints. Service-core calls are monkeypatched;
the dashboard reads a real (temp) BOBI_HOME via the bobi_install fixture."""

import yaml
from fastapi.testclient import TestClient

from bobi import service
from bobi.webapp import server

TOKEN = "test-token-123"


def _testclient():
    # The Host guard only allows loopback; TestClient defaults to "testserver".
    app = server.build_app(token=TOKEN)
    return TestClient(app, base_url="http://127.0.0.1")


def _client():
    c = _testclient()
    c.headers.update({"x-bobi-webui-token": TOKEN})
    return c


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


# --- lifecycle actions -----------------------------------------------------

class _FakeStartup:
    pid = 4242


class _FakeSpawn:
    startup = _FakeStartup()


# --- hosted onboarding (the setup app mounted under /setup) ----------------

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
        assert r.json() == {"url": "/setup/", "name": "new-team",
                            "resumed": False}
        cur = c.get("/api/setup/current").json()
        assert cur == {"active": True, "name": "new-team"}
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

    def test_hosted_page_serves_with_base_and_token(self, bobi_install,
                                                    monkeypatch):
        c = _client()
        self._open(c, monkeypatch)
        r = c.get("/setup/")
        assert r.status_code == 200
        assert TOKEN in r.text
        assert '"/setup/static/app.js"' in r.text

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
        assert bare.get("/setup/api/state").status_code == 403
        assert c.get("/setup/api/state").status_code == 200

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
        body = c.post("/setup/api/finish").json()
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
        assert r.json()["url"].startswith("/setup/?open=")
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
        r = c.post("/setup/api/rename", json={"name": "eng-team"})
        assert r.status_code == 200
        body = c.post("/setup/api/finish").json()
        assert body["redirect"] == "/#/"
        assert not paths.agent_dir("new-team").exists()
        assert paths.agent_run_root("eng-team").is_dir()

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
