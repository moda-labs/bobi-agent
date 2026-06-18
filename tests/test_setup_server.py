"""Tests for the modastack setup web server — security, serialization, and the
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
    sentinel = "===MODASTACK-SPEC==="

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


@pytest.fixture(autouse=True)
def _isolate_environ():
    """Saving a credential writes the secret into ``os.environ`` (actions.py
    ``save_credential`` does ``os.environ[var] = value``) so the live setup
    process can use it immediately. ``monkeypatch`` can't undo that direct app
    write, so without isolation a saved ``VENN_API_KEY``/token bleeds into later
    tests and changes their build/author behavior. Snapshot and restore the
    environment around every test in this module."""
    import os
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def project(tmp_path):
    return tmp_path


@pytest.fixture
def home(tmp_path):
    """A stand-in for the user's home, so the ~/modastack-agents library and the
    folder picker stay off the real filesystem. Pass home_root=home to _client."""
    h = tmp_path / "home"
    h.mkdir()
    return h


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

    def test_ping_is_alive_and_nonce_guarded(self, project):
        # The heartbeat's liveness endpoint: 200 {ok:true} with the nonce,
        # 403 without it (it rides the same /api guard as everything else).
        app = server.build_app(SetupState(), project, nonce=NONCE)
        c = _testclient(app)
        assert c.get("/api/ping").status_code == 403   # no nonce
        c.headers.update({server.NONCE_HEADER: NONCE})
        r = c.get("/api/ping")
        assert r.status_code == 200 and r.json() == {"ok": True}


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
        assert keys == {"github", "salesforce"}   # concrete name, not "crm"
        assert any(card["key"] == "slack" for card in data["catalog"])

    def test_reports_venn_configured(self, project, monkeypatch):
        monkeypatch.setattr(services, "venn_connected_names", lambda *a, **k: None)
        monkeypatch.delenv("VENN_API_KEY", raising=False)
        c = _client(SetupState(), project)
        assert c.get("/api/connect").json()["venn_configured"] is False
        c.post("/api/credential", json={"var_name": "VENN_API_KEY",
                                        "value": "venn_key_123"})
        assert c.get("/api/connect").json()["venn_configured"] is True

    def test_hosted_mcp_surfaces_as_an_mcp_card(self, project, monkeypatch):
        # A service Venn doesn't cover but that ships a hosted MCP resolves to an
        # mcp card (wired into mcp_servers), not custom.
        monkeypatch.setattr(services, "venn_connected_names",
                            lambda *a, **k: None)
        monkeypatch.delenv("VENN_API_KEY", raising=False)
        s = SetupState()
        s.spec.services = [{"name": "stripe"}]
        c = _client(s, project)
        card = c.get("/api/connect").json()["cards"][0]
        assert card["key"] == "stripe"
        assert card["kind"] == "mcp"
        assert card["via"] == "hosted MCP"

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

    def test_credential_value_for_copy(self, project, monkeypatch):
        # Copy-to-clipboard support: the value is retrievable on loopback so the
        # page can copy it without rendering it.
        monkeypatch.delenv("LINEAR_API_KEY", raising=False)
        c = _client(SetupState(), project)
        c.post("/api/credential", json={"var_name": "LINEAR_API_KEY",
                                        "value": "lin_api_copyme"})
        r = c.get("/api/credential/value?var=LINEAR_API_KEY")
        assert r.status_code == 200
        assert r.json()["value"] == "lin_api_copyme"
        assert c.get("/api/credential/value?var=NOPE_TOKEN").status_code == 404


# --- Venn setup flow: discover account MCPs, apply picks -----------------

class TestVennSetup:
    def _verified(self, monkeypatch, names):
        """Stub list_servers_verified to return these available service names."""
        import modastack.venn as venn_mod

        class _S:
            def __init__(self, name):
                self.server_id = self.server_name = name
                self.connected = True
        monkeypatch.setattr(venn_mod, "list_servers_verified",
                            lambda key: [_S(n) for n in names])

    def _verified_raises(self, monkeypatch):
        import modastack.venn as venn_mod
        def boom(key):
            raise venn_mod.VennError("Venn rejected the API key (unauthorized).")
        monkeypatch.setattr(venn_mod, "list_servers_verified", boom)

    def test_servers_needs_a_key(self, project, monkeypatch):
        monkeypatch.delenv("VENN_API_KEY", raising=False)
        c = _client(SetupState(), project)
        data = c.get("/api/venn/servers").json()
        assert data["ok"] is False and "key" in data["error"].lower()

    def test_servers_lists_available_names(self, project, monkeypatch):
        monkeypatch.setenv("VENN_API_KEY", "k")
        self._verified(monkeypatch, ["gmail", "salesforce", "notion"])
        c = _client(SetupState(), project)
        data = c.get("/api/venn/servers").json()
        assert data["ok"] is True
        assert data["servers"] == ["gmail", "notion", "salesforce"]   # sorted names

    def test_servers_bad_key_is_an_error_state(self, project, monkeypatch):
        monkeypatch.setenv("VENN_API_KEY", "bad")
        self._verified_raises(monkeypatch)
        c = _client(SetupState(), project)
        data = c.get("/api/venn/servers").json()
        assert data["ok"] is False and "unauthorized" in data["error"].lower()

    def test_connect_saves_only_on_success(self, project, monkeypatch):
        monkeypatch.delenv("VENN_API_KEY", raising=False)
        self._verified(monkeypatch, ["gmail", "slack"])
        c = _client(SetupState(), project)
        data = c.post("/api/venn/connect", json={"key": "venn_good"}).json()
        assert data["ok"] is True and "gmail" in data["servers"]
        # the verified key is now persisted
        env = (project / ".modastack" / ".env").read_text()
        assert "VENN_API_KEY=venn_good" in env

    def test_connect_bad_key_not_saved(self, project, monkeypatch):
        monkeypatch.delenv("VENN_API_KEY", raising=False)
        self._verified_raises(monkeypatch)
        c = _client(SetupState(), project)
        data = c.post("/api/venn/connect", json={"key": "venn_bad"}).json()
        assert data["ok"] is False
        envf = project / ".modastack" / ".env"
        assert not envf.exists() or "VENN_API_KEY" not in envf.read_text()

    def test_apply_reconciles_toggles(self, project):
        # gmail toggled on (added), salesforce in the universe but off (removed),
        # github untouched (outside the picker universe).
        s = SetupState()
        s.spec.services = [{"name": "github"}, {"name": "salesforce"}]
        c = _client(s, project)
        r = c.post("/api/venn/apply", json={
            "servers": ["gmail"],
            "available": ["gmail", "salesforce", "slack"]}).json()
        assert r["added"] == ["gmail"] and r["removed"] == ["salesforce"]
        assert [x["name"] for x in s.spec.services] == ["github", "gmail"]

    def test_apply_turning_all_off_removes_them(self, project):
        s = SetupState()
        s.spec.services = [{"name": "gmail"}, {"name": "slack"}]
        c = _client(s, project)
        r = c.post("/api/venn/apply", json={
            "servers": [], "available": ["gmail", "slack"]}).json()
        assert set(r["removed"]) == {"gmail", "slack"}
        assert s.spec.services == []

    def test_apply_requires_a_list(self, project):
        c = _client(SetupState(), project)
        assert c.post("/api/venn/apply", json={"servers": "gmail"}).status_code == 400


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


# --- panel edits: role / automation / connection -------------------------

class TestPanelEdits:
    def _with_role(self):
        s = SetupState()
        s.spec.roles = [{"name": "lead", "responsibility": "classify"}]
        return s

    def test_role_update_patches_fields_and_marks_complete(self, project):
        s = self._with_role()
        c = _client(s, project)
        r = c.post("/api/role/update", json={"index": 0, "fields": {
            "responsibility": "triage issues",
            "good_looks_like": "fast accurate triage",
            "systems": "github, slack", "triggers": "on new issue"}})
        assert r.status_code == 200
        role = s.spec.roles[0]
        assert role["systems"] == ["github", "slack"]
        assert role["status"] == "complete"          # all four dimensions filled
        assert r.json()["spec"]["readiness"]["roles"] == "enough"
        assert s.validated is False

    def test_role_update_partial_stays_in_progress(self, project):
        s = self._with_role()
        c = _client(s, project)
        c.post("/api/role/update", json={"index": 0,
                                         "fields": {"good_looks_like": "x"}})
        assert s.spec.roles[0]["status"] == "in_progress"

    def test_role_update_bad_index_400(self, project):
        c = _client(self._with_role(), project)
        assert c.post("/api/role/update",
                      json={"index": 9, "fields": {}}).status_code == 400

    def test_automation_update_patches_role_and_command(self, project):
        s = SetupState()
        s.spec.autonomous = [{"description": "digest", "leash": "notify"}]
        c = _client(s, project)
        r = c.post("/api/automation/update", json={"index": 0, "fields": {
            "role": "lead", "command": "summarize", "cadence": "1d",
            "leash": "act"}})
        assert r.status_code == 200
        item = s.spec.autonomous[0]
        assert item["role"] == "lead" and item["command"] == "summarize"
        assert item["leash"] == "act" and item["cadence"] == "1d"
        assert s.spec.autonomous_confirmed is True

    def test_service_remove_drops_by_key(self, project, monkeypatch):
        monkeypatch.setattr(services, "venn_connected_names", lambda *a, **k: None)
        s = SetupState()
        s.spec.services = [{"name": "github"}, {"name": "linear"}]
        c = _client(s, project)
        r = c.post("/api/service/remove", json={"service_key": "github"})
        assert r.status_code == 200
        names = [x["name"] for x in s.spec.services]
        assert names == ["linear"]
        assert s.validated is False

    def test_service_remove_requires_key(self, project):
        c = _client(SetupState(), project)
        assert c.post("/api/service/remove", json={}).status_code == 400

    def test_mcp_add_api_key_connection(self, project, monkeypatch):
        monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
        s = SetupState()
        c = _client(s, project)
        r = c.post("/api/mcp/add", json={
            "name": "PostHog", "url": "https://mcp.posthog.com/mcp",
            "auth": "api_key", "api_key": "ph_secret_123"})
        assert r.status_code == 200 and r.json()["ok"] is True
        # persisted as a user-defined MCP (keyed by slug, label kept)
        entry = s.spec.mcp_servers["posthog"]
        assert entry["url"] == "https://mcp.posthog.com/mcp"
        assert entry["auth"] == "api_key" and entry["secret_var"] == "POSTHOG_API_KEY"
        assert entry["label"] == "PostHog"
        # also a team service, so it renders as a row
        assert any((x.get("name") or "").lower() == "posthog" for x in s.spec.services)
        # the key landed in .env, never in the response
        assert "ph_secret_123" not in r.text
        assert "POSTHOG_API_KEY=ph_secret_123" in (project / ".modastack" / ".env").read_text()

    def test_mcp_add_oauth_flags_pending(self, project, monkeypatch):
        monkeypatch.delenv("ACME_OAUTH_CLIENT_SECRET", raising=False)
        s = SetupState()
        c = _client(s, project)
        r = c.post("/api/mcp/add", json={
            "name": "Acme", "url": "https://mcp.acme.com/mcp", "auth": "oauth",
            "client_id": "cid_1", "client_secret": "csecret_1"}).json()
        assert r["ok"] is True and r["oauth_pending"] is True
        entry = s.spec.mcp_servers["acme"]
        assert entry["client_id_var"] == "ACME_OAUTH_CLIENT_ID"
        env = (project / ".modastack" / ".env").read_text()
        assert "ACME_OAUTH_CLIENT_ID=cid_1" in env
        assert "ACME_OAUTH_CLIENT_SECRET=csecret_1" in env

    def test_mcp_add_requires_name_and_url(self, project):
        c = _client(SetupState(), project)
        assert c.post("/api/mcp/add", json={"url": "https://x/mcp"}).status_code == 400
        assert c.post("/api/mcp/add", json={"name": "X"}).status_code == 400
        # non-http URL rejected
        assert c.post("/api/mcp/add",
                      json={"name": "X", "url": "ftp://x"}).status_code == 400

    def test_connect_surfaces_user_mcp_card(self, project, monkeypatch):
        monkeypatch.setattr(services, "venn_connected_names", lambda *a, **k: None)
        s = SetupState()
        c = _client(s, project)
        c.post("/api/mcp/add", json={
            "name": "PostHog", "url": "https://mcp.posthog.com/mcp",
            "auth": "api_key", "api_key": "ph_x"})
        cards = c.get("/api/connect").json()["cards"]
        ph = next(c for c in cards if c["key"] == "posthog")
        assert ph["kind"] == "mcp" and ph["name"] == "PostHog"
        # NOT verified in setup, so never "connected" — "added" (key set) at most.
        assert ph["via"] == "hosted MCP" and ph["status"] == "added"
        assert ph["user_mcp"] is True

    def test_connect_user_mcp_without_auth_flags_needs_auth(self, project, monkeypatch):
        monkeypatch.setattr(services, "venn_connected_names", lambda *a, **k: None)
        s = SetupState()
        c = _client(s, project)
        c.post("/api/mcp/add", json={"name": "Acme", "url": "https://mcp.acme.com/mcp"})
        ph = next(x for x in c.get("/api/connect").json()["cards"] if x["key"] == "acme")
        assert ph["status"] == "needs_auth"   # no creds given → not "connected"


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

    def test_reveal_opens_the_source_folder(self, project, monkeypatch):
        import subprocess
        calls = []
        monkeypatch.setattr(subprocess, "Popen",
                            lambda argv, *a, **k: calls.append(argv))
        _, c = self._built(project)
        r = c.post("/api/reveal")
        assert r.status_code == 200 and r.json()["ok"] is True
        # launched the OS file manager on the team's source dir
        assert calls and str(project / "agents" / "triage-bot") in calls[0]

    def test_reveal_404_when_no_source_yet(self, project, monkeypatch):
        import subprocess
        monkeypatch.setattr(subprocess, "Popen",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))
        c = _client(SetupState(team_name="ghost"), project)
        assert c.post("/api/reveal").status_code == 404

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
    def test_finish_sets_flag_and_keeps_server_alive(self, project):
        # Finish marks the session complete but does NOT stop the server — the
        # page transitions to the team hub, a re-entrant editor.
        s = SetupState(stage=Stage.DONE)
        c = _client(s, project)
        r = c.post("/api/finish")
        assert r.json()["finished"] is True
        assert s.finished is True


class TestHome:
    def test_lists_library_teams_with_descriptions(self, project, home):
        _seed_library_team(home, "legacy-bot")
        c = _client(SetupState(), project, home_root=home)
        r = c.get("/api/home")
        assert r.status_code == 200
        teams = r.json()["teams"]
        assert any(t["name"] == "legacy-bot" for t in teams)
        bot = next(t for t in teams if t["name"] == "legacy-bot")
        # description comes from the team's agent.md first paragraph
        assert "triage issues" in bot["description"].lower()

    def test_empty_library_lists_nothing(self, project, home):
        c = _client(SetupState(), project, home_root=home)
        assert c.get("/api/home").json()["teams"] == []


class TestRunStart:
    def test_spawns_modastack_start_in_project(self, project, monkeypatch):
        calls = {}

        def fake_popen(args, **kw):
            calls["args"] = args
            calls["cwd"] = kw.get("cwd")
            return object()

        monkeypatch.setattr("subprocess.Popen", fake_popen)
        c = _client(SetupState(stage=Stage.DONE), project)
        r = c.post("/api/run-start")
        assert r.status_code == 200 and r.json()["ok"] is True
        assert calls["args"][1:] == ["-m", "modastack", "start"]
        assert calls["cwd"] == str(project)

    def test_reports_error_when_spawn_fails(self, project, monkeypatch):
        def boom(*a, **k):
            raise OSError("no exec")

        monkeypatch.setattr("subprocess.Popen", boom)
        c = _client(SetupState(stage=Stage.DONE), project)
        r = c.post("/api/run-start")
        assert r.status_code == 500
        assert "no exec" in r.json()["error"]


# --- intro: create / open + location -------------------------------------

def _seed_team(project, name="legacy-bot", *, parent="agents"):
    """Write a minimal valid team source under <project>/<parent>/<name>/."""
    src = project / parent / name
    (src / "roles" / "lead").mkdir(parents=True)
    (src / "agent.yaml").write_text(
        "agent: " + name + "\nversion: 0.1.0\nentry_point: lead\n"
        "services:\n  - name: github\n    events: true\nchat: slack\n")
    (src / "agent.md").write_text("# " + name + "\n\nWatch the repo and triage issues.\n")
    (src / "roles" / "lead" / "ROLE.md").write_text("# Lead\n\nClassify and route issues.\n")
    return src


def _seed_library_team(home, name="legacy-bot"):
    """Write a minimal valid team source into the ~/modastack-agents library."""
    return _seed_team(home / "modastack-agents", name, parent=".")


class TestListTeamsIn:
    def test_missing_directory_is_empty(self, tmp_path):
        from modastack.setup import open_mode
        assert open_mode.list_teams_in(tmp_path / "nope") == []

    def test_a_path_thats_a_file_is_empty(self, tmp_path):
        from modastack.setup import open_mode
        f = tmp_path / "f.txt"
        f.write_text("x")
        assert open_mode.list_teams_in(f) == []

    def test_scans_dir_itself_and_children(self, tmp_path):
        from modastack.setup import open_mode
        # the dir itself is a team AND it contains a child team
        (tmp_path / "agent.yaml").write_text("agent: root-team\n")
        (tmp_path / "child").mkdir()
        (tmp_path / "child" / "agent.yaml").write_text("agent: child-team\n")
        names = {t["name"] for t in open_mode.list_teams_in(tmp_path)}
        assert names == {"root-team", "child-team"}


class TestIntro:
    def test_intro_scans_the_library_by_default(self, project, home):
        # The default scan + create location is the machine-wide library
        # (~/modastack-agents), not the cwd — a team isn't tied to where it installs.
        _seed_library_team(home, "legacy-bot")
        c = _client(SetupState(), project, home_root=home)
        data = c.get("/api/intro").json()
        names = {t["name"] for t in data["teams"]}
        assert "legacy-bot" in names
        library = str((home / "modastack-agents").resolve())
        assert data["default_location"] == library
        assert data["scan_dir"] == library

    def test_intro_finds_team_at_library_root(self, project, home):
        # The library folder itself may be a team (create writes straight into
        # its named subfolder, but a flat layout is fine too) — show the
        # agent.yaml name, not the folder name.
        src = home / "modastack-agents"
        (src / "roles" / "aide").mkdir(parents=True)
        (src / "agent.yaml").write_text(
            "agent: personal-assistant\nversion: 0.1.0\nentry_point: aide\n")
        (src / "agent.md").write_text("# pa\n\nHelp out.\n")
        (src / "roles" / "aide" / "ROLE.md").write_text("# Aide\n\nHelp.\n")
        c = _client(SetupState(), project, home_root=home)
        teams = c.get("/api/intro").json()["teams"]
        assert any(t["name"] == "personal-assistant"
                   and t["path"] == str(src.resolve()) for t in teams)

    def test_teams_scans_a_chosen_directory(self, project, home):
        # Modify asks which folder to scan — point it anywhere under home.
        elsewhere = home / "projects" / "acme"
        _seed_team(elsewhere, "triage-bot", parent="agents")
        c = _client(SetupState(), project, home_root=home)
        scan = str(elsewhere / "agents")
        d = c.get("/api/teams", params={"dir": scan}).json()
        assert d["dir"] == str((elsewhere / "agents").resolve())
        paths = {t["path"] for t in d["teams"]}
        assert str((elsewhere / "agents" / "triage-bot").resolve()) in paths

    def test_teams_rejects_path_outside_home(self, project, home):
        c = _client(SetupState(), project, home_root=home)
        r = c.get("/api/teams", params={"dir": "/etc"})
        assert r.status_code == 400

    def test_teams_defaults_to_library(self, project, home):
        # No dir → scans the library (same as intro's default scan).
        _seed_library_team(home, "legacy-bot")
        c = _client(SetupState(), project, home_root=home)
        d = c.get("/api/teams").json()
        assert d["dir"] == str((home / "modastack-agents").resolve())
        assert "legacy-bot" in {t["name"] for t in d["teams"]}

    def test_teams_accepts_relative_path_under_home(self, project, home):
        # A relative dir re-bases under home (not the process cwd).
        _seed_team(home / "work", "triage-bot", parent="agents")
        c = _client(SetupState(), project, home_root=home)
        d = c.get("/api/teams", params={"dir": "work/agents"}).json()
        assert d["dir"] == str((home / "work" / "agents").resolve())
        assert "triage-bot" in {t["name"] for t in d["teams"]}

    def test_start_open_rejects_fork_inside_source(self, project, home):
        src = home / "modastack-agents" / "pa"
        src.mkdir(parents=True)
        (src / "agent.yaml").write_text("agent: pa\n")
        c = _client(SetupState(), project, home_root=home)
        r = c.post("/api/start", json={"mode": "open", "team_path": str(src),
                                       "location": str(src / "fork")})
        assert r.status_code == 400  # can't copy a folder into its own subdir

    def test_start_open_in_place_is_a_noop_copy(self, project, home):
        # The default modify location is the team's own path — copy_into is a
        # no-op and the cards reverse-fill from it in place.
        src = _seed_library_team(home, "aide-team")
        c = _client(SetupState(), project, home_root=home)
        r = c.post("/api/start", json={"mode": "open", "team_path": str(src),
                                       "location": str(src)})
        assert r.status_code == 200
        assert r.json()["spec"]["goal"]  # reverse-filled in place

    def test_start_create_sets_location_and_advances(self, project):
        c = _client(SetupState(), project)
        r = c.post("/api/start", json={"mode": "create", "name": "My Triage Team",
                                       "location": "agent-teams/my-triage-team"})
        assert r.status_code == 200
        d = r.json()
        assert d["stage"] == "design"
        assert d["mode"] == "create"
        assert d["source_dir"] == "agent-teams/my-triage-team"
        assert d["team_name"] == "my-triage-team"

    def test_start_open_reverse_fills_and_copies(self, project, home):
        src = _seed_library_team(home, "legacy-bot")
        c = _client(SetupState(), project, home_root=home)
        r = c.post("/api/start", json={"mode": "open", "team_path": str(src),
                                       "location": "agent-teams/legacy-bot"})
        assert r.status_code == 200
        d = r.json()
        assert d["stage"] == "design"
        assert d["mode"] == "open"
        # cards reverse-filled from the existing pack
        assert d["spec"]["goal"]
        assert any(role["name"] == "lead" for role in d["spec"]["roles"])
        assert {s["name"] for s in d["spec"]["services"]} == {"github"}
        assert d["chat"] == "slack"
        assert d["spec"]["readiness"]["goal"] == "enough"
        # source copied into the working location (relative → under the project)
        assert (project / "agent-teams" / "legacy-bot" / "agent.yaml").is_file()
        # the chat opens with a recap of what the team already does (not the
        # blank "what do you want to build?" greeting)
        assert d["messages"], "expected a seeded summary message"
        opener = d["messages"][0]
        assert opener["role"] == "assistant"
        assert "legacy-bot" in opener["content"]
        assert "github" in opener["content"]      # its services are recapped
        assert "Slack" in opener["content"]        # its chat channel is recapped

    def test_start_rejects_modastack_location(self, project):
        c = _client(SetupState(), project)
        r = c.post("/api/start", json={"mode": "create",
                                       "location": ".modastack/team"})
        assert r.status_code == 400

    def test_start_open_unknown_team_400(self, project, home):
        c = _client(SetupState(), project, home_root=home)
        r = c.post("/api/start", json={
            "mode": "open", "team_path": str(home / "modastack-agents" / "ghost"),
            "location": "agent-teams/ghost"})
        assert r.status_code == 400  # not a team (no agent.yaml)

    def test_start_registry_fetches_and_reverse_fills(self, project, monkeypatch):
        # The registry fetch is stubbed: it materializes a team at `dest`,
        # mirroring registry.fetch + copy_into without hitting the network.
        from modastack.setup import open_mode

        def fake_fetch_into(proj, name, dest):
            _seed_team(proj, name)  # writes agents/<name>/
            open_mode.copy_into(proj / "agents" / name, dest)

        monkeypatch.setattr(open_mode, "fetch_into", fake_fetch_into)
        c = _client(SetupState(), project)
        r = c.post("/api/start", json={"mode": "registry", "team": "eng-team",
                                       "location": "modastack/eng-team"})
        assert r.status_code == 200
        d = r.json()
        assert d["stage"] == "design"
        # Registry-derived teams use the non-lossy edit path (mode "open").
        assert d["mode"] == "open"
        assert d["spec"]["goal"]
        assert (project / "modastack" / "eng-team" / "agent.yaml").is_file()

    def test_start_registry_without_team_400(self, project):
        c = _client(SetupState(), project)
        r = c.post("/api/start", json={"mode": "registry",
                                       "location": "modastack/x"})
        assert r.status_code == 400

    def test_browse_lists_home_dirs(self, project, home):
        (home / "work" / "alpha").mkdir(parents=True)
        (home / "work" / "beta").mkdir(parents=True)
        (home / "work" / ".hidden").mkdir(parents=True)
        c = _client(SetupState(), project, home_root=home)
        work = home / "work"
        d = c.get("/api/browse", params={"path": str(work)}).json()
        assert d["path"] == str(work.resolve())
        assert d["parent"] == str(home.resolve())  # one level up is home
        assert "alpha" in d["dirs"] and "beta" in d["dirs"]
        assert ".hidden" not in d["dirs"]  # dotfiles hidden

    def test_browse_defaults_to_library(self, project, home):
        c = _client(SetupState(), project, home_root=home)
        d = c.get("/api/browse").json()  # no path → the library, created on demand
        assert d["path"] == str((home / "modastack-agents").resolve())
        assert (home / "modastack-agents").is_dir()

    def test_browse_confined_to_home(self, project, home):
        c = _client(SetupState(), project, home_root=home)
        # An escape attempt collapses back to the home root.
        d = c.get("/api/browse", params={"path": "/etc"}).json()
        assert d["path"] == str(home.resolve())
        assert d["parent"] is None

    def test_browse_404_on_a_file(self, project, home):
        (home / "notes.txt").write_text("x")
        c = _client(SetupState(), project, home_root=home)
        r = c.get("/api/browse", params={"path": str(home / "notes.txt")})
        assert r.status_code == 404

    def test_browse_survives_library_taken_by_a_file(self, project, home):
        # If ~/modastack-agents already exists as a FILE, the lazy mkdir must
        # not 500 the GET — it falls back to listing home.
        (home / "modastack-agents").write_text("not a dir")
        (home / "work").mkdir()
        c = _client(SetupState(), project, home_root=home)
        d = c.get("/api/browse").json()
        assert d["path"] == str(home.resolve())   # fell back to home
        assert "work" in d["dirs"]

    def test_rename_sets_team_name(self, project):
        st = SetupState(stage=Stage.DESIGN, team_name="auto-name")
        c = _client(st, project)
        d = c.post("/api/rename", json={"name": "My Cooler Team"}).json()
        assert d["team_name"] == "my-cooler-team"

    def test_rename_rejects_empty(self, project):
        c = _client(SetupState(stage=Stage.DESIGN), project)
        r = c.post("/api/rename", json={"name": "   "})
        assert r.status_code == 400

    def test_rename_renames_the_team_named_source_folder(self, project):
        # modify/registry put the source at <location>/<team-name>; renaming
        # must move that folder and repoint source_dir so the folder on disk
        # matches the new name.
        src = project / "modastack" / "a-personal-assistant-team"
        src.mkdir(parents=True)
        (src / "agent.yaml").write_text("agent: a-personal-assistant-team\n")
        st = SetupState(stage=Stage.DESIGN, team_name="a-personal-assistant-team",
                        source_dir="modastack/a-personal-assistant-team")
        c = _client(st, project)
        d = c.post("/api/rename", json={"name": "personal-assistant"}).json()
        assert d["team_name"] == "personal-assistant"
        assert d["source_dir"] == "modastack/personal-assistant"
        assert (project / "modastack" / "personal-assistant" / "agent.yaml").is_file()
        assert not (project / "modastack" / "a-personal-assistant-team").exists()

    def test_rename_leaves_non_team_named_folder_alone(self, project):
        # create's folder is "modastack", not named after the team — left as chosen.
        (project / "modastack").mkdir()
        st = SetupState(stage=Stage.DESIGN, team_name="triage", source_dir="modastack")
        c = _client(st, project)
        d = c.post("/api/rename", json={"name": "triage-bot"}).json()
        assert d["team_name"] == "triage-bot"
        assert d["source_dir"] == "modastack"

    def test_rename_conflict_when_target_folder_exists(self, project):
        (project / "modastack" / "old").mkdir(parents=True)
        (project / "modastack" / "taken").mkdir(parents=True)
        st = SetupState(stage=Stage.DESIGN, team_name="old", source_dir="modastack/old")
        c = _client(st, project)
        r = c.post("/api/rename", json={"name": "taken"})
        assert r.status_code == 409
        assert (project / "modastack" / "old").exists()  # original untouched
