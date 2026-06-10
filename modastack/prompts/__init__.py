"""Framework prompt files — loaded at runtime for all agent sessions.

Role prompts resolve from <project>/.modastack/roles/{role}/ROLE.md
(installed there by `modastack install` from the agent team, or
overridden per-project).

Tools (loaded into all agent contexts):
  - <project>/.modastack/tools/*.md — service interaction guides
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent
BASE_PATH = PROMPTS_DIR / "base.md"
