"""Gather context from all registered channels.

Each channel in manager/channels/ implements gather(), hash_key(), and
format_context(). Adding a new input source (Slack, Notion, etc.) means
adding a new channel module — no changes to this file.
"""

import hashlib
import json
import logging
import time
from pathlib import Path

from manager.channels import linear, github, workers

log = logging.getLogger(__name__)

MANAGER_DIR = Path.home() / ".dispatch" / "manager"
CONTEXT_PATH = MANAGER_DIR / "context.json"
MEMORY_PATH = MANAGER_DIR / "memory.md"

# Register channels — add new ones here
CHANNELS = [linear, github, workers]


async def gather_all() -> dict:
    """Gather context from all channels. Returns the full context dict."""
    channel_data = {}
    for ch in CHANNELS:
        name = ch.__name__.split(".")[-1]
        try:
            channel_data[name] = await ch.gather({})
        except Exception as e:
            log.error(f"Channel {name} failed: {e}")
            channel_data[name] = []

    memory = ""
    if MEMORY_PATH.exists():
        memory = MEMORY_PATH.read_text()

    context = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "channels": channel_data,
        "memory": memory,
    }

    MANAGER_DIR.mkdir(parents=True, exist_ok=True)
    CONTEXT_PATH.write_text(json.dumps(context, indent=2, default=str))

    total = sum(len(v) for v in channel_data.values())
    log.info(f"Context gathered: {total} items across {len(CHANNELS)} channels")
    return context


def context_hash(context: dict) -> str:
    """Compute a hash for change detection across all channels."""
    parts = []
    for ch in CHANNELS:
        name = ch.__name__.split(".")[-1]
        items = context.get("channels", {}).get(name, [])
        parts.append(ch.hash_key(items))
    combined = "|".join(parts)
    return hashlib.md5(combined.encode()).hexdigest()


def write_context_prompt(context: dict) -> str:
    """Format all channel data into the manager's prompt."""
    lines = [f"# Modabot Manager Tick — {context['timestamp']}", ""]

    for ch in CHANNELS:
        name = ch.__name__.split(".")[-1]
        items = context.get("channels", {}).get(name, [])
        text = ch.format_context(items)
        if text:
            lines.append(text)

    if context.get("memory"):
        lines.append(f"\n## Memory\n{context['memory']}")

    return "\n".join(lines)
