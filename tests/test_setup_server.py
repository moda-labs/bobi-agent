"""Tests for the bobi setup web server — security, serialization, and the
deterministic + streaming endpoints. Driven by Starlette's TestClient with
an injected fake LLM source: no network, no CLI."""

import json
import re

import pytest
import yaml
from fastapi.testclient import TestClient

from bobi import paths
from bobi.setup import services
from bobi.setup.state import SetupState, Stage
from bobi.setup.webui import server

NONCE = "test-nonce-123"


def _fake_digest(reply, **payload):
    sentinel = "===BOBI-SPEC==="

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
def _isolate_environ(tmp_path):
    """Saving a credential writes the secret into ``os.environ`` (actions.py
    ``save_credential`` does ``os.environ[var] = value``) so the live setup
    process can use it immediately. ``monkeypatch`` can't undo that direct app
    write, so without isolation a saved ``VENN_API_KEY``/token bleeds into later
    tests and changes their build/author behavior. Snapshot and restore the
    environment around every test in this module. Also isolate BOBI_HOME so
    setup never touches the real machine-wide agent library."""
    import os
    saved = dict(os.environ)
    home = tmp_path / ".bobi"
    home.mkdir()
    os.environ["BOBI_HOME"] = str(home)
    os.environ.pop("BOBI_ROOT", None)
    paths.bind_root(None)
    yield
    paths.bind_root(None)
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def project():
    run = paths.agent_run_root("setup-test")
    run.mkdir(parents=True, exist_ok=True)
    paths.workspace_dir(run).mkdir(parents=True, exist_ok=True)
    return run


@pytest.fixture
def home():
    """A stand-in for BOBI_HOME, so the agent library and the
    folder picker stay off the real filesystem. Pass home_root=home to _client."""
    h = paths.home_dir()
    h.mkdir(exist_ok=True)
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

    def test_page_loads_shared_tokens_before_app_css(self, project):
        app = server.build_app(SetupState(), project, nonce=NONCE)
        c = _testclient(app)
        html = c.get("/").text
        assert '<link rel="stylesheet" href="/static/tokens.css" />' in html
        assert html.index("/static/tokens.css") < html.index("/static/app.css")

    def test_static_serves_shared_tokens(self, project):
        app = server.build_app(SetupState(), project, nonce=NONCE)
        c = _testclient(app)
        r = c.get("/static/tokens.css")
        assert r.status_code == 200
        assert "text/css" in r.headers["content-type"]
        assert "--bg: #F4F1EA;" in r.text
        assert "--accent: #C8612B;" in r.text

    def test_app_css_does_not_define_design_tokens(self, project):
        app = server.build_app(SetupState(), project, nonce=NONCE)
        c = _testclient(app)
        css = c.get("/static/app.css").text
        declarations = set(re.findall(r"(?m)^\s*(--[\w-]+)\s*:", css))
        assert not {"--bg", "--surface", "--accent", "--slab-bg"} & declarations

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


# --- harness -------------------------------------------------------------

