"""Tests for the setup session's in-process tools.

Handlers are plain async functions (SdkMcpTool.handler) — they run here
directly against a constructed SetupState, no live session needed.
"""

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from modastack.setup.state import INTERVIEW_KEYS, SetupState, Stage
from modastack.setup.tools import make_setup_tools


def _tools(state, project, prompt_fn=None):
    return {t.name: t.handler for t in make_setup_tools(state, project, prompt_fn)}


def _call(handler, **args):
    return asyncio.run(handler(args))


def _text(result):
    return result["content"][0]["text"]


def _write_minimal_pack(pack_dir: Path, entry="manager", with_adhoc=True):
    (pack_dir / "roles" / entry).mkdir(parents=True, exist_ok=True)
    (pack_dir / "roles" / entry / "ROLE.md").write_text(f"# {entry}\nYou run things.")
    (pack_dir / "agent.md").write_text("# Team\nA test team.")
    (pack_dir / "agent.yaml").write_text(yaml.dump({
        "version": "1.0.0", "entry_point": entry,
        "services": [{"name": "github", "events": True}],
    }))
    wf = pack_dir / "workflows"
    wf.mkdir(exist_ok=True)
    if with_adhoc:
        (wf / "adhoc.yaml").write_text(yaml.dump({
            "name": "adhoc", "trigger": "Any ad-hoc task.",
            "description": "Open-ended task.",
            "steps": [{"name": "task", "prompt": "${{input.task}}"}],
        }))


@pytest.fixture
def project(tmp_path):
    return tmp_path


@pytest.fixture
def build_state():
    answers = {k: "none" for k in INTERVIEW_KEYS}
    answers.update(purpose="p", roles="r", services="github", chat="none")
    return SetupState(branch="build", team_name="my-team", answers=answers)


class TestStageGating:
    def test_out_of_stage_call_refused(self, project):
        state = SetupState()  # stage: choose
        tools = _tools(state, project)
        result = _call(tools["record_answer"], question="purpose", answer="x")
        assert result.get("is_error")
        assert "interview" in _text(result)

    def test_every_gated_tool_refuses_outside_its_stage(self, project):
        state = SetupState(stage=Stage.DONE)
        tools = _tools(state, project)
        for name in ["list_teams", "get_team_details", "select_team",
                     "record_answer", "save_credential", "check_venn",
                     "venn_cli", "record_monitor", "skip_discovery",
                     "validate_team", "install_team"]:
            result = _call(tools[name], **{k: "x" for k in
                           {"name": 1, "branch": 1, "question": 1, "answer": 1,
                            "var_name": 1, "service": 1, "instructions": 1,
                            "args": 1, "command": 1, "description": 1,
                            "interval": 1, "event": 1, "reason": 1,
                            "services": 1}})
            assert result.get("is_error"), f"{name} acted out of stage"


