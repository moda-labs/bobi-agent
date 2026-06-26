"""Framework prompt files — loaded at runtime for all agent sessions.

Role prompts resolve from <project>/.bobi/roles/{role}/ROLE.md
(installed there by `bobi install` from the agent team, or
overridden per-project).

Tools (loaded into all agent contexts):
  - <project>/.bobi/tools/*.md — service interaction guides
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent
BASE_PATH = PROMPTS_DIR / "base.md"
# Framework default policy-curator prompt (#456). Team-overridable via
# <project>/.bobi/prompts/curator.md — see MonitorScheduler._load_curator_prompt.
CURATOR_PATH = PROMPTS_DIR / "curator.md"
