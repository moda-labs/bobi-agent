"""The plan-artifact CI check, exercised against real git history.

The check is a shell script, not a module, so these tests drive it the way CI
does: build a throwaway repo, commit a base and a head, run the script over the
pair, assert on its exit code and diagnostic.

Every negative here is MUTATION-PROVEN — each test names the guard whose
removal must turn it red. A negative assertion ("this diff is rejected") passes
both when the guard fires and when the check never looked at the file, so the
mutant is what separates those.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "check-plan-artifact.sh"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "plan-snapshot.md"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose base commit holds the frozen fixture at plans/widget.md."""
    r = tmp_path / "repo"
    (r / "plans").mkdir(parents=True)
    _git(r.parent, "init", "-q", str(r))
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "Test")
    shutil.copy(FIXTURE, r / "plans" / "widget.md")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    return r


def _commit(repo: Path, body: str, msg: str = "head") -> None:
    (repo / "plans" / "widget.md").write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def _run(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), "HEAD~1", "HEAD"],
        cwd=repo, capture_output=True, text=True,
    )


def _plan(repo: Path) -> str:
    return (repo / "plans" / "widget.md").read_text()


# ---------------------------------------------------------------------------
# The check accepts what it should
# ---------------------------------------------------------------------------

class TestAccepts:
    def test_a_marker_transition_with_an_appended_round_log(self, repo):
        """The ordinary worker mutation: flip one marker, append to the log."""
        body = _plan(repo).replace(
            "- [ ] Evict pre-existing stale entries",
            "- [wip] Evict pre-existing stale entries",
        ) + "\n2026-07-30 - picked the lazy eviction path.\n"
        _commit(repo, body)

        result = _run(repo)
        assert result.returncode == 0, result.stderr

    def test_a_dated_amendment_is_additive_and_passes(self, repo):
        """Amendments ADD text above the fence; they never rewrite it. The rule
        is insertion-only rather than byte-identical precisely so this passes."""
        body = _plan(repo).replace(
            "## Notes",
            "## Amendments\n\n- 2026-07-30: added a scope note.\n\n## Notes",
        )
        _commit(repo, body)

        result = _run(repo)
        assert result.returncode == 0, result.stderr

    def test_an_amendment_AND_the_work_in_one_pull_request(self, repo):
        """The check takes no position on whether the plan rides the same PR as
        the work. Both in one diff is legitimate and must pass; an earlier
        version rejected it, because demanding byte-identity forced a
        "did this touch the appendix?" heuristic to guess worker-versus-human."""
        body = _plan(repo).replace(
            "## Notes",
            "## Amendments\n\n- 2026-07-30: added a scope note.\n\n## Notes",
        ).replace(
            "- [ ] Evict stale entries on first read",
            "- [x] Evict stale entries on first read",
        ) + "\n2026-07-30 - did the eviction work.\n"
        _commit(repo, body)

        result = _run(repo)
        assert result.returncode == 0, result.stderr

    def test_flipping_a_marker_INSIDE_the_appendix(self, repo):
        """Regression. Rule 4a once demanded a byte-exact appendix prefix, which
        forbade the single most ordinary operation in the model: flipping an
        appendix item's marker. No test caught it because the fixture worded its
        review-surface and appendix items differently, so every marker test had
        been hitting the review surface only."""
        body = _plan(repo).replace(
            "- [ ] Evict stale entries on first read",
            "- [x] Evict stale entries on first read",
        )
        _commit(repo, body)

        result = _run(repo)
        assert result.returncode == 0, result.stderr

    def test_rewriting_an_appendix_ITEM_is_still_caught(self, repo):
        """The other side of marker-awareness: normalizing markers must not
        blind 4a to a rewritten item, or the appendix could be edited freely."""
        body = _plan(repo).replace(
            "- [ ] Evict stale entries on first read",
            "- [ ] Evict stale entries lazily, some other way",
        )
        _commit(repo, body)

        result = _run(repo)
        assert result.returncode == 1
        assert "appendix was rewritten, not appended to" in result.stderr

    def test_flipping_the_status_field(self, repo):
        """A plan's Status is state, like a marker — every plan transitions
        Draft -> Approved -> done, and freezing it would make the check unusable
        on exactly the PRs that advance a plan."""
        body = _plan(repo).replace(
            "**Status:** Approved ·", "**Status:** Draft \u2014 needs re-approval ·",
        )
        _commit(repo, body)

        result = _run(repo)
        assert result.returncode == 0, result.stderr

    def test_other_metadata_on_the_status_line_stays_frozen(self, repo):
        """Normalizing the status VALUE must not unprotect the rest of the line.
        Only text up to the first separator is treated as state."""
        body = _plan(repo).replace("**Created:** 2026-07-29", "**Created:** 2020-01-01")
        _commit(repo, body)

        result = _run(repo)
        assert result.returncode == 1
        assert "existing review-surface text was modified or deleted" in result.stderr

    def test_a_diff_touching_no_plan_is_a_no_op(self, repo):
        (repo / "README.md").write_text("hello\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "unrelated")

        result = _run(repo)
        assert result.returncode == 0
        assert "nothing to check" in result.stdout


# ---------------------------------------------------------------------------
# The check rejects what it should
# ---------------------------------------------------------------------------

class TestRejects:
    def test_existing_prose_rewritten_above_the_fence(self, repo):
        """MUTATION-PROOF — mutant: drop the review-surface comparison (step
        4b). The property: the document a human approved and the document that
        got built must stay the same one. Rewriting an existing line is how they
        silently diverge."""
        body = _plan(repo).replace(
            "Move the key to the immutable id.",
            "Move the key to the immutable id, and also rewrite the eviction policy.",
        ) + "\n2026-07-30 - noted.\n"
        _commit(repo, body)

        result = _run(repo)
        assert result.returncode == 1
        assert "existing review-surface text was modified or deleted" in result.stderr

    def test_prose_deleted_above_the_fence(self, repo):
        """Deletion is the other half of "modified or deleted", and it is the
        sneakier one — dropping an inconvenient constraint leaves no trace in
        the remaining text."""
        body = _plan(repo).replace(
            "- [ ] Evict pre-existing stale entries on first read after deploy\n", ""
        ) + "\n2026-07-30 - noted.\n"
        _commit(repo, body)

        result = _run(repo)
        assert result.returncode == 1
        assert "existing review-surface text was modified or deleted" in result.stderr

    def test_an_f_marker_without_a_state_tag(self, repo):
        """MUTATION-PROOF — mutant: drop the [f] state-tag assertion (step 2).
        A prose-only [f] is the stale-marker defect: still failed, but for a
        reason nobody can mechanically re-check."""
        body = _plan(repo).replace(
            "- [ ] Evict pre-existing stale entries on first read after deploy",
            "- [f] Evict pre-existing stale entries — turned out to be hard",
        )
        _commit(repo, body)

        result = _run(repo)
        assert result.returncode == 1
        assert "without a machine-readable state: tag" in result.stderr

    def test_the_round_log_rewritten_rather_than_appended(self, repo):
        """MUTATION-PROOF — mutant: drop the prefix comparison (step 4a). A
        reviewer reads the appendix as a chronology, so editing its middle is
        rewriting history, not recording it."""
        body = _plan(repo).replace(
            "A composite\nwould have kept the rename information in the key, which is exactly what makes\nthe entry unreachable after a rename.",
            "No particular reason.",
        )
        _commit(repo, body)

        result = _run(repo)
        assert result.returncode == 1
        assert "appendix was rewritten, not appended to" in result.stderr

    def test_an_appendix_item_with_no_verify_or_judgement(self, repo):
        """MUTATION-PROOF — mutant: drop the gate-line classification (step 3).
        An item with neither tag is not checkable, so 'done' against it asserts
        nothing at all."""
        body = _plan(repo).replace(
            "- [ ] Evict stale entries on first read\n      verify: pytest tests/test_widget_cache.py::test_evicts_stale -q",
            "- [ ] Evict stale entries on first read",
        )
        _commit(repo, body)

        result = _run(repo)
        assert result.returncode == 1
        assert "neither a verify: nor a judgement: tag" in result.stderr


# ---------------------------------------------------------------------------
# Malformed input fails with a diagnostic, never a traceback
# ---------------------------------------------------------------------------

class TestMalformed:
    def test_unresolved_conflict_markers(self, repo):
        body = _plan(repo).replace(
            "## Problem",
            "<<<<<<< HEAD\n## Problem\n=======\n## The Problem\n>>>>>>> other\n",
        )
        _commit(repo, body)

        result = _run(repo)
        assert result.returncode == 1
        assert "unresolved merge-conflict markers" in result.stderr
        assert "Traceback" not in result.stderr

    def test_a_second_fence(self, repo):
        """A truncated or double-opened appendix makes 'below the fence'
        ambiguous, so it is rejected rather than guessed at."""
        _commit(repo, _plan(repo) + "\n```checklist\n- [ ] stray\n")

        result = _run(repo)
        assert result.returncode == 1
        assert "the appendix must open exactly once" in result.stderr
        assert "Traceback" not in result.stderr

    def test_a_brand_new_plan_file_still_gets_its_appendix_checked(self, repo):
        """Regression from self-review. The gate-line check used to sit AFTER
        the "no base to compare" early-exit, so a plan file ADDED in a PR
        skipped it entirely — and Phase 3's renderer creates appendices on new
        files, which is exactly the case that matters."""
        (repo / "plans" / "fresh.md").write_text(
            "# New plan\n\n```checklist\n- [ ] an item with no tag at all\n```\n"
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "add a new plan")

        result = _run(repo)
        assert result.returncode == 1
        assert "neither a verify: nor a judgement: tag" in result.stderr

    def test_misuse_exits_two_not_one(self):
        result = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True,
        )
        assert result.returncode == 2
        assert "usage:" in result.stderr


# ---------------------------------------------------------------------------
# The check never runs a verify: string
# ---------------------------------------------------------------------------

class TestNeverExecutesVerify:
    def test_a_hostile_verify_is_not_executed(self, repo, tmp_path):
        """The standing invariant. `verify:` is attacker-reachable shell — a
        plan arrives by pull request from any account — and the reason no
        provenance gate wraps it is that NOTHING runs one unattended. If this
        test ever fails, the gate has to come back with the executor."""
        canary = tmp_path / "pwned"
        # Appended, so the diff is a well-formed worker mutation and the only
        # thing under test is whether the string gets executed.
        body = _plan(repo) + (
            f"\n- [ ] Innocuous-looking item\n      verify: touch {canary}\n"
        )
        _commit(repo, body)

        result = _run(repo)

        assert not canary.exists(), "the check executed a verify: string"
        # And it still passed structurally: judging a verify: is the worker's
        # job, not this script's. The script only asks whether a tag is present.
        assert result.returncode == 0, result.stderr
