"""Regression: a single NDJSON message >1 MB must not kill the session (#719).

The Claude SDK defaults ``max_buffer_size`` to 1 MB and raises
``CLIJSONDecodeError`` on the FIRST NDJSON line above it, permanently killing
the reader task for that connection. Bobi now sets a generous explicit ceiling
(``bobi.brain.claude.DEFAULT_MAX_BUFFER_SIZE``) so a legitimate large message —
e.g. an agent told to ``Read`` a multi-MB file or an inlined image — parses
instead of taking the session down.

This drives the real SDK transport's line reader end-to-end with a fake stdout
stream: with our buffer the oversized line parses; with the old 1 MB default the
same line raises — proving the default was the killer and our value fixes it.
"""

import json

import pytest

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._errors import CLIJSONDecodeError
from claude_agent_sdk._internal.transport.subprocess_cli import (
    _DEFAULT_MAX_BUFFER_SIZE,
    SubprocessCLITransport,
)

from bobi.brain.claude import _max_buffer_size


class _FakeStdout:
    """Async line iterator standing in for the CLI subprocess stdout."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeProcess:
    """Truthy stand-in whose clean exit keeps read_messages() from raising a
    ProcessError after the stream drains."""

    async def wait(self):
        return 0


def _transport(max_buffer_size):
    opts = ClaudeAgentOptions(
        cli_path="/usr/bin/claude", max_buffer_size=max_buffer_size
    )
    t = SubprocessCLITransport(prompt="hi", options=opts)
    return t


async def _read_all(transport, line):
    transport._process = _FakeProcess()
    transport._stdout_stream = _FakeStdout([line])
    return [msg async for msg in transport.read_messages()]


def _oversized_message():
    """One JSON message whose serialized length exceeds 1 MB."""
    payload = {"type": "assistant", "text": "x" * (2 * 1024 * 1024)}  # ~2 MB
    line = json.dumps(payload)
    assert len(line) > _DEFAULT_MAX_BUFFER_SIZE  # would blow the old default
    return line, payload


@pytest.mark.asyncio
async def test_oversized_message_parses_under_bobi_buffer():
    line, payload = _oversized_message()
    transport = _transport(_max_buffer_size())

    messages = await _read_all(transport, line)

    assert messages == [payload]  # parsed, session survives


@pytest.mark.asyncio
async def test_oversized_message_would_kill_the_sdk_default():
    """Control: the same line raises under the SDK's 1 MB default — proving the
    default was the defect and our generous ceiling is what fixes it."""
    line, _ = _oversized_message()
    transport = _transport(_DEFAULT_MAX_BUFFER_SIZE)

    with pytest.raises(CLIJSONDecodeError):
        await _read_all(transport, line)


def test_bobi_default_exceeds_the_sdk_default():
    assert _max_buffer_size() > _DEFAULT_MAX_BUFFER_SIZE
