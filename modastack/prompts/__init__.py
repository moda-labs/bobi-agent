"""Framework prompt files — loaded at runtime by manager and agent sessions.

Resolution order (for both manager and agent prompts):
  1. Built-in (shipped with modastack) — always loaded
  2. Repo override (<repo>/.modastack/) — loaded if present, extends or replaces
"""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent
MANAGER_BASE_PATH = PROMPTS_DIR / "manager_base.md"
MANAGER_ENGINEERING_PATH = PROMPTS_DIR / "manager_engineering.md"
AGENT_BASE_PATH = PROMPTS_DIR / "agent_base.md"
AGENTS_DIR = PROMPTS_DIR / "agents"
