"""Shared fake Claude-SDK message protocol for the unit suites (D102/Q042).

One definition of the SDK contract the tests model, instead of four files
each re-deriving it with drifting field sets. ``FakeResultMessage`` is the
superset of every copy's fields (every field has a default, so existing
constructions keep working); ``FakeClient`` is the rounds-based variant —
each ``receive_response()`` call replays the next pre-loaded round.

When the real SDK result-message shape changes (deferred_tool_use,
api_error_status, usage, ...), it changes here once.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class FakeTextBlock:
    text: str


@dataclass
class FakeAssistantMessage:
    content: list
    model: str = "claude-test"


@dataclass
class FakeDeferredToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class FakeResultMessage:
    subtype: str = "success"
    duration_ms: int = 1000
    duration_api_ms: int = 800
    is_error: bool = False
    num_turns: int = 5
    session_id: str = "sess-abc"
    total_cost_usd: float | None = 0.10
    result: str | None = None
    api_error_status: int | None = None
    deferred_tool_use: FakeDeferredToolUse | None = None
    stop_reason: str | None = None
    errors: list[str] | None = None
    usage: dict | None = None


class FakeClient:
    """Mimics ClaudeSDKClient with controllable message sequences."""

    def __init__(self, rounds: list[list]):
        """rounds: list of message-lists; each round is one receive_response() call."""
        self._rounds = list(rounds)
        self._round_idx = 0
        self.connected = False
        self.queries: list[str] = []
        self.disconnected = False
        self._connect_prompt: str | None = None

    async def connect(self, prompt=None):
        self.connected = True
        self._connect_prompt = prompt

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

    async def receive_response(self):
        if self._round_idx >= len(self._rounds):
            return
        msgs = self._rounds[self._round_idx]
        self._round_idx += 1
        for msg in msgs:
            yield msg

    async def disconnect(self):
        self.disconnected = True
