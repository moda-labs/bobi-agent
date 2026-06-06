"""Framework prompt files — loaded at runtime for all agent sessions.

Resolution order for roles:
  1. base.md — generic capabilities shared by all agents
  2. agents/{agent_name}/roles/{role}.md — shipped agent pack
  3. <project>/.modastack/roles/{role}.md — project override (replaces pack)
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent
BASE_PATH = PROMPTS_DIR / "base.md"
AGENTS_DIR = Path(__file__).parent.parent.parent / "agents"
