"""Decision log prompts: framework base contract + role-specific usage.

Issue #175: the director derived 'what I manage' from session records,
which resurrected stale launch records on restart. The decision log is
now a framework-level concept (base.md) with role-specific extensions
in eng-team director and project lead prompts.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_PROMPT = REPO_ROOT / "modastack" / "prompts" / "base.md"
DIRECTOR_PROMPT = REPO_ROOT / "agents" / "eng-team" / "roles" / "director" / "ROLE.md"
LEAD_PROMPT = REPO_ROOT / "agents" / "eng-team" / "roles" / "project_lead" / "ROLE.md"


class TestBaseDecisionLogContract:
    """The framework base prompt must define the decision log contract for all agents."""

    def setup_method(self):
        self.text = BASE_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_has_decision_log_section(self):
        assert "decision log" in self.lower, (
            "Base prompt must have a decision log section"
        )

    def test_documents_index_md_structure(self):
        assert "INDEX.md" in self.text, (
            "Base prompt must document INDEX.md structure"
        )

    def test_has_startup_section(self):
        assert "on startup" in self.lower, (
            "Base prompt must instruct agents to read decision log on startup"
        )

    def test_reads_before_processing_events(self):
        assert "before processing" in self.lower, (
            "Base prompt must tell agents to read the log before processing events"
        )

    def test_has_preference_recording_section(self):
        assert "recording preferences" in self.lower, (
            "Base prompt must have a section on recording preferences"
        )

    def test_requires_provenance(self):
        assert "provenance" in self.lower, (
            "Base prompt must require provenance on recorded entries"
        )

    def test_survives_session_rotation(self):
        assert "survive" in self.lower and "rotation" in self.lower, (
            "Base prompt must state the decision log survives session rotation"
        )


class TestDirectorDecisionLog:
    """The director prompt must define the decision log as source of truth."""

    def setup_method(self):
        self.text = DIRECTOR_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_defines_decision_log_section(self):
        assert "decision log" in self.lower, (
            "Director prompt must have a decision log section"
        )

    def test_decision_log_is_source_of_truth(self):
        assert "source of truth" in self.lower, (
            "Director prompt must declare the decision log as source of truth"
        )

    def test_defines_managed_repos_yaml_block(self):
        assert "managed_repos" in self.text, (
            "Director prompt must define managed_repos in the YAML block"
        )

    def test_index_md_structure_documented(self):
        assert "INDEX.md" in self.text, (
            "Director prompt must document the INDEX.md structure"
        )


class TestDirectorStartupReconciliation:
    """On startup the director must reconcile the log against live agents."""

    def setup_method(self):
        self.text = DIRECTOR_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_has_reconciliation_section(self):
        assert "startup reconciliation" in self.lower, (
            "Director prompt must have a startup reconciliation section"
        )

    def test_reads_decision_log_on_startup(self):
        assert "read" in self.lower and "decision log" in self.lower, (
            "Director prompt must read the decision log on startup"
        )

    def test_checks_live_agents(self):
        assert "modastack agents list" in self.lower, (
            "Director prompt must check live agents during reconciliation"
        )

    def test_relaunches_missing_leads(self):
        assert "relaunch" in self.lower, (
            "Director prompt must relaunch leads missing from live agents"
        )

    def test_cancels_stale_leads(self):
        assert "cancel" in self.lower and "stale" in self.lower, (
            "Director prompt must cancel stale leads not in the decision log"
        )

    def test_never_replays_old_sessions(self):
        assert "never replay" in self.lower, (
            "Director prompt must explicitly forbid replaying old session records"
        )


class TestDirectorOnboardingProvenance:
    """Onboarding must write to the decision log with provenance."""

    def setup_method(self):
        self.text = DIRECTOR_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_writes_to_log_before_launching(self):
        # The prompt must instruct writing to the log before launching
        write_pos = self.lower.find("write to the decision log")
        launch_pos = self.lower.find("launch a project lead")
        assert write_pos != -1 and launch_pos != -1, (
            "Director prompt must mention both writing to log and launching"
        )
        assert write_pos < launch_pos, (
            "Director prompt must write to the decision log BEFORE launching the lead"
        )

    def test_requires_provenance_on_onboard(self):
        assert "provenance" in self.lower, (
            "Director prompt must require provenance (who, when) on onboard entries"
        )

    def test_offboarding_updates_log(self):
        # Find offboarding section and check it mentions updating the log
        offboard_pos = self.lower.find("offboarding")
        assert offboard_pos != -1, "Director prompt must have offboarding section"
        offboard_text = self.lower[offboard_pos:]
        assert "decision log" in offboard_text, (
            "Offboarding section must update the decision log"
        )


class TestDirectorListFromLog:
    """'What are you managing?' must answer from the decision log."""

    def setup_method(self):
        self.text = DIRECTOR_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_listing_reads_from_log(self):
        # Find the listing section and verify it reads from the log
        listing_pos = self.lower.find("listing managed repos")
        assert listing_pos != -1, "Director prompt must have listing section"
        listing_text = self.lower[listing_pos:]
        assert "decision log" in listing_text or "index.md" in listing_text, (
            "Listing section must read from the decision log"
        )

    def test_listing_cross_checks_live_status(self):
        listing_pos = self.lower.find("listing managed repos")
        assert listing_pos != -1
        listing_text = self.lower[listing_pos:]
        assert "cross-check" in listing_text or "modastack agents list" in listing_text, (
            "Listing must cross-check with live agent status"
        )


class TestDirectorHumanPreferences:
    """Human preferences must be recorded to survive session rotation."""

    def setup_method(self):
        self.text = DIRECTOR_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_has_preferences_section(self):
        assert "human preferences" in self.lower or "recording human preferences" in self.lower, (
            "Director prompt must have a section on recording human preferences"
        )

    def test_records_with_provenance(self):
        # Preferences section must mention provenance
        pref_pos = self.lower.find("recording human preferences")
        assert pref_pos != -1
        pref_text = self.lower[pref_pos:]
        assert "user_id" in pref_text or "who said" in pref_text or "provenance" in pref_text, (
            "Preferences must be recorded with provenance"
        )

    def test_survives_rotation(self):
        assert "survive" in self.lower and "rotation" in self.lower, (
            "Director prompt must state preferences survive session rotation"
        )

    def test_applied_on_startup(self):
        pref_pos = self.lower.find("recording human preferences")
        assert pref_pos != -1
        pref_text = self.lower[pref_pos:]
        assert "startup" in pref_text, (
            "Preferences must be applied on startup"
        )


class TestProjectLeadDecisionLog:
    """Project lead must use its own decision log for operational state."""

    def setup_method(self):
        self.text = LEAD_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_has_decision_log_section(self):
        assert "decision log" in self.lower, (
            "Project lead prompt must have a decision log section"
        )

    def test_records_standing_instructions(self):
        assert "standing instruction" in self.lower, (
            "Project lead prompt must mention recording standing instructions"
        )

    def test_reads_on_startup(self):
        assert "on startup" in self.lower or "before processing events" in self.lower, (
            "Project lead prompt must read the decision log on startup"
        )
