"""Event consumer — batches events and wakes the manager.

Replaces the old polling watcher. Instead of hashing context every 5s,
it blocks until events arrive, batches them, and calls the manager once.
The manager only runs when there's something to reason about.
"""

import json
import logging
import time
from pathlib import Path

import truststore
truststore.inject_into_ssl()

from .bus import get_bus
from .pollers import start_pollers
from .webhook_server import start_server
from manager.loop import call_manager, DECISIONS_LOG
from manager.executor import execute_actions, post_thinking_placeholder

log = logging.getLogger(__name__)

MEMORY_PATH = Path.home() / ".dispatch" / "manager" / "memory.md"


def _format_events_for_prompt(events: list[dict]) -> str:
    """Format batched events into text the manager can read."""
    lines = [f"# Modabot — {len(events)} new events", ""]

    by_source = {}
    for e in events:
        by_source.setdefault(e["source"], []).append(e)

    for source, source_events in by_source.items():
        lines.append(f"## {source.title()} Events")
        for e in source_events:
            lines.append(f"- **{e['type']}** ({e['timestamp']})")
            data = e.get("data", {})
            for k, v in data.items():
                if v and k != "recent_comments":
                    lines.append(f"  {k}: {str(v)[:200]}")
            if "recent_comments" in data:
                for c in data["recent_comments"][-2:]:
                    lines.append(f"  💬 [{c.get('author', '?')}]: {c.get('body', '')[:150]}")
        lines.append("")

    memory = ""
    if MEMORY_PATH.exists():
        memory = MEMORY_PATH.read_text()
    if memory:
        lines.append(f"## Memory\n{memory}")

    return "\n".join(lines)


def _format_context_with_events(events: list[dict]) -> dict:
    """Build context dict compatible with the manager's call interface."""
    return {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "events": events,
        "channels": {},  # Legacy compat
    }


async def process_batch(events: list[dict]) -> dict:
    """Process a batch of events through the manager."""
    import asyncio

    # Post thinking indicator for Slack DMs
    for e in events:
        if e["source"] == "slack" and e["type"] == "slack.message":
            ch_id = e.get("data", {}).get("channel_id", "")
            if ch_id:
                await post_thinking_placeholder(ch_id)

    # Build context with events
    context = _format_context_with_events(events)
    prompt_text = _format_events_for_prompt(events)

    # Call manager with events as the context
    actions, reasoning = call_manager(context, prompt_override=prompt_text)

    # Execute actions
    summary = await execute_actions(actions) if actions else {"executed": 0, "skipped": 0, "errors": 0}

    # Log decisions
    DECISIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": context["timestamp"],
        "events": len(events),
        "event_types": list(set(e["type"] for e in events)),
        "reasoning": reasoning,
        "actions": actions,
        "outcome": summary,
    }
    with open(DECISIONS_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

    if actions:
        log.info(f"Manager decided {len(actions)} actions: "
                 + ", ".join(a.get("type", "?") for a in actions))

    return summary


def run(webhook_port: int = 8080, use_webhooks: bool = False,
        batch_window: float = 5.0, github_secret: str = "",
        linear_secret: str = "", slack_signing_secret: str = ""):
    """Main event loop.

    batch_window: seconds to wait for events to accumulate before processing.
    use_webhooks: if True, start webhook server and skip Linear poller.
    """
    import asyncio

    log.info("Modabot starting (event-driven mode)")

    bus = get_bus()

    # Start webhook server if configured
    if use_webhooks:
        start_server(port=webhook_port, github_secret=github_secret,
                     linear_secret=linear_secret,
                     slack_signing_secret=slack_signing_secret)

    # Start pollers (exclude sources that have webhooks)
    exclude = []
    if use_webhooks:
        exclude = ["linear"]  # Workers always need polling, Slack depends on Events API setup
    start_pollers(exclude=exclude)

    log.info(f"Listening for events (batch window: {batch_window}s)")
    tick_count = 0

    while True:
        # Wait for at least one event
        bus.wait(timeout=batch_window)

        # Drain all pending events
        events = bus.drain()
        if not events:
            continue

        tick_count += 1
        log.info(f"Batch #{tick_count}: {len(events)} events — "
                 + ", ".join(set(e["type"] for e in events)))

        try:
            summary = asyncio.run(process_batch(events))
            if summary.get("executed", 0) > 0 or summary.get("errors", 0) > 0:
                log.info(f"Result: {json.dumps(summary)}")
        except Exception as e:
            log.error(f"Batch processing failed: {e}")
