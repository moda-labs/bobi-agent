"""Stateless one-shot streaming LLM transport for the setup brain.

Per the eng-review lock, the digestion brain holds **no** long-lived SDK
session: every turn is a fresh streaming call fed a fully-assembled context
(spec-so-far + rolling summary + last-N raw messages). This module is the
pure transport — text in, streamed text out — and knows nothing about the
digestion contract (slot deltas, summaries); the caller assembles the
prompt and parses the result.

The streaming source is injectable (`stream_fn`) so the digestion logic can
be tested hermetically against a scripted fake — no network, no CLI. The
default source is a one-shot `query()` against the Claude Code CLI with
partial messages enabled, so the UI gets the token-by-token "pour".
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Callable, Optional


class LLMError(Exception):
    """A streaming call failed before producing usable output."""


# A stalled stream — no token for this many seconds — is treated as a failure
# rather than hung forever. Generous enough to cover model "thinking" and slow
# first tokens, tight enough that a dead nested-CLI subprocess surfaces as a
# clean "build failed / try again" instead of an infinite spinner.
STREAM_IDLE_TIMEOUT = 120.0


# A stream source: keyword-only (system_prompt, user_prompt, model, cwd) →
# async iterator of text chunks.
StreamFn = Callable[..., AsyncIterator[str]]

# These are pure text completions — the model must answer in prose, never
# reach for a tool. In the claude_code CLI the built-in tools are present by
# default; left enabled, the model sometimes emits a Write/Edit tool call
# (e.g. when authoring a file) instead of returning text, which both
# bypasses our structured pour AND, under a tight turn cap, fails the call
# with "Reached maximum number of turns". So we disallow every built-in and
# give a generous turn budget as a safety net.
_NO_TOOLS = ["Task", "Bash", "BashOutput", "KillBash", "Glob", "Grep",
             "Read", "Edit", "Write", "NotebookEdit", "WebFetch",
             "WebSearch", "TodoWrite", "ExitPlanMode"]
DEFAULT_MAX_TURNS = 8


def _delta_text(event) -> str:
    """Pull the text out of one raw Anthropic streaming event, or ''."""
    if not isinstance(event, dict):
        return ""
    if event.get("type") == "content_block_delta":
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            return delta.get("text", "")
    return ""


async def _sdk_stream(*, system_prompt: str, user_prompt: str,
                      model: Optional[str] = None,
                      cwd: Optional[str] = None) -> AsyncIterator[str]:
    """Default source: a stateless one-shot `query()` with partial streaming.

    No MCP servers, no tools, no session resume — a clean text completion.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        StreamEvent,
        TextBlock,
        query,
    )
    from modastack.sdk import get_cli_path

    options = ClaudeAgentOptions(
        cwd=cwd,
        model=model,
        cli_path=get_cli_path(),
        max_turns=DEFAULT_MAX_TURNS,
        disallowed_tools=_NO_TOOLS,
        permission_mode="bypassPermissions",
        include_partial_messages=True,
        system_prompt=system_prompt,
    )

    saw_partial = False
    try:
        async for msg in query(prompt=user_prompt, options=options):
            if isinstance(msg, StreamEvent):
                text = _delta_text(msg.event)
                if text:
                    saw_partial = True
                    yield text
            elif isinstance(msg, AssistantMessage) and not saw_partial:
                # Fallback path when partials never arrived: emit whole blocks.
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        yield block.text
            elif isinstance(msg, ResultMessage):
                # is_error is set on API/tool failures; surface it cleanly.
                if getattr(msg, "is_error", False) and not saw_partial:
                    raise LLMError(getattr(msg, "result", "") or
                                   "the model returned an error")
    except LLMError:
        raise
    except Exception as e:  # SDK/transport failure → uniform error type
        raise LLMError(str(e)) from e


async def stream(system_prompt: str, user_prompt: str, *,
                 model: Optional[str] = None, cwd: Optional[str] = None,
                 stream_fn: Optional[StreamFn] = None,
                 idle_timeout: Optional[float] = STREAM_IDLE_TIMEOUT
                 ) -> AsyncIterator[str]:
    """Stream a single completion as text chunks. `stream_fn` overrides the
    source (tests inject a scripted fake).

    Each token must arrive within `idle_timeout` seconds of the previous one; a
    longer gap is treated as a stalled call and raised as `LLMError` (so the
    build pour can't hang forever on a dead subprocess). Pass `None` to wait
    indefinitely. The underlying source is always closed on the way out, so a
    timed-out SDK subprocess gets cancelled rather than leaked."""
    fn = stream_fn or _sdk_stream
    agen = fn(system_prompt=system_prompt, user_prompt=user_prompt,
              model=model, cwd=cwd)
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(agen.__anext__(),
                                               timeout=idle_timeout)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError as e:
                raise LLMError(
                    f"the model stalled — no output for "
                    f"{idle_timeout:.0f}s") from e
            yield chunk
    finally:
        aclose = getattr(agen, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass


async def complete(system_prompt: str, user_prompt: str, *,
                   model: Optional[str] = None, cwd: Optional[str] = None,
                   stream_fn: Optional[StreamFn] = None) -> str:
    """Run a completion to the end and return the full assembled text."""
    parts: list[str] = []
    async for chunk in stream(system_prompt, user_prompt, model=model,
                              cwd=cwd, stream_fn=stream_fn):
        parts.append(chunk)
    return "".join(parts)
