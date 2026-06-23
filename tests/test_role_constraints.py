"""Role prompts must enforce delegation and responsiveness constraints.

The project lead prompt must never allow hands-on work (reading files,
running tests, writing code, creating PRs). Issue #149: a lead entered
a debugging loop and became unresponsive to inbox messages for 120s+.

These tests catch regressions — if someone loosens the prompt back to
"a single quick read-only command is fine", the test fails.
"""

from pathlib import Path

import pytest
import yaml

# The pristine, in-repo reference team. These constraints are generic (they
# apply to any eng org), so they're validated against eng-team-core. The Moda
# house bindings (codex as the adversarial reviewer, Linear) live in the private
# moda-eng-team overlay and are validated there.
REPO_ROOT = Path(__file__).resolve().parent.parent
ENG_TEAM = REPO_ROOT / "agents" / "eng-team-core"
LEAD_PROMPT = ENG_TEAM / "roles" / "project_lead" / "ROLE.md"
ENGINEER_PROMPT = ENG_TEAM / "roles" / "engineer" / "ROLE.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
ISSUE_LIFECYCLE = ENG_TEAM / "workflows" / "issue-lifecycle.yaml"
AGENT_YAML = ENG_TEAM / "agent.yaml"


class TestProjectLeadDelegation:

    @pytest.fixture(autouse=True)
    def _load_prompt(self):
        self.text = LEAD_PROMPT.read_text()

    def test_forbids_hands_on_work(self):
        assert "never do hands-on work" in self.text.lower(), (
            "Project lead prompt must explicitly forbid hands-on work"
        )

    def test_forbids_reading_source_files(self):
        assert "read source files" in self.text.lower(), (
            "Project lead prompt must mention not reading source files"
        )

    def test_forbids_running_tests(self):
        assert "run tests" in self.text.lower(), (
            "Project lead prompt must mention not running tests"
        )

    def test_forbids_writing_code(self):
        assert "write code" in self.text.lower(), (
            "Project lead prompt must mention not writing code"
        )

    def test_forbids_creating_prs(self):
        assert "create prs" in self.text.lower(), (
            "Project lead prompt must mention not creating PRs"
        )

    def test_no_quick_command_loophole(self):
        """The old prompt said 'a single quick read-only command is fine'.

        That loophole led to the lead entering debugging loops. The prompt
        must not contain language that permits running commands directly.
        """
        assert "read-only command is fine" not in self.text.lower(), (
            "Project lead prompt must not contain the 'quick command' loophole"
        )

    def test_warns_about_debugging_loops(self):
        assert "debugging loop" in self.text.lower(), (
            "Project lead prompt must warn about the debugging loop anti-pattern"
        )

    def test_requires_few_seconds_responsiveness(self):
        assert "few seconds" in self.text.lower(), (
            "Project lead prompt must set max blocking time to 'a few seconds'"
        )

    def test_delegates_investigations(self):
        assert "delegate investigations" in self.text.lower(), (
            "Project lead prompt must require delegating investigations"
        )


class TestProjectLeadStandingInstructions:
    """Standing operational instructions must be encoded in the role prompt.

    These instructions were learned from Jun 12-18 operations and must
    survive restarts and context compression. Issue #296 / MDS-55.
    """

    @pytest.fixture(autouse=True)
    def _load_prompt(self):
        self.text = LEAD_PROMPT.read_text()

    def test_auto_fix_ci_failures(self):
        assert "auto-fix ci failures" in self.text.lower(), (
            "Project lead prompt must instruct auto-dispatching on CI failures"
        )

    def test_ci_failures_escalate_only_if_unfixable(self):
        assert "only escalate" in self.text.lower(), (
            "CI failure instruction must say to escalate only if unfixable"
        )

    def test_ci_failures_cover_all_branches(self):
        """Issue #323: auto-fix must cover human-authored PRs, not just
        agent-authored ones. A failing check on any open PR blocks the
        merge queue, so all branches get auto-fixed."""
        text = self.text.lower()
        assert "agent-authored" in text and "human-authored" in text, (
            "CI failure instruction must explicitly cover both "
            "agent-authored and human-authored PR branches"
        )

    def test_auto_pickup_agent_labeled_issues(self):
        assert "auto-pickup agent-labeled issues" in self.text.lower(), (
            "Project lead prompt must instruct auto-pickup of agent-labeled issues"
        )

    def test_agent_label_no_assignment_needed(self):
        assert "do not wait for explicit" in self.text.lower(), (
            "Agent-label instruction must say no explicit assignment needed"
        )

    def test_answer_all_questions(self):
        assert "answer all questions" in self.text.lower(), (
            "Project lead prompt must require answering all questions"
        )

    def test_answer_questions_on_closed_prs(self):
        assert "merged, or closed" in self.text.lower(), (
            "Question-answering must cover merged and closed PRs"
        )

    def test_summarize_before_dispatching(self):
        assert "summarize before dispatching" in self.text.lower(), (
            "Project lead prompt must require summarizing before dispatching"
        )

    def test_pr_branches_off_main(self):
        assert "pr branches must be based off" in self.text.lower(), (
            "Project lead prompt must enforce PR branches off main"
        )

    def test_ticket_as_task_dispatch(self):
        assert "pass the ticket reference as the" in self.text.lower(), (
            "Project lead prompt must enforce ticket-as-task dispatch format"
        )

    def test_merge_conflict_auto_dispatch(self):
        assert "conflict_detected" in self.text.lower(), (
            "Project lead prompt must handle merge conflict auto-dispatch"
        )


