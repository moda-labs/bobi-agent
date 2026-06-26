"""Prompt contracts for durable team knowledge: framework base + role usage.

Issue #175: the director derived 'what I manage' from session records,
which resurrected stale launch records on restart, so durable knowledge
became a prompt-level concept.

#456/#460: the framework base contract is now the **team-policy** model — a
curator-maintained, read-only ``policy.md`` injected as ``## Team Policy`` —
replacing the old agent-maintained decision log (the bloat source behind the
rotation wedge). Durable knowledge is made persistent by stating it plainly in
the transcript (the ``policy-curator`` distills it); agents never self-maintain
a per-session log. Volatile operational state (live leads, in-flight tickets)
is re-derived from source (GitHub/Linear/``agents list``), not stored. The
eng-team director/project-lead role prompts have been migrated to this model;
the contracts below assert the policy-model behavior.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_PROMPT = REPO_ROOT / "bobi" / "prompts" / "base.md"
DIRECTOR_PROMPT = REPO_ROOT / "agents" / "eng-team" / "roles" / "director" / "ROLE.md"
LEAD_PROMPT = REPO_ROOT / "agents" / "eng-team" / "roles" / "project_lead" / "ROLE.md"


class TestBasePolicyContract:
    """The framework base prompt must define the read-only team-policy contract."""

    def setup_method(self):
        self.text = BASE_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_has_team_policy_section(self):
        assert "## team policy" in self.lower, (
            "Base prompt must have a Team Policy section"
        )

    def test_policy_is_read_only(self):
        assert "read-only" in self.lower or "read only" in self.lower, (
            "Base prompt must state Team Policy is injected read-only"
        )

    def test_agents_do_not_write_policy(self):
        assert "you do not write it" in self.lower or "do not edit" in self.lower, (
            "Base prompt must tell agents they do not write the policy"
        )

    def test_curator_is_single_writer(self):
        assert "policy-curator" in self.lower or "curator" in self.lower, (
            "Base prompt must name the policy-curator as the writer"
        )

    def test_knowledge_made_durable_via_transcript(self):
        assert "transcript" in self.lower, (
            "Base prompt must explain durability comes from stating things in the transcript"
        )

    def test_volatile_state_rederived_from_source(self):
        assert "re-derived" in self.lower or "rederived" in self.lower, (
            "Base prompt must state volatile state is re-derived from source, not stored"
        )

    def test_no_per_session_journal_or_flush(self):
        assert "no per-session journal" in self.lower or "no flush" in self.lower, (
            "Base prompt must state there is no per-session journal/flush on rotation"
        )


class TestDirectorManagedFromSource:
    """The director must derive 'what I manage' from live source, not a log."""

    def setup_method(self):
        self.text = DIRECTOR_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_no_decision_log(self):
        assert "decision log" not in self.lower, (
            "Director prompt must not reference a decision log under the policy model"
        )

    def test_no_index_md(self):
        assert "index.md" not in self.lower, (
            "Director prompt must not reference INDEX.md under the policy model"
        )

    def test_managed_derived_from_subscriptions(self):
        assert "subscription" in self.lower and "github:" in self.lower, (
            "Director prompt must derive managed repos from its GitHub subscriptions"
        )

    def test_reads_team_policy_block(self):
        assert "team policy" in self.lower, (
            "Director prompt must reference the read-only Team Policy block"
        )

    def test_does_not_write_policy(self):
        assert "never write it" in self.lower or "never write" in self.lower, (
            "Director prompt must state durable knowledge is read but never written by the director"
        )


class TestDirectorStartupReconciliation:
    """On startup the director reconciles subscriptions against live agents."""

    def setup_method(self):
        self.text = DIRECTOR_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_has_reconciliation_section(self):
        assert "startup reconciliation" in self.lower, (
            "Director prompt must have a startup reconciliation section"
        )

    def test_derives_from_subscriptions(self):
        assert "subscription" in self.lower, (
            "Director prompt must derive managed repos from configured subscriptions on startup"
        )

    def test_checks_live_agents(self):
        assert "bobi agents list" in self.lower, (
            "Director prompt must check live agents during reconciliation"
        )

    def test_relaunches_missing_leads(self):
        assert "relaunch" in self.lower, (
            "Director prompt must relaunch leads missing from live agents"
        )

    def test_cancels_stale_leads(self):
        assert "cancel" in self.lower and "stale" in self.lower, (
            "Director prompt must cancel stale leads not corresponding to a managed repo"
        )

    def test_never_replays_old_sessions(self):
        assert "never replay" in self.lower, (
            "Director prompt must explicitly forbid replaying old session transcripts"
        )


class TestDirectorOnboardingProvenance:
    """Onboarding launches a lead and surfaces provenance in the transcript."""

    def setup_method(self):
        self.text = DIRECTOR_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_launches_a_lead(self):
        assert "launch a project lead" in self.lower, (
            "Onboarding must launch a project lead"
        )

    def test_no_decision_log_write_step(self):
        assert "write to the decision log" not in self.lower, (
            "Onboarding must not write to a decision log under the policy model"
        )

    def test_subscription_is_durable_routing_record(self):
        assert "subscription" in self.lower, (
            "Onboarding must treat the lead's subscription as the durable routing record"
        )

    def test_surfaces_provenance_in_transcript(self):
        # Provenance is stated plainly in the transcript for the curator,
        # not written to any file.
        assert "transcript" in self.lower and "user_id" in self.lower, (
            "Onboarding must state provenance (who, when) plainly in the transcript"
        )

    def test_offboarding_cancels_lead_no_log(self):
        offboard_pos = self.lower.find("offboarding")
        assert offboard_pos != -1, "Director prompt must have an offboarding section"
        offboard_text = self.lower[offboard_pos:offboard_pos + 600]
        assert "cancel" in offboard_text, (
            "Offboarding must cancel the lead"
        )
        assert "decision log" not in offboard_text, (
            "Offboarding must not update a decision log under the policy model"
        )
        assert "transcript" in offboard_text, (
            "Offboarding must note the offboard plainly in the transcript for provenance"
        )


class TestDirectorListFromLiveSource:
    """'What are you managing?' must answer from live source, not a log."""

    def setup_method(self):
        self.text = DIRECTOR_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_listing_reads_live(self):
        listing_pos = self.lower.find("listing managed repos")
        assert listing_pos != -1, "Director prompt must have a listing section"
        listing_text = self.lower[listing_pos:listing_pos + 800]
        assert "subscription" in listing_text, (
            "Listing must answer from the director's configured subscriptions"
        )
        assert "decision log" not in listing_text and "index.md" not in listing_text, (
            "Listing must not read from a decision log under the policy model"
        )

    def test_listing_uses_agents_list_for_status(self):
        listing_pos = self.lower.find("listing managed repos")
        assert listing_pos != -1
        listing_text = self.lower[listing_pos:listing_pos + 800]
        assert "bobi agents list" in listing_text, (
            "Listing must annotate live status from bobi agents list"
        )


class TestDirectorHumanPreferences:
    """Human preferences flow to the curated Team Policy via the transcript."""

    def setup_method(self):
        self.text = DIRECTOR_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_has_preferences_section(self):
        assert "human preferences" in self.lower, (
            "Director prompt must have a section on human preferences"
        )

    def test_preferences_stated_in_transcript_with_provenance(self):
        pref_pos = self.lower.find("human preferences and standing instructions")
        assert pref_pos != -1, "Director prompt must have the preferences section"
        pref_text = self.lower[pref_pos:pref_pos + 800]
        assert "transcript" in pref_text, (
            "Preferences must be stated plainly in the transcript"
        )
        assert "user_id" in pref_text, (
            "Preferences must include provenance (who said it via Slack user_id)"
        )

    def test_director_does_not_maintain_preferences(self):
        pref_pos = self.lower.find("human preferences and standing instructions")
        assert pref_pos != -1
        pref_text = self.lower[pref_pos:pref_pos + 800]
        assert "maintain a preferences section" in pref_text, (
            "Director must NOT maintain a preferences section itself"
        )

    def test_preferences_fold_into_team_policy(self):
        pref_pos = self.lower.find("human preferences and standing instructions")
        assert pref_pos != -1
        pref_text = self.lower[pref_pos:pref_pos + 800]
        assert "policy-curator" in pref_text and "team policy" in pref_text, (
            "Preferences must be folded into the read-only Team Policy by the curator"
        )


class TestProjectLeadDurableKnowledge:
    """Project lead's durable knowledge is the read-only Team Policy, not a log."""

    def setup_method(self):
        self.text = LEAD_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_no_decision_log(self):
        assert "decision log" not in self.lower, (
            "Project lead prompt must not reference a decision log under the policy model"
        )

    def test_no_index_md(self):
        assert "index.md" not in self.lower, (
            "Project lead prompt must not reference INDEX.md under the policy model"
        )

    def test_reads_team_policy_block(self):
        assert "team policy" in self.lower, (
            "Project lead prompt must reference the read-only Team Policy block"
        )

    def test_durability_via_transcript(self):
        assert "transcript" in self.lower, (
            "Project lead must make knowledge durable by stating it in the transcript"
        )

    def test_records_standing_instructions(self):
        assert "standing instruction" in self.lower, (
            "Project lead prompt must mention surfacing standing instructions"
        )

    def test_volatile_state_rederived(self):
        assert "re-derived" in self.lower or "rederived" in self.lower, (
            "Project lead must not store volatile state — it is re-derived from source"
        )
