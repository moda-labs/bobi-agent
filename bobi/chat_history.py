"""Web-UI chat persistence and Claude transcript replay.

Extracted from the agent UI server (#525) so the unified webapp and the
per-agent container UI share one implementation.

Two sources back a chat panel, in preference order:

1. The Claude Code JSONL transcript (:func:`read_transcript_messages`) —
   the durable source of truth for an agent session's conversation.
2. The web-UI chat log (:func:`read_chat`/:func:`append_chat`) — each
   web-UI exchange is appended to ``webui-chat.jsonl`` beside the
   session's state, a local fallback for sessions whose transcript
   cannot be resolved yet. This is the web-UI conversation specifically,
   not the agent's full event/tool transcript.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

CHAT_HISTORY_LIMIT = 200


def safe_name(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name


def _chat_log_path(project: Path, name: str) -> Path:
    from bobi import paths
    return paths.sessions_dir(project) / name / "webui-chat.jsonl"


def read_chat(project: Path, name: str, limit: int = CHAT_HISTORY_LIMIT) -> list[dict]:
    """Load the persisted web-UI conversation for an agent (oldest→newest)."""
    path = _chat_log_path(project, name)
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        role, text = obj.get("role"), obj.get("text", "")
        if role in ("user", "agent") and text:
            out.append({"role": role, "text": text})
    return out[-limit:]


def append_chat(project: Path, name: str, role: str, text: str) -> None:
    path = _chat_log_path(project, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"role": role, "text": text, "ts": time.time()}) + "\n")


# --- Claude transcript replay -------------------------------------------

def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return ""


def _claude_projects_dirs() -> list[Path]:
    dirs = []
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if cfg:
        dirs.append(Path(cfg) / "projects")
    dirs.append(Path.home() / ".claude" / "projects")

    seen = set()
    out = []
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _transcript_path(session_id: str) -> Path | None:
    if not session_id:
        return None
    for projects in _claude_projects_dirs():
        if not projects.exists():
            continue
        for project_dir in projects.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / f"{session_id}.jsonl"
            if candidate.exists():
                return candidate
    return None


def read_transcript_messages(session_id: str,
                             limit: int = CHAT_HISTORY_LIMIT) -> list[dict]:
    path = _transcript_path(session_id)
    if not path:
        return []

    out = []
    for line in path.read_text().splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        msg_type = obj.get("type", "")
        if msg_type not in ("human", "user", "assistant"):
            continue
        text = _extract_text(obj.get("message", {}).get("content", "")).strip()
        if not text:
            continue
        role = "agent" if msg_type == "assistant" else "user"
        out.append({"role": role, "text": text})
    return out[-limit:]