class TestHarnessEndpoint:
    def test_harness_reports_status(self, project, monkeypatch):
        from bobi.setup import harness
        monkeypatch.setattr(harness.shutil, "which", lambda n: "/bin/claude")
        monkeypatch.setattr(harness, "_oauth_credentials_present", lambda: False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        c = _client(SetupState(), project, model="claude-opus-4-8")
        d = c.get("/api/harness").json()
        assert d["agent"] == "Claude Code"
        assert d["model"] == "claude-opus-4-8"
        assert d["authenticated"] is True
        assert d["auth_mode"] == "api_key"

    def test_harness_is_nonce_guarded(self, project):
        app = server.build_app(SetupState(), project, nonce=NONCE)
        c = _testclient(app)
        assert c.get("/api/harness").status_code == 403

    def test_missing_cli_blocks_message_early(self, project, monkeypatch):
        # A missing CLI is reliable (cheap `shutil.which` check, no keychain
        # probe), so block up front with install guidance and never reach the
        # digestion brain (real path: stream_fn=None).
        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: None)
        c = _client(SetupState(team_name="t"), project)   # stream_fn=None
        r = c.post("/api/message", json={"text": "build a triage bot"})
        assert r.status_code == 200
        assert "event: error" in r.text
        assert "CLI isn't installed" in r.text
        assert "event: delta" not in r.text              # digestion never ran

    def test_unauthed_failure_enriches_with_login_hint(
            self, project, monkeypatch):
        # CLI present but not authed: we DON'T pre-block (auth is unreliable to
        # detect) — we let the call run and, only if it fails, turn the cryptic
        # transport error into an actionable login hint.
        import shutil
        from bobi.setup import harness
        monkeypatch.setattr(shutil, "which", lambda name: "/bin/claude")
        monkeypatch.setattr(harness, "harness_status", lambda model=None:
            harness.HarnessStatus(
                agent="Claude Code", model="default", cli_present=True,
                authenticated=False, auth_mode=None,
                login_command="claude auth login"))

        async def _boom(*a, **k):
            raise RuntimeError("transport died")
            yield  # pragma: no cover — makes this an async generator
        monkeypatch.setattr("bobi.setup.digestion.digest_turn", _boom)

        c = _client(SetupState(team_name="t"), project)   # stream_fn=None
        r = c.post("/api/message", json={"text": "build a triage bot"})
        assert r.status_code == 200
        assert "event: error" in r.text
        assert "claude auth login" in r.text
        assert "not be logged into Claude Code" in r.text

    def test_authed_failure_surfaces_raw_error_not_login_hint(
            self, project, monkeypatch):
        # A working (authed) harness that hits a transient error must NOT be
        # told to log in — that would be a misleading false alarm.
        import shutil
        from bobi.setup import harness
        monkeypatch.setattr(shutil, "which", lambda name: "/bin/claude")
        monkeypatch.setattr(harness, "harness_status", lambda model=None:
            harness.HarnessStatus(
                agent="Claude Code", model="default", cli_present=True,
                authenticated=True, auth_mode="api_key",
                login_command="claude auth login"))

        async def _boom(*a, **k):
            raise RuntimeError("rate limited")
            yield  # pragma: no cover
        monkeypatch.setattr("bobi.setup.digestion.digest_turn", _boom)

        c = _client(SetupState(team_name="t"), project)
        r = c.post("/api/message", json={"text": "build a triage bot"})
        assert "rate limited" in r.text
        assert "claude auth login" not in r.text


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
        env = (project / ".env").read_text()
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
        import bobi.venn as venn_mod

        class _S:
            def __init__(self, name):
                self.server_id = self.server_name = name
                self.connected = True
        monkeypatch.setattr(venn_mod, "list_servers_verified",
                            lambda key: [_S(n) for n in names])

    def _verified_raises(self, monkeypatch):
        import bobi.venn as venn_mod
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
        env = (project / ".env").read_text()
        assert "VENN_API_KEY=venn_good" in env

    def test_connect_bad_key_not_saved(self, project, monkeypatch):
        monkeypatch.delenv("VENN_API_KEY", raising=False)
        self._verified_raises(monkeypatch)
        c = _client(SetupState(), project)
        data = c.post("/api/venn/connect", json={"key": "venn_bad"}).json()
        assert data["ok"] is False
        envf = project / ".env"
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

    def test_apply_turning_venn_off_removes_only_venn(self, project):
        # Two Venn services, both toggled off → both removed.
        s = SetupState()
        s.spec.services = [{"name": "gmail"}, {"name": "notion"}]
        c = _client(s, project)
        r = c.post("/api/venn/apply", json={
            "servers": [], "available": ["gmail", "notion"]}).json()
        assert set(r["removed"]) == {"gmail", "notion"}
        assert s.spec.services == []

    def test_apply_never_removes_a_native_service(self, project):
        # Venn's catalog can include "slack" (a NATIVE connector here). Leaving
        # it untoggled in the Venn picker must NOT remove the native service.
        s = SetupState()
        s.spec.services = [{"name": "slack"}, {"name": "gmail"}]
        c = _client(s, project)
        r = c.post("/api/venn/apply", json={
            "servers": [], "available": ["slack", "gmail"]}).json()
        assert r["removed"] == ["gmail"]                 # only the venn one
        assert [x["name"] for x in s.spec.services] == ["slack"]   # native kept

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

    def test_build_pour_then_validate_then_install(self, project, home):
        state = self._ready_state()
        c = _client(state, project, stream_fn=_fake_author())

        r = c.post("/api/build")
        assert r.status_code == 200
        assert "event: file_start" in r.text
        assert "event: state" in r.text
        assert (home / "agents" / "triage-bot" / "src" / "agent.yaml").exists()

        v = c.post("/api/validate").json()
        assert v["passed"] is True

        i = c.post("/api/install").json()
        assert i["installed"] == "triage-bot"
        assert (project / "package" / "agent.yaml").exists()

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
        assert "POSTHOG_API_KEY=ph_secret_123" in (project / ".env").read_text()

    def test_mcp_add_rejects_oauth(self, project):
        # OAuth-authed MCPs aren't supported yet — only api_key / none.
        c = _client(SetupState(), project)
        assert c.post("/api/mcp/add", json={
            "name": "Acme", "url": "https://mcp.acme.com/mcp",
            "auth": "oauth"}).status_code == 400

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

    def test_add_without_key_flags_needs_auth(self, project, monkeypatch):
        # No key given → defaults to api_key with no secret yet → flagged
        # "needs an API key", never "connected".
        monkeypatch.setattr(services, "venn_connected_names", lambda *a, **k: None)
        s = SetupState()
        c = _client(s, project)
        c.post("/api/mcp/add", json={"name": "Acme", "url": "https://mcp.acme.com/mcp"})
        assert s.spec.mcp_servers["acme"]["auth"] == "api_key"
        ph = next(x for x in c.get("/api/connect").json()["cards"] if x["key"] == "acme")
        assert ph["status"] == "needs_auth"
        assert "api key" in ph["note"].lower()

    def test_add_public_server_no_auth(self, project, monkeypatch):
        # Explicit auth=none → a public server, added without a key.
        monkeypatch.setattr(services, "venn_connected_names", lambda *a, **k: None)
        s = SetupState()
        c = _client(s, project)
        c.post("/api/mcp/add", json={
            "name": "DeepWiki", "url": "https://mcp.deepwiki.com/mcp", "auth": "none"})
        ph = next(x for x in c.get("/api/connect").json()["cards"] if x["key"] == "deepwiki")
        assert ph["status"] == "added" and "public" in ph["note"].lower()

    def test_remove_drops_the_user_mcp_entry_and_card(self, project, monkeypatch):
        # Removing a user MCP must drop its mcp_servers entry too, or the row
        # lingers (it's rendered from mcp_servers, not just services).
        monkeypatch.setattr(services, "venn_connected_names", lambda *a, **k: None)
        s = SetupState()
        c = _client(s, project)
        c.post("/api/mcp/add", json={
            "name": "PostHog", "url": "https://mcp.posthog.com/mcp",
            "auth": "api_key", "api_key": "ph_x"})
        assert "posthog" in s.spec.mcp_servers
        c.post("/api/service/remove", json={"service_key": "posthog"})
        assert "posthog" not in s.spec.mcp_servers       # entry gone
        keys = {x["key"] for x in c.get("/api/connect").json()["cards"]}
        assert "posthog" not in keys                     # row gone

    def test_mcp_add_rejects_newline_in_key(self, project, monkeypatch):
        # A pasted key with a newline would inject an extra .env line.
        monkeypatch.delenv("EVIL_API_KEY", raising=False)
        c = _client(SetupState(), project)
        r = c.post("/api/mcp/add", json={
            "name": "Evil", "url": "https://mcp.evil.com/mcp",
            "auth": "api_key", "api_key": "abc\nBOBI_X=1"})
        assert r.status_code == 400

    def test_mcp_add_stdio_command_connection(self, project, monkeypatch):
        # A local command-based (stdio) server: name + command (+ args + env).
        monkeypatch.delenv("SUBSTACK_API_KEY", raising=False)
        s = SetupState()
        c = _client(s, project)
        r = c.post("/api/mcp/add", json={
            "name": "Substack", "transport": "stdio",
            "command": "substack-mcp", "args": "--stdio --port 0",
            "env": [{"name": "SUBSTACK_API_KEY", "value": "sk_123"},
                    {"name": "SUBSTACK_PUBLICATION", "value": ""}]})
        assert r.status_code == 200 and r.json()["ok"] is True
        entry = s.spec.mcp_servers["substack"]
        assert entry["type"] == "stdio" and entry["command"] == "substack-mcp"
        assert entry["args"] == ["--stdio", "--port", "0"]   # shell-split
        assert entry["env_vars"] == ["SUBSTACK_API_KEY", "SUBSTACK_PUBLICATION"]
        # also a team service, so it renders as a row
        assert any((x.get("name") or "").lower() == "substack"
                   for x in s.spec.services)
        # the value landed in .env as a ${VAR} ref, never in the response
        assert "sk_123" not in r.text
        env_text = (project / ".env").read_text()
        assert "SUBSTACK_API_KEY=sk_123" in env_text

    def test_mcp_add_stdio_requires_command(self, project):
        c = _client(SetupState(), project)
        assert c.post("/api/mcp/add", json={
            "name": "X", "transport": "stdio"}).status_code == 400

    def test_mcp_add_stdio_rejects_bad_env_var_name(self, project):
        c = _client(SetupState(), project)
        r = c.post("/api/mcp/add", json={
            "name": "X", "transport": "stdio", "command": "x-mcp",
            "env": [{"name": "not-upper", "value": "v"}]})
        assert r.status_code == 400

    def test_mcp_add_command_without_transport_is_stdio(self, project):
        # A command (and no URL) is inferred as stdio even without `transport`.
        s = SetupState()
        c = _client(s, project)
        r = c.post("/api/mcp/add", json={"name": "Local", "command": "my-mcp"})
        assert r.status_code == 200
        assert s.spec.mcp_servers["local"]["type"] == "stdio"

    def test_mcp_with_divergent_slug_supersedes_placeholder(self, project, monkeypatch):
        # Bug: a service guessed as "substack" plus an MCP added as "substack-mcp"
        # (slug "substack_mcp") showed TWO cards — a "needs connect" placeholder
        # and the connected MCP — because dedup matched on exact key.
        monkeypatch.setattr(services, "venn_connected_names", lambda *a, **k: None)
        monkeypatch.setattr(services, "live_venn_catalog", lambda *a, **k: set())
        s = SetupState()
        s.spec.services = [{"name": "substack"}]   # earlier guessed placeholder
        c = _client(s, project)
        c.post("/api/mcp/add", json={
            "name": "substack-mcp", "transport": "stdio", "command": "uv",
            "args": "run --directory /x substack-mcp",
            "env": [{"name": "SUBSTACK_COOKIE", "value": "abc"}]})
        cards = c.get("/api/connect").json()["cards"]
        subs = [cc for cc in cards if "substack" in cc["key"]]
        assert len(subs) == 1                      # exactly one, not two
        assert subs[0]["via"] == "local command" and subs[0]["status"] == "added"

    def test_removing_mcp_does_not_resurrect_placeholder(self, project, monkeypatch):
        monkeypatch.setattr(services, "venn_connected_names", lambda *a, **k: None)
        monkeypatch.setattr(services, "live_venn_catalog", lambda *a, **k: set())
        s = SetupState()
        s.spec.services = [{"name": "substack"}]
        c = _client(s, project)
        c.post("/api/mcp/add", json={
            "name": "substack-mcp", "transport": "stdio", "command": "uv",
            "args": "run --directory /x substack-mcp"})
        c.post("/api/service/remove", json={"service_key": "substack_mcp"})
        cards = c.get("/api/connect").json()["cards"]
        assert not [cc for cc in cards if "substack" in cc["key"]]

    def test_edit_preserves_saved_secret_and_updates_in_place(self, project, monkeypatch):
        # Editing a stdio MCP (re-submit with `replaces`): a blank env value keeps
        # the saved secret, edits apply in place, and no duplicate entry appears.
        monkeypatch.delenv("SUBSTACK_COOKIE", raising=False)
        s = SetupState()
        c = _client(s, project)
        c.post("/api/mcp/add", json={
            "name": "substack-mcp", "transport": "stdio", "command": "uv",
            "args": "run --directory /x substack-mcp",
            "env": [{"name": "SUBSTACK_COOKIE", "value": "sek"}]})
        assert list(s.spec.mcp_servers) == ["substack_mcp"]
        # Edit: change args, leave the cookie blank (keep current).
        c.post("/api/mcp/add", json={
            "name": "substack-mcp", "transport": "stdio", "command": "uv",
            "args": "run --directory /y substack-mcp",
            "env": [{"name": "SUBSTACK_COOKIE", "value": ""}],
            "replaces": "substack_mcp"})
        assert list(s.spec.mcp_servers) == ["substack_mcp"]   # no duplicate
        entry = s.spec.mcp_servers["substack_mcp"]
        assert entry["args"][-2] == "/y"                      # edit applied
        assert entry["env_vars"] == ["SUBSTACK_COOKIE"]       # declaration kept
        env_text = (project / ".env").read_text()
        assert "SUBSTACK_COOKIE=sek" in env_text              # secret preserved

    def test_edit_rename_rekeys_without_leaving_stale_entry(self, project, monkeypatch):
        monkeypatch.setattr(services, "venn_connected_names", lambda *a, **k: None)
        monkeypatch.setattr(services, "live_venn_catalog", lambda *a, **k: set())
        s = SetupState()
        c = _client(s, project)
        c.post("/api/mcp/add", json={
            "name": "substack-mcp", "transport": "stdio", "command": "uv",
            "args": "run x"})
        c.post("/api/mcp/add", json={
            "name": "Substack", "transport": "stdio", "command": "uv",
            "args": "run x", "replaces": "substack_mcp"})
        # Old key gone, new key present, exactly one — and one card.
        assert list(s.spec.mcp_servers) == ["substack"]
        cards = c.get("/api/connect").json()["cards"]
        assert len([cc for cc in cards if "substack" in cc["key"]]) == 1

    def test_state_serializes_mcp_servers(self, project):
        s = SetupState()
        c = _client(s, project)
        c.post("/api/mcp/add", json={
            "name": "substack-mcp", "transport": "stdio", "command": "uv",
            "args": "run x", "env": [{"name": "SUBSTACK_COOKIE"}]})
        # The UI needs the stored config to repopulate the edit form.
        spec = c.get("/api/connect")  # touch
        from bobi.setup.webui.server import serialize_state
        sp = serialize_state(s)["spec"]
        assert "substack_mcp" in sp["mcp_servers"]
        assert sp["mcp_servers"]["substack_mcp"]["command"] == "uv"

    def _stub_probe(self, monkeypatch, *, run_result):
        """Fake probe: a propose call (call_name=None) lists tools + a suggestion;
        a run call (call_name=...) returns the given run_result."""
        import shutil
        import bobi.setup.mcp_probe as mcp_probe

        # The connection-test path is pure Python (mcp_probe), but /api/message
        # gates on the CLI being present first. Simulate a present CLI so these
        # tests don't depend on `claude` being installed on the runner.
        monkeypatch.setattr(shutil, "which", lambda name: "/bin/claude")

        async def fake_probe(entry, proj, *, call_name=None, **kw):
            if call_name is None:
                return {"ok": True, "count": 3, "suggested": "substack_get_notes_feed",
                        "tools": ["substack_get_notes_feed", "substack_get_profile",
                                  "substack_post_note"]}
            return {**run_result, "called": call_name}
        monkeypatch.setattr(mcp_probe, "probe", fake_probe)
        monkeypatch.setattr(services, "venn_connected_names", lambda *a, **k: None)
        monkeypatch.setattr(services, "live_venn_catalog", lambda *a, **k: set())

    def _added_substack(self, s, c):
        c.post("/api/mcp/add", json={
            "name": "substack-mcp", "transport": "stdio", "command": "uv",
            "args": "run x"})

    def test_chat_test_proposes_a_tool_then_runs_on_confirm(self, project, monkeypatch):
        # Turn 1 proposes a safe tool (nothing runs); turn 2 ("yes") runs it and
        # marks the row connected.
        self._stub_probe(monkeypatch, run_result={
            "ok": True, "live_ok": True, "output": "note: hello", "live_error": None})
        s = SetupState()
        c = _client(s, project)
        self._added_substack(s, c)
        body1 = c.post("/api/message",
                       json={"text": "test the substack connection"}).text
        assert "substack_get_notes_feed" in body1          # proposed, not run
        assert s.spec.mcp_servers["substack_mcp"].get("last_test") is None
        assert s.pending_test["proposed"] == "substack_get_notes_feed"
        body2 = c.post("/api/message", json={"text": "yes"}).text
        assert "worked" in body2 and "note: hello" in body2
        assert s.spec.mcp_servers["substack_mcp"]["last_test"]["live_ok"] is True
        assert not s.pending_test
        card = next(x for x in c.get("/api/connect").json()["cards"]
                    if x["key"] == "substack_mcp")
        assert card["status"] == "connected"

    def test_chat_test_user_can_name_a_different_readonly_tool(self, project, monkeypatch):
        self._stub_probe(monkeypatch, run_result={
            "ok": True, "live_ok": True, "output": "", "live_error": None})
        s = SetupState()
        c = _client(s, project)
        self._added_substack(s, c)
        c.post("/api/message", json={"text": "test the substack connection"})
        c.post("/api/message", json={"text": "call substack_get_profile"})
        # The named read-only tool (not the proposal) is what ran.
        assert s.spec.mcp_servers["substack_mcp"]["last_test"]["called"] == "substack_get_profile"

    def test_chat_test_refuses_to_run_a_named_write_tool(self, project, monkeypatch):
        # Safety: naming a write tool must NOT execute it as a connection test.
        self._stub_probe(monkeypatch, run_result={"ok": True, "live_ok": True})
        s = SetupState()
        c = _client(s, project)
        self._added_substack(s, c)
        c.post("/api/message", json={"text": "test the substack connection"})
        body = c.post("/api/message", json={"text": "call substack_post_note"}).text
        assert "won't" in body.lower() or "write" in body.lower()
        # No tool was run → no test verdict recorded.
        assert s.spec.mcp_servers["substack_mcp"].get("last_test") is None

    def test_chat_test_failed_call_marks_error(self, project, monkeypatch):
        self._stub_probe(monkeypatch, run_result={
            "ok": True, "live_ok": False, "live_error": "No Substack cookie configured"})
        s = SetupState()
        c = _client(s, project)
        self._added_substack(s, c)
        c.post("/api/message", json={"text": "test the substack connection"})
        body2 = c.post("/api/message", json={"text": "yes"}).text
        assert "No Substack cookie" in body2
        card = next(x for x in c.get("/api/connect").json()["cards"]
                    if x["key"] == "substack_mcp")
        assert card["status"] == "error"

    def test_chat_test_decline_clears_pending(self, project, monkeypatch):
        self._stub_probe(monkeypatch, run_result={"ok": True, "live_ok": True})
        s = SetupState()
        c = _client(s, project)
        self._added_substack(s, c)
        c.post("/api/message", json={"text": "test the substack connection"})
        body2 = c.post("/api/message", json={"text": "no thanks"}).text
        assert "skipped" in body2.lower()
        assert not s.pending_test
        assert s.spec.mcp_servers["substack_mcp"].get("last_test") is None

    def test_ordinary_chat_does_not_trigger_a_test(self, project, monkeypatch):
        # A normal design message must fall through to digestion, not the probe.
        import bobi.setup.mcp_probe as mcp_probe
        called = {"probe": False}

        async def fake_probe(*a, **k):
            called["probe"] = True
            return {"ok": True, "count": 0, "tools": []}
        monkeypatch.setattr(mcp_probe, "probe", fake_probe)

        async def fake_digest(state, project, msg, **kw):
            state.messages.append({"role": "assistant", "content": "ok"})
            yield "ok"
        monkeypatch.setattr("bobi.setup.digestion.digest_turn", fake_digest)
        s = SetupState()
        s.spec.mcp_servers = {"substack_mcp": {"type": "stdio", "command": "uv"}}
        c = _client(s, project)
        c.post("/api/message", json={"text": "add a project lead role"}).text
        assert called["probe"] is False

    def test_mcp_detect_endpoint(self, project, tmp_path):
        # The detect endpoint runs the static scan and returns the recipe.
        srv = tmp_path / "acme"
        (srv / "src" / "acme_mcp").mkdir(parents=True)
        (srv / "pyproject.toml").write_text(
            '[project]\nname = "acme-mcp"\nversion = "0.1.0"\n\n'
            '[project.scripts]\nacme-mcp = "acme_mcp.server:main"\n')
        (srv / "src" / "acme_mcp" / "__init__.py").write_text("")
        (srv / "src" / "acme_mcp" / "server.py").write_text(
            'import os\nT = os.environ.get("ACME_TOKEN", "")\n')
        c = _client(SetupState(), project, home_root=tmp_path)
        r = c.post("/api/mcp/detect", json={"path": str(srv)})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True and d["command"] == "uv"
        assert d["args"][-1] == "acme-mcp"
        assert any(e["name"] == "ACME_TOKEN" and e["secret"] for e in d["env"])

    def test_mcp_detect_requires_path(self, project):
        c = _client(SetupState(), project)
        assert c.post("/api/mcp/detect", json={}).status_code == 400

    def test_mcp_detect_bad_folder(self, project, tmp_path):
        c = _client(SetupState(), project, home_root=tmp_path)
        r = c.post("/api/mcp/detect", json={"path": str(tmp_path / "nope")})
        assert r.status_code == 400 and r.json()["ok"] is False

    def test_mcp_detect_rejects_path_outside_home(self, project, tmp_path):
        # The scan is confined to the home tree, like the folder picker.
        c = _client(SetupState(), project, home_root=tmp_path / "home")
        (tmp_path / "home").mkdir()
        r = c.post("/api/mcp/detect", json={"path": "/etc"})
        assert r.status_code == 400 and "home" in r.json()["error"]

    def test_connect_surfaces_stdio_card(self, project, monkeypatch):
        monkeypatch.setattr(services, "venn_connected_names", lambda *a, **k: None)
        monkeypatch.delenv("SUBSTACK_API_KEY", raising=False)
        s = SetupState()
        c = _client(s, project)
        # env var declared but no value yet → card flags it needs that var.
        c.post("/api/mcp/add", json={
            "name": "Substack", "transport": "stdio", "command": "substack-mcp",
            "env": [{"name": "SUBSTACK_API_KEY"}]})
        card = next(x for x in c.get("/api/connect").json()["cards"]
                    if x["key"] == "substack")
        assert card["kind"] == "mcp" and card["via"] == "local command"
        assert card["status"] == "needs_auth"
        assert "SUBSTACK_API_KEY" in card["note"]
        assert card["summary"] == "substack-mcp"
        envf = project / ".env"
        assert not envf.exists() or "BOBI_X" not in envf.read_text()


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
        s, c = self._built(project)
        r = c.post("/api/reveal")
        assert r.status_code == 200 and r.json()["ok"] is True
        # launched the OS file manager on the team's source dir
        assert calls and str(paths.agent_source_dir(s.team_name)) in calls[0]

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
        assert (paths.agent_source_dir("triage-bot") / "agent.md").read_text() \
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


