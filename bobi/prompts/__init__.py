"""Framework prompt files — loaded at runtime for all agent sessions.

Role prompts resolve from <run>/package/roles/{role}/ROLE.md,
installed there by `bobi agents install` from the agent team.

Tools (loaded into all agent contexts):
  - <run>/package/tools/*.md — service interaction guides
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent
BASE_PATH = PROMPTS_DIR / "base.md"
# Framework default policy-curator prompt (#456). Team-overridable via
# <run>/package/prompts/curator.md — see MonitorScheduler._load_curator_prompt.
CURATOR_PATH = PROMPTS_DIR / "curator.md"
