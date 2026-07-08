"""Prompt contracts for durable team knowledge: framework base + role usage.

Issue #175: the director derived 'what I manage' from session records,
which resurrected stale launch records on restart, so durable knowledge
became a prompt-level concept.

#456/#460: the framework base contract is now the **team-policy** model - a
sleep_cycle-maintained, read-only ``long_term_memory.md`` injected as ``## Long-Term Memory`` -
replacing the old agent-maintained decision log (the bloat source behind the
rotation wedge). Durable knowledge is made persistent by stating it plainly in
the transcript (the ``sleep-cycle`` distills it); agents never self-maintain
a per-session log. Volatile operational state (live leads, in-flight tickets)
is re-derived from source (GitHub/Linear/``agents list``), not stored. The
eng-team director and engineer role prompts have been migrated to this model;
the contracts below assert the policy-model behavior.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_PROMPT = REPO_ROOT / "bobi" / "prompts" / "base.md"
DIRECTOR_PROMPT = REPO_ROOT / "agents" / "eng-team" / "roles" / "director" / "ROLE.md"
ENGINEER_PROMPT = REPO_ROOT / "agents" / "eng-team" / "roles" / "engineer" / "ROLE.md"


class TestBasePolicyContract:
    """The framework base prompt must define the read-only team-policy contract."""

    def setup_method(self):
        self.text = BASE_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_has_long_term_memory_section(self):
        assert "## long-term memory" in self.lower, (
            "Base prompt must have a Long-Term Memory section"
        )

    def test_policy_is_read_only(self):
        assert "read-only" in self.lower or "read only" in self.lower, (
            "Base prompt must state Long-Term Memory is injected read-only"
        )

    def test_agents_do_not_write_policy(self):
        assert "you do not write it" in self.lower or "do not edit" in self.lower, (
            "Base prompt must tell agents they do not write the policy"
        )

    def test_sleep_cycle_is_single_writer(self):
        assert "sleep-cycle" in self.lower or "sleep_cycle" in self.lower, (
            "Base prompt must name the sleep-cycle as the writer"
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
    """The director must derive routing/status from live source, not a log."""

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

    def test_managed_derived_from_configuration(self):
        assert "managed repos" in self.lower and "agent.yaml" in self.lower, (
            "Director prompt must derive managed repos from package/configuration"
        )

    def test_reads_long_term_memory_block(self):
        assert "long-term memory" in self.lower, (
            "Director prompt must reference the read-only Long-Term Memory block"
        )

    def test_does_not_write_policy(self):
        assert "never write it" in self.lower or "never write" in self.lower, (
            "Director prompt must state durable knowledge is read but never written by the director"
        )


class TestDirectorStatusModel:
    """Without project leads, status comes from durable/live sources."""

    def setup_method(self):
        self.text = DIRECTOR_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_has_status_model_section(self):
        assert "status model" in self.lower, (
            "Director prompt must have a status model section"
        )

    def test_reads_active_worker_sessions(self):
        assert "active worker sessions" in self.lower, (
            "Director prompt must synthesize status from active worker sessions"
        )

    def test_checks_live_agents(self):
        assert "bobi agent <agent> subagents list" in self.lower, (
            "Director prompt must check live subagents for status"
        )

    def test_reads_workflow_handoffs(self):
        assert "workflow handoffs" in self.lower, (
            "Director prompt must synthesize status from workflow handoffs"
        )

    def test_does_not_launch_retired_role(self):
        assert "--role project_lead" not in self.lower, (
            "Director prompt must not launch the retired persistent role"
        )

    def test_has_legacy_cleanup_path(self):
        assert "legacy session cleanup" in self.lower and "project_lead" in self.lower, (
            "Director prompt must document cleanup for stale upgraded sessions"
        )

    def test_never_replays_old_sessions(self):
        assert "never replay" in self.lower, (
            "Director prompt must explicitly forbid replaying old session transcripts"
        )


class TestDirectorDispatchContract:
    """Director launches async worker workflows with complete context."""

    def setup_method(self):
        self.text = DIRECTOR_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_launches_engineer_workers(self):
        assert "--role engineer" in self.lower, (
            "Director must launch engineer workers"
        )

    def test_no_project_lead_dispatch(self):
        assert "--role project_lead" not in self.lower, (
            "Director must not dispatch persistent project leads"
        )

    def test_dispatch_includes_source_reference(self):
        assert "source event type" in self.lower and "requester" in self.lower, (
            "Worker launch contract must include source event and requester context"
        )

    def test_routes_common_event_classes(self):
        for workflow in [
            "issue-lifecycle",
            "pr-feedback",
            "pr-closed",
            "merge-conflict",
            "build-failure",
            "adhoc",
        ]:
            assert workflow in self.lower, (
                f"Director prompt must route {workflow} events"
            )

    def test_does_not_perform_repo_work_inline(self):
        assert "do not edit repo files directly" in self.lower, (
            "Director must preserve the async-only repo-work boundary"
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
        assert "managed repos" in listing_text, (
            "Listing must answer from configured managed repos"
        )
        assert "decision log" not in listing_text and "index.md" not in listing_text, (
            "Listing must not read from a decision log under the policy model"
        )

    def test_listing_uses_agents_list_for_status(self):
        listing_pos = self.lower.find("listing managed repos")
        assert listing_pos != -1
        listing_text = self.lower[listing_pos:listing_pos + 800]
        assert "bobi agent <agent> subagents list" in listing_text, (
            "Listing must annotate live worker status from subagents list"
        )


class TestDirectorHumanPreferences:
    """Human preferences flow to the curated Long-Term Memory via the transcript."""

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

    def test_preferences_fold_into_long_term_memory(self):
        pref_pos = self.lower.find("human preferences and standing instructions")
        assert pref_pos != -1
        pref_text = self.lower[pref_pos:pref_pos + 800]
        assert "sleep-cycle" in pref_text and "long-term memory" in pref_text, (
            "Preferences must be folded into the read-only Long-Term Memory by the sleep_cycle"
        )


class TestEngineerDurableKnowledge:
    """Engineer durable knowledge is the read-only Long-Term Memory, not a log."""

    def setup_method(self):
        self.text = ENGINEER_PROMPT.read_text()
        self.lower = self.text.lower()

    def test_no_decision_log(self):
        assert "decision log" not in self.lower, (
            "Engineer prompt must not reference a decision log under the policy model"
        )

    def test_no_index_md(self):
        assert "index.md" not in self.lower, (
            "Engineer prompt must not reference INDEX.md under the policy model"
        )

    def test_reads_long_term_memory_block(self):
        assert "long-term memory" in self.lower, (
            "Engineer prompt must reference the read-only Long-Term Memory block"
        )

    def test_durability_via_transcript(self):
        assert "transcript" in self.lower, (
            "Engineer must make knowledge durable by stating it in the transcript"
        )

    def test_records_standing_instructions(self):
        assert "standing instruction" in self.lower, (
            "Engineer prompt must mention surfacing standing instructions"
        )

    def test_volatile_state_rederived(self):
        assert "re-derived" in self.lower or "rederived" in self.lower, (
            "Engineer must not store volatile state - it is re-derived from source"
        )
