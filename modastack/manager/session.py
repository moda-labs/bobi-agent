"""Persistent manager session via ClaudeSDKClient.

The manager runs as a long-lived interactive Claude Code session.
Events are injected via client.query(), responses are read from
the message stream. Sessions survive restarts via resume.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from modastack.config import GlobalConfig
from modastack.sdk import get_cli_path, save_session_id, load_session_id

log = logging.getLogger(__name__)

SESSION_NAME = "moda-manager"
_ROLES_DIR = Path(__file__).resolve().parent.parent.parent / "roles" / "manager"
MANAGER_PROMPT_PATH = _ROLES_DIR / "prompt.md"
ACTIVITY_LOG = Path.home() / ".modastack" / "manager" / "activity.jsonl"

_client: Any | None = None
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_ready = threading.Event()
_last_response: str = ""
_state: str = "stopped"


def _load_manager_prompt() -> str:
    core = MANAGER_PROMPT_PATH.read_text()
    role_name = "engineering"
    try:
        config = GlobalConfig.load()
        role_name = getattr(config, "manager_role", None) or "engineering"
    except Exception:
        pass
    role_path = _ROLES_DIR / f"{role_name}.md"
    if role_path.exists():
        core += "\n\n" + role_path.read_text()
    return core


def _build_startup_prompt() -> str:
    prompt = _load_manager_prompt()
    config = GlobalConfig.load()
    repos = ", ".join(p.name for p in config.repos)
    return (
        f"You are the Modastack manager. "
        f"You are managing these repos: {repos}. "
        f"From now on, you will receive human messages and system event batches. "
        f"Respond naturally — the transport layer handles delivery. "
        f"Act directly using your tools.\n\n{prompt}"
    )


async def _run_manager() -> None:
    global _client, _state, _last_response

    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )

    saved_id = load_session_id(SESSION_NAME)

    config = GlobalConfig.load()
    cwd = str(Path(__file__).parent.parent)
    if config.repos:
        cwd = str(config.repos[0])

    options = ClaudeAgentOptions(
        cwd=cwd,
        permission_mode="bypassPermissions",
        cli_path=get_cli_path(),
        resume=saved_id or None,
        system_prompt={"type": "preset", "preset": "claude_code"},
    )

    _client = ClaudeSDKClient(options)

    startup_prompt = _build_startup_prompt() if not saved_id else None
    await _client.connect(startup_prompt)
    _state = "running"
    _ready.set()
    log.info(f"Manager session connected (resume={saved_id or 'new'})")

    try:
        async for msg in _client.receive_messages():
            if isinstance(msg, AssistantMessage):
                text_parts = []
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                if text_parts:
                    _last_response = "\n".join(text_parts)
                    _log_activity("response", {"text": _last_response[:500]})

            elif isinstance(msg, ResultMessage):
                save_session_id(SESSION_NAME, msg.session_id)
                _state = "waiting_input"
                _log_activity("Stop", {"session_id": msg.session_id})
    except Exception as e:
        log.error(f"Manager session error: {e}")
        _state = "error"
    finally:
        if _client:
            await _client.disconnect()
            _client = None
        _state = "stopped"


def _manager_thread() -> None:
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_run_manager())
    except Exception as e:
        log.error(f"Manager thread crashed: {e}")
    finally:
        _loop.close()
        _loop = None


def start_or_resume(cwd: str = None) -> bool:
    global _thread
    if is_alive():
        log.info("Manager session already running")
        return True

    _ready.clear()
    _thread = threading.Thread(target=_manager_thread, daemon=True, name="manager-sdk")
    _thread.start()

    if _ready.wait(timeout=60):
        log.info("Manager session ready")
        return True

    log.error("Manager session failed to start within 60s")
    return False


def inject(text: str) -> bool:
    """Send a message into the manager session. Thread-safe."""
    if not _client or not _loop:
        log.warning("Manager not running — cannot inject")
        return False

    future = asyncio.run_coroutine_threadsafe(_client.query(text), _loop)
    try:
        future.result(timeout=10)
        _state_update("working")
        _log_activity("UserPromptSubmit", {"text": text[:200]})
        return True
    except Exception as e:
        log.error(f"Manager inject failed: {e}")
        return False


def detect_state() -> str:
    """Return manager state: 'waiting_input' | 'working' | 'stopped' | 'error'"""
    return _state


def wait_until_ready(timeout: int = 60) -> bool:
    return _ready.wait(timeout=timeout)


def is_alive() -> bool:
    return _thread is not None and _thread.is_alive() and _state not in ("stopped", "error")


def read_last_response() -> str | None:
    return _last_response if _last_response else None


def capture(lines: int = 50) -> str:
    """For CLI compatibility -- returns last response."""
    return _last_response or "(no response yet)"


def get_session_id() -> str:
    return load_session_id(SESSION_NAME)


def _state_update(new_state: str) -> None:
    global _state
    _state = new_state


def _log_activity(event: str, data: dict | None = None) -> None:
    ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {"event": event, "ts": time.time()}
    if data:
        entry.update(data)
    with open(ACTIVITY_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
