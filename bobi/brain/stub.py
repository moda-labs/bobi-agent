"""Programmable stub brain - a scripted :class:`BrainSession` for tests.

A test double that speaks the exact provider-agnostic brain contract
(``connect`` / ``query`` / ``receive_response`` / ``disconnect``) but never
touches a vendor CLI or the network. It lets a test drive a *real* manager
process - real ``session.py`` turn loop, real event delivery, real supervision -
to a deterministic state without the ``claude`` CLI or credentials, and to
trigger specific runtime behavior (idle, wedge, crash) on demand.

This is the ONE stub both test surfaces share: the public integration suites
(``tests/integration``) and the private deploy-package sidecar e2e both select
it with ``BOBI_BRAIN=stub`` so they exercise the identical runtime path,
swapping only the brain at the boundary.

Behavior is scripted through the turn text. Any query (an inbox message routed
to the manager, or the startup prompt) is scanned for a directive token
``__stub__:<verb>[:<arg>]``; with no directive the turn completes normally.
Because the inbox message text reaches ``query()`` verbatim, a test publishes a
message over the event bus to steer the runtime, and observes the assistant
text and lifecycle that come back - the bidirectional seam.

Directives:
  ``__stub__:idle``          complete the turn (the default) -> manager idle.
  ``__stub__:reply:<text>``  complete, using ``<text>`` as the assistant reply.
  ``__stub__:options``       complete, replying with a JSON dump of the scalar
                             session options ``make_session`` received - lets
                             an e2e prove a launch flag (model, effort) reached
                             the session (#778).
  ``__stub__:hang[:<secs>]`` stall the turn ``<secs>`` seconds (default: long
                             enough to trip wedge detection) before completing,
                             so the manager reads as ``running``/``wedged``.
  ``__stub__:error``         complete with ``TurnResult(is_error=True)``.
  ``__stub__:exit[:<code>]`` hard-exit the process (default 0) mid-turn, to
                             exercise supervisor crash-restart.

The brain is registered but GATED: :meth:`StubBrain.make_session` raises unless
``BOBI_STUB_BRAIN`` is set, so an accidental ``BOBI_BRAIN=stub`` in production
fails loud instead of silently running a no-op agent.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, AsyncIterator

from bobi.brain.base import (
    AssistantText,
    BrainCapabilities,
    BrainMessage,
    BrainSession,
    StreamDelta,
    TurnResult,
)

# Env acknowledgement that this test-only brain is intentional. Registered
# always (so both test surfaces resolve the same brain) but inert in production.
STUB_BRAIN_ENV = "BOBI_STUB_BRAIN"

# A hang with no explicit duration stalls far past any wedge threshold so the
# manager stays in-turn until the test releases it (by stopping the process).
_DEFAULT_HANG_SECONDS = 86_400.0

_DIRECTIVE = re.compile(r"__stub__:(\w+)(?::(\S+))?")


def _parse_directive(text: str | None) -> tuple[str, str | None]:
    """Return the ``(verb, arg)`` scripted in *text*, or the idle default."""
    if not text:
        return "idle", None
    m = _DIRECTIVE.search(text)
    if not m:
        return "idle", None
    return m.group(1), m.group(2)


class _StubSession:
    """A scripted :class:`BrainSession` - no CLI, no network."""

    provider = "stub"

    def __init__(
        self, resume: str | None = None, options: dict | None = None,
    ) -> None:
        # The resume token echoes back on every TurnResult so a resumed session
        # keeps a stable id, matching how a real brain threads session_id.
        self._session_id = resume or "stub-session"
        self._options = options or {}
        self._pending: str | None = None

    async def connect(self, prompt: str | None = None) -> None:
        # Returns promptly (a real connect can be slow; the stub never is) so
        # the manager leaves "starting" as soon as the startup turn drains.
        self._pending = prompt

    async def query(self, text: str) -> None:
        self._pending = text

    async def disconnect(self) -> None:
        return None

    async def receive_response(self) -> AsyncIterator[BrainMessage]:
        """Yield one scripted turn, then stop (the TurnResult is the terminal
        marker the turn loop waits for)."""
        verb, arg = _parse_directive(self._pending)

        if verb == "exit":
            # Hard process death mid-turn - the supervisor's crash path. Flush
            # so any captured log shows the trigger before the process vanishes.
            code = _int_arg(arg, 0)
            os._exit(code)

        if verb == "hang":
            await asyncio.sleep(_float_arg(arg, _DEFAULT_HANG_SECONDS))

        if verb == "options":
            # Echo the scalar session options make_session received - the
            # observability seam for e2e tests proving a launch flag or
            # config value (model, effort) actually reached the session.
            import json
            reply = json.dumps(
                {k: v for k, v in self._options.items()
                 if isinstance(v, (str, int, bool))},
                sort_keys=True,
            )
        elif verb == "reply":
            reply = arg
        else:
            reply = f"stub ack: {(self._pending or '').strip()[:120]}"
        yield AssistantText(text=reply, usage=None)
        yield TurnResult(
            session_id=self._session_id,
            is_error=(verb == "error"),
            result_text=reply,
        )


def _int_arg(arg: str | None, default: int) -> int:
    try:
        return int(arg) if arg is not None else default
    except ValueError:
        return default


def _float_arg(arg: str | None, default: float) -> float:
    try:
        return float(arg) if arg is not None else default
    except ValueError:
        return default


class StubBrain:
    """Factory for scripted stub sessions (test-only, env-gated)."""

    name = "stub"
    provider = "stub"
    capabilities = BrainCapabilities(cross_model_resume=True)

    def _guard(self) -> None:
        if not os.environ.get(STUB_BRAIN_ENV):
            raise RuntimeError(
                "the stub brain is test-only; set BOBI_STUB_BRAIN=1 to select "
                "it (an unguarded BOBI_BRAIN=stub in production is a mistake)."
            )

    def make_session(
        self,
        *,
        cwd: str | None = None,
        system_prompt: Any = None,
        resume: str | None = None,
        options: dict | None = None,
    ) -> BrainSession:
        self._guard()
        return _StubSession(resume=resume, options=options)

    async def stream_once(
        self,
        *,
        system_prompt: Any = None,
        user_prompt: str = "",
        model: str | None = None,
        cwd: str | None = None,
        options: dict | None = None,
    ) -> AsyncIterator[BrainMessage]:
        """One-shot scripted completion (the stateless setup/digestion path)."""
        self._guard()
        verb, arg = _parse_directive(user_prompt)
        reply = arg if verb == "reply" else f"stub ack: {user_prompt.strip()[:120]}"
        yield StreamDelta(text=reply)
        yield AssistantText(text=reply, usage=None)
        yield TurnResult(session_id="stub-session", is_error=(verb == "error"),
                         result_text=reply)
