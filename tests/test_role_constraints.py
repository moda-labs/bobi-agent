"""Role prompts must enforce delegation and responsiveness constraints.

The project lead prompt must never allow hands-on work (reading files,
running tests, writing code, creating PRs). Issue #149: a lead entered
a debugging loop and became unresponsive to inbox messages for 120s+.

These tests catch regressions — if someone loosens the prompt back to
"a single quick read-only command is fine", the test fails.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LEAD_PROMPT = REPO_ROOT / "agents" / "eng-team" / "roles" / "project_lead" / "ROLE.md"


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
