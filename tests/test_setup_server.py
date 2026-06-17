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


@pytest.fixture
def home(tmp_path):
    """A stand-in for the user's home, so the ~/bobbi-agents library and the
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
    """Write a minimal valid team source into the ~/bobbi-agents library."""
    return _seed_team(home / "bobbi-agents", name, parent=".")


class TestIntro:
    def test_intro_scans_the_library_by_default(self, project, home):
        # The default scan + create location is the machine-wide library
        # (~/bobbi-agents), not the cwd — a team isn't tied to where it installs.
        _seed_library_team(home, "legacy-bot")
        c = _client(SetupState(), project, home_root=home)
        data = c.get("/api/intro").json()
        names = {t["name"] for t in data["teams"]}
        assert "legacy-bot" in names
        library = str((home / "bobbi-agents").resolve())
        assert data["default_location"] == library
        assert data["scan_dir"] == library

    def test_intro_finds_team_at_library_root(self, project, home):
        # The library folder itself may be a team (create writes straight into
        # its named subfolder, but a flat layout is fine too) — show the
        # agent.yaml name, not the folder name.
        src = home / "bobbi-agents"
        (src / "roles" / "aide").mkdir(parents=True)
        (src / "agent.yaml").write_text(
            "agent: personal-assistant\nversion: 0.1.0\nentry_point: aide\n")
        (src / "agent.md").write_text("# pa\n\nHelp out.\n")
        (src / "roles" / "aide" / "ROLE.md").write_text("# Aide\n\nHelp.\n")
        c = _client(SetupState(), project, home_root=home)
        teams = c.get("/api/intro").json()["teams"]
        assert {"name": "personal-assistant",
                "path": str(src.resolve())} in teams

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

    def test_start_open_rejects_fork_inside_source(self, project, home):
        src = home / "bobbi-agents" / "pa"
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
            "mode": "open", "team_path": str(home / "bobbi-agents" / "ghost"),
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
                                       "location": "bobbi/eng-team"})
        assert r.status_code == 200
        d = r.json()
        assert d["stage"] == "design"
        # Registry-derived teams use the non-lossy edit path (mode "open").
        assert d["mode"] == "open"
        assert d["spec"]["goal"]
        assert (project / "bobbi" / "eng-team" / "agent.yaml").is_file()

    def test_start_registry_without_team_400(self, project):
        c = _client(SetupState(), project)
        r = c.post("/api/start", json={"mode": "registry",
                                       "location": "bobbi/x"})
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
        assert d["path"] == str((home / "bobbi-agents").resolve())
        assert (home / "bobbi-agents").is_dir()

    def test_browse_confined_to_home(self, project, home):
        c = _client(SetupState(), project, home_root=home)
        # An escape attempt collapses back to the home root.
        d = c.get("/api/browse", params={"path": "/etc"}).json()
        assert d["path"] == str(home.resolve())
        assert d["parent"] is None

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
        src = project / "bobbi" / "a-personal-assistant-team"
        src.mkdir(parents=True)
        (src / "agent.yaml").write_text("agent: a-personal-assistant-team\n")
        st = SetupState(stage=Stage.DESIGN, team_name="a-personal-assistant-team",
                        source_dir="bobbi/a-personal-assistant-team")
        c = _client(st, project)
        d = c.post("/api/rename", json={"name": "personal-assistant"}).json()
        assert d["team_name"] == "personal-assistant"
        assert d["source_dir"] == "bobbi/personal-assistant"
        assert (project / "bobbi" / "personal-assistant" / "agent.yaml").is_file()
        assert not (project / "bobbi" / "a-personal-assistant-team").exists()

    def test_rename_leaves_non_team_named_folder_alone(self, project):
        # create's folder is "bobbi", not named after the team — left as chosen.
        (project / "bobbi").mkdir()
        st = SetupState(stage=Stage.DESIGN, team_name="triage", source_dir="bobbi")
        c = _client(st, project)
        d = c.post("/api/rename", json={"name": "triage-bot"}).json()
        assert d["team_name"] == "triage-bot"
        assert d["source_dir"] == "bobbi"

    def test_rename_conflict_when_target_folder_exists(self, project):
        (project / "bobbi" / "old").mkdir(parents=True)
        (project / "bobbi" / "taken").mkdir(parents=True)
        st = SetupState(stage=Stage.DESIGN, team_name="old", source_dir="bobbi/old")
        c = _client(st, project)
        r = c.post("/api/rename", json={"name": "taken"})
        assert r.status_code == 409
        assert (project / "bobbi" / "old").exists()  # original untouched
