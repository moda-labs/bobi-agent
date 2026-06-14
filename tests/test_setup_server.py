"""Tests for the bobbi setup web server — security, serialization, and the
deterministic + streaming endpoints. Driven by Starlette's TestClient with
an injected fake LLM source: no network, no CLI."""

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from modastack.setup import services
from modastack.setup.state import SetupState, Stage
from modastack.setup.webui import server

NONCE = "test-nonce-123"


def _fake_digest(reply, **payload):
    sentinel = "===BOBBI-SPEC==="

    async def fn(*, system_prompt, user_prompt, model, cwd):
        yield reply + "\n" + sentinel + "\n" + json.dumps(payload)
    return fn


def _fake_author():
    async def fn(*, system_prompt, user_prompt, model, cwd):
        yield "# Generated\n\nYou carry out the goal.\n"
    return fn


def _testclient(app):
    # TestClient defaults to host "testserver"; the server's Host guard only
    # allows loopback, so pin the base URL to 127.0.0.1.
    return TestClient(app, base_url="http://127.0.0.1")


def _client(state, project, **kw):
    app = server.build_app(state, project, nonce=NONCE, **kw)
    c = _testclient(app)
    c.headers.update({server.NONCE_HEADER: NONCE})
    return c


@pytest.fixture
def project(tmp_path):
    return tmp_path


# --- security ------------------------------------------------------------

class TestSecurity:
    def test_api_requires_nonce(self, project):
        app = server.build_app(SetupState(), project, nonce=NONCE)
        c = _testclient(app)
        assert c.get("/api/state").status_code == 403
        c.headers.update({server.NONCE_HEADER: "wrong"})
        assert c.get("/api/state").status_code == 403
        c.headers.update({server.NONCE_HEADER: NONCE})
        assert c.get("/api/state").status_code == 200

    def test_page_does_not_require_nonce_and_embeds_it(self, project):
        app = server.build_app(SetupState(), project, nonce=NONCE)
        c = _testclient(app)
        r = c.get("/")
        assert r.status_code == 200
        assert NONCE in r.text
        assert "{{NONCE}}" not in r.text

    def test_foreign_host_rejected(self, project):
        app = server.build_app(SetupState(), project, nonce=NONCE)
        c = _testclient(app)
        r = c.get("/", headers={"host": "evil.example.com"})
        assert r.status_code == 403


# --- serialize_state -----------------------------------------------------

class TestSerializeState:
    def test_exposes_stage_spec_and_blocker(self, project):
        s = SetupState(stage=Stage.CHAT)           # goal empty → build blocked
        data = server.serialize_state(s)
        assert data["stage"] == "chat"
        assert data["stages"][0] == "start"
        assert "chat" in data["stages"]
        assert data["chat"] == ""
        assert set(data["spec"]["readiness"]) == {
            "goal", "roles", "autonomous", "services"}
        assert "goal" in data["advance_blocker"]

    def test_no_blocker_once_goal_set(self, project):
        s = SetupState(stage=Stage.CHAT)
        s.spec.goal = "Do the thing."
        assert server.serialize_state(s)["advance_blocker"] is None


# --- conversation turn ---------------------------------------------------

class TestMessageEndpoint:
    def test_streams_reply_and_routes_spec(self, project):
        state = SetupState(team_name="t")
        fn = _fake_digest("A triage bot, got it.",
                          deltas={"goal": "Triage issues."},
                          summary="triage", readiness={"goal": "enough"})
        c = _client(state, project, stream_fn=fn)
        r = c.post("/api/message", json={"text": "build a triage bot"})
        assert r.status_code == 200
        body = r.text
        assert "A triage bot, got it." in body
        assert "event: state" in body
        # routed + persisted
        assert state.spec.goal == "Triage issues."
        assert SetupState.load(project).spec.goal == "Triage issues."

    def test_empty_message_errors_in_stream(self, project):
        c = _client(SetupState(team_name="t"), project)
        r = c.post("/api/message", json={"text": "  "})
        assert "event: error" in r.text

    def test_pasted_secret_is_redacted_and_announced(self, project):
        state = SetupState(team_name="t")
        fn = _fake_digest("noted.", summary="s")
        c = _client(state, project, stream_fn=fn)
        secret = "ghp_" + "a" * 36
        r = c.post("/api/message", json={"text": f"my github token {secret}"})
        assert "event: redacted" in r.text
        assert secret not in r.text          # never echoed back
        assert secret not in state.messages[0]["content"]
        assert "[redacted]" in state.messages[0]["content"]


