"""Framework prompt files — loaded at runtime for all agent sessions.

Resolution order for agent packs:
  1. <project>/agents/{name}           — project-level (visible)
  2. <project>/.modastack/agents/{name} — project override (hidden)
  3. ~/.modastack/agents/{name}         — user cache (fetched from remote)

Resolution order for role prompts:
  1. <project>/.modastack/roles/{role}/ROLE.md — project override
  2. Agent pack roles/{role}/ROLE.md           — from resolved agent pack
  3. Built-in: modastack/prompts/agents/{role}/ROLE.md — framework-shipped

Tools (loaded into all agent contexts from the pack):
  - Agent pack tools/*.md — service interaction guides
  - <project>/.modastack/tools/*.md — project-level tool overrides
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent
BASE_PATH = PROMPTS_DIR / "base.md"
BUILTIN_AGENTS_DIR = PROMPTS_DIR / "agents"
AGENTS_CACHE_DIR = Path.home() / ".modastack" / "agents"