# --- intro: create / open + location -------------------------------------

def _write_team_source(src, name="legacy-bot"):
    """Write a minimal valid team source at src."""
    (src / "roles" / "lead").mkdir(parents=True)
    (src / "agent.yaml").write_text(
        "agent: " + name + "\nversion: 0.1.0\nentry_point: lead\n"
        "services:\n  - name: github\n    events: true\nchat: slack\n")
    (src / "agent.md").write_text("# " + name + "\n\nWatch the repo and triage issues.\n")
    (src / "roles" / "lead" / "ROLE.md").write_text("# Lead\n\nClassify and route issues.\n")
    return src


def _seed_team(project, name="legacy-bot", *, parent="agents"):
    """Write a minimal valid team source under <project>/<parent>/<name>/."""
    return _write_team_source(project / parent / name, name)


def _seed_library_team(home, name="legacy-bot"):
    """Write a minimal valid team source into the BOBI_HOME agent library."""
    return _write_team_source(home / "agents" / name / "src", name)


class TestListTeamsIn:
    def test_missing_directory_is_empty(self, tmp_path):
        from bobi.setup import open_mode
        assert open_mode.list_teams_in(tmp_path / "nope") == []

    def test_a_path_thats_a_file_is_empty(self, tmp_path):
        from bobi.setup import open_mode
        f = tmp_path / "f.txt"
        f.write_text("x")
        assert open_mode.list_teams_in(f) == []

    def test_scans_dir_itself_and_children(self, tmp_path):
        from bobi.setup import open_mode
        # the dir itself is a team AND it contains a child team
        (tmp_path / "agent.yaml").write_text("agent: root-team\n")
        (tmp_path / "child").mkdir()
        (tmp_path / "child" / "agent.yaml").write_text("agent: child-team\n")
        names = {t["name"] for t in open_mode.list_teams_in(tmp_path)}
        assert names == {"root-team", "child-team"}


