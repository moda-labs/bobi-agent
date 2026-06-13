"""Tests for the setup state machine — gating rules and persistence."""

from pathlib import Path

import pytest

from modastack.setup.state import (
    INTERVIEW_KEYS,
    SetupState,
    Stage,
    source_tree_hash,
)


def _interviewed_state(**overrides) -> SetupState:
    """A state that has completed the interview for branch 'build'."""
    answers = {k: "none" for k in INTERVIEW_KEYS}
    answers.update(purpose="manage sales outreach",
                   roles="researcher, copywriter",
                   services="salesforce, email",
                   chat="slack")
    s = SetupState(stage=Stage.INTERVIEW, branch="build",
                   team_name="sales-outreach", answers=answers)
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


class TestRequireStage:
    def test_allows_current_stage(self):
        s = SetupState()
        assert s.require_stage(Stage.CHOOSE) is None

    def test_allows_any_listed_stage(self):
        s = SetupState(stage=Stage.DISCOVERY)
        assert s.require_stage(Stage.SERVICES, Stage.DISCOVERY) is None

    def test_refuses_other_stage_with_reason(self):
        s = SetupState()
        reason = s.require_stage(Stage.GENERATE)
        assert "generate" in reason and "choose" in reason


class TestChooseTransitions:
    def test_choose_to_interview_requires_build_branch(self):
        s = SetupState()
        assert "select_team" in s.can_advance(Stage.INTERVIEW)
        s.branch = "use-as-is"
        s.team_name = "eng-team"
        assert s.can_advance(Stage.INTERVIEW) is not None

    def test_choose_to_interview_ok_for_build(self):
        s = SetupState(branch="build", team_name="sales-outreach")
        assert s.can_advance(Stage.INTERVIEW) is None

    def test_use_as_is_jumps_to_install(self):
        s = SetupState(branch="use-as-is", team_name="eng-team")
        assert s.can_advance(Stage.INSTALL) is None

    def test_build_may_not_jump_to_install(self):
        s = SetupState(branch="build", team_name="sales-outreach")
        assert s.can_advance(Stage.INSTALL) is not None

    def test_jump_requires_selected_team(self):
        s = SetupState(branch="use-as-is")
        assert "select_team" in s.can_advance(Stage.INSTALL)


class TestInterviewGate:
    def test_incomplete_interview_refused_with_gaps(self):
        s = SetupState(stage=Stage.INTERVIEW, branch="build", team_name="x",
                       answers={"purpose": "do things"})
        reason = s.can_advance(Stage.SERVICES)
        assert "roles" in reason and "chat" in reason

    def test_blank_required_answer_refused(self):
        s = _interviewed_state()
        s.answers["purpose"] = "  "
        assert "purpose" in s.can_advance(Stage.SERVICES)

    def test_optional_questions_may_be_none_but_must_be_recorded(self):
        s = _interviewed_state()
        del s.answers["gates"]
        assert "gates" in s.can_advance(Stage.SERVICES)
        s.answers["gates"] = "none"
        assert s.can_advance(Stage.SERVICES) is None


class TestDiscoveryAndGenerate:
    def test_services_to_discovery_is_open(self):
        s = _interviewed_state(stage=Stage.SERVICES)
        assert s.can_advance(Stage.DISCOVERY) is None

    def test_skipping_discovery_needs_a_reason(self):
        s = _interviewed_state(stage=Stage.SERVICES)
        assert "skip_discovery" in s.can_advance(Stage.GENERATE)
        s.discovery_skipped_reason = "no venn event sources"
        assert s.can_advance(Stage.GENERATE) is None

    def test_leaving_discovery_requires_monitors_or_skip(self):
        s = _interviewed_state(stage=Stage.DISCOVERY)
        assert "record_monitor" in s.can_advance(Stage.GENERATE)
        s.monitors_recorded = [{"name": "m", "command": "venn ...",
                                "interval": "5m", "event": "e/f"}]
        assert s.can_advance(Stage.GENERATE) is None

    def test_leaving_discovery_with_skip_reason(self):
        s = _interviewed_state(stage=Stage.DISCOVERY,
                               discovery_skipped_reason="venn CLI missing")
        assert s.can_advance(Stage.GENERATE) is None

    def test_generate_to_install_requires_validation(self):
        s = _interviewed_state(stage=Stage.GENERATE)
        assert "validate_team" in s.can_advance(Stage.INSTALL)
        s.validated = True
        assert s.can_advance(Stage.INSTALL) is None


class TestInstallAndDone:
    def test_done_requires_installed(self):
        s = _interviewed_state(stage=Stage.INSTALL, validated=True)
        assert "install" in s.can_advance(Stage.DONE)
        s.installed = True
        assert s.can_advance(Stage.DONE) is None

    def test_no_stage_skipping(self):
        s = _interviewed_state(stage=Stage.INTERVIEW)
        assert "in order" in s.can_advance(Stage.GENERATE)

    def test_no_backward_moves(self):
        s = _interviewed_state(stage=Stage.GENERATE)
        assert s.can_advance(Stage.INTERVIEW) is not None

    def test_self_transition_refused(self):
        s = SetupState()
        assert "already" in s.can_advance(Stage.CHOOSE)


class TestPersistence:
    def test_round_trip(self, tmp_path):
        s = _interviewed_state(stage=Stage.SERVICES, session_id="abc-123",
                               credentials_saved=["SLACK_BOT_TOKEN"])
        s.save(tmp_path)
        loaded = SetupState.load(tmp_path)
        assert loaded.stage == Stage.SERVICES
        assert loaded.branch == "build"
        assert loaded.answers["purpose"] == "manage sales outreach"
        assert loaded.session_id == "abc-123"
        assert loaded.credentials_saved == ["SLACK_BOT_TOKEN"]

    def test_load_missing_returns_none(self, tmp_path):
        assert SetupState.load(tmp_path) is None

    def test_load_corrupt_returns_none(self, tmp_path):
        f = tmp_path / ".modastack" / "state" / "setup.json"
        f.parent.mkdir(parents=True)
        f.write_text("{not json")
        assert SetupState.load(tmp_path) is None

    def test_clear(self, tmp_path):
        SetupState().save(tmp_path)
        SetupState.clear(tmp_path)
        assert SetupState.load(tmp_path) is None

    def test_ignores_unknown_fields(self, tmp_path):
        s = SetupState()
        s.save(tmp_path)
        f = tmp_path / ".modastack" / "state" / "setup.json"
        import json
        data = json.loads(f.read_text())
        data["from_the_future"] = True
        f.write_text(json.dumps(data))
        assert SetupState.load(tmp_path) is not None


class TestSourceTreeHash:
    def test_stable_and_content_sensitive(self, tmp_path):
        pack = tmp_path / "agents" / "x"
        (pack / "roles" / "manager").mkdir(parents=True)
        (pack / "agent.yaml").write_text("entry_point: manager\n")
        (pack / "roles" / "manager" / "ROLE.md").write_text("# Manager\n")

        h1 = source_tree_hash(pack)
        assert h1 == source_tree_hash(pack)

        (pack / "agent.yaml").write_text("entry_point: lead\n")
        assert source_tree_hash(pack) != h1

    def test_missing_dir_is_empty(self, tmp_path):
        assert source_tree_hash(tmp_path / "nope") == ""
