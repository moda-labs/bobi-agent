"""Framework prompt files — loaded at runtime for all agent sessions.

Role prompts resolve from <run>/package/roles/{role}/ROLE.md,
installed there by `bobi agents install` from the agent team.

Tools (loaded into all agent contexts):
  - <run>/package/tools/*.md — service interaction guides
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent
BASE_PATH = PROMPTS_DIR / "base.md"
# Framework communication baseline, rendered into every team's brain-native
# global-instructions files by bobi.brain.instructions. Operator opt-out:
# BOBI_COMMUNICATION_STYLE=off.
COMMUNICATION_STYLE_PATH = PROMPTS_DIR / "communication_style.md"
# Framework default sleep-cycle prompt (#456). Team-overridable via
# <run>/package/prompts/sleep_cycle.md — see MonitorScheduler._load_sleep_cycle_prompt.
SLEEP_CYCLE_PATH = PROMPTS_DIR / "sleep_cycle.md"

# Self-diagnosis prompt handed to the sub-agent that root-causes a suspected
# bug in bobi itself. Printed by `bobi feedback rca`, never auto-loaded: it
# costs nothing until a framework bug is actually hit. Operator opt-out:
# BOBI_FRAMEWORK_RCA=off.
FRAMEWORK_BUG_RCA_PATH = PROMPTS_DIR / "framework-bug-rca.md"

# Deprecated alias for one release.
CURATOR_PATH = SLEEP_CYCLE_PATH