class TestIntro:
    def test_intro_scans_the_library_by_default(self, project, home):
        # The default scan location is the machine-wide Bobi Agent library, not
        # the cwd — a source isn't tied to where setup was launched.
        _seed_library_team(home, "legacy-bot")
        c = _client(SetupState(), project, home_root=home)
        data = c.get("/api/intro").json()
        names = {t["name"] for t in data["teams"]}
        assert "legacy-bot" in names
        library = str((home / "agents").resolve())
        assert data["default_location"] == str((home / "agents" / "new-agent" / "src").resolve())
        assert data["scan_dir"] == library

    def test_intro_finds_team_at_library_root(self, project, home):
        # A scanned folder itself may be a team — show the agent.yaml name, not
        # the folder name.
        src = home / "agents"
        (src / "roles" / "aide").mkdir(parents=True)
        (src / "agent.yaml").write_text(
            "agent: personal-assistant\nversion: 0.1.0\nentry_point: aide\n")
        (src / "agent.md").write_text("# pa\n\nHelp out.\n")
        (src / "roles" / "aide" / "ROLE.md").write_text("# Aide\n\nHelp.\n")
        c = _client(SetupState(), project, home_root=home)
        teams = c.get("/api/intro").json()["teams"]
        assert any(t["name"] == "personal-assistant"
                   and t["path"] == str(src.resolve()) for t in teams)

    def test_intro_includes_team_at_custom_source_dir(self, project, home):
        # A team authored at the location the user gave at /api/start lives
        # outside the default library. bobi persisted that path in
        # source_dir, so the home screen must surface the team — scanning only
        # the library hides a team the user explicitly placed elsewhere.
        src = home / "projects" / "moda"
        _seed_team(src, "sales-prep", parent=".")     # team at <src>/sales-prep
        state = SetupState(mode="create", team_name="sales-prep",
                           source_dir=str(src))
        c = _client(state, project, home_root=home)
        teams = c.get("/api/intro").json()["teams"]
        by_name = {t["name"]: t for t in teams}
        assert "sales-prep" in by_name
        assert by_name["sales-prep"]["path"] == str((src / "sales-prep").resolve())

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
        assert d["dir"] == str((home / "agents").resolve())
        assert "legacy-bot" in {t["name"] for t in d["teams"]}

    def test_teams_accepts_relative_path_under_home(self, project, home):
        # A relative dir re-bases under home (not the process cwd).
        _seed_team(home / "work", "triage-bot", parent="agents")
        c = _client(SetupState(), project, home_root=home)
        d = c.get("/api/teams", params={"dir": "work/agents"}).json()
        assert d["dir"] == str((home / "work" / "agents").resolve())
        assert "triage-bot" in {t["name"] for t in d["teams"]}

    def test_start_open_rejects_fork_inside_source(self, project, home):
        src = home / "agents" / "pa" / "src"
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
        assert d["source_dir"] == str((paths.home_dir() / "agent-teams" / "my-triage-team").resolve())
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
        # source copied into the working location (relative → under BOBI_HOME)
        assert (home / "agent-teams" / "legacy-bot" / "agent.yaml").is_file()
        # the chat opens with a recap of what the team already does (not the
        # blank "what do you want to build?" greeting)
        assert d["messages"], "expected a seeded summary message"
        opener = d["messages"][0]
        assert opener["role"] == "assistant"
        assert "legacy-bot" in opener["content"]
        assert "github" in opener["content"]      # its services are recapped
        assert "Slack" in opener["content"]        # its chat channel is recapped

    def test_start_rejects_source_inside_run(self, project):
        c = _client(SetupState(), project)
        r = c.post("/api/start", json={"mode": "create",
                                       "location": str(project / "src")})
        assert r.status_code == 400

    def test_start_open_unknown_team_400(self, project, home):
        c = _client(SetupState(), project, home_root=home)
        r = c.post("/api/start", json={
            "mode": "open", "team_path": str(home / "agents" / "ghost" / "src"),
            "location": "agent-teams/ghost"})
        assert r.status_code == 400  # not a team (no agent.yaml)

    def test_start_open_refuses_to_clobber_a_different_team(self, project, home):
        # Review F4: importing/forking a team into a location already occupied by
        # a DIFFERENT team must be refused — copy_into uses copytree(dirs_exist_ok)
        # and would otherwise merge the two into a corrupted hybrid. The existing
        # team must be left untouched.
        existing = _seed_library_team(home, "myteam")
        keep = "# myteam\n\nORIGINAL LIBRARY TEAM — keep me.\n"
        (existing / "agent.md").write_text(keep)
        # A different team on disk that happens to share the basename "myteam".
        import_src = _seed_team(home, "myteam", parent="elsewhere")
        (import_src / "agent.md").write_text("# myteam\n\nIMPORTED COPY.\n")

        c = _client(SetupState(), project, home_root=home)
        r = c.post("/api/start", json={
            "mode": "open", "team_path": str(import_src), "location": str(existing)})
        assert r.status_code == 409
        # the existing team's source is untouched (not merged/overwritten)
        assert (existing / "agent.md").read_text() == keep

    def test_start_registry_fetches_and_reverse_fills(self, project, home, monkeypatch):
        # The registry fetch is stubbed: it materializes a team at `dest`,
        # mirroring registry.fetch + copy_into without hitting the network.
        from bobi.setup import open_mode

        def fake_fetch_into(proj, name, dest):
            _seed_team(proj, name)  # writes agents/<name>/
            open_mode.copy_into(proj / "agents" / name, dest)

        monkeypatch.setattr(open_mode, "fetch_into", fake_fetch_into)
        c = _client(SetupState(), project)
        r = c.post("/api/start", json={"mode": "registry", "team": "eng-team",
                                       "location": "bobi/eng-team"})
        assert r.status_code == 200
        d = r.json()
        assert d["stage"] == "design"
        # Registry-derived teams use the non-lossy edit path (mode "open").
        assert d["mode"] == "open"
        assert d["spec"]["goal"]
        assert (home / "bobi" / "eng-team" / "agent.yaml").is_file()

    def test_start_registry_refuses_to_clobber_an_existing_team(self, project, home):
        # Selecting a (bundled/registry) template into a location already holding
        # a team must not overwrite it — same data-loss class as the open-mode
        # guard. Found by /qa: bundled templates made this path reachable.
        existing = _seed_library_team(home, "market-research")
        keep = "# market-research\n\nMY CUSTOMIZED TEAM — keep me.\n"
        (existing / "agent.md").write_text(keep)
        c = _client(SetupState(), project, home_root=home)
        r = c.post("/api/start", json={
            "mode": "registry", "team": "market-research", "location": str(existing)})
        assert r.status_code == 409
        assert (existing / "agent.md").read_text() == keep

    def test_start_registry_without_team_400(self, project):
        c = _client(SetupState(), project)
        r = c.post("/api/start", json={"mode": "registry",
                                       "location": "bobi/x"})
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
        assert d["path"] == str((home / "agents").resolve())
        assert (home / "agents").is_dir()

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
        # If the canonical agents root already exists as a FILE, the lazy mkdir must
        # not 500 the GET — it falls back to listing home.
        alt_home = home.parent / "browse-home"
        alt_home.mkdir()
        (alt_home / "agents").write_text("not a dir")
        (alt_home / "work").mkdir()
        c = _client(SetupState(), project, home_root=alt_home)
        d = c.get("/api/browse").json()
        assert d["path"] == str(alt_home.resolve())   # fell back to home
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
        src = paths.home_dir() / "bobi" / "a-personal-assistant-team"
        src.mkdir(parents=True)
        (src / "agent.yaml").write_text("agent: a-personal-assistant-team\n")
        st = SetupState(stage=Stage.DESIGN, team_name="a-personal-assistant-team",
                        source_dir="bobi/a-personal-assistant-team")
        c = _client(st, project)
        d = c.post("/api/rename", json={"name": "personal-assistant"}).json()
        assert d["team_name"] == "personal-assistant"
        assert d["source_dir"] == "bobi/personal-assistant"
        assert (paths.home_dir() / "bobi" / "personal-assistant" / "agent.yaml").is_file()
        assert not (paths.home_dir() / "bobi" / "a-personal-assistant-team").exists()

    def test_rename_leaves_non_team_named_folder_alone(self, project):
        # create's folder is "bobi", not named after the team — left as chosen.
        (paths.home_dir() / "bobi").mkdir()
        st = SetupState(stage=Stage.DESIGN, team_name="triage", source_dir="bobi")
        c = _client(st, project)
        d = c.post("/api/rename", json={"name": "triage-bot"}).json()
        assert d["team_name"] == "triage-bot"
        assert d["source_dir"] == "bobi"

    def test_rename_conflict_when_target_folder_exists(self, project):
        (paths.home_dir() / "bobi" / "old").mkdir(parents=True)
        (paths.home_dir() / "bobi" / "taken").mkdir(parents=True)
        st = SetupState(stage=Stage.DESIGN, team_name="old", source_dir="bobi/old")
        c = _client(st, project)
        r = c.post("/api/rename", json={"name": "taken"})
        assert r.status_code == 409
        assert (paths.home_dir() / "bobi" / "old").exists()  # original untouched


