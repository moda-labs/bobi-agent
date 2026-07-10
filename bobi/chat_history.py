"""Web-UI chat persistence and agent transcript replay.

Extracted from the agent UI server (#525) so the unified webapp and the
per-agent container UI share one implementation.

Two sources back a chat panel, in preference order:

1. The agent session's transcript (:func:`read_transcript_messages`) — the
   durable source of truth for its conversation. Each brain writes its own
   on-disk format: the Claude Code JSONL transcript under ``~/.claude/projects``,
   or a Codex *rollout* under ``$CODEX_HOME/sessions``. The reader dispatches on
   the session's recorded brain and falls back across formats so an agent
   renders regardless of which brain produced it.
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
                             limit: int = CHAT_HISTORY_LIMIT,
                             *, brain: str | None = None) -> list[dict]:
    """Replay an agent session's transcript as ``{role, text}`` messages.

    *brain* selects the on-disk format: ``"codex"`` reads a Codex rollout,
    anything else (``"claude"``, ``"gateway"``, ``"stub"``) reads a Claude Code
    JSONL transcript. When the brain is unrecorded (``""``/``None``) and no
    Claude transcript resolves, a Codex rollout is tried as a last resort so a
    codex-brained session still renders for callers that don't record which
    brain wrote it (e.g. the hosted supervisor). A Claude session id never
    matches a Codex rollout filename, so the fallback is safe. An explicit
    non-codex brain never triggers the codex tree walk.
    """
    if brain == "codex":
        return read_codex_transcript_messages(session_id, limit)
    messages = _read_claude_transcript_messages(session_id, limit)
    if messages or brain not in (None, ""):
        return messages
    return read_codex_transcript_messages(session_id, limit)


def _read_claude_transcript_messages(session_id: str,
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


# --- Codex rollout replay ------------------------------------------------

def _codex_rollout_path(session_id: str) -> Path | None:
    """The Codex rollout file for *session_id*, if one exists.

    Codex writes one rollout per thread at
    ``$CODEX_HOME/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<session_id>.jsonl``.
    The session id is the thread id bobi records as the resume token, so a
    filename suffix match locates it regardless of the date partition. The
    newest match wins if a thread id somehow recurs.
    """
    if not session_id:
        return None
    from bobi.brain.codex_config import codex_home

    root = codex_home() / "sessions"
    if not root.exists():
        return None
    matches = sorted(root.rglob(f"*{session_id}.jsonl"))
    return matches[-1] if matches else None


def _codex_text(content) -> str:
    """Join the text blocks of a Codex ``message`` payload's content list."""
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if (isinstance(block, dict)
                and block.get("type") in ("input_text", "output_text", "text")):
            parts.append(block.get("text", ""))
    return "\n".join(p for p in parts if p)


# Codex injects non-conversational context into the model input as ``user``-role
# messages — an ``<environment_context>`` block each turn and a one-time
# ``<user_instructions>`` block. They are not anything the operator typed, so they
# must not render as chat bubbles. (The agent system prompt that Codex prepends to
# the first turn of a fresh thread is a separate, brain-level MVP gap tracked in
# codex.py — the reader deliberately does not try to strip that here.)
_CODEX_INJECTED_PREFIXES = ("<environment_context>", "<user_instructions>")


def _codex_injected(text: str) -> bool:
    return text.startswith(_CODEX_INJECTED_PREFIXES)


def read_codex_transcript_messages(session_id: str,
                                   limit: int = CHAT_HISTORY_LIMIT) -> list[dict]:
    """Replay a Codex rollout as ``{role, text}`` chat messages.

    Codex records a session as an NDJSON *rollout* rather than a Claude
    transcript. The conversational turns are ``response_item`` entries whose
    payload is a ``message`` with ``role`` user/assistant; developer/system
    rows, reasoning, tool calls, event bookkeeping, and Codex-injected context
    blocks (see :data:`_CODEX_INJECTED_PREFIXES`) are skipped. Mirrors
    :func:`read_transcript_messages`'s output shape.
    """
    path = _codex_rollout_path(session_id)
    if not path:
        return []

    out = []
    for line in path.read_text().splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") != "response_item":
            continue
        payload = obj.get("payload") or {}
        if payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _codex_text(payload.get("content")).strip()
        if not text or _codex_injected(text):
            continue
        out.append({"role": "agent" if role == "assistant" else "user",
                    "text": text})
    return out[-limit:]
