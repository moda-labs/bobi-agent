"""Framework prompt files — loaded at runtime for all agent sessions.

Resolution order:
  1. base.md — generic capabilities shared by all agents
  2. agents/{role}.md — built-in role prompt (shipped with modastack)
  3. <project>/.modastack/agents/{role}.md — project override (replaces built-in)
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent
BASE_PATH = PROMPTS_DIR / "base.md"
AGENTS_DIR = PROMPTS_DIR / "agents"

# Kept for any code that still references these (will be removed)
AGENT_BASE_PATH = BASE_PATH
MANAGER_BASE_PATH = BASE_PATH