class TestWorkflowYaml:
    def test_yaml_preview(self, project):
        s = SetupState(stage=Stage.DESIGN)
        s.spec.roles = [{"name": "lead", "responsibility": "runs it"}]
        s.spec.workflows = [{"name": "triage", "trigger": "an issue lands",
                             "steps": [{"name": "look", "role": "lead",
                                        "prompt": "Look at it.",
                                        "hitl": True}]}]
        c = _client(s, project)
        r = c.get("/api/workflow/yaml", params={"index": 0})
        assert r.status_code == 200
        data = r.json()
        assert data["path"] == "workflows/triage.yaml"
        wf = yaml.safe_load(data["yaml"])
        assert [st["name"] for st in wf["steps"]] == ["look", "look-approval"]

    def test_bad_or_missing_index(self, project):
        c = _client(SetupState(), project)
        assert c.get("/api/workflow/yaml",
                     params={"index": "x"}).status_code == 400
        assert c.get("/api/workflow/yaml",
                     params={"index": 3}).status_code == 404

    def test_state_serializes_workflows(self, project):
        s = SetupState()
        s.spec.workflows = [{"name": "w"}]
        s.spec.workflows_confirmed = True
        c = _client(s, project)
        spec = c.get("/api/state").json()["spec"]
        assert spec["workflows"] == [{"name": "w"}]
        assert spec["workflows_confirmed"] is True


