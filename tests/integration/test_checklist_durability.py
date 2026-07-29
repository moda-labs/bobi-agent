"""Commit-per-item bounds what a death can lose to one item.

This is the durability claim the whole model rests on: the artifact is the
state, so a worker that dies loses at most the item it was on, and a
re-dispatch picks up from the last committed marker.

Deliberately brain-free. The property belongs to the PROTOCOL — "persist after
each item" — not to any model's behavior, so proving it with a real git repo and
a scripted worker is both honest and deterministic. Git is the instantiation
under test here, not the requirement: the protocol only asks for per-item
durability, and a commit is how that is spelled when the work lives in a
repository. A stub brain would not
strengthen this; it would only add a fake worker in front of the same git
operations. Whether a real model *follows* the protocol is a different claim
and lives in `tests/e2e/test_checklist_worker.py`, where the risk actually is.

The worker here is a small script, not an agent. It does exactly what
`skills/checklist-execution.md` prescribes — mark, work, verify, check off,
commit — and can be killed at a chosen item.
"""

from __future__ import annotations

import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ARTIFACT = "plans/work.md"

# The work list lives in the appendix, which is where a renderer puts it; the
# review surface above the fence is prose. Keeping each item in exactly one
# place is also what makes "touched once" measurable.
PLAN = """\
# Five things

## Purpose

Produce five output files, one per item, to exercise the durability property.

```checklist
- [ ] item one
      verify: test -f out/one
- [ ] item two
      verify: test -f out/two
- [ ] item three
      verify: test -f out/three
- [ ] item four
      verify: test -f out/four
- [ ] item five
      verify: test -f out/five
```
"""

# A worker that follows the protocol. `die_at` makes it SIGKILL itself
# mid-item: AFTER writing the item's output but BEFORE committing, which is the
# worst moment — the tree holds uncommitted work that the next worker must not
# trust and must not try to salvage by mutating the tree.
WORKER = '''\
import os, re, signal, subprocess, sys
from pathlib import Path

repo, die_at = Path(sys.argv[1]), int(sys.argv[2])
ordinals = ["one", "two", "three", "four", "five"]

def git(*args):
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)

def read():
    return (repo / "plans/work.md").read_text()

# --- session start: read the artifact ONCE -------------------------------
body = read()
(repo / "reads.log").open("a").write("full-read\\n")

while True:
    m = re.search(r"^- \\[ \\] item (\\w+)$", body, re.M)
    if not m:
        break
    word = m.group(1)
    n = ordinals.index(word) + 1

    # mark [wip]
    body = body.replace(f"- [ ] item {word}", f"- [wip] item {word}", 1)
    (repo / "plans/work.md").write_text(body)

    # do the work
    (repo / "out").mkdir(exist_ok=True)
    (repo / "out" / word).write_text("done\\n")

    if n == die_at:
        # Uncommitted work in the tree, marker still [wip]. Hard kill.
        os.kill(os.getpid(), signal.SIGKILL)

    # verify, then check off
    assert (repo / "out" / word).exists()
    body = body.replace(f"- [wip] item {word}", f"- [x] item {word}")
    (repo / "plans/work.md").write_text(body)

    git("add", "-A")
    git("commit", "-q", "-m", f"item {word}")
'''


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "plans").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(r))
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "Test")
    (r / ARTIFACT).write_text(PLAN)
    (r / "worker.py").write_text(WORKER)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "plan")
    return r


def _dispatch(repo: Path, die_at: int = 0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(repo / "worker.py"), str(repo), str(die_at)],
        capture_output=True, text=True,
    )


def _markers(repo: Path) -> list[str]:
    """Committed marker state, read from HEAD rather than the working tree."""
    body = _git(repo, "show", f"HEAD:{ARTIFACT}")
    return re.findall(r"^- \[(.)\] item", body, re.M)


def _reset_to_last_commit(repo: Path) -> None:
    """What a human does before re-dispatching onto a dead worker's branch.

    `checkout --` alone is not enough: the dead worker's output files are
    UNTRACKED, so they survive it and the next worker would inherit
    half-finished work it did not do. Discarding both is what makes the loss
    bound exactly one item.
    """
    _git(repo, "checkout", "--", ".")
    _git(repo, "clean", "-qfd")


class TestDeathLosesOneItem:
    def test_a_kill_at_item_three_is_carried_to_all_checked(self, repo):
        first = _dispatch(repo, die_at=3)
        assert first.returncode == -signal.SIGKILL

        # Two items committed; item three died mid-flight.
        assert _markers(repo) == ["x", "x", " ", " ", " "]
        assert (repo / "out" / "three").exists(), "died after doing the work"
        assert "three" not in _git(repo, "log", "--oneline")

        # What the dead worker left in the tree is NOT trustworthy state. A
        # re-dispatch resets to the last commit rather than salvaging it — the
        # protocol's read-only rule, and the reason loss is bounded.
        _reset_to_last_commit(repo)
        assert not (repo / "out" / "three").exists(), "item three's work is gone"

        second = _dispatch(repo)
        assert second.returncode == 0, second.stderr

        assert _markers(repo) == ["x", "x", "x", "x", "x"]

    def test_the_lost_work_is_exactly_one_item(self, repo):
        """The bound, stated as a measurement rather than a hope: everything
        before the dying item survived in commits, and nothing after it had
        started."""
        assert _dispatch(repo, die_at=3).returncode == -signal.SIGKILL
        _reset_to_last_commit(repo)

        assert (repo / "out" / "one").exists()
        assert (repo / "out" / "two").exists()
        assert not (repo / "out" / "three").exists()
        assert not (repo / "out" / "four").exists()

    def test_every_transition_is_its_own_commit(self, repo):
        """Why the branch lineage is readable as proof-of-work on the PR."""
        assert _dispatch(repo).returncode == 0
        log = _git(repo, "log", "--format=%s").split("\n")
        assert [l for l in log if l.startswith("item ")] == [
            "item five", "item four", "item three", "item two", "item one",
        ]

    def test_a_clean_run_never_leaves_an_uncommitted_tree(self, repo):
        assert _dispatch(repo).returncode == 0
        assert _git(repo, "status", "--porcelain") == ""


class TestReadOncePerSession:
    """The cost property. Re-reading per item is what the deleted driver did;
    on a real ~250-item plan it costs millions of tokens before any work, and
    grows superlinearly because the round log lives in the same file."""

    def test_a_five_item_run_reads_the_artifact_once(self, repo):
        assert _dispatch(repo).returncode == 0
        reads = (repo / "reads.log").read_text().count("full-read")
        assert reads == 1, f"read the full artifact {reads}x for 5 items"

    def test_a_re_dispatch_reads_once_more_and_only_once(self, repo):
        """A cold start pays the read — that is what makes it a cold start —
        but only one per session, not one per remaining item."""
        assert _dispatch(repo, die_at=3).returncode == -signal.SIGKILL
        _reset_to_last_commit(repo)
        assert _dispatch(repo).returncode == 0

        reads = (repo / "reads.log").read_text().count("full-read")
        assert reads == 2, f"two sessions should read twice, got {reads}"
