"""One turn primitive: drain a brain session's response stream (#1048).

Every driver of turns (the workflow orchestrator's step loop, the spawn
path's supervised agent loop) used to carry its own copy of the same drain:
iterate ``receive_response``, capture the last assistant text, save the
session id off the terminal result, write the activity records, and
normalize the three ways a stream can die. The copies drifted (#845 fixed
the orchestrator's stop record but not the spawn path's; the network-drop
message existed in two spellings). ``drain_turn`` is the single copy.

``bobi.session.Session._drain_turn`` deliberately stays separate: it is the
persistent-session drain, entangled with rotation (#454), the in-turn
keepalive (#721), per-turn cost recording, and decode-error recovery (#719).
It migrates onto this primitive only if that can be done without rewriting
it; until then this module owns the two spawn-style drains only.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from bobi.brain.base import AssistantText, TurnResult
# Safe only while bobi.sdk keeps its own bobi.brain imports function-local
# (sdk.py does, deliberately) - hoisting those would close an import cycle
# through this module.
from bobi.sdk import log_activity, save_session_id

log = logging.getLogger(__name__)


def network_drop_error() -> str:
    return "network drop: response stream ended before turn result"


def timeout_error(timeout: int | None = None) -> str:
    if timeout is None:
        return "subprocess timeout while draining response"
    return f"subprocess timeout after {timeout}s"


def tool_crash_error(error: BaseException | str) -> str:
    message = str(error).strip() or error.__class__.__name__
    if message.startswith("tool crash:"):
        return message
    return f"tool crash: {message}"


@dataclass
class TurnOutcome:
    """What one drained turn produced.

    ``result`` is the brain's terminal message; it is None exactly when the
    DRAIN itself failed - the stream broke before a terminal result arrived -
    and ``failure`` / ``failure_kind`` then carry the normalized diagnosis
    (``timeout`` / ``tool_crash`` / ``network_drop``). A turn the brain
    itself reports as failed still returns its ``TurnResult`` here: callers
    read ``is_error`` / ``error_kind`` off it, because what counts as a
    failed turn (and what to do about it) is caller policy, not the drain's.
    """

    result: TurnResult | None
    final_text: str
    failure: str = ""
    failure_kind: str = ""


async def drain_turn(client, session_name: str, *, model: str) -> TurnOutcome:
    """Drain one turn from *client*, owning the per-turn bookkeeping.

    On every non-empty assistant message: capture it as the turn's final
    text and write the ``response`` activity record. On the terminal
    ``TurnResult``: save the session id (recorded with *model* so the
    store's model record stays in step with mid-run switches, #642) and
    write the full-fact ``stop`` record (#845 - every terminal fact the
    brain reported, so diagnosing a failure never needs the vendor CLI's
    own transcript).

    Cancellation passes through untouched: an outer ``asyncio.timeout``
    cancels this coroutine and converts at its own boundary, so a caller's
    wall-clock enforcement (D067) still lands in the caller's handler.
    """
    final_text = ""
    stream = client.receive_response()
    try:
        async for msg in stream:
            if isinstance(msg, AssistantText):
                if msg.text:
                    final_text = msg.text
                    log_activity("response", {"text": msg.text[:500]},
                                 session=session_name)
            elif isinstance(msg, TurnResult):
                save_session_id(session_name, msg.session_id, model=model)
                log_activity("stop", {
                    "session_id": msg.session_id,
                    "is_error": msg.is_error,
                    "error_kind": msg.error_kind,
                    "error_message": msg.error_message,
                    "api_error_status": msg.api_error_status,
                    "num_turns": msg.num_turns,
                    "duration_ms": msg.duration_ms,
                }, session=session_name)
                return TurnOutcome(msg, final_text)
    except asyncio.TimeoutError:
        error = timeout_error()
        log.error("Drain timeout for '%s': %s", session_name, error)
        return TurnOutcome(None, final_text, error, "timeout")
    except Exception as e:
        error = tool_crash_error(e)
        log.error("Drain error for '%s': %s", session_name, error)
        return TurnOutcome(None, final_text, error, "tool_crash")
    finally:
        # Returning at the terminal result leaves an async generator suspended
        # at its yield; close it NOW so adapter-side teardown (e.g. the codex
        # runner's subprocess reaping) runs deterministically instead of at
        # the loop's asyncgen-shutdown hook.
        aclose = getattr(stream, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass
    error = network_drop_error()
    log.error("Drain for '%s' ended without a terminal result: %s",
              session_name, error)
    return TurnOutcome(None, final_text, error, "network_drop")