class TestAutomationTrigger:
    def test_update_accepts_trigger(self, project):
        s = SetupState(stage=Stage.DESIGN)
        s.spec.autonomous = [{"description": "d", "leash": "notify"}]
        c = _client(s, project)
        r = c.post("/api/automation/update",
                   json={"index": 0, "fields": {"trigger": "event",
                                                "cadence": "when a PR opens"}})
        assert r.status_code == 200
        assert s.spec.autonomous[0]["trigger"] == "event"
        assert s.spec.autonomous[0]["cadence"] == "when a PR opens"

    def test_bogus_trigger_is_ignored(self, project):
        s = SetupState(stage=Stage.DESIGN)
        s.spec.autonomous = [{"description": "d"}]
        c = _client(s, project)
        r = c.post("/api/automation/update",
                   json={"index": 0, "fields": {"trigger": "banana"}})
        assert r.status_code == 200
        assert "trigger" not in s.spec.autonomous[0]


class TestSlackFinalize:
    @pytest.fixture(autouse=True)
    def _no_host_slack_env(self):
        # A real SLACK_BOT_TOKEN exported on the dev machine must not leak in
        # (it would turn the name-rejection test into a live Slack call).
        # _isolate_environ restores the host environment afterwards.
        import os
        os.environ.pop("SLACK_BOT_TOKEN", None)
        os.environ.pop("SLACK_CHANNELS", None)

    def test_channel_id_saves_without_token(self, project):
        from bobi.setup import actions
        s = SetupState()
        c = _client(s, project)
        r = c.post("/api/slack/channel", json={"channel": "C0ABC123"})
        assert r.status_code == 200
        assert r.json()["channel"] == "C0ABC123"
        assert actions.read_env(project)["SLACK_CHANNELS"] == "C0ABC123"
        assert "SLACK_CHANNELS" in s.credentials_saved

    def test_channel_name_without_token_is_rejected(self, project):
        c = _client(SetupState(), project)
        r = c.post("/api/slack/channel", json={"channel": "#general"})
        assert r.status_code == 400
        assert "token" in r.json()["error"]

    def test_empty_channel_rejected(self, project):
        c = _client(SetupState(), project)
        assert c.post("/api/slack/channel",
                      json={"channel": " "}).status_code == 400

    def test_test_requires_token_then_channel(self, project):
        import os
        c = _client(SetupState(), project)
        assert c.post("/api/slack/test").status_code == 400
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
        r = c.post("/api/slack/test")
        assert r.status_code == 400
        assert "channel" in r.json()["error"]

    def test_test_posts_to_first_channel(self, project, monkeypatch):
        import os
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
        os.environ["SLACK_CHANNELS"] = "C111, C222"
        calls = []
        import bobi.slack

        def fake_post(token, channel, text, *a, **kw):
            calls.append((token, channel, text))
            return {"ok": True}
        monkeypatch.setattr(bobi.slack, "post_slack_message", fake_post)
        c = _client(SetupState(team_name="crew"), project)
        r = c.post("/api/slack/test")
        assert r.status_code == 200
        assert calls[0][1] == "C111"
        assert "crew" in calls[0][2]


