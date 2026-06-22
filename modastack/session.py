"""Unified session — Claude Code client with an inbox.

Every session is identical: a ClaudeSDKClient connected to an inbox
drain loop. Each session subscribes to its own ``inbox/<self>`` topic on the
event server and injects arriving messages into the Claude session in order.
The only difference between a "manager" and an "agent" is what extra topics it
subscribes to (the manager also subscribes to external resource topics).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
from pathlib import Path

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

# Default rotation cap — absolute context-fill tokens, not a window fraction.
DEFAULT_ROTATION_TOKEN_CAP = 275_000

# Control sentinel for `modastack compact` (#433). Delivered as an inbox
# message body; the run loop recognizes it, flags rotation, and never forwards
# it to the model. An exact-match constant, so a human message can't trip it.
COMPACT_SENTINEL = "\x00__modastack_compact__\x00"


def _context_fill_tokens(usage: dict | None) -> int:
    """True context-window fill for a turn.

    The prompt sent to the model is ``input_tokens`` (uncached) +
    ``cache_read_input_tokens`` (the cached prefix) +
    ``cache_creation_input_tokens`` (newly cached this turn). With prompt
    caching on, almost the whole conversation lands in ``cache_read`` and
    ``input_tokens`` alone is a wildly low proxy — which is why the old
    rotation check (input_tokens only) never fired and the manager ran to
    ~424K. Sum all three for the real fill.
    """
    if not usage:
        return 0
    return (
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
    )

# Background event-subscription retry cadence (#409). When the initial
# registration handshake with the event server times out, the session boots
# anyway and a daemon thread keeps retrying with capped exponential backoff —
# events are queued/sequenced/resumable, so a late registration just resumes
# the stream from the saved cursor.
SUBSCRIPTION_RETRY_BASE = 2.0
SUBSCRIPTION_RETRY_MAX = 60.0


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
        subscribe: list[str] | None = None,
    ) -> None:
        self.name = name
        self.cwd = cwd
        self.role = role
        # Extra event topics beyond this session's own inbox/<self> (e.g. the
        # manager's external resource topics). inbox/<self> is always added.
        self._subscribe = list(subscribe or [])
        self.inbox = Inbox(name)
        self._system_prompt = system_prompt or {
            "type": "preset",
            "preset": "claude_code",
        }
        self._on_response = on_response
        opts = extra_options or {}
        self._rotation_token_cap = opts.pop("rotation_token_cap", DEFAULT_ROTATION_TOKEN_CAP)
        self._extra_options = opts

        self._client = None
        self._subscription = None
        self._sub_retry_stop = threading.Event()
        self._sub_retry_thread: threading.Thread | None = None
        # Guards the hand-off of a background-registered subscription to stop().
        self._sub_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._keep_alive: asyncio.Event | None = None
        self._input_ready: asyncio.Event | None = None
        self._state = "stopped"
        self._last_response = ""
        self._last_is_error = False
        self._total_cost_usd = 0.0
        self._total_duration_ms = 0
        self._total_turns = 0

        # Context rotation state (Steps 1-4, #273)
        self._rotate_pending = False
        # Manual `compact` bypasses the no-op-flush guard (rotate even if the
        # decision log didn't change — the durable INDEX.md is already there).
        self._rotate_force = False
        self._rotate_reason = "context_cap"
        self._rotation_count = 0
        self._flush_snapshot_mtime = 0.0
        self._flush_snapshot_hash = ""

    def detect_state(self) -> str:
        return self._state

    def _set_state(self, state: str) -> None:
        """Update state and wake any waiter when the session becomes idle or terminal."""
        self._state = state
        if state in ("waiting_input", "stopped", "error") and self._input_ready:
            self._input_ready.set()

    # -----------------------------------------------------------------
    # Context rotation (Steps 1, 4 — #273)
    # -----------------------------------------------------------------

    def _verify_flush(self) -> bool:
        """Check that the decision log changed after a flush prompt.

        Returns True if INDEX.md was modified (mtime or content hash
        changed), False if the flush was a no-op.
        """
        try:
            from modastack import paths
            from modastack.memory import memory_dir_for_session
            index = memory_dir_for_session(paths.state_dir(), self.name) / "INDEX.md"
            if not index.is_file():
                return False
            new_mtime = index.stat().st_mtime
            new_hash = hashlib.md5(index.read_bytes()).hexdigest()
            changed = (
                new_mtime != self._flush_snapshot_mtime
                or new_hash != self._flush_snapshot_hash
            )
            if not changed:
                log.warning(
                    "Flush no-op for '%s' — INDEX.md unchanged, skipping rotation",
                    self.name,
                )
            return changed
        except Exception:
            log.debug("Flush verification failed for '%s'", self.name, exc_info=True)
            return False

    def _snapshot_index(self) -> None:
        """Capture INDEX.md mtime + content hash before injecting flush prompt."""
        try:
            from modastack import paths
            from modastack.memory import memory_dir_for_session
            index = memory_dir_for_session(paths.state_dir(), self.name) / "INDEX.md"
            if index.is_file():
                self._flush_snapshot_mtime = index.stat().st_mtime
                self._flush_snapshot_hash = hashlib.md5(index.read_bytes()).hexdigest()
            else:
                self._flush_snapshot_mtime = 0.0
                self._flush_snapshot_hash = ""
        except Exception:
            self._flush_snapshot_mtime = 0.0
            self._flush_snapshot_hash = ""

    async def _rotate(self) -> None:
        """Lightweight client cycle — keep inbox alive, only swap the SDK client.

        Does NOT call stop()/start() which would tear down the inbox and the
        event subscription (WS client + drain thread). Only cycles self._client,
        so the session stays addressable across a rotation.
        """
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        log.info("Rotating session '%s' (rotation #%d)", self.name, self._rotation_count + 1)

        # Clear saved session ID so next connect is fresh
        save_session_id(self.name, "")

        # Disconnect old client
        if self._client:
            await self._client.disconnect()
            self._client = None

        # Rebuild system prompt — reloads the decision log
        self._system_prompt = self._rebuild_system_prompt()

        # Create fresh client with resume=None
        options = ClaudeAgentOptions(
            cwd=self.cwd,
            permission_mode="bypassPermissions",
            cli_path=get_cli_path(),
            system_prompt=self._system_prompt,
            **self._extra_options,
        )
        self._client = ClaudeSDKClient(options)
        await self._client.connect()

        # Drain the connect turn (system prompt acknowledgment)
        await self._drain_turn()

        self._rotate_pending = False
        reason = self._rotate_reason
        self._rotate_force = False
        self._rotate_reason = "context_cap"
        self._rotation_count += 1

        # Step 6: Observability — log rotation event
        log_activity(
            "rotation",
            {
                "count": self._rotation_count,
                "reason": reason,
            },
            session=self.name,
        )
        # Also emit to events.jsonl via the event client
        try:
            from modastack.events.client import _log_event
            _log_event(
                {
                    "type": "session.rotated",
                    "source": "modastack",
                    "payload": {
                        "session": self.name,
                        "rotation_count": self._rotation_count,
                        "reason": reason,
                    },
                },
                session_id=self.name,
            )
        except Exception:
            pass

        registry = get_registry()
        registry.update(self.name, status="idle", rotation_count=self._rotation_count)
        log.info("Session '%s' rotated successfully (count=%d)", self.name, self._rotation_count)

    def _rebuild_system_prompt(self) -> dict:
        """Rebuild the system prompt, reloading the decision log."""
        try:
            from modastack.subagent import _load_memory_for_session
            memory_prompt = _load_memory_for_session(self.name)
            if memory_prompt and isinstance(self._system_prompt, dict):
                base_append = self._system_prompt.get("append", "")
                # Strip old decision log section if present
                if "## Decision Log" in base_append:
                    base_append = base_append.split("## Decision Log")[0].rstrip()
                new_append = base_append
                if memory_prompt:
                    new_append = f"{base_append}\n\n{memory_prompt}" if base_append else memory_prompt
                return {**self._system_prompt, "append": new_append}
        except Exception:
            log.debug("Failed to reload memory for '%s'", self.name, exc_info=True)
        return self._system_prompt

    async def _drain_turn(self) -> str:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        self._last_response = ""
        if self._input_ready:
            self._input_ready.clear()
        self._set_state("working")
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
                    cost = msg.total_cost_usd or 0.0
                    self._total_cost_usd += cost
                    self._total_duration_ms += msg.duration_ms
                    self._total_turns += msg.num_turns
                    # Record cost with model_usage breakdown
                    model_usage = getattr(msg, "model_usage", None)
                    if cost > 0 or model_usage:
                        model = ""
                        input_tokens = 0
                        output_tokens = 0
                        if model_usage:
                            for m in (model_usage if isinstance(model_usage, list) else [model_usage]):
                                model = getattr(m, "model", "") or model
                                input_tokens += getattr(m, "input_tokens", 0) or 0
                                output_tokens += getattr(m, "output_tokens", 0) or 0
                        registry.record_cost(
                            self.name, cost, model=model,
                            provider="anthropic",
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                        )
                    if msg.is_error:
                        self._set_state("error")
                        log.error(f"Session '{self.name}' error: {self._last_response[:200]}")
                        registry.update(self.name, status="error", session_id=msg.session_id)
                    else:
                        self._set_state("waiting_input")
                        registry.update(self.name, status="idle", session_id=msg.session_id)
                        # Step 2: Check rotation cap against TRUE context fill.
                        # input_tokens alone is the uncached delta (≈0 on a warm
                        # turn) — the conversation lives in cache_read. Summing
                        # the cache fields is what actually measures the window.
                        context_tokens = _context_fill_tokens(msg.usage)
                        if context_tokens >= self._rotation_token_cap:
                            log.info(
                                "Session '%s' context=%d tokens exceeds cap=%d — "
                                "rotation pending",
                                self.name, context_tokens, self._rotation_token_cap,
                            )
                            self._rotate_pending = True
                            self._rotate_reason = "context_cap"
        except Exception as e:
            log.error(f"Drain failed for '{self.name}': {e}")
            self._set_state("error")
            registry.update(self.name, status="error")

        # Turn complete — clear any "is thinking…" Slack indicators the drain
        # loop started for this turn. The slack-reply CLI can't do this (it
        # runs in a subprocess without the manager's loop registry), so the
        # indicator would otherwise refresh itself forever.
        try:
            from modastack.events.channels import stop_all_refresh_loops
            stop_all_refresh_loops()
        except Exception:
            pass

        return self._last_response

    async def _process_message(self, msg: Message) -> None:
        """Wait for ready state, inject a message, and optionally respond."""
        deadline = 300.0  # seconds
        while self._state not in ("waiting_input", "stopped", "error"):
            if self._input_ready:
                try:
                    await asyncio.wait_for(self._input_ready.wait(), timeout=deadline)
                except asyncio.TimeoutError:
                    pass
                break
            else:
                # Fallback: no event yet (shouldn't happen after _run)
                await asyncio.sleep(0.5)

        if self._state in ("stopped", "error"):
            if msg.wait:
                self.inbox.respond(msg, f"session {self._state}")
            return

        if self._state != "waiting_input":
            log.warning(f"Session '{self.name}' never became ready for inbox message")
            if msg.wait:
                self.inbox.respond(msg, "session not ready")
            return

        # `modastack compact` control signal — flag a forced rotation and let
        # the idle loop flush + rotate. Never forward it to the model.
        if msg.text == COMPACT_SENTINEL:
            log.info("Session '%s' received compact request — rotation pending", self.name)
            self._rotate_pending = True
            self._rotate_force = True
            self._rotate_reason = "manual"
            if msg.wait:
                self.inbox.respond(msg, "compaction requested; rotating at next idle")
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
                self.inbox.respond(msg, response)
        except Exception as e:
            log.error(f"Inbox processing failed for '{self.name}': {e}")
            if msg.wait:
                self.inbox.respond(msg, f"error: {e}")
            self._set_state("error")

    async def _inbox_loop(self) -> None:
        loop = asyncio.get_running_loop()

        while True:
            msg = await loop.run_in_executor(
                None, lambda: self.inbox.recv(timeout=2.0)
            )
            if msg is None:
                if self._keep_alive and self._keep_alive.is_set():
                    break
                # Step 3: Act at idle — rotate when pending and queue is empty
                if self._rotate_pending and self.inbox._queue.empty():
                    await self._do_flush_and_rotate()
                continue

            await self._process_message(msg)

    async def _do_flush_and_rotate(self) -> None:
        """Flush the decision log, verify it, and rotate if successful."""
        log.info("Session '%s' idle with rotation pending — flushing decision log", self.name)

        # Step 4: Snapshot INDEX.md before flush
        self._snapshot_index()

        # Inject flush prompt to ask the agent to save its decision log
        flush_prompt = (
            "SYSTEM: Context rotation imminent. Your conversation is approaching "
            "the context limit. Before rotation, update your decision log at "
            ".modastack/state/memory/{session}/ — write any important decisions, "
            "context, or operational state to INDEX.md that you'll need after "
            "restart. Be thorough: this is your only continuity mechanism."
        ).format(session=self.name)

        try:
            await self._client.query(flush_prompt)
            await self._drain_turn()
        except Exception as e:
            log.warning("Flush prompt failed for '%s': %s", self.name, e)
            # Don't rotate on flush failure — retry next idle cycle
            return

        # Step 4: Verify the flush actually changed INDEX.md. A no-op flush
        # normally aborts (don't drop context we failed to persist) — but a
        # manual `compact` rotates anyway: the durable INDEX.md is already in
        # place, "nothing new to write" is not a reason to refuse the operator.
        if not self._verify_flush() and not self._rotate_force:
            # No-op flush — skip rotation, retry next idle cycle
            return

        # Flush verified — rotate
        try:
            await self._rotate()
        except Exception as e:
            log.error("Rotation failed for '%s': %s", self.name, e, exc_info=True)
            self._rotate_pending = False  # Don't retry indefinitely

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

        self._input_ready = asyncio.Event()
        self._set_state("running")
        registry = get_registry()
        registry.update(self.name, status="running")

        if startup_prompt and not resume_id:
            await self._drain_turn()
        elif startup_prompt and resume_id:
            await self._client.query(startup_prompt)
            await self._drain_turn()
        else:
            self._set_state("waiting_input")
            registry.update(self.name, status="idle")

        self._ready.set()
        log.info(f"Session '{self.name}' ready")

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
            self._set_state("stopped")
            registry.update(self.name, status="stopped")

    def _thread_target(self, startup_prompt: str | None) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run(startup_prompt))
        except (KeyboardInterrupt, SystemExit) as e:
            log.info(f"Session '{self.name}' exiting: {type(e).__name__}")
            raise
        except Exception as e:
            log.error(f"Session '{self.name}' crashed: {e}", exc_info=True)
            self._set_state("error")
        finally:
            self._loop.close()
            self._loop = None

    def _start_subscription(self) -> None:
        """Subscribe this session to its own inbox topic (+ any extras).

        Every session is addressable: it registers one event-server deployment
        carrying ``inbox/<self>`` plus any extra topics (the manager's external
        resource topics). The drain feeds arriving messages into this session's
        in-process inbox queue.

        A failed initial registration is **never fatal** (#409). Registration
        used to be terminal for coordinators — a timed-out handshake re-raised,
        the session went to ``error`` state and died, taking out project leads
        and sub-agents at init. But the event server queues, sequences, and
        replays events, so a late registration simply resumes the stream from
        the saved cursor. A transient timeout must not kill the session: we boot
        now and retry registration in the background until it lands.
        """
        keys = [f"inbox/{self.name}"]
        for key in self._subscribe:
            if key not in keys:
                keys.append(key)
        # Clear any stop signal from a prior lifecycle so a reused Session can
        # subscribe again (Sessions are single-use today, but a stuck flag here
        # would silently disable all reconnects).
        self._sub_retry_stop.clear()
        try:
            from modastack.paths import modastack_root
            from modastack.subagent import _start_event_subscription
            # One fast probe on the boot path — a healthy registration lands in
            # ~ms. We deliberately DON'T burn the full in-band retry budget here:
            # blocking start() for tens of seconds on a slow event server would
            # stall the manager's boot and trip liveness probes. The background
            # loop below owns the patient, backed-off retries instead.
            self._subscription = _start_event_subscription(
                self.name, keys, modastack_root(), register_attempts=1)
        except Exception:
            log.warning(
                "Event subscription registration failed for '%s' — booting "
                "anyway and retrying in the background; queued events resume "
                "on reconnect", self.name, exc_info=True,
            )
            self._retry_subscription_in_background(keys)

    def _retry_subscription_in_background(self, keys: list[str]) -> None:
        """Keep retrying event-server registration off the boot path.

        Runs in a daemon thread with capped exponential backoff. Repeated
        failures are logged but never terminate the process. On success the
        subscription is wired in and the thread exits; ``stop()`` signals it to
        give up so a shutting-down session leaves no live client behind.
        """
        def _loop() -> None:
            from modastack.paths import modastack_root
            from modastack.subagent import _start_event_subscription
            delay = SUBSCRIPTION_RETRY_BASE
            attempt = 0
            while not self._sub_retry_stop.is_set():
                if self._sub_retry_stop.wait(delay):
                    return
                attempt += 1
                try:
                    # One attempt per loop iteration — the loop's own backoff is
                    # the retry cadence, so don't nest the in-band retry budget.
                    sub = _start_event_subscription(
                        self.name, keys, modastack_root(), register_attempts=1)
                except Exception as e:
                    delay = min(delay * 2, SUBSCRIPTION_RETRY_MAX)
                    log.warning(
                        "Background event-subscription retry #%d for '%s' "
                        "failed: %s — retrying in %.0fs",
                        attempt, self.name, e, delay,
                    )
                    continue
                # Wire in (or discard) under the lock so we can't race stop():
                # either stop() tears this client down, or we discard it here —
                # never leave a live client+drain that stop() already skipped.
                with self._sub_lock:
                    shutting_down = self._sub_retry_stop.is_set()
                    if not shutting_down:
                        self._subscription = sub
                if shutting_down:
                    sub.stop()
                    return
                log.info(
                    "Event subscription established for '%s' after %d "
                    "background retr%s", self.name, attempt,
                    "y" if attempt == 1 else "ies",
                )
                return

        self._sub_retry_thread = threading.Thread(
            target=_loop, daemon=True, name=f"sub-retry-{self.name}",
        )
        self._sub_retry_thread.start()

    def start(self, startup_prompt: str | None = None, timeout: int = 120) -> bool:
        """Start the session in a daemon thread.

        Registers the in-process inbox and starts the session's event
        subscription immediately so it is addressable (via ``inbox/<self>``)
        before the Claude client finishes connecting. Returns True when the
        session is ready for messages.
        """
        if self._thread and self._thread.is_alive():
            return True

        self.inbox.start()
        self._start_subscription()

        from modastack.sdk import compute_manifest_hash
        registry = get_registry()
        registry.register(
            SessionEntry(
                name=self.name,
                session_id=load_session_id(self.name) or "",
                role=self.role,
                cwd=self.cwd,
                status="starting",
                pid=os.getpid(),
                image_hash=compute_manifest_hash(),
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
        # Tell any background registration retry to give up before we tear the
        # subscription down, so it can't wire in a fresh client mid-shutdown.
        self._sub_retry_stop.set()
        if self._sub_retry_thread:
            self._sub_retry_thread.join(timeout=5)
            self._sub_retry_thread = None
        if self._thread:
            self._thread.join(timeout=15)
        # Tear down the event subscription (WS client + drain thread) BEFORE
        # unregistering the inbox, so the drain can't push into — or warn about —
        # a closed inbox on its way out. Swap under the lock: a background retry
        # that registered concurrently either handed its client to us here, or
        # saw the stop flag and tore its own down — never both, never neither.
        with self._sub_lock:
            sub, self._subscription = self._subscription, None
        if sub is not None:
            sub.stop()
        self.inbox.close()

    def is_alive(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self._state not in ("stopped", "error")
        )

    def wait_until_ready(self, timeout: int = 60) -> bool:
        return self._ready.wait(timeout=timeout)
