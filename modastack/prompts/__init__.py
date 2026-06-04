"""Framework prompt files — loaded at runtime by manager and agent sessions."""

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent
MANAGER_BASE_PATH = PROMPTS_DIR / "manager_base.md"
AGENT_BASE_PATH = PROMPTS_DIR / "agent_base.md"