# --- advance -------------------------------------------------------------

class TestAdvance:
    def test_blocks_build_without_goal(self, project):
        c = _client(SetupState(stage=Stage.CONNECT), project)
        r = c.post("/api/advance", json={"to": "build"})
        assert r.status_code == 409
        assert "goal" in r.json()["error"]

    def test_advances_when_clear(self, project):
        s = SetupState(stage=Stage.START)
        c = _client(s, project)
        r = c.post("/api/advance", json={"to": "design"})
        assert r.status_code == 200
        assert r.json()["stage"] == "design"
        assert SetupState.load(project).stage == Stage.DESIGN

    def test_unknown_stage_400(self, project):
        c = _client(SetupState(), project)
        assert c.post("/api/advance", json={"to": "nope"}).status_code == 400


# --- connect + credential ------------------------------------------------

class TestConnect:
    def test_cards_reflect_spec_services(self, project, monkeypatch):
        monkeypatch.setattr(services, "venn_connected_names",
                            lambda *a, **k: None)
        s = SetupState()
        s.spec.services = [{"name": "github"}, {"name": "salesforce"}]
        c = _client(s, project)
        data = c.get("/api/connect").json()
        keys = {card["key"] for card in data["cards"]}
        assert keys == {"github", "crm"}
        assert any(card["key"] == "slack" for card in data["catalog"])

    def test_credential_saved_to_env(self, project, monkeypatch):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        c = _client(SetupState(), project)
        r = c.post("/api/credential", json={
            "var_name": "SLACK_BOT_TOKEN", "service": "slack",
            "value": "xoxb-secret-value-1234"})
        assert r.status_code == 200
        assert r.json()["saved"] is True
        env = (project / ".modastack" / ".env").read_text()
        assert "SLACK_BOT_TOKEN=xoxb-secret-value-1234" in env
        # the secret never appears in the response
        assert "xoxb-secret-value-1234" not in r.text

    def test_bad_credential_name_400(self, project):
        c = _client(SetupState(), project)
        r = c.post("/api/credential", json={"var_name": "bad-name",
                                            "value": "x"})
        assert r.status_code == 400


# --- build + validate + install ------------------------------------------

class TestBuildInstall:
    def _ready_state(self):
        s = SetupState(team_name="triage-bot")
        s.spec.goal = "Triage incoming issues and route them."
        s.spec.roles = [{"name": "lead", "responsibility": "classify"}]
        s.spec.services = [{"name": "github"}]
        return s

    def test_build_pour_then_validate_then_install(self, project):
        state = self._ready_state()
        c = _client(state, project, stream_fn=_fake_author())

        r = c.post("/api/build")
        assert r.status_code == 200
        assert "event: file_start" in r.text
        assert "event: state" in r.text
        assert (project / "agents" / "triage-bot" / "agent.yaml").exists()

        v = c.post("/api/validate").json()
        assert v["passed"] is True

        i = c.post("/api/install").json()
        assert i["installed"] == "triage-bot"
        assert (project / ".modastack" / "agent.yaml").exists()

    def test_install_blocked_when_unvalidated(self, project):
        state = self._ready_state()
        c = _client(state, project, stream_fn=_fake_author())
        c.post("/api/build")
        # skip validate → install must refuse (stale/again)
        r = c.post("/api/install")
        assert r.status_code == 409
        assert "validate" in r.json()["error"]


