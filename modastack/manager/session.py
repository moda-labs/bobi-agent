"""Persistent manager session via ClaudeSDKClient.

The manager runs as a long-lived interactive Claude Code session.
Events are injected via client.query(), responses are read from
the message stream. Sessions survive restarts via the registry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from modastack.config import GlobalConfig
from modastack.sdk import (
    get_cli_path, save_session_id, load_session_id, log_activity,
    get_registry, SessionEntry,
)

PROMPT_HASH_PATH = Path.home() / ".modastack" / "sessions" / "prompt_hash"

log = logging.getLogger(__name__)

SESSION_NAME = "moda-manager"
_ROLES_DIR = Path(__file__).resolve().parent.parent.parent / "roles" / "manager"
MANAGER_PROMPT_PATH = _ROLES_DIR / "prompt.md"

_client: Any | None = None
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_ready = threading.Event()
_keep_alive: asyncio.Event | None = None
_last_response: str = ""
_state: str = "stopped"
_last_inject_error: str = ""
# Serializes inject() across callers (event drain loop + workflow
# consultation nodes run on different threads but share one SDK client).
_inject_lock = threading.Lock()
# Called from _drain_turn whenever the model emits text.  The consumer
# sets this to post Slack replies as they arrive rather than reading
# _last_response after the drain (which can return stale text when the
# SDK yields a leftover ResultMessage from a previous turn).
_response_callback: Any | None = None


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


def _list_workflows() -> str:
    try:
        from modastack.workflow.triggers import WORKFLOWS_DIR
        from modastack.workflow.schema import load_workflow
        lines = []
        for f in sorted(WORKFLOWS_DIR.glob("*.yaml")):
            try:
                wf = load_workflow(f)
                lines.append(f"- {wf.name}: trigger={wf.trigger.event}, {len(wf.nodes)} nodes")
            except Exception:
                continue
        return "\n".join(lines) if lines else "No workflows found."
    except Exception:
        return ""


def _build_startup_prompt() -> str:
    prompt = _load_manager_prompt()
    config = GlobalConfig.load()
    repos = ", ".join(p.name for p in config.repos)
    workflows = _list_workflows()
    return (
        f"You are the Modastack manager. "
        f"You are managing these repos: {repos}. "
        f"You receive ALL events and decide what to do with each one. "
        f"Act directly using your tools.\n\n{prompt}\n\n"
        f"## Available workflows\n\n{workflows}"
    )


async def _drain_turn() -> None:
    """Drain receive_response() for one turn until ResultMessage."""
    global _last_response, _state

    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    registry = get_registry()
    # Clear the previous turn's reply before draining. Otherwise a turn that
    # ends without emitting any assistant text (e.g. tool-only) leaves a stale
    # response in place, and a consultation node reads the wrong answer.
    _last_response = ""
    _state_update("working")

    try:
        async for msg in _client.receive_response():
            if isinstance(msg, AssistantMessage):
                text_parts = []
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                if text_parts:
                    _last_response = "\n".join(text_parts)
                    log_activity("response", {"text": _last_response[:500]}, session=SESSION_NAME)
                    if _response_callback:
                        try:
                            _response_callback(_last_response)
                        except Exception as cb_err:
                            log.warning(f"Response callback failed: {cb_err}")

            elif isinstance(msg, ResultMessage):
                save_session_id(SESSION_NAME, msg.session_id)
                _state_update("waiting_input")
                registry.update(SESSION_NAME, status="idle", session_id=msg.session_id)
                log_activity("Stop", {"session_id": msg.session_id}, session=SESSION_NAME)
    except Exception as e:
        log.error(f"Drain failed ({type(e).__name__}): {e}")
        _state = "error"
        registry.update(SESSION_NAME, status="error")
        if _keep_alive is not None:
            _keep_alive.set()


async def _run_manager() -> None:
    global _client, _state, _keep_alive

    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
    )

    saved_id = load_session_id(SESSION_NAME)

    config = GlobalConfig.load()
    cwd = str(Path(__file__).parent.parent)
    if config.repos:
        cwd = str(config.repos[0])

    for attempt in range(2):
        resume_id = saved_id if attempt == 0 else None

        options = ClaudeAgentOptions(
            cwd=cwd,
            permission_mode="bypassPermissions",
            cli_path=get_cli_path(),
            resume=resume_id or None,
            system_prompt={"type": "preset", "preset": "claude_code"},
            strict_mcp_config=True,
        )

        _client = ClaudeSDKClient(options)

        startup_prompt = _build_startup_prompt() if not resume_id else None
        try:
            await _client.connect(startup_prompt)
            break
        except Exception as e:
            if resume_id and attempt == 0:
                log.warning(f"Resume failed (stale session?), retrying fresh: {e}")
                save_session_id(SESSION_NAME, "")
                _client = None
                continue
            raise

    _state = "running"
    _ready.set()
    log.info(f"Manager session connected (resume={resume_id or 'new'})")

    registry = get_registry()
    registry.register(SessionEntry(
        name=SESSION_NAME, session_id=saved_id or "", role="manager",
        cwd=cwd, status="idle",
    ))

    prompt_text = _build_startup_prompt()
    prompt_hash = hashlib.sha256(prompt_text.encode()).hexdigest()[:16]

    try:
        if not resume_id:
            await _drain_turn()
        else:
            saved_hash = PROMPT_HASH_PATH.read_text().strip() if PROMPT_HASH_PATH.exists() else ""
            if saved_hash != prompt_hash:
                log.info(f"Prompt changed ({saved_hash[:8]}→{prompt_hash[:8]}), re-injecting")
                await _client.query(
                    "Your instructions have been updated. "
                    "Read and follow these from now on:\n\n" + prompt_text
                )
                await _drain_turn()
            _state_update("waiting_input")

        PROMPT_HASH_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROMPT_HASH_PATH.write_text(prompt_hash)
        # Keep the event loop alive — inject() schedules work on it
        _keep_alive = asyncio.Event()
        await _keep_alive.wait()
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
    global _thread, _keep_alive
    if is_alive():
        log.info("Manager session already running")
        return True

    if _keep_alive is not None:
        _keep_alive.set()
    if _thread is not None and _thread.is_alive():
        log.info("Waiting for old manager thread to exit")
        _thread.join(timeout=15)
        if _thread.is_alive():
            log.warning("Old manager thread did not exit — proceeding anyway")
    _thread = None
    _keep_alive = None

    _ready.clear()
    _thread = threading.Thread(target=_manager_thread, daemon=True, name="manager-sdk")
    _thread.start()

    if _ready.wait(timeout=60):
        log.info("Manager session ready")
        return True

    log.error("Manager session failed to start within 60s")
    return False


async def _inject_and_drain(text: str) -> None:
    log.info("inject: sending query")
    await _client.query(text)
    log.info("inject: query sent, draining")
    await _drain_turn()
    log.info(f"inject: drain complete, state={_state}")


def inject_capture(
    text: str, timeout: int = 300, wait_for_ready: int = 0
) -> tuple[bool, str]:
    """Inject text, block until the turn ends, and return ``(ok, response)``.

    The response is snapshotted from `_last_response` **while `_inject_lock`
    is still held**, so it is guaranteed to be the reply produced by *this*
    injected turn.

    This is the race-free way to read a turn's response. A separate
    `read_last_response()` call cannot offer that guarantee: `_last_response`
    is a single module-global shared by every inject caller (event drain loop,
    workflow consultation nodes, dashboard `/api/consult` — all on different
    threads sharing one SDK client, see `_inject_lock`). `inject()` releases
    the lock the instant it returns, so a concurrent inject can clear and
    overwrite `_last_response` before the caller reads it — delivering the
    wrong turn's text (e.g. a Slack reply shifted onto an unrelated message).
    Capturing under the lock closes that window.

    Serialized via `_inject_lock`: overlapping queries on the single shared
    SDK client corrupt the stream.

    `wait_for_ready` is how many seconds to wait for a busy manager to
    return to `waiting_input` before giving up. It defaults to 0, which
    fails fast — preserving the drain loop's drop-on-busy behavior.
    Workflow consultation nodes pass a positive value so they queue behind
    whatever the manager is currently doing instead of failing outright.

    On failure, returns ``(False, "")`` and sets `last_inject_error()` with
    the reason so callers can surface it rather than reporting an opaque
    "inject failed".
    """
    global _state, _last_inject_error

    if not _client or not _loop:
        _last_inject_error = "manager not running"
        log.warning("Manager not running — cannot inject")
        return False, ""

    with _inject_lock:
        deadline = time.monotonic() + max(0, wait_for_ready)
        while _state != "waiting_input":
            if _state in ("stopped", "error"):
                _last_inject_error = f"manager state={_state}"
                log.warning(f"Manager not injectable (state={_state}) — dropping inject")
                return False, ""
            if time.monotonic() >= deadline:
                _last_inject_error = f"manager busy (state={_state})"
                log.warning(
                    f"Manager not ready for input (state={_state}) after "
                    f"{wait_for_ready}s — dropping inject"
                )
                return False, ""
            time.sleep(1)

        log.info(f"Inject: {text[:100]}")
        log_activity("UserPromptSubmit", {"text": text[:200]}, session=SESSION_NAME)
        future = asyncio.run_coroutine_threadsafe(_inject_and_drain(text), _loop)
        try:
            future.result(timeout=timeout)
            _last_inject_error = ""
            # Snapshot the reply while still holding _inject_lock so it is
            # bound to this turn and cannot be clobbered by a concurrent inject.
            return True, _last_response
        except TimeoutError:
            _last_inject_error = f"manager did not finish within {timeout}s"
            log.error(f"Manager inject timed out after {timeout}s")
            future.cancel()
            return False, ""
        except Exception as e:
            _last_inject_error = f"{type(e).__name__}: {e}"
            log.error(f"Manager inject failed ({type(e).__name__}): {e}")
            _state = "error"
            if _keep_alive is not None:
                _keep_alive.set()
            future.cancel()
            return False, ""


def inject(text: str, timeout: int = 300, wait_for_ready: int = 0) -> bool:
    """Inject text and block until the turn ends; return whether it succeeded.

    Thin wrapper over `inject_capture()` for callers that only need the
    success flag. Callers that need the turn's response should use
    `inject_capture()` so the reply is captured atomically with the inject
    rather than re-read from the shared global afterward.
    """
    ok, _ = inject_capture(text, timeout=timeout, wait_for_ready=wait_for_ready)
    return ok


def last_inject_error() -> str:
    """Reason the most recent inject() returned False (empty if it succeeded)."""
    return _last_inject_error


def detect_state() -> str:
    return _state


def set_response_callback(fn) -> None:
    """Set a callback that fires whenever the model emits text.

    The consumer uses this to post Slack replies as they stream in,
    bypassing the stale-response problem with read-after-drain.
    """
    global _response_callback
    _response_callback = fn


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
