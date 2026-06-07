"""Unified session — Claude Code client with an inbox.

Every session is identical: a ClaudeSDKClient connected to an inbox
drain loop. Messages arrive via the inbox HTTP server and are
injected into the Claude session in order. The only difference
between a "manager" and an "agent" is what feeds the inbox.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from modastack.inbox import Inbox, Message
from modastack.sdk import (
    get_cli_path,
    save_session_id,
    load_session_id,
    log_activity,
    get_registry,
    SessionEntry,
)

log = logging.getLogger(__name__)


class Session:
    """A Claude Code session with an inbox for receiving messages."""

    def __init__(
        self,
        name: str,
        cwd: str,
        system_prompt: dict | None = None,
        on_response=None,
        extra_options: dict | None = None,
        role: str = "engineer",
    ) -> None:
        self.name = name
        self.cwd = cwd
        self.role = role
        self.inbox = Inbox(name)
        self._system_prompt = system_prompt or {
            "type": "preset",
            "preset": "claude_code",
        }
        self._on_response = on_response
        self._extra_options = extra_options or {}

        self._client = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._keep_alive: asyncio.Event | None = None
        self._state = "stopped"
        self._last_response = ""
        self._last_is_error = False
        self._total_cost_usd = 0.0
        self._total_duration_ms = 0
        self._total_turns = 0

    @property
    def port(self) -> int:
        return self.inbox.port

    def detect_state(self) -> str:
        return self._state

    async def _drain_turn(self) -> str:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        self._last_response = ""
        self._state = "working"
        registry = get_registry()
        registry.update(self.name, status="running")

        try:
            async for msg in self._client.receive_response():
                if isinstance(msg, AssistantMessage):
                    text_parts = [
                        b.text for b in msg.content if isinstance(b, TextBlock)
                    ]
                    if text_parts:
                        self._last_response = "\n".join(text_parts)
                        log_activity(
                            "response",
                            {"text": self._last_response[:500]},
                            session=self.name,
                        )
                        if self._on_response and self._last_response.strip():
                            try:
                                self._on_response(self._last_response)
                            except Exception:
                                pass
                elif isinstance(msg, ResultMessage):
                    save_session_id(self.name, msg.session_id)
                    self._last_is_error = msg.is_error
                    self._total_cost_usd += msg.total_cost_usd or 0.0
                    self._total_duration_ms += msg.duration_ms
                    self._total_turns += msg.num_turns
                    if msg.is_error:
                        self._state = "error"
                        log.error(f"Session '{self.name}' error: {self._last_response[:200]}")
                        registry.update(self.name, status="error", session_id=msg.session_id)
                    else:
                        self._state = "waiting_input"
                        registry.update(self.name, status="idle", session_id=msg.session_id)
        except Exception as e:
            log.error(f"Drain failed for '{self.name}': {e}")
            self._state = "error"
            registry.update(self.name, status="error")

        return self._last_response

    async def _process_message(self, msg: Message) -> None:
        """Wait for ready state, inject a message, and optionally respond."""
        for _ in range(600):
            if self._state == "waiting_input":
                break
            if self._state in ("stopped", "error"):
                if msg.wait:
                    self.inbox.respond(msg.id, f"session {self._state}")
                return
            await asyncio.sleep(0.5)
        else:
            log.warning(f"Session '{self.name}' never became ready for inbox message")
            if msg.wait:
                self.inbox.respond(msg.id, "session not ready")
            return

        try:
            log_activity(
                "inbox",
                {"sender": msg.sender, "text": msg.text[:200]},
                session=self.name,
            )
            await self._client.query(msg.text)
            response = await self._drain_turn()
            if msg.wait:
                self.inbox.respond(msg.id, response)
        except Exception as e:
            log.error(f"Inbox processing failed for '{self.name}': {e}")
            if msg.wait:
                self.inbox.respond(msg.id, f"error: {e}")
            self._state = "error"

    async def _inbox_loop(self) -> None:
        loop = asyncio.get_running_loop()

        while True:
            msg = await loop.run_in_executor(
                None, lambda: self.inbox.recv(timeout=2.0)
            )
            if msg is None:
                if self._keep_alive and self._keep_alive.is_set():
                    break
                continue

            await self._process_message(msg)

    async def _run(self, startup_prompt: str | None = None) -> None:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        saved_id = load_session_id(self.name)
        resume_id = saved_id or None

        options = ClaudeAgentOptions(
            cwd=self.cwd,
            permission_mode="bypassPermissions",
            cli_path=get_cli_path(),
            resume=resume_id,
            system_prompt=self._system_prompt,
            **self._extra_options,
        )

        self._client = ClaudeSDKClient(options)

        try:
            connect_prompt = startup_prompt if not resume_id else None
            await self._client.connect(connect_prompt)
        except Exception as e:
            if resume_id:
                log.warning(f"Resume failed for '{self.name}', retrying fresh: {e}")
                save_session_id(self.name, "")
                options = ClaudeAgentOptions(
                    cwd=self.cwd,
                    permission_mode="bypassPermissions",
                    cli_path=get_cli_path(),
                    system_prompt=self._system_prompt,
                    **self._extra_options,
                )
                self._client = ClaudeSDKClient(options)
                await self._client.connect(startup_prompt)
            else:
                raise

        self._state = "running"
        registry = get_registry()
        registry.update(self.name, status="running")

        if startup_prompt and not resume_id:
            await self._drain_turn()
        elif startup_prompt and resume_id:
            await self._client.query(startup_prompt)
            await self._drain_turn()
        else:
            self._state = "waiting_input"
            registry.update(self.name, status="idle")

        self._ready.set()
        log.info(f"Session '{self.name}' ready (port={self.inbox.port})")

        inbox_task = asyncio.create_task(self._inbox_loop())

        self._keep_alive = asyncio.Event()
        try:
            await self._keep_alive.wait()
        finally:
            inbox_task.cancel()
            try:
                await inbox_task
            except asyncio.CancelledError:
                pass
            if self._client:
                await self._client.disconnect()
                self._client = None
            self._state = "stopped"
            registry.update(self.name, status="stopped")

    def _thread_target(self, startup_prompt: str | None) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run(startup_prompt))
        except Exception as e:
            log.error(f"Session '{self.name}' crashed: {e}")
            self._state = "error"
        finally:
            self._loop.close()
            self._loop = None

    def start(self, startup_prompt: str | None = None, timeout: int = 120) -> bool:
        """Start the session in a daemon thread.

        Starts the inbox HTTP server immediately so the session is
        addressable before the Claude client finishes connecting.
        Returns True when the session is ready for messages.
        """
        if self._thread and self._thread.is_alive():
            return True

        self.inbox.start()

        registry = get_registry()
        registry.register(
            SessionEntry(
                name=self.name,
                session_id=load_session_id(self.name) or "",
                role=self.role,
                cwd=self.cwd,
                status="starting",
                inbox_port=self.inbox.port,
                pid=os.getpid(),
            )
        )

        self._ready.clear()
        self._thread = threading.Thread(
            target=self._thread_target,
            args=(startup_prompt,),
            daemon=True,
            name=f"session-{self.name}",
        )
        self._thread.start()

        if self._ready.wait(timeout=timeout):
            return True
        log.error(f"Session '{self.name}' failed to start within {timeout}s")
        return False

    def get_session_id(self) -> str:
        return load_session_id(self.name)

    def stop(self) -> None:
        if self._keep_alive:
            self._keep_alive.set()
        if self._thread:
            self._thread.join(timeout=15)
        self.inbox.close()

    def is_alive(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self._state not in ("stopped", "error")
        )

    def wait_until_ready(self, timeout: int = 60) -> bool:
        return self._ready.wait(timeout=timeout)
