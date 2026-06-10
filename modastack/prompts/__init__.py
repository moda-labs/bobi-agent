"""Framework prompt files — loaded at runtime for all agent sessions.

Resolution order for agent teams:
  1. <project>/agents/{name}           — project-level (checked in)
  2. <project>/.modastack/agents/{name} — local agents (overrides + cached)

Resolution order for role prompts:
  1. <project>/.modastack/roles/{role}/ROLE.md — project override
  2. Agent team roles/{role}/ROLE.md           — from resolved agent team
  3. Built-in: modastack/prompts/agents/{role}/ROLE.md — framework-shipped

Tools (loaded into all agent contexts from the pack):
  - Agent team tools/*.md — service interaction guides
  - <project>/.modastack/tools/*.md — project-level tool overrides
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent
BASE_PATH = PROMPTS_DIR / "base.md"
BUILTIN_AGENTS_DIR = PROMPTS_DIR / "agents"