class TestShutdown:
    def test_shutdown_flips_should_exit(self, project):
        app = server.build_app(SetupState(), project, nonce=NONCE)

        class Srv:
            should_exit = False
        app.state.uvicorn_server = Srv()
        c = _testclient(app)
        c.headers.update({server.NONCE_HEADER: NONCE})
        assert c.post("/api/shutdown").json()["ok"] is True
        assert app.state.uvicorn_server.should_exit is True

    def test_shutdown_without_server_is_noop(self, project):
        c = _client(SetupState(), project)
        assert c.post("/api/shutdown").status_code == 200


class TestSlackFinalizeHardening:
    @pytest.fixture(autouse=True)
    def _no_host_slack_env(self):
        import os
        os.environ.pop("SLACK_BOT_TOKEN", None)
        os.environ.pop("SLACK_CHANNELS", None)

    def test_resolution_failure_is_redacted(self, project, monkeypatch):
        import os

        import bobi.slack
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-supersecrettokenvalue123"

        def boom(token, name, **kw):
            raise RuntimeError(f"slack said no for {token}")
        monkeypatch.setattr(bobi.slack, "resolve_channel_id", boom)
        c = _client(SetupState(), project)
        r = c.post("/api/slack/channel", json={"channel": "#general"})
        assert r.status_code == 502
        assert "xoxb-supersecrettokenvalue123" not in r.json()["error"]

    def test_name_resolves_and_saves_id(self, project, monkeypatch):
        import os

        import bobi.slack
        from bobi.setup import actions
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-test"
        monkeypatch.setattr(bobi.slack, "resolve_channel_id",
                            lambda token, name, **kw: "C0RESOLVED")
        s = SetupState()
        c = _client(s, project)
        r = c.post("/api/slack/channel", json={"channel": "#general"})
        assert r.status_code == 200
        assert r.json()["channel"] == "C0RESOLVED"
        assert actions.read_env(project)["SLACK_CHANNELS"] == "C0RESOLVED"

    def test_plain_word_without_token_is_not_saved_verbatim(self, project):
        # "Customers" starts with C and is alnum, but it's a NAME — saving it
        # as an ID would scope the adapter to a channel that doesn't exist.
        c = _client(SetupState(), project)
        r = c.post("/api/slack/channel", json={"channel": "Customers"})
        assert r.status_code == 400

    def test_message_failure_is_redacted(self, project, monkeypatch):
        import os

        import bobi.slack
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-supersecrettokenvalue123"
        os.environ["SLACK_CHANNELS"] = "C111"

        def boom(token, channel, text, *a, **kw):
            raise RuntimeError(f"boom {token}")
        monkeypatch.setattr(bobi.slack, "post_slack_message", boom)
        c = _client(SetupState(), project)
        r = c.post("/api/slack/test")
        assert r.status_code == 502
        assert "xoxb-supersecrettokenvalue123" not in r.json()["error"]

    def test_exported_env_wins_over_stale_dotenv(self, project, monkeypatch):
        import os

        import bobi.slack
        from bobi.setup import actions
        env = actions.read_env(project)
        env["SLACK_BOT_TOKEN"] = "xoxb-staletoken000000"
        actions.write_env(project, env)
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-freshtoken000000"
        calls = []

        def fake(token, name, **kw):
            calls.append(token)
            return "C0RESOLVED"
        monkeypatch.setattr(bobi.slack, "resolve_channel_id", fake)
        c = _client(SetupState(), project)
        assert c.post("/api/slack/channel",
                      json={"channel": "#g"}).status_code == 200
        assert calls == ["xoxb-freshtoken000000"]


class TestBuildGoalFloor:
    def test_build_refuses_empty_goal(self, project):
        # The one hard floor holds even on a direct /api/build call — no
        # placeholder team from an empty spec.
        s = SetupState(stage=Stage.BUILD)
        c = _client(s, project)
        r = c.post("/api/build")
        assert "goal is still empty" in r.text
        assert "file_start" not in r.text
