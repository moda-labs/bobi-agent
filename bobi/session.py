"""Unified session — a pluggable agent brain with an inbox.

Every session is identical: a :class:`~bobi.brain.BrainSession` (Claude
Code by default; see epic #485) connected to an inbox drain loop. The session
drives the brain through the provider-agnostic ``connect`` / ``query`` /
``receive_response`` / ``disconnect`` contract and consumes normalized
``AssistantText`` / ``TurnResult`` messages — never a vendor SDK's classes.
Each session subscribes to its own ``inbox/<self>`` topic on the
event server and injects arriving messages into the Claude session in order.
The only difference between a "manager" and an "agent" is what extra topics it
subscribes to (the manager also subscribes to external resource topics).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import threading
from pathlib import Path

from bobi.brain import AssistantText, TurnResult, get_brain
from bobi.inbox import Inbox, Message
from bobi.sdk import (
    save_session_id,
    load_session_id,
    load_resumable_session_id,
    log_activity,
    get_registry,
    SessionEntry,
)

log = logging.getLogger(__name__)

# Default rotation cap — absolute context-fill tokens, not a window fraction.
DEFAULT_ROTATION_TOKEN_CAP = 275_000

# Rotation reconnect bounds (#456, wedge mechanism #3). Rotation cycles the SDK
# client: disconnect → rebuild prompt → connect → drain the connect turn. Both
# the connect() and that first drain are network/subprocess work that can hang
# indefinitely with no timeout — the actual 2026-06-23/24 director wedge. We
# wrap the reconnect in a hard timeout, retry a bounded number of times, then
# recover into an addressable connected client rather than a silent park. A
# connect + single ack turn is lighter than the old flush turn, but this must
# stay above the Claude initialize deadline so the SDK can surface retryable
# initialize timeouts instead of being preempted by the outer rotation wrapper.
ROTATION_RECONNECT_TIMEOUT = 240.0
ROTATION_MAX_RECONNECT_ATTEMPTS = 3
ROTATION_RECONNECT_BACKOFF = 2.0

# Control sentinel for the named compact command (#433). Delivered as an inbox
# message body; the run loop recognizes it, flags rotation, and never forwards
# it to the model. An exact-match constant, so a human message can't trip it.
COMPACT_SENTINEL = "\x00__bobi_compact__\x00"


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


def _rotation_error_message(err: BaseException | None) -> str:
    """Return a non-empty, diagnosable rotation failure string.

    ``asyncio.TimeoutError`` commonly has no message, so ``str(err)`` is empty.
    Rotation failure records are operational breadcrumbs; they must name the
    failure class even when the exception object carries no text.
    """
    if err is None:
        return "unknown rotation reconnect failure"

    err_type = type(err).__name__
    try:
        text = str(err).strip()
    except Exception:
        text = ""
    if text:
        message = f"{err_type}: {text}"
    elif isinstance(err, asyncio.TimeoutError):
        message = (
            f"{err_type}: rotation reconnect timed out after "
            f"{ROTATION_RECONNECT_TIMEOUT:.0f}s"
        )
    else:
        message = f"{err_type}: no error message provided"

    cause = err.__cause__ or err.__context__
    if cause is not None and cause is not err:
        try:
            cause_text = str(cause).strip()
        except Exception:
            cause_text = ""
        cause_text = cause_text or "no error message provided"
        message = f"{message}; caused by {type(cause).__name__}: {cause_text}"
    return message

# Background event-subscription retry cadence (#409). When the initial
# registration handshake with the event server times out, the session boots
# anyway and a daemon thread keeps retrying with capped exponential backoff —
# events are queued/sequenced/resumable, so a late registration just resumes
# the stream from the saved cursor.
SUBSCRIPTION_RETRY_BASE = 2.0
SUBSCRIPTION_RETRY_MAX = 60.0

# In-band retry for transient turn-level API errors (e.g. 529 Overloaded, rate
# limits). A transient error is scoped to a single turn — the SDK client stays
# connected — so we re-issue the same query with capped exponential backoff
# before giving up. This must never leave the session terminally wedged: see
# _drain_turn / _process_message. Retries are bounded so a genuinely failing
# turn surfaces its error to the caller instead of looping forever.
#
# The retry budget and the "what counts as transient" set/classifier live in
# bobi.transient so the sub-agent spawn/executor path agrees on the same
# definition (MDS-65, §4.3). Re-imported here for back-compat — existing call
# sites and tests reference these names off bobi.session.
from bobi.transient import (  # noqa: F401  (re-exported for back-compat)
    TURN_RETRY_BASE,
    TURN_RETRY_MAX_ATTEMPTS,
    TRANSIENT_API_STATUSES,
    is_transient_api_error,
)

# Liveness guard for queued work while a session is temporarily not ready
# (active turn, rotation, reconnect, or recovery). The message must remain
# queued until the session recovers; after this window, emit a best-effort
# operator alert so the stuck state is visible.
SESSION_UNREACHABLE_ALERT_AFTER = 120.0
SESSION_READY_WAIT_POLL = 1.0


def _emit_session_unreachable_alert(
    *,
    session: str,
    state: str,
    message_id: str,
    sender: str,
    wait: bool,
    elapsed: float,
) -> None:
    """Emit a best-effort alert for a queued message stuck behind readiness."""
    try:
        from bobi.events.publish import post_event
        post_event(
            "system/session.unreachable",
            {
                "session": session,
                "state": state,
                "message_id": message_id,
                "sender": sender,
                "wait": wait,
                "elapsed_seconds": round(elapsed, 1),
                "text": (
                    f"Session '{session}' has been unreachable for "
                    f"{elapsed:.0f}s while a queued message waits; current "
                    f"state: {state}."
                ),
            },
        )
    except Exception:
        log.warning("Failed to emit session unreachable alert", exc_info=True)


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

        # The agent brain (Claude Code by default). A factory: every fresh
        # connect/rotation/recovery builds a new BrainSession from it (#485).
        self._brain = get_brain()
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
        self._last_api_error_status: int | None = None
        self._total_cost_usd = 0.0
        self._total_duration_ms = 0
        self._total_turns = 0

        # Context rotation state (Steps 1-4, #273). Rotation now cycles the
        # client directly (no decision-log flush — #456 removed it); the only
        # rotation work is the bounded, recoverable reconnect (#456 mech #3).
        self._rotate_pending = False
        self._rotate_reason = "context_cap"
        self._rotation_count = 0

    def detect_state(self) -> str:
        return self._state

    def _set_state(self, state: str) -> None:
        """Update state and wake any waiter when the session becomes idle or terminal."""
        self._state = state
        if state in ("waiting_input", "stopped", "error") and self._input_ready:
            self._input_ready.set()

    def _is_transient_turn_error(self) -> bool:
        """Whether the last turn's error is worth retrying.

        Thin delegate over the shared classifier (bobi.transient): prefers
        the SDK-reported ``api_error_status`` (e.g. 529); falls back to sniffing
        the response text for overload/rate-limit/timeout phrasing when no status
        was surfaced.
        """
        return is_transient_api_error(
            self._last_api_error_status, self._last_response or ""
        )

    def _stop_status_indicators(self) -> None:
        """Clear any Slack "is thinking…" refresh loops this manager started.

        Normally cleared at the end of a turn (see ``_drain_turn``), but a
        message dropped before it runs a turn (session stopped/error/not ready)
        would otherwise leave the indicator refreshing itself forever.
        """
        try:
            from bobi.events.channels import stop_all_refresh_loops
            stop_all_refresh_loops()
        except Exception:
            pass

    # -----------------------------------------------------------------
    # Context rotation (Steps 1, 4 — #273)
    # -----------------------------------------------------------------

    def _make_brain_session(self, resume: str | None = None):
        """Build a fresh brain session (boot, rotation reconnect, recovery).

        ``resume=None`` yields a clean fresh-connect session; pass a saved id to
        resume. Brain-specific extras (skills, max_turns, mcp_servers, …) ride
        ``_extra_options``; the adapter supplies the shared defaults
        (``permission_mode``, ``cli_path``).
        """
        return self._brain.make_session(
            cwd=self.cwd,
            system_prompt=self._system_prompt,
            resume=resume,
            options=self._extra_options,
        )

    def _session_model(self) -> str:
        """The effective configured model for this session (#617).

        The explicit option if set, else the process default the adapter
        would fill in - the string recorded alongside the saved session id
        so a resume under a different model starts fresh instead.
        """
        from bobi.brain import resolve_model_option
        return resolve_model_option(self._extra_options.get("model"))

    async def _safe_disconnect(self, client) -> None:
        """Disconnect a client, swallowing errors — used to discard a hung or
        partially-connected client so a stalled reconnect can't leak a `claude`
        subprocess per retry."""
        try:
            await client.disconnect()
        except Exception:
            log.debug("Disconnect raised for '%s' during rotation", self.name,
                      exc_info=True)

    async def _attempt_reconnect(self) -> None:
        """One bounded reconnect attempt: fresh client → connect → drain the
        connect turn, all under ROTATION_RECONNECT_TIMEOUT.

        Raises asyncio.TimeoutError (the hang the wedge was made of) or a connect
        exception on failure; on either, the freshly-built client is discarded
        before the exception propagates so the caller can retry cleanly.
        """
        client = self._make_brain_session(resume=None)

        async def _connect_and_drain() -> None:
            await client.connect()
            # Publish the client only after connect() returns so a hung connect
            # never leaves a half-live client addressable; _drain_turn reads it.
            self._client = client
            await self._drain_turn()

        try:
            await asyncio.wait_for(
                _connect_and_drain(), timeout=ROTATION_RECONNECT_TIMEOUT
            )
        except BaseException:
            # wait_for cancels the hung connect()/receive_response() await, but
            # the partially-connected client + its subprocess must be dropped.
            await self._safe_disconnect(client)
            if self._client is client:
                self._client = None
            raise

    async def _rotate(self) -> None:
        """Lightweight client cycle — keep inbox alive, only swap the SDK client.

        Does NOT call stop()/start() which would tear down the inbox and the
        event subscription (WS client + drain thread). Only cycles self._client,
        so the session stays addressable across a rotation.

        The reconnect is bounded and recoverable (#456 mechanism #3): the
        connect + connect-turn drain are wrapped in a timeout and retried a
        bounded number of times; if every attempt fails the session recovers
        into an addressable connected client rather than hanging forever or
        dropping into the terminal "error" state that would deafen it (#443).
        Raises only if even that final recovery fails — loudly, never silently.
        """
        log.info("Rotating session '%s' (rotation #%d)", self.name, self._rotation_count + 1)

        # Clear saved session ID so the reconnect is fresh.
        save_session_id(self.name, "")

        # Disconnect old client.
        if self._client:
            await self._safe_disconnect(self._client)
            self._client = None

        # Rebuild system prompt — reloads the team policy (#456).
        self._system_prompt = self._rebuild_system_prompt()

        # Bounded, recoverable reconnect.
        last_err: BaseException | None = None
        attempt_errors: list[dict] = []
        reconnected = False
        for attempt in range(1, ROTATION_MAX_RECONNECT_ATTEMPTS + 1):
            try:
                await self._attempt_reconnect()
                reconnected = True
                break
            except asyncio.TimeoutError as e:
                last_err = e
                attempt_errors.append({
                    "attempt": attempt,
                    "error": _rotation_error_message(e),
                })
                log.error(
                    "Rotation reconnect for '%s' timed out after %.0fs "
                    "(attempt %d/%d)",
                    self.name, ROTATION_RECONNECT_TIMEOUT, attempt,
                    ROTATION_MAX_RECONNECT_ATTEMPTS,
                )
            except Exception as e:
                last_err = e
                attempt_errors.append({
                    "attempt": attempt,
                    "error": _rotation_error_message(e),
                })
                log.error(
                    "Rotation reconnect for '%s' failed (attempt %d/%d): %s",
                    self.name, attempt, ROTATION_MAX_RECONNECT_ATTEMPTS,
                    _rotation_error_message(e),
                )
            if attempt < ROTATION_MAX_RECONNECT_ATTEMPTS:
                await asyncio.sleep(ROTATION_RECONNECT_BACKOFF * attempt)

        if not reconnected:
            # Exhausted — recover into an addressable state (or surface
            # terminally if even that fails). Raises on terminal failure.
            await self._recover_rotation_failure(last_err, attempt_errors)

        self._rotate_pending = False
        reason = self._rotate_reason
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
            from bobi.events.client import _log_event
            _log_event(
                {
                    "type": "session.rotated",
                    "source": "bobi",
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

    async def _recover_rotation_failure(
        self,
        err: BaseException | None,
        attempt_errors: list[dict] | None = None,
    ) -> None:
        """Recover after the bounded reconnect exhausted its attempts (#456 #3).

        Fails loudly (log.error + a session.rotation_failed activity/event) and
        then re-establishes a connected client via a fresh connect so the
        session is addressable again — NOT left in the disconnected state
        _rotate() created (self._client is None) and NOT dropped into the
        terminal "error" state that deafens the session (#443). Only if even
        this final fresh connect fails does the session surface terminally —
        loudly, never a silent hang.
        """
        error_message = _rotation_error_message(err)
        attempt_errors = attempt_errors or [
            {"attempt": ROTATION_MAX_RECONNECT_ATTEMPTS, "error": error_message}
        ]

        log.error(
            "Rotation reconnect exhausted for '%s' after %d attempts: %s — "
            "attempting fresh-connect recovery",
            self.name, ROTATION_MAX_RECONNECT_ATTEMPTS, error_message,
        )
        try:
            log_activity(
                "rotation_failed",
                {
                    "attempts": ROTATION_MAX_RECONNECT_ATTEMPTS,
                    "error": error_message,
                    "attempt_errors": attempt_errors,
                },
                session=self.name,
            )
            from bobi.events.client import _log_event
            _log_event(
                {
                    "type": "session.rotation_failed",
                    "source": "bobi",
                    "payload": {
                        "session": self.name,
                        "attempts": ROTATION_MAX_RECONNECT_ATTEMPTS,
                        "error": error_message,
                        "attempt_errors": attempt_errors,
                    },
                },
                session_id=self.name,
            )
        except Exception:
            pass

        # Final recovery: a fresh connected client (resume=None — the saved id
        # was already cleared at the top of _rotate). No connect-prompt, so this
        # connect cannot hang on receive_response; bound it anyway.
        client = self._make_brain_session(resume=None)
        try:
            await asyncio.wait_for(
                client.connect(), timeout=ROTATION_RECONNECT_TIMEOUT
            )
        except BaseException as e2:
            await self._safe_disconnect(client)
            final_error_message = _rotation_error_message(e2)
            try:
                log_activity(
                    "rotation_failed",
                    {
                        "attempts": ROTATION_MAX_RECONNECT_ATTEMPTS,
                        "error": error_message,
                        "attempt_errors": attempt_errors,
                        "final_recovery_error": final_error_message,
                    },
                    session=self.name,
                )
                from bobi.events.client import _log_event
                _log_event(
                    {
                        "type": "session.rotation_failed",
                        "source": "bobi",
                        "payload": {
                            "session": self.name,
                            "attempts": ROTATION_MAX_RECONNECT_ATTEMPTS,
                            "error": error_message,
                            "attempt_errors": attempt_errors,
                            "final_recovery_error": final_error_message,
                        },
                    },
                    session_id=self.name,
                )
            except Exception:
                pass
            log.error(
                "Final rotation recovery failed for '%s': %s — surfacing terminally",
                self.name, final_error_message,
            )
            self._client = None
            self._set_state("error")
            get_registry().update(self.name, status="error")
            raise
        self._client = client
        self._set_state("waiting_input")
        get_registry().update(self.name, status="idle")
        log.warning(
            "Session '%s' recovered to a fresh connected client after a failed "
            "rotation — addressable, not wedged", self.name,
        )

    def _rebuild_system_prompt(self) -> dict:
        """Rebuild the system prompt, reloading the team policy (#456).

        A rotated session re-reads policy.md here — the passive pickup path for
        a curator update (no inbox push needed for a routine distillation).
        """
        try:
            from bobi.subagent import _load_policy_prompt
            policy_prompt = _load_policy_prompt()
            if isinstance(self._system_prompt, dict):
                base_append = self._system_prompt.get("append", "")
                # Strip a previously-injected policy section so it isn't doubled
                # across rotations.
                if "## Team Policy" in base_append:
                    base_append = base_append.split("## Team Policy")[0].rstrip()
                if policy_prompt:
                    new_append = (
                        f"{base_append}\n\n{policy_prompt}" if base_append else policy_prompt
                    )
                else:
                    new_append = base_append
                return {**self._system_prompt, "append": new_append}
        except Exception:
            log.debug("Failed to reload policy for '%s'", self.name, exc_info=True)
        return self._system_prompt

    async def _drain_turn(self) -> str:
        self._last_response = ""
        if self._input_ready:
            self._input_ready.clear()
        self._set_state("working")
        registry = get_registry()
        registry.update(self.name, status="running")

        # Per-call usage from the LAST assistant message in the turn — the
        # single representative API call we measure context fill from (#454).
        # The ResultMessage's usage is the turn AGGREGATE: in a multi-step turn
        # (model → tool → model → …) the cached prefix is re-read on every call,
        # so summing its cache_read counts the context N times (real_context×N)
        # and fires a perpetual false "rotation pending". One call's usage is
        # the actual window fill.
        last_assistant_usage: dict | None = None

        try:
            async for msg in self._client.receive_response():
                if isinstance(msg, AssistantText):
                    if msg.usage is not None:
                        last_assistant_usage = msg.usage
                    if msg.text:
                        self._last_response = msg.text
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
                elif isinstance(msg, TurnResult):
                    save_session_id(self.name, msg.session_id,
                                    model=self._session_model())
                    self._last_is_error = msg.is_error
                    cost = msg.total_cost_usd or 0.0
                    self._total_cost_usd += cost
                    self._total_duration_ms += msg.duration_ms
                    self._total_turns += msg.num_turns
                    # Record cost with the brain's normalized per-model usage
                    # breakdown, attributed to the brain's provider (not a
                    # hardcoded "anthropic" — #485).
                    if cost > 0 or msg.costs:
                        model = ""
                        input_tokens = 0
                        output_tokens = 0
                        for c in msg.costs:
                            model = c.model or model
                            input_tokens += c.input_tokens
                            output_tokens += c.output_tokens
                        registry.record_cost(
                            self.name, cost, model=model,
                            provider=getattr(self._client, "provider", "anthropic"),
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                        )
                    self._last_api_error_status = msg.api_error_status
                    if msg.is_error:
                        # A turn-level API error (e.g. 529 Overloaded, rate
                        # limit) is transient and scoped to this turn — the SDK
                        # client stays connected. It must NOT drop the session
                        # into the terminal "error" state: _process_message
                        # silently rejects every future message while the state
                        # is "error" and is_alive() reports the session dead, so
                        # a single 529 would deafen the agent until a process
                        # restart (#443). Surface the error but return to ready so the
                        # next event is served (the caller may also retry — see
                        # _process_message).
                        log.error(
                            "Session '%s' turn error (api_status=%s): %s",
                            self.name,
                            self._last_api_error_status,
                            self._last_response[:200],
                        )
                        self._set_state("waiting_input")
                        registry.update(
                            self.name, status="idle", session_id=msg.session_id
                        )
                    else:
                        self._set_state("waiting_input")
                        registry.update(self.name, status="idle", session_id=msg.session_id)
                        # Step 2: Check rotation cap against TRUE context fill,
                        # measured from a SINGLE representative API call (the
                        # last assistant message's usage) — NOT the cumulative
                        # ResultMessage usage, which sums cache_read across every
                        # model call in the turn and over-counts by ×N (#454).
                        # input_tokens alone is the uncached delta (≈0 on a warm
                        # turn) — the conversation lives in cache_read — so we
                        # still sum input + cache_read + cache_creation, just of
                        # that one call.
                        context_tokens = _context_fill_tokens(last_assistant_usage)
                        if context_tokens >= self._rotation_token_cap:
                            log.info(
                                "Session '%s' context=%d tokens exceeds cap=%d — "
                                "rotation pending",
                                self.name, context_tokens, self._rotation_token_cap,
                            )
                            # Over-cap is non-negotiable: the session MUST shed
                            # context. With the decision-log flush removed (#456),
                            # a pending rotation now just cycles the client at the
                            # next idle — nothing to no-op-livelock on.
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
        self._stop_status_indicators()

        return self._last_response

    async def _process_message(self, msg: Message) -> None:
        """Wait for ready state, inject a message, and optionally respond."""
        wait_started = time.monotonic()
        alerted_unreachable = False
        while self._state not in ("waiting_input", "stopped", "error"):
            elapsed = time.monotonic() - wait_started
            if (
                not alerted_unreachable
                and elapsed >= SESSION_UNREACHABLE_ALERT_AFTER
            ):
                _emit_session_unreachable_alert(
                    session=self.name,
                    state=self._state,
                    message_id=msg.id,
                    sender=msg.sender,
                    wait=msg.wait,
                    elapsed=elapsed,
                )
                alerted_unreachable = True
            if self._input_ready:
                if self._input_ready.is_set():
                    # A set event with a still-not-ready state is stale. Do not
                    # clear it here: a real ready transition could race between
                    # the loop condition and this branch. Sleep briefly instead
                    # so stale events cannot spin the loop.
                    await asyncio.sleep(SESSION_READY_WAIT_POLL)
                else:
                    try:
                        await asyncio.wait_for(
                            self._input_ready.wait(),
                            timeout=SESSION_READY_WAIT_POLL,
                        )
                    except asyncio.TimeoutError:
                        pass
            else:
                # Fallback: no event yet (shouldn't happen after _run)
                await asyncio.sleep(SESSION_READY_WAIT_POLL)

        if self._state in ("stopped", "error"):
            # Dropping the message means no turn runs, so clear any Slack
            # "thinking…" indicator here — _drain_turn (which normally clears
            # it) is never reached.
            self._stop_status_indicators()
            if msg.wait:
                self.inbox.respond(msg, f"session {self._state}")
            return

        if self._state != "waiting_input":
            log.warning(f"Session '{self.name}' never became ready for inbox message")
            self._stop_status_indicators()
            if msg.wait:
                self.inbox.respond(msg, "session not ready")
            return

        # Compact control signal — flag a forced rotation and let
        # the idle loop flush + rotate. Never forward it to the model.
        if msg.text == COMPACT_SENTINEL:
            log.info("Session '%s' received compact request — rotation pending", self.name)
            self._rotate_pending = True
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

            # Self-heal transient turn errors (529 Overloaded, rate limits):
            # re-issue the same query with capped backoff so the triggering
            # event is answered instead of dropped. Bounded so a persistently
            # failing turn surfaces its error rather than looping forever.
            attempt = 0
            while (
                self._last_is_error
                and self._is_transient_turn_error()
                and attempt < TURN_RETRY_MAX_ATTEMPTS
                and self._state == "waiting_input"
            ):
                delay = TURN_RETRY_BASE * (2 ** attempt)
                attempt += 1
                log.warning(
                    "Session '%s' turn hit transient error (api_status=%s); "
                    "retry %d/%d after %.1fs",
                    self.name, self._last_api_error_status, attempt,
                    TURN_RETRY_MAX_ATTEMPTS, delay,
                )
                if delay:
                    await asyncio.sleep(delay)
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
                # Act at idle — rotate when pending and the queue is empty. The
                # decision-log flush is gone (#456); rotation is now just the
                # bounded, recoverable client cycle in _rotate().
                if self._rotate_pending and self.inbox._queue.empty():
                    try:
                        await self._rotate()
                    except Exception as e:
                        # _rotate only raises when even fresh-connect recovery
                        # failed (it already surfaced the error + set state).
                        # Clear the pending flag so the idle loop doesn't spin.
                        log.error("Rotation failed terminally for '%s': %s",
                                  self.name, e, exc_info=True)
                        self._rotate_pending = False
                continue

            await self._process_message(msg)

    async def _run(self, startup_prompt: str | None = None) -> None:
        saved_id = load_resumable_session_id(self.name, self._session_model())
        resume_id = saved_id or None

        self._client = self._make_brain_session(resume=resume_id)

        try:
            connect_prompt = startup_prompt if not resume_id else None
            await self._client.connect(connect_prompt)
        except Exception as e:
            if resume_id:
                log.warning(f"Resume failed for '{self.name}', retrying fresh: {e}")
                save_session_id(self.name, "")
                self._client = self._make_brain_session(resume=None)
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
            from bobi.paths import bobi_root
            from bobi.subagent import _start_event_subscription
            # One fast probe on the boot path — a healthy registration lands in
            # ~ms. We deliberately DON'T burn the full in-band retry budget here:
            # blocking start() for tens of seconds on a slow event server would
            # stall the manager's boot and trip liveness probes. The background
            # loop below owns the patient, backed-off retries instead.
            self._subscription = _start_event_subscription(
                self.name, keys, bobi_root(), register_attempts=1)
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
            from bobi.paths import bobi_root
            from bobi.subagent import _start_event_subscription
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
                        self.name, keys, bobi_root(), register_attempts=1)
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

        from bobi.sdk import compute_manifest_hash
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