class TestChoose:
    def test_list_teams_includes_local_packs(self, project, monkeypatch):
        _write_minimal_pack(project / "agents" / "local-team")
        monkeypatch.setattr("modastack.registry.list_remote", lambda _: [])
        state = SetupState()
        result = _call(_tools(state, project)["list_teams"])
        teams = json.loads(_text(result))
        assert any(t.get("name") == "local-team" for t in teams)

    def test_select_build_rejects_bad_slug(self, project):
        state = SetupState()
        result = _call(_tools(state, project)["select_team"],
                       name="My Team!", branch="build")
        assert result.get("is_error")

    def test_select_build_rejects_existing_dir(self, project):
        _write_minimal_pack(project / "agents" / "taken")
        state = SetupState()
        result = _call(_tools(state, project)["select_team"],
                       name="taken", branch="build")
        assert result.get("is_error")
        assert "already exists" in _text(result)

    def test_select_build_commits_state(self, project):
        state = SetupState()
        result = _call(_tools(state, project)["select_team"],
                       name="sales-outreach", branch="build")
        assert not result.get("is_error")
        assert state.branch == "build"
        assert state.team_name == "sales-outreach"
        # checkpointed to disk
        assert SetupState.load(project).team_name == "sales-outreach"

    def test_select_use_as_is_requires_resolvable_team(self, project, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("offline")
        monkeypatch.setattr("modastack.registry.fetch", boom)
        state = SetupState()
        result = _call(_tools(state, project)["select_team"],
                       name="ghost-team", branch="use-as-is")
        assert result.get("is_error")


class TestInterview:
    def test_record_answer_tracks_remaining(self, project):
        state = SetupState(stage=Stage.INTERVIEW, branch="build", team_name="t")
        tools = _tools(state, project)
        result = _call(tools["record_answer"], question="purpose", answer="do x")
        assert "still unrecorded" in _text(result)
        for k in INTERVIEW_KEYS[1:]:
            _call(tools["record_answer"], question=k, answer="none")
        result = _call(tools["record_answer"], question="purpose", answer="do x")
        assert "all seven" in _text(result)

    def test_unknown_question_refused(self, project):
        state = SetupState(stage=Stage.INTERVIEW, branch="build", team_name="t")
        result = _call(_tools(state, project)["record_answer"],
                       question="favorite_color", answer="blue")
        assert result.get("is_error")


class TestSaveCredential:
    def test_secret_written_to_env_never_returned(self, project, monkeypatch):
        # Register the var with monkeypatch so the handler's os.environ
        # write is rolled back after the test.
        monkeypatch.setenv("SLACK_BOT_TOKEN", "placeholder")
        monkeypatch.delenv("SLACK_BOT_TOKEN")
        state = SetupState(stage=Stage.SERVICES)
        secret = "xoxb-very-secret-value-12345"
        tools = _tools(state, project, prompt_fn=lambda v, s, i: secret)
        result = _call(tools["save_credential"], var_name="SLACK_BOT_TOKEN",
                       service="slack", instructions="paste the bot token")
        payload = json.loads(_text(result))
        assert payload["saved"] is True
        assert secret not in json.dumps(result)
        env_text = (project / ".modastack" / ".env").read_text()
        assert f"SLACK_BOT_TOKEN={secret}" in env_text
        assert "SLACK_BOT_TOKEN" in state.credentials_saved

    def test_empty_input_is_skip(self, project):
        state = SetupState(stage=Stage.SERVICES)
        tools = _tools(state, project, prompt_fn=lambda v, s, i: "")
        result = _call(tools["save_credential"], var_name="LINEAR_API_KEY",
                       service="linear", instructions="")
        assert json.loads(_text(result))["skipped"] is True
        assert not (project / ".modastack" / ".env").exists()

    def test_merges_with_existing_env(self, project, monkeypatch):
        monkeypatch.setenv("NEW_VAR", "placeholder")
        monkeypatch.delenv("NEW_VAR")
        env = project / ".modastack" / ".env"
        env.parent.mkdir(parents=True)
        env.write_text("EXISTING=keep\n")
        state = SetupState(stage=Stage.SERVICES)
        tools = _tools(state, project, prompt_fn=lambda v, s, i: "val")
        _call(tools["save_credential"], var_name="NEW_VAR", service="", instructions="")
        text = env.read_text()
        assert "EXISTING=keep" in text and "NEW_VAR=val" in text

    def test_bad_var_name_refused(self, project):
        state = SetupState(stage=Stage.SERVICES)
        tools = _tools(state, project, prompt_fn=lambda v, s, i: "x")
        result = _call(tools["save_credential"], var_name="not-a-var",
                       service="", instructions="")
        assert result.get("is_error")

    def test_framework_vars_refused(self, project):
        state = SetupState(stage=Stage.SERVICES)
        tools = _tools(state, project, prompt_fn=lambda v, s, i: "x")
        result = _call(tools["save_credential"],
                       var_name="MODASTACK_VENN_API_BASE",
                       service="", instructions="")
        assert result.get("is_error")

    def test_refreshes_process_environment(self, project, monkeypatch):
        # load_dotenv never overwrites os.environ, so a corrected
        # credential must be pushed there by save_credential itself.
        import os
        monkeypatch.setenv("LINEAR_API_KEY", "stale-value")
        state = SetupState(stage=Stage.SERVICES)
        tools = _tools(state, project, prompt_fn=lambda v, s, i: "fresh-value")
        _call(tools["save_credential"], var_name="LINEAR_API_KEY",
              service="linear", instructions="")
        assert os.environ["LINEAR_API_KEY"] == "fresh-value"


class TestVennTools:
    def test_check_venn_requires_key(self, project, monkeypatch):
        monkeypatch.delenv("VENN_API_KEY", raising=False)
        state = SetupState(stage=Stage.SERVICES)
        result = _call(_tools(state, project)["check_venn"], services=["email"])
        assert result.get("is_error")
        assert "save_credential" in _text(result)

    def test_check_venn_reports_connections(self, project, monkeypatch):
        env = project / ".modastack" / ".env"
        env.parent.mkdir(parents=True)
        env.write_text("VENN_API_KEY=k\n")
        from modastack.venn import ServiceCheck
        monkeypatch.setattr(
            "modastack.venn.check_services",
            lambda key, req: ServiceCheck(connected=["email"], missing=["crm"]))
        state = SetupState(stage=Stage.SERVICES)
        result = _call(_tools(state, project)["check_venn"],
                       services=["email", "crm"])
        payload = json.loads(_text(result))
        assert payload == {"connected": ["email"], "missing": ["crm"]}

    def test_venn_cli_passes_key_and_relays_refusal(self, project, monkeypatch):
        env = project / ".modastack" / ".env"
        env.parent.mkdir(parents=True)
        env.write_text("VENN_API_KEY=k\n")
        from modastack.setup.venn_cli import VennResult
        seen = {}
        def fake_run(args, key, timeout=60):
            seen["args"], seen["key"] = args, key
            return VennResult(ok=True, output="[]")
        monkeypatch.setattr("modastack.setup.tools.run_venn", fake_run)
        state = SetupState(stage=Stage.DISCOVERY)
        result = _call(_tools(state, project)["venn_cli"], args="help list_servers")
        assert not result.get("is_error")
        assert seen == {"args": "help list_servers", "key": "k"}


class TestRecordMonitor:
    def test_requires_command_or_description(self, project):
        state = SetupState(stage=Stage.DISCOVERY)
        result = _call(_tools(state, project)["record_monitor"],
                       name="m", command="", description="",
                       interval="5m", event="x/y")
        assert result.get("is_error")

    def test_unparseable_interval_refused(self, project):
        state = SetupState(stage=Stage.DISCOVERY)
        result = _call(_tools(state, project)["record_monitor"],
                       name="m", command="venn ...", description="",
                       interval="every 15 minutes", event="x/y")
        assert result.get("is_error")
        assert "interval" in _text(result).lower()

    def test_records_and_replaces_by_name(self, project):
        state = SetupState(stage=Stage.DISCOVERY)
        tools = _tools(state, project)
        _call(tools["record_monitor"], name="new-emails",
              command="venn tools execute -s g -t list -a '{}'",
              description="", interval="5m", event="email/received")
        _call(tools["record_monitor"], name="new-emails",
              command="venn tools execute -s g -t list -a '{\"q\": \"is:unread\"}'",
              description="", interval="10m", event="email/received")
        assert len(state.monitors_recorded) == 1
        assert state.monitors_recorded[0]["interval"] == "10m"


class TestValidateTeam:
    def test_passing_pack_sets_validated(self, project, build_state):
        build_state.stage = Stage.GENERATE
        _write_minimal_pack(project / "agents" / "my-team")
        result = _call(_tools(build_state, project)["validate_team"])
        assert "validation passed" in _text(result)
        assert build_state.validated is True
        assert build_state.validated_hash

    def test_missing_adhoc_fails(self, project, build_state):
        build_state.stage = Stage.GENERATE
        build_state.validated_hash = "left-over-from-earlier-pass"
        _write_minimal_pack(project / "agents" / "my-team", with_adhoc=False)
        result = _call(_tools(build_state, project)["validate_team"])
        assert "FAILED" in _text(result)
        assert "adhoc.yaml" in _text(result)
        assert build_state.validated is False
        assert build_state.validated_hash == ""

    def test_saved_secret_value_detected(self, project, build_state):
        # Exact-value scan catches secrets no vendor-prefix shape knows.
        build_state.stage = Stage.GENERATE
        env = project / ".modastack" / ".env"
        env.parent.mkdir(parents=True)
        env.write_text("STRIPE_KEY=zq_live_totally_novel_shape_123\n")
        pack = project / "agents" / "my-team"
        _write_minimal_pack(pack)
        (pack / "agent.md").write_text(
            "# Team\nUses key zq_live_totally_novel_shape_123 for Stripe.")
        result = _call(_tools(build_state, project)["validate_team"])
        assert "literal secret" in _text(result)

    def test_bad_monitor_interval_in_pack_fails(self, project, build_state):
        build_state.stage = Stage.GENERATE
        pack = project / "agents" / "my-team"
        _write_minimal_pack(pack)
        (pack / "monitors").mkdir()
        (pack / "monitors" / "defaults.yaml").write_text(yaml.dump({
            "monitors": [{"name": "m", "command": "venn ...",
                          "interval": "hourly", "event": "e/f"}]}))
        result = _call(_tools(build_state, project)["validate_team"])
        assert "FAILED" in _text(result)
        assert "defaults.yaml" in _text(result)

    def test_bad_workflow_yaml_fails(self, project, build_state):
        build_state.stage = Stage.GENERATE
        pack = project / "agents" / "my-team"
        _write_minimal_pack(pack)
        (pack / "workflows" / "broken.yaml").write_text(
            yaml.dump({"name": "broken", "steps": [{"prompt": "no name"}]}))
        result = _call(_tools(build_state, project)["validate_team"])
        assert "FAILED" in _text(result)
        assert "broken.yaml" in _text(result)

    def test_missing_entry_point_role_fails(self, project, build_state):
        build_state.stage = Stage.GENERATE
        pack = project / "agents" / "my-team"
        _write_minimal_pack(pack)
        (pack / "agent.yaml").write_text(yaml.dump({"entry_point": "ghost"}))
        result = _call(_tools(build_state, project)["validate_team"])
        assert "ghost" in _text(result)
        assert "FAILED" in _text(result)

    def test_literal_secret_fails(self, project, build_state):
        build_state.stage = Stage.GENERATE
        pack = project / "agents" / "my-team"
        _write_minimal_pack(pack)
        cfg = yaml.safe_load((pack / "agent.yaml").read_text())
        cfg["slack"] = {"bot_token": "xoxb-1234567890-abcdef"}
        (pack / "agent.yaml").write_text(yaml.dump(cfg))
        result = _call(_tools(build_state, project)["validate_team"])
        assert "literal secret" in _text(result)

    def test_recorded_monitors_must_be_written(self, project, build_state):
        build_state.stage = Stage.GENERATE
        build_state.monitors_recorded = [
            {"name": "m", "command": "venn ...", "interval": "5m", "event": "e/f"}]
        _write_minimal_pack(project / "agents" / "my-team")
        result = _call(_tools(build_state, project)["validate_team"])
        assert "defaults.yaml is missing" in _text(result)


class TestInstallTeam:
    def test_installs_and_reports_missing_credentials(self, project, build_state):
        pack = project / "agents" / "my-team"
        _write_minimal_pack(pack)
        cfg = yaml.safe_load((pack / "agent.yaml").read_text())
        cfg["slack"] = {"bot_token": "${SLACK_BOT_TOKEN}"}
        (pack / "agent.yaml").write_text(yaml.dump(cfg))

        from modastack.setup.state import source_tree_hash
        build_state.stage = Stage.INSTALL
        build_state.validated = True
        build_state.validated_hash = source_tree_hash(pack)

        result = _call(_tools(build_state, project)["install_team"])
        payload = json.loads(_text(result))
        assert payload["installed"] == "my-team"
        assert "SLACK_BOT_TOKEN" in payload["missing_credentials"]
        assert (project / ".modastack" / "agent.yaml").exists()
        assert (project / ".modastack" / "install-manifest.json").exists()
        assert (project / ".modastack" / ".gitignore").exists()
        assert build_state.installed is True

    def test_stale_validation_refused(self, project, build_state):
        pack = project / "agents" / "my-team"
        _write_minimal_pack(pack)
        build_state.stage = Stage.INSTALL
        build_state.validated = True
        build_state.validated_hash = "old-hash"
        result = _call(_tools(build_state, project)["install_team"])
        assert result.get("is_error")
        assert "changed since" in _text(result)
        assert build_state.validated is False


class TestFlowControl:
    def test_advance_refusal_is_actionable(self, project):
        state = SetupState()
        result = _call(_tools(state, project)["advance_stage"],
                       to="interview", summary="")
        assert result.get("is_error")
        assert "select_team" in _text(result)

    def test_advance_moves_and_persists(self, project):
        state = SetupState(branch="build", team_name="t")
        result = _call(_tools(state, project)["advance_stage"],
                       to="interview", summary="building t")
        assert not result.get("is_error")
        assert state.stage == Stage.INTERVIEW
        assert SetupState.load(project).stage == Stage.INTERVIEW

    def test_unknown_stage_refused(self, project):
        state = SetupState()
        result = _call(_tools(state, project)["advance_stage"],
                       to="profit", summary="")
        assert result.get("is_error")

    def test_finish_only_from_done(self, project):
        state = SetupState(stage=Stage.INSTALL)
        result = _call(_tools(state, project)["finish_setup"], message="bye")
        assert result.get("is_error")
        state.stage = Stage.DONE
        result = _call(_tools(state, project)["finish_setup"], message="bye")
        assert not result.get("is_error")
        assert state.finished is True
