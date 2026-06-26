"""Tests for the setup state machine — the 8-stage create spine, soft
readiness, gating, the accumulating Spec, and persistence."""

import pytest

from bobi.setup.state import (
    SPEC_SLOTS,
    Readiness,
    SetupState,
    Spec,
    Stage,
    source_tree_hash,
)


def _goaled(**overrides) -> SetupState:
    """A state with a non-empty goal (clears the only hard conversation floor)."""
    s = SetupState(team_name="sales-outreach")
    s.spec.goal = "Manage sales outreach end to end."
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


class TestStages:
    def test_stages_in_order(self):
        assert [s.value for s in Stage] == [
            "start", "design", "automate", "connect", "chat",
            "build", "review", "install", "done"]

    def test_default_stage_is_start(self):
        assert SetupState().stage == Stage.START

    def test_chat_is_soft_between_connect_and_build(self):
        s = _goaled(stage=Stage.CONNECT)
        assert s.can_advance(Stage.CHAT) is None        # never gated
        s.stage = Stage.CHAT
        assert s.can_advance(Stage.BUILD) is None        # goal set


class TestRequireStage:
    def test_allows_current_stage(self):
        assert SetupState().require_stage(Stage.START) is None

    def test_allows_any_listed_stage(self):
        s = SetupState(stage=Stage.CONNECT)
        assert s.require_stage(Stage.AUTOMATE, Stage.CONNECT) is None

    def test_refuses_other_stage_with_reason(self):
        reason = SetupState().require_stage(Stage.BUILD)
        assert "build" in reason and "start" in reason


class TestSoftAdvance:
    def test_conversation_stages_advance_without_gates(self):
        # Start → Design → Automate → Connect never blocks: readiness is soft.
        s = SetupState()
        assert s.can_advance(Stage.DESIGN) is None
        s.stage = Stage.DESIGN
        assert s.can_advance(Stage.AUTOMATE) is None
        s.stage = Stage.AUTOMATE
        assert s.can_advance(Stage.CONNECT) is None

    def test_thin_or_empty_slots_do_not_block(self):
        # Everything empty except the goal floor — still free to move on.
        s = _goaled(stage=Stage.DESIGN)
        assert s.spec.readiness_for("roles") == Readiness.EMPTY
        assert s.can_advance(Stage.AUTOMATE) is None
        assert s.can_advance(Stage.CONNECT) is None

    def test_backward_moves_always_allowed(self):
        # The wizard is a re-entrant editor — go back to Design anytime.
        s = _goaled(stage=Stage.CONNECT)
        assert s.can_advance(Stage.DESIGN) is None
        assert s.can_advance(Stage.START) is None

    def test_self_transition_refused(self):
        assert "already" in SetupState().can_advance(Stage.START)


class TestHardFloors:
    def test_build_requires_non_empty_goal(self):
        s = SetupState(stage=Stage.CONNECT)            # goal still empty
        assert "goal" in s.can_advance(Stage.BUILD)
        s.spec.goal = "Do the thing."
        assert s.can_advance(Stage.BUILD) is None

    def test_goal_floor_blocks_forward_jumps_past_build(self):
        # Jumping Design → Install must still clear the Build goal floor.
        s = SetupState(stage=Stage.DESIGN)
        assert "goal" in s.can_advance(Stage.INSTALL)

    def test_install_requires_validation(self):
        s = _goaled(stage=Stage.BUILD)
        assert "validation" in s.can_advance(Stage.INSTALL)
        s.validated = True
        assert s.can_advance(Stage.INSTALL) is None

    def test_done_requires_installed(self):
        s = _goaled(stage=Stage.INSTALL, validated=True)
        assert "installed" in s.can_advance(Stage.DONE)
        s.installed = True
        assert s.can_advance(Stage.DONE) is None

    def test_forward_jump_install_to_done_requires_install(self):
        # Reaching Done by jump still needs validation AND install.
        s = _goaled(stage=Stage.BUILD, validated=True)
        assert "installed" in s.can_advance(Stage.DONE)


class TestAdvanceBlocker:
    def test_blocker_reflects_next_step_only(self):
        # Connect → Chat is never gated; the goal floor only blocks the step
        # into Build, i.e. when sitting in Chat.
        assert SetupState(stage=Stage.CONNECT).advance_blocker() is None
        s = SetupState(stage=Stage.CHAT)
        assert "goal" in s.advance_blocker()
        s.spec.goal = "Do the thing."
        assert s.advance_blocker() is None

    def test_no_blocker_at_terminal_stage(self):
        assert SetupState(stage=Stage.DONE).advance_blocker() is None

    def test_clear_blocker_in_open_conversation_stage(self):
        assert SetupState(stage=Stage.START).advance_blocker() is None


