"""Persistent manager session via ClaudeSDKClient.

The manager runs as a long-lived interactive Claude Code session.
Events are injected via client.query(), responses are read from
the message stream. Sessions survive restarts via the registry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from modastack.config import GlobalConfig
from modastack.sdk import (
    get_cli_path, save_session_id, load_session_id, log_activity,
    get_registry, SessionEntry,
)

log = logging.getLogger(__name__)

SESSION_NAME = "moda-manager"
_ROLES_DIR = Path(__file__).resolve().parent.parent.parent / "roles" / "manager"
MANAGER_PROMPT_PATH = _ROLES_DIR / "prompt.md"

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


def _relay_to_slack(text: str) -> None:
    try:
        config = GlobalConfig.load()
        token = config.slack_bot_token
        channel = config.slack_dm_channel or "D0B51JP1N4C"
        if not token:
            return
        text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
        text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', text)
        if len(text) > 3000:
            text = text[:3000] + '\n_(truncated)_'
        payload = json.dumps({"channel": channel, "text": text}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log.debug(f"Slack relay failed: {e}")


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

    registry = get_registry()
    registry.register(SessionEntry(
        name=SESSION_NAME, session_id=saved_id or "", role="manager",
        cwd=cwd, status="idle",
    ))

    try:
        async for msg in _client.receive_messages():
            if isinstance(msg, AssistantMessage):
                text_parts = []
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                if text_parts:
                    _last_response = "\n".join(text_parts)
                    log_activity("response", {"text": _last_response[:500]})
                    _relay_to_slack(_last_response)

            elif isinstance(msg, ResultMessage):
                save_session_id(SESSION_NAME, msg.session_id)
                _state = "waiting_input"
                registry.update(SESSION_NAME, status="idle", session_id=msg.session_id)
                log_activity("Stop", {"session_id": msg.session_id})
    except Exception as e:
        log.error(f"Manager session error: {e}")
        _state = "error"
        registry.update(SESSION_NAME, status="error")
    finally:
        if _client:
            await _client.disconnect()
            _client = None
        _state = "stopped"
        registry.update(SESSION_NAME, status="stopped")


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
    if not _client or not _loop:
        log.warning("Manager not running — cannot inject")
        return False

    future = asyncio.run_coroutine_threadsafe(_client.query(text), _loop)
    try:
        future.result(timeout=10)
        _state_update("working")
        log_activity("UserPromptSubmit", {"text": text[:200]})
        return True
    except Exception as e:
        log.error(f"Manager inject failed: {e}")
        return False


def detect_state() -> str:
    return _state


def wait_until_ready(timeout: int = 60) -> bool:
    return _ready.wait(timeout=timeout)


def is_alive() -> bool:
    return _thread is not None and _thread.is_alive() and _state not in ("stopped", "error")


def read_last_response() -> str | None:
    return _last_response if _last_response else None


def capture(lines: int = 50) -> str:
    return _last_response or "(no response yet)"


def get_session_id() -> str:
    return load_session_id(SESSION_NAME)


def _state_update(new_state: str) -> None:
    global _state
    _state = new_state
    registry = get_registry()
    status_map = {"working": "running", "waiting_input": "idle"}
    registry.update(SESSION_NAME, status=status_map.get(new_state, new_state))
