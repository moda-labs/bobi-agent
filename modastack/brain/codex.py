"""OpenAI Codex brain adapter (epic #485, Phase 2 — MVP).

Backs the provider-agnostic :class:`~modastack.brain.base.BrainSession` with the
Codex CLI's headless contract (validated in the #485 Phase-0 spike):
``codex exec --json`` streams NDJSON turn events, ``codex exec resume <thread>``
continues a prior session by its rollout, and ``thread.started.thread_id`` is the
resume token.

This is a **stateless-per-turn** session: there is no long-lived process: each
``receive_response`` cold-starts a ``codex exec`` subprocess that (re)plays the
thread, streams one turn, and exits. That satisfies the persistent
``connect``/``query``/``receive_response``/``disconnect`` interface the manager
loop drives — the manager already injects between turns — at the cost of a
process spawn per turn. A hot ``app-server`` session is a later optimization.

MVP scope / known gaps (tracked in the spec): no per-message cost in dollars
(token counts only); MCP servers are not yet forwarded (Codex reads
``~/.codex/config.toml``); the system prompt is prepended to the first turn of a
fresh thread rather than written as ``AGENTS.md``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from modastack.brain.base import (
    AssistantText,
    BrainCost,
    BrainMessage,
    BrainSession,
    TurnResult,
)

# Headless, non-interactive, fully autonomous: the box is the sandbox, so we
# bypass Codex's approval/sandbox prompts (the analog of Claude's
# ``bypassPermissions``). ``--skip-git-repo-check`` lets it run outside a repo.
_EXEC_FLAGS = (
    "--json",
    "--skip-git-repo-check",
    "--dangerously-bypass-approvals-and-sandbox",
)


def _instructions(system_prompt: Any) -> str:
    """Extract the agent instructions from a brain system_prompt.

    The call sites still build Claude's ``{"preset": ..., "append": "<role +
    tools + policy>"}`` dict; for Codex we take the ``append`` text. A plain
    string is used as-is.
    """
    if isinstance(system_prompt, dict):
        return str(system_prompt.get("append", "") or "")
    if isinstance(system_prompt, str):
        return system_prompt
    return ""


def _map_usage(u: dict) -> dict:
    """Codex ``turn.completed.usage`` → the token dict session.py's rotation
    metric expects (input + cache_read + cache_creation)."""
    return {
        "input_tokens": u.get("input_tokens", 0) or 0,
        # Codex calls the cached prefix ``cached_input_tokens``.
        "cache_read_input_tokens": u.get("cached_input_tokens", 0) or 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": u.get("output_tokens", 0) or 0,
    }


def _costs(u: dict, model: str) -> list[BrainCost]:
    inp = (u.get("input_tokens", 0) or 0) + (u.get("cached_input_tokens", 0) or 0)
    return [BrainCost(model=model or "codex", input_tokens=inp,
                      output_tokens=u.get("output_tokens", 0) or 0)]


async def _spawn_codex(argv: list[str], cwd: str) -> AsyncIterator[dict]:
    """Run ``codex exec`` and yield its NDJSON events as parsed dicts.

    stdin is ``/dev/null`` — ``codex exec`` blocks reading a piped-but-open stdin
    (Phase-0 gotcha). Non-JSON lines (banners) are skipped.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            s = line.decode("utf-8", "replace").strip()
            if not s:
                continue
            try:
                yield json.loads(s)
            except json.JSONDecodeError:
                continue
    finally:
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        await proc.wait()


class _CodexSession:
    """A :class:`BrainSession` backed by per-turn ``codex exec`` subprocesses."""

    provider = "openai"

    def __init__(
        self,
        *,
        cwd: str,
        instructions: str,
        resume: str | None = None,
        model: str = "",
        runner=None,
    ) -> None:
        self._cwd = cwd or "."
        self._instructions = instructions
        self._thread_id = resume or None
        self._model = model
        self._runner = runner or _spawn_codex
        self._pending: str | None = None

    async def connect(self, prompt: str | None = None) -> None:
        # No persistent process — just stash any startup prompt for the first
        # receive_response (mirrors how session.py drains the connect turn).
        self._pending = prompt

    async def query(self, text: str) -> None:
        self._pending = text

    async def disconnect(self) -> None:
        return None

    def _build_argv(self, prompt: str) -> list[str]:
        flags = list(_EXEC_FLAGS)
        if self._model:
            flags += ["-m", self._model]
        if self._thread_id:
            # `resume` has a narrower flag set (no -C/--sandbox); cwd comes from
            # the subprocess cwd. The bypass/skip flags are accepted here too.
            return ["codex", "exec", "resume", self._thread_id, *flags, prompt]
        return ["codex", "exec", *flags, prompt]

    async def receive_response(self) -> AsyncIterator[BrainMessage]:
        if self._pending is None:
            # No input to run (e.g. a rotation reconnect's drain) — emit a no-op
            # result so the caller advances to waiting_input instead of hanging.
            yield TurnResult(session_id=self._thread_id or "")
            return

        prompt = self._pending
        self._pending = None
        # A fresh thread carries no context, so prepend the agent instructions;
        # a resumed thread already has them in its rollout.
        if not self._thread_id and self._instructions:
            prompt = f"{self._instructions}\n\n{prompt}"

        argv = self._build_argv(prompt)
        async for ev in self._runner(argv, self._cwd):
            etype = ev.get("type")
            if etype == "thread.started":
                self._thread_id = ev.get("thread_id") or self._thread_id
            elif etype == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    yield AssistantText(text=item["text"])
            elif etype == "turn.completed":
                usage = ev.get("usage") or {}
                # Carry the turn usage on a text-less assistant message so the
                # rotation metric sees it (Codex has no per-message usage).
                yield AssistantText(text="", usage=_map_usage(usage))
                yield TurnResult(
                    session_id=self._thread_id or "",
                    costs=_costs(usage, self._model),
                )
                return
            elif etype in ("turn.failed", "error"):
                err = ev.get("error")
                msg = err.get("message") if isinstance(err, dict) else None
                yield TurnResult(
                    session_id=self._thread_id or "",
                    is_error=True,
                    result_text=msg or ev.get("message") or "codex turn failed",
                )
                return

        # Stream ended without a terminal event — treat as a lost turn.
        yield TurnResult(
            session_id=self._thread_id or "",
            is_error=True,
            result_text="codex exited without completing the turn",
        )


class CodexBrain:
    """Factory for Codex sessions (``brain: {kind: codex}``)."""

    name = "codex"
    provider = "openai"

    def make_session(
        self,
        *,
        cwd: str | None,
        system_prompt: Any,
        resume: str | None = None,
        options: dict | None = None,
    ) -> BrainSession:
        opts = options or {}
        return _CodexSession(
            cwd=cwd or ".",
            instructions=_instructions(system_prompt),
            resume=resume,
            # Claude-specific options (skills/hooks/permission_mode/max_turns)
            # don't apply to Codex; only a model override is honored.
            model=str(opts.get("model", "") or ""),
        )
