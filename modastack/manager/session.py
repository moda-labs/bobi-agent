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

from modastack.sdk import (
    get_cli_path, save_session_id, load_session_id, log_activity,
    get_registry, SessionEntry, SESSION_DIR,
)

log = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
MANAGER_BASE_PATH = _PROMPTS_DIR / "manager_base.md"


class ManagerSession:
    """A single manager session bound to one repo."""

    def __init__(self, repo_path: Path, session_name: str | None = None):
        self.repo_path = repo_path
        self.session_name = session_name or f"moda-mgr-{repo_path.name}"
        self.cwd = str(repo_path)

        self._client: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._keep_alive: asyncio.Event | None = None
        self._last_response: str = ""
        self._state: str = "stopped"
        self._last_inject_error: str = ""
        self._inject_lock = threading.Lock()
        self._response_callback: Any | None = None
        self._prompt_hash_path = SESSION_DIR / self.session_name / "prompt_hash"

    def _load_manager_prompt(self) -> str:
        core = MANAGER_BASE_PATH.read_text()

        # Load global user manager role prompt
        global_mgr = Path.home() / ".modastack" / "manager.md"
        if global_mgr.exists():
            core += "\n\n" + global_mgr.read_text()

        # Load this repo's manager role prompt
        repo_mgr = self.repo_path / ".modastack" / "manager.md"
        if repo_mgr.exists():
            core += f"\n\n## {self.repo_path.name} policies\n\n" + repo_mgr.read_text()

        return core

    def _list_workflows(self) -> str:
        try:
            from modastack.workflow.triggers import WORKFLOWS_DIR, USER_WORKFLOWS_DIR
            from modastack.workflow.schema import load_workflow

            lines = []
            sources = [WORKFLOWS_DIR]
            if USER_WORKFLOWS_DIR.exists():
                sources.append(USER_WORKFLOWS_DIR)
            repo_wf = self.repo_path / ".modastack" / "workflows"
            if repo_wf.exists():
                sources.append(repo_wf)

            seen = set()
            for d in reversed(sources):
                for f in sorted(d.glob("*.yaml")):
                    if f.stem in seen:
                        continue
                    seen.add(f.stem)
                    try:
                        wf = load_workflow(f)
                        lines.append(f"- {wf.name}: trigger={wf.trigger.event}, {len(wf.nodes)} nodes")
                    except Exception:
                        continue
            return "\n".join(lines) if lines else "No workflows found."
        except Exception:
            return ""

    def _build_startup_prompt(self) -> str:
        prompt = self._load_manager_prompt()
        workflows = self._list_workflows()
        return (
            f"You are the Modastack manager. "
            f"You are managing repo: {self.repo_path.name}. "
            f"You receive ALL events and decide what to do with each one. "
            f"Act directly using your tools.\n\n{prompt}\n\n"
            f"## Available workflows\n\n{workflows}"
        )

    async def _drain_turn(self) -> None:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

        registry = get_registry()
        self._last_response = ""
        self._state_update("working")

        try:
            async for msg in self._client.receive_response():
                if isinstance(msg, AssistantMessage):
                    text_parts = []
                    tool_parts = []
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            tool_input = str(block.input.get("command", block.input.get("description", "")))[:150] if isinstance(block.input, dict) else str(block.input)[:150]
                            tool_parts.append(f"```{block.name}: {tool_input}```")
                            log_activity("tool_use", {"tool": block.name, "input": str(block.input)[:500]}, session=self.session_name)
                    if text_parts or tool_parts:
                        combined = "\n".join(text_parts + tool_parts) if tool_parts else "\n".join(text_parts)
                        self._last_response = "\n".join(text_parts) if text_parts else ""
                        log_activity("response", {"text": combined[:500]}, session=self.session_name)
                        if self._response_callback and combined.strip():
                            try:
                                self._response_callback(combined)
                            except Exception as cb_err:
                                log.warning(f"Response callback failed: {cb_err}")

                elif isinstance(msg, ResultMessage):
                    save_session_id(self.session_name, msg.session_id)
                    self._state_update("waiting_input")
                    registry.update(self.session_name, status="idle", session_id=msg.session_id)
                    log_activity("Stop", {"session_id": msg.session_id}, session=self.session_name)
        except Exception as e:
            log.error(f"Drain failed ({type(e).__name__}): {e}")
            self._state = "error"
            registry.update(self.session_name, status="error")
            if self._keep_alive is not None:
                self._keep_alive.set()

    async def _run(self) -> None:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        saved_id = load_session_id(self.session_name)

        for attempt in range(2):
            resume_id = saved_id if attempt == 0 else None

            options = ClaudeAgentOptions(
                cwd=self.cwd,
                permission_mode="bypassPermissions",
                cli_path=get_cli_path(),
                resume=resume_id or None,
                system_prompt={"type": "preset", "preset": "claude_code"},
                strict_mcp_config=True,
            )

            self._client = ClaudeSDKClient(options)

            startup_prompt = self._build_startup_prompt() if not resume_id else None
            try:
                await self._client.connect(startup_prompt)
                break
            except Exception as e:
                if resume_id and attempt == 0:
                    log.warning(f"Resume failed (stale session?), retrying fresh: {e}")
                    save_session_id(self.session_name, "")
                    self._client = None
                    continue
                raise

        self._state = "running"
        self._ready.set()
        log.info(f"Manager session '{self.session_name}' connected (resume={resume_id or 'new'})")

        registry = get_registry()
        registry.register(SessionEntry(
            name=self.session_name, session_id=saved_id or "", role="manager",
            cwd=self.cwd, status="idle",
        ))

        prompt_text = self._build_startup_prompt()
        prompt_hash = hashlib.sha256(prompt_text.encode()).hexdigest()[:16]

        try:
            if not resume_id:
                await self._drain_turn()
            else:
                saved_hash = self._prompt_hash_path.read_text().strip() if self._prompt_hash_path.exists() else ""
                if saved_hash != prompt_hash:
                    log.info(f"Prompt changed ({saved_hash[:8]}→{prompt_hash[:8]}), re-injecting")
                    await self._client.query(
                        "Your instructions have been updated. "
                        "Read and follow these from now on:\n\n" + prompt_text
                    )
                    await self._drain_turn()
                self._state_update("waiting_input")

            self._prompt_hash_path.parent.mkdir(parents=True, exist_ok=True)
            self._prompt_hash_path.write_text(prompt_hash)
            self._keep_alive = asyncio.Event()
            await self._keep_alive.wait()
        except Exception as e:
            log.error(f"Manager session error: {e}")
            self._state = "error"
            registry.update(self.session_name, status="error")
        finally:
            if self._client:
                await self._client.disconnect()
                self._client = None
            self._state = "stopped"
            registry.update(self.session_name, status="stopped")

    def _thread_target(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        except Exception as e:
            log.error(f"Manager thread crashed: {e}")
        finally:
            self._loop.close()
            self._loop = None

    def start_or_resume(self) -> bool:
        if self.is_alive():
            log.info(f"Manager session '{self.session_name}' already running")
            return True

        if self._keep_alive is not None:
            self._keep_alive.set()
        if self._thread is not None and self._thread.is_alive():
            log.info("Waiting for old manager thread to exit")
            self._thread.join(timeout=15)
            if self._thread.is_alive():
                log.warning("Old manager thread did not exit — proceeding anyway")
        self._thread = None
        self._keep_alive = None

        self._ready.clear()
        self._thread = threading.Thread(
            target=self._thread_target, daemon=True,
            name=f"manager-{self.session_name}",
        )
        self._thread.start()

        if self._ready.wait(timeout=60):
            log.info(f"Manager session '{self.session_name}' ready")
            return True

        log.error(f"Manager session '{self.session_name}' failed to start within 60s")
        return False

    async def _inject_and_drain(self, text: str) -> None:
        log.info("inject: sending query")
        await self._client.query(text)
        log.info("inject: query sent, draining")
        await self._drain_turn()
        log.info(f"inject: drain complete, state={self._state}")

    def inject_capture(
        self, text: str, timeout: int = 300, wait_for_ready: int = 0
    ) -> tuple[bool, str]:
        if not self._client or not self._loop:
            self._last_inject_error = "manager not running"
            log.warning("Manager not running — cannot inject")
            return False, ""

        with self._inject_lock:
            deadline = time.monotonic() + max(0, wait_for_ready)
            while self._state != "waiting_input":
                if self._state in ("stopped", "error"):
                    self._last_inject_error = f"manager state={self._state}"
                    log.warning(f"Manager not injectable (state={self._state}) — dropping inject")
                    return False, ""
                if time.monotonic() >= deadline:
                    self._last_inject_error = f"manager busy (state={self._state})"
                    log.warning(
                        f"Manager not ready for input (state={self._state}) after "
                        f"{wait_for_ready}s — dropping inject"
                    )
                    return False, ""
                time.sleep(1)

            log.info(f"Inject: {text[:100]}")
            log_activity("UserPromptSubmit", {"text": text[:200]}, session=self.session_name)
            future = asyncio.run_coroutine_threadsafe(self._inject_and_drain(text), self._loop)
            try:
                future.result(timeout=timeout)
                self._last_inject_error = ""
                return True, self._last_response
            except TimeoutError:
                self._last_inject_error = f"manager did not finish within {timeout}s"
                log.error(f"Manager inject timed out after {timeout}s")
                future.cancel()
                self._state = "waiting_input"
                return False, ""
            except Exception as e:
                self._last_inject_error = f"{type(e).__name__}: {e}"
                log.error(f"Manager inject failed ({type(e).__name__}): {e}")
                self._state = "error"
                if self._keep_alive is not None:
                    self._keep_alive.set()
                future.cancel()
                return False, ""

    def inject(self, text: str, timeout: int = 300, wait_for_ready: int = 0) -> bool:
        ok, _ = self.inject_capture(text, timeout=timeout, wait_for_ready=wait_for_ready)
        return ok

    def last_inject_error(self) -> str:
        return self._last_inject_error

    def detect_state(self) -> str:
        return self._state

    def set_response_callback(self, fn) -> None:
        self._response_callback = fn

    def wait_until_ready(self, timeout: int = 60) -> bool:
        return self._ready.wait(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._state not in ("stopped", "error")

    def read_last_response(self) -> str | None:
        return self._last_response if self._last_response else None

    def capture(self, lines: int = 50) -> str:
        return self._last_response or "(no response yet)"

    def get_session_id(self) -> str:
        return load_session_id(self.session_name)

    def _state_update(self, new_state: str) -> None:
        self._state = new_state
        registry = get_registry()
        status_map = {"working": "running", "waiting_input": "idle"}
        registry.update(self.session_name, status=status_map.get(new_state, new_state))


# ---------------------------------------------------------------------------
# Backward-compat module-level API
#
# These delegate to _default_session, which is set by consumer.run().
# Callers that haven't been updated to use ManagerSession directly
# (dashboard, cli, etc.) continue to work through these wrappers.
# ---------------------------------------------------------------------------

_default_session: ManagerSession | None = None


def set_default_session(session: ManagerSession) -> None:
    """Set the module-level default session (called by consumer.run)."""
    global _default_session
    _default_session = session


def get_default_session() -> ManagerSession | None:
    return _default_session


def start_or_resume(cwd: str = None) -> bool:
    if _default_session is None:
        log.warning("No default session configured")
        return False
    return _default_session.start_or_resume()


def inject_capture(
    text: str, timeout: int = 300, wait_for_ready: int = 0
) -> tuple[bool, str]:
    if _default_session is None:
        return False, ""
    return _default_session.inject_capture(text, timeout=timeout, wait_for_ready=wait_for_ready)


def inject(text: str, timeout: int = 300, wait_for_ready: int = 0) -> bool:
    if _default_session is None:
        return False
    return _default_session.inject(text, timeout=timeout, wait_for_ready=wait_for_ready)


def last_inject_error() -> str:
    if _default_session is None:
        return "no session"
    return _default_session.last_inject_error()


def detect_state() -> str:
    if _default_session is None:
        return "stopped"
    return _default_session.detect_state()


def set_response_callback(fn) -> None:
    if _default_session is not None:
        _default_session.set_response_callback(fn)


def wait_until_ready(timeout: int = 60) -> bool:
    if _default_session is None:
        return False
    return _default_session.wait_until_ready(timeout=timeout)


def is_alive() -> bool:
    if _default_session is None:
        return False
    return _default_session.is_alive()


def read_last_response() -> str | None:
    if _default_session is None:
        return None
    return _default_session.read_last_response()


def capture(lines: int = 50) -> str:
    if _default_session is None:
        return "(no session)"
    return _default_session.capture(lines)


def get_session_id() -> str:
    if _default_session is None:
        return ""
    return _default_session.get_session_id()
