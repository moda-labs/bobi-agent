"""Shared SDK layer — common primitives for manager and engineer agents.

Both the manager (long-lived interactive session via ClaudeSDKClient)
and engineers (one-shot phases via query()) share:
- CLI path resolution
- Session ID persistence
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

CLAUDE_CLI = shutil.which("claude") or "/opt/homebrew/bin/claude"
SESSION_DIR = Path.home() / ".modastack" / "sessions"


def get_cli_path() -> str:
    return CLAUDE_CLI


def save_session_id(name: str, session_id: str) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    (SESSION_DIR / f"{name}.id").write_text(session_id)


def load_session_id(name: str) -> str:
    path = SESSION_DIR / f"{name}.id"
    if path.exists():
        return path.read_text().strip()
    return ""
