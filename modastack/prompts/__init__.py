"""Framework prompt files — loaded at runtime for all agent sessions.

Resolution order for agent packs:
  1. <project>/agents/{name}           — project-level (visible)
  2. <project>/.modastack/agents/{name} — project override (hidden)
  3. ~/.modastack/agents/{name}         — user cache (fetched from remote)

Resolution order for role prompts:
  1. <project>/.modastack/roles/{role}.md — project override
  2. Agent pack roles/{role}.md           — from resolved agent pack
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent
BASE_PATH = PROMPTS_DIR / "base.md"
AGENTS_CACHE_DIR = Path.home() / ".modastack" / "agents"