# --- automate ------------------------------------------------------------

class TestAutomate:
    def test_suggest_returns_ideas(self, project):
        async def fake(*, system_prompt, user_prompt, model, cwd):
            yield json.dumps([{"description": "Flag stale PRs",
                               "leash": "notify", "cadence": "1d"}])
        s = SetupState()
        s.spec.goal = "Ship PRs."
        c = _client(s, project, stream_fn=fake)
        data = c.post("/api/automate/suggest").json()
        assert data["suggestions"][0]["description"] == "Flag stale PRs"

    def test_commit_sets_autonomous_and_confirms(self, project):
        s = SetupState()
        c = _client(s, project)
        r = c.post("/api/automate", json={"behaviors": [
            {"description": "daily digest", "leash": "act", "cadence": "1d"}]})
        assert r.status_code == 200
        assert s.spec.autonomous_confirmed is True
        assert s.spec.autonomous[0]["description"] == "daily digest"
        assert r.json()["spec"]["readiness"]["autonomous"] == "enough"

    def test_commit_empty_is_a_real_decision(self, project):
        s = SetupState()
        c = _client(s, project)
        c.post("/api/automate", json={"behaviors": []})
        assert s.spec.autonomous == []
        assert s.spec.autonomous_confirmed is True


# --- chat (how you talk to the team) -------------------------------------

class TestChat:
    def test_set_channel(self, project):
        s = SetupState()
        c = _client(s, project)
        r = c.post("/api/chat", json={"channel": "slack"})
        assert r.status_code == 200
        assert s.chat == "slack"
        assert r.json()["chat"] == "slack"
        assert SetupState.load(project).chat == "slack"

    def test_rejects_unknown_channel(self, project):
        c = _client(SetupState(), project)
        assert c.post("/api/chat", json={"channel": "smoke"}).status_code == 400


# --- review file endpoints -----------------------------------------------

class TestReviewFiles:
    def _built(self, project):
        s = SetupState(team_name="triage-bot")
        s.spec.goal = "Triage incoming issues."
        s.spec.roles = [{"name": "lead", "responsibility": "classify"}]
        c = _client(s, project, stream_fn=_fake_author())
        c.post("/api/build")
        return s, c

    def test_list_and_read(self, project):
        _, c = self._built(project)
        files = c.get("/api/files").json()["files"]
        assert "agent.yaml" in files
        assert "roles/lead/ROLE.md" in files
        content = c.get("/api/file", params={"path": "agent.yaml"}).json()
        assert "entry_point" in content["content"]

    def test_read_rejects_traversal(self, project):
        _, c = self._built(project)
        r = c.get("/api/file", params={"path": "../../../etc/passwd"})
        assert r.status_code == 404

    def test_write_invalidates_validation(self, project):
        s, c = self._built(project)
        c.post("/api/validate")
        assert s.validated is True
        r = c.post("/api/file", json={"path": "agent.md",
                                      "content": "# edited\n"})
        assert r.status_code == 200
        assert s.validated is False
        assert (project / "agents" / "triage-bot" / "agent.md").read_text() \
            == "# edited\n"

    def test_write_rejects_escape(self, project):
        _, c = self._built(project)
        r = c.post("/api/file", json={"path": "../escape.txt", "content": "x"})
        assert r.status_code == 400


# --- finish --------------------------------------------------------------

class TestFinish:
    def test_finish_sets_flag_and_calls_hook(self, project):
        called = {}
        s = SetupState(stage=Stage.DONE)
        app = server.build_app(s, project, nonce=NONCE,
                               on_finish=lambda: called.setdefault("done", True))
        c = _testclient(app)
        c.headers.update({server.NONCE_HEADER: NONCE})
        r = c.post("/api/finish")
        assert r.json()["finished"] is True
        assert called.get("done") is True
        assert s.finished is True