class TestReleaseTimeOnlyVersionConvention:
    """Feature PRs must never bump the version or edit the changelog.

    Issue #325: version bumps and CHANGELOG.md entries are a release-time
    concern only — modastack's own contributor convention, documented in
    CLAUDE.md. These tests fail if the documented convention regresses.

    The *engineer-prompt* side of this guard (telling the engineer to revert
    `/ship`'s version/changelog bump) is Moda house policy and lives in the
    private moda-eng-team overlay engineer role — it is validated there, not
    against the pristine eng-team-core.
    """

    def test_contributor_docs_state_convention(self):
        text = CLAUDE_MD.read_text().lower()
        assert "feature prs must not bump the version" in text, (
            "CLAUDE.md must document the no-version-bump-in-feature-PRs convention"
        )

    def test_contributor_docs_forbid_changelog_edit(self):
        text = CLAUDE_MD.read_text().lower()
        assert "changelog.md" in text and "release time" in text, (
            "CLAUDE.md must state CHANGELOG.md entries are added at release time"
        )


class TestAdversarialReviewStep:
    """eng-team-core must wire a tool-agnostic adversarial-review seam.

    A `plan_review` step in issue-lifecycle runs the engineer's *adversarial
    review gate* (a generic seam — the overlay binds it to a concrete tool like
    Codex) on the just-written spec and captures the critique in the handoff for
    the human approval gate. These tests fail if the generic step or its seam
    wording regresses. The concrete Codex binding + tools/codex.md guide live in
    the private moda-eng-team overlay and are validated there (#285).
    """

    def _steps(self):
        wf = yaml.safe_load(ISSUE_LIFECYCLE.read_text())
        return {s["name"]: s for s in wf["steps"] if "name" in s}

    def test_plan_review_step_exists(self):
        assert "plan_review" in self._steps(), (
            "issue-lifecycle.yaml must contain a plan_review step"
        )

    def test_plan_review_runs_after_spec_before_approval(self):
        """The critique must reach the human at the approval gate, so the
        step sits between `spec` and `await_approval`."""
        order = list(self._steps())
        assert order.index("spec") < order.index("plan_review") < order.index(
            "await_approval"
        ), "plan_review must run after spec and before await_approval"

    def test_plan_review_captures_critique_in_handoff(self):
        step = self._steps()["plan_review"]
        required = step.get("handoff", {}).get("required", [])
        assert "review_critique" in required, (
            "plan_review handoff must require review_critique"
        )
        assert "review_verdict" in required, (
            "plan_review handoff must require review_verdict"
        )

    def test_plan_review_uses_generic_seam(self):
        """Core stays tool-agnostic: the step delegates to the engineer's
        'adversarial review gate' binding, never a hard-coded tool."""
        step = self._steps()["plan_review"]
        assert "adversarial review gate" in step["prompt"], (
            "plan_review must reference the generic adversarial review gate seam"
        )
        assert "codex" not in step["prompt"].lower(), (
            "core's plan_review must NOT name a concrete tool (Codex is an "
            "overlay binding)"
        )

    def test_no_codex_connections_or_mcp_shim(self):
        """CLI-first contract: core must never add a `connections:` block or
        reference the retired codex_exec MCP shim (#403)."""
        agent_cfg = AGENT_YAML.read_text()
        assert "connections:" not in agent_cfg, (
            "agent.yaml must not add a connections: block (#285 CLI-first)"
        )
        assert "codex_exec" not in agent_cfg, (
            "agent.yaml must not reference the retired codex_exec MCP shim (#403)"
        )
