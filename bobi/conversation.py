"""Conversation references - channel-agnostic addressing for chat replies (#618).

A conversation reference is an opaque string the agent echoes back verbatim
(via ``bobi reply``) to answer into the thread/DM an inbound event came from.
Adapters in the event server build refs; this module parses them. The agent
never learns platform addressing semantics.

Grammar (mirrors ``event-server/core/src/conversation.ts``)::

    <source>:<scope>:<chat_type>:<chat_id>[:thread:<thread_id>]

where ``scope`` is the platform's tenancy unit (Slack team id, WhatsApp phone
number id). Segment values never contain ``:`` for the id shapes we carry.
"""

from __future__ import annotations

from dataclasses import dataclass

CHAT_TYPES = frozenset({"dm", "group", "channel"})


@dataclass(frozen=True)
class Conversation:
    source: str
    scope: str
    chat_type: str
    chat_id: str
    thread_id: str = ""


def build_conversation(conv: Conversation) -> str:
    segments = [conv.source, conv.scope, conv.chat_type, conv.chat_id]
    if conv.thread_id:
        segments.append(conv.thread_id)
    for seg in segments:
        if not seg or ":" in seg:
            raise ValueError(f"invalid conversation segment: {seg!r}")
    base = f"{conv.source}:{conv.scope}:{conv.chat_type}:{conv.chat_id}"
    return f"{base}:thread:{conv.thread_id}" if conv.thread_id else base


def parse_conversation(ref: str) -> Conversation | None:
    if not isinstance(ref, str):
        return None
    parts = ref.split(":")
    if len(parts) not in (4, 6):
        return None
    if any(p == "" for p in parts):
        return None
    source, scope, chat_type, chat_id = parts[:4]
    if chat_type not in CHAT_TYPES:
        return None
    thread_id = ""
    if len(parts) == 6:
        if parts[4] != "thread":
            return None
        thread_id = parts[5]
    return Conversation(source, scope, chat_type, chat_id, thread_id)