class TestSpecReadiness:
    def test_slots_are_the_canonical_four(self):
        assert SPEC_SLOTS == ("goal", "roles", "autonomous", "services")

    def test_empty_slot_falls_back_to_empty(self):
        spec = Spec()
        assert spec.readiness_for("goal") == Readiness.EMPTY
        assert spec.readiness_for("roles") == Readiness.EMPTY

    def test_populated_slot_falls_back_to_thin(self):
        spec = Spec(goal="something", roles=[{"name": "lead"}])
        assert spec.readiness_for("goal") == Readiness.THIN
        assert spec.readiness_for("roles") == Readiness.THIN

    def test_brain_score_overrides_structural_fallback(self):
        spec = Spec(goal="something", readiness={"goal": "enough"})
        assert spec.readiness_for("goal") == Readiness.ENOUGH

    def test_autonomous_needs_explicit_confirmation(self):
        # An empty autonomous list is a real decision — only "thin" once
        # the user has explicitly confirmed (even confirming "nothing").
        spec = Spec()
        assert spec.readiness_for("autonomous") == Readiness.EMPTY
        spec.autonomous_confirmed = True
        assert spec.readiness_for("autonomous") == Readiness.THIN

    def test_unknown_slot_raises(self):
        with pytest.raises(ValueError):
            Spec().readiness_for("nonsense")


class TestPersistence:
    def test_round_trip_including_nested_spec(self, tmp_path):
        s = _goaled(stage=Stage.AUTOMATE, session_id="abc-123",
                    credentials_saved=["SLACK_BOT_TOKEN"], summary="so far…")
        s.spec.roles = [{"name": "researcher", "responsibility": "find leads"}]
        s.spec.autonomous = [{"description": "morning digest", "leash": "notify",
                              "cadence": "1d"}]
        s.spec.autonomous_confirmed = True
        s.spec.services = [{"name": "salesforce", "status": "implied"}]
        s.spec.readiness = {"goal": "enough"}
        s.messages = [{"role": "user", "content": "build me a thing"}]
        s.save(tmp_path)

        loaded = SetupState.load(tmp_path)
        assert loaded.stage == Stage.AUTOMATE
        assert loaded.mode == "create"
        assert loaded.team_name == "sales-outreach"
        assert isinstance(loaded.spec, Spec)
        assert loaded.spec.goal == "Manage sales outreach end to end."
        assert loaded.spec.roles[0]["responsibility"] == "find leads"
        assert loaded.spec.autonomous_confirmed is True
        assert loaded.spec.readiness_for("goal") == Readiness.ENOUGH
        assert loaded.summary == "so far…"
        assert loaded.messages[0]["content"] == "build me a thing"
        assert loaded.session_id == "abc-123"
        assert loaded.credentials_saved == ["SLACK_BOT_TOKEN"]

    def test_round_trip_phase_and_role_dimensions(self, tmp_path):
        # The interview phase and the four per-role dimensions persist verbatim.
        s = _goaled(phase="role:researcher")
        s.spec.roles = [{"name": "researcher", "responsibility": "find leads",
                         "good_looks_like": "qualified leads daily",
                         "systems": ["salesforce", "email"],
                         "triggers": "every weekday morning",
                         "status": "complete"}]
        s.spec.autonomous = [{"description": "morning digest", "leash": "notify",
                              "cadence": "1d", "role": "researcher",
                              "command": "summarize new leads"}]
        s.save(tmp_path)
        loaded = SetupState.load(tmp_path)
        assert loaded.phase == "role:researcher"
        role = loaded.spec.roles[0]
        assert role["good_looks_like"] == "qualified leads daily"
        assert role["systems"] == ["salesforce", "email"]
        assert role["triggers"] == "every weekday morning"
        assert role["status"] == "complete"
        assert loaded.spec.autonomous[0]["role"] == "researcher"
        assert loaded.spec.autonomous[0]["command"] == "summarize new leads"

    def test_load_missing_returns_none(self, tmp_path):
        assert SetupState.load(tmp_path) is None

    def test_load_corrupt_returns_none(self, tmp_path):
        f = tmp_path / ".bobi" / "state" / "setup.json"
        f.parent.mkdir(parents=True)
        f.write_text("{not json")
        assert SetupState.load(tmp_path) is None

    def test_default_state_round_trips_with_empty_spec(self, tmp_path):
        SetupState().save(tmp_path)
        loaded = SetupState.load(tmp_path)
        assert loaded.stage == Stage.START
        assert isinstance(loaded.spec, Spec)
        assert loaded.spec.goal == ""

    def test_clear(self, tmp_path):
        SetupState().save(tmp_path)
        SetupState.clear(tmp_path)
        assert SetupState.load(tmp_path) is None

    def test_ignores_unknown_fields(self, tmp_path):
        SetupState().save(tmp_path)
        f = tmp_path / ".bobi" / "state" / "setup.json"
        import json
        data = json.loads(f.read_text())
        data["from_the_future"] = True
        data["spec"]["also_from_the_future"] = 42
        f.write_text(json.dumps(data))
        loaded = SetupState.load(tmp_path)
        assert loaded is not None
        assert isinstance(loaded.spec, Spec)


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
