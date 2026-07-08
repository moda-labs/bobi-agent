"""Framework prompt files — loaded at runtime for all agent sessions.

Role prompts resolve from <run>/package/roles/{role}/ROLE.md,
installed there by `bobi agents install` from the agent team.

Tools (loaded into all agent contexts):
  - <run>/package/tools/*.md — service interaction guides
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent
BASE_PATH = PROMPTS_DIR / "base.md"
# Framework default sleep-cycle prompt (#456). Team-overridable via
# <run>/package/prompts/sleep_cycle.md — see MonitorScheduler._load_sleep_cycle_prompt.
SLEEP_CYCLE_PATH = PROMPTS_DIR / "sleep_cycle.md"

# Deprecated alias for one release.
CURATOR_PATH = SLEEP_CYCLE_PATH
