"""The framework must not grow a checklist execution engine.

This is how "we removed an engine" is proven rather than asserted. The whole
point of the checklist model is that the loop is prompt text: the worker reads
markdown, edits markers with its own file tools, and runs `verify:` in its own
shell. The moment `bobi/` learns to parse the artifact, write a marker, or
execute a `verify:`, the thing that was deleted has been rebuilt — and it will
have been rebuilt one small helper at a time, which is why this is a test and
not a code-review convention.

Every scan here has a POSITIVE control: the same detector is run against a
planted offender and must find it. An absence assertion goes green both when
the framework is clean and when the detector is broken, and only the control
separates those.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BOBI = Path(__file__).resolve().parents[1] / "bobi"

# The artifact's fence and marker vocabulary. Framework code has no business
# knowing any of it.
ARTIFACT_TOKENS = [
    re.compile(r"```checklist"),
    re.compile(r"-\s*\[wip\]"),
    re.compile(r"""["']-\s*\[[ x]\]"""),      # a marker as a string literal
    re.compile(r"state:(blocked-on-human|verify-failed)"),
]

# Executing a proposed proof. `verify:` is attacker-reachable shell; nothing in
# the framework may run one unattended.
VERIFY_EXECUTION = [
    re.compile(r"verify_line|run_verify|execute_verify|_run_verify"),
    re.compile(r"""["']verify:["']"""),
]


def _py_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _scan(root: Path, patterns: list[re.Pattern]) -> list[str]:
    hits: list[str] = []
    for path in _py_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(p.search(line) for p in patterns):
                hits.append(f"{path.relative_to(root.parent)}:{lineno}: {line.strip()}")
    return hits


@pytest.fixture
def planted(tmp_path: Path) -> Path:
    """A fake package containing exactly what must never appear in bobi/."""
    pkg = tmp_path / "fake_bobi"
    pkg.mkdir()
    (pkg / "artifact.py").write_text(
        'FENCE = "```checklist"\n'
        'def check_off(line):\n'
        '    return line.replace("- [ ]", "- [x]")\n'
    )
    (pkg / "runner.py").write_text(
        'import subprocess\n'
        'def run_verify(cmd):\n'
        '    return subprocess.run(cmd, shell=True)\n'
    )
    return pkg


class TestNoArtifactParsing:
    def test_the_framework_does_not_know_the_artifact_format(self):
        hits = _scan(BOBI, ARTIFACT_TOKENS)
        assert hits == [], (
            "bobi/ references the checklist artifact format. The artifact is a "
            "lifecycle convention, not a framework property — if the framework "
            "parses it, the execution engine has grown back:\n  "
            + "\n  ".join(hits)
        )

    def test_the_detector_finds_a_planted_offender(self, planted):
        """Positive control for the assertion above."""
        assert _scan(planted, ARTIFACT_TOKENS)


class TestNoVerifyExecution:
    def test_the_framework_never_runs_a_verify(self):
        hits = _scan(BOBI, VERIFY_EXECUTION)
        assert hits == [], (
            "bobi/ appears to execute a `verify:` string. Nothing may run one "
            "unattended — and if that ever changes, the default-deny "
            "provenance gate has to come back with it:\n  "
            + "\n  ".join(hits)
        )

    def test_the_detector_finds_a_planted_runner(self, planted):
        """Positive control for the assertion above."""
        assert _scan(planted, VERIFY_EXECUTION)


class TestNoChecklistModule:
    def test_there_is_no_bobi_checklist_package(self):
        """Earlier drafts of the plan put `bobi/checklist/artifact.py` and
        `bobi/checklist/verify.py` here. Both encoded a lifecycle convention
        inside the framework and were cut; this keeps them cut."""
        assert not (BOBI / "checklist").exists()
