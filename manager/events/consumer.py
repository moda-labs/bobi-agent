"""Event consumer — batches events and feeds them to the persistent manager session.

Events arrive from the bus. The consumer formats them and injects them
into the manager's tmux session. The manager responds with actions
(captured from the pane) or handles things directly via tools.
"""

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import truststore
truststore.inject_into_ssl()

from .bus import get_bus
from .pollers import start_pollers
from .slack_socket import start_socket_mode
from .webhook_server import start_server
from manager.session import start_or_resume, inject, capture, detect_state, is_alive
from manager.executor import execute_actions, post_thinking_placeholder

log = logging.getLogger(__name__)

DECISIONS_LOG = Path.home() / ".modastack" / "manager" / "decisions.jsonl"


def _format_events(events: list[dict]) -> str:
    """Format batched events into a compact message for the manager session."""
    lines = [f"[{len(events)} new events at {time.strftime('%H:%M:%S')}]"]

    for e in events:
        data = e.get("data", {})
        detail = data.get("text", "") or data.get("title", "") or data.get("body", "") or ""
        if len(detail) > 200:
            detail = detail[:200] + "..."

        line = f"  {e['source']}/{e['type']}"
        if data.get("issue_id"):
            line += f" {data['issue_id']}"
        if data.get("from"):
            line += f" from {data['from']}"
        if detail:
            line += f": {detail}"

        # Include IDs the manager might need
        if data.get("linear_id"):
            line += f" (linear_id: {data['linear_id']})"
        if data.get("channel_id"):
            line += f" (channel_id: {data['channel_id']})"
        if data.get("repo"):
            line += f" (repo: {data['repo']})"

        lines.append(line)

    lines.append("")
    lines.append("Respond with a JSON array of actions, or use tools directly.")
    return "\n".join(lines)


def _extract_actions_from_pane(pane_text: str) -> list[dict]:
    """Try to extract JSON actions from the manager's pane output."""
    # Strip markdown code fences
    stripped = re.sub(r'```json\s*', '', pane_text)
    stripped = re.sub(r'```\s*', '', stripped).strip()

    # Find JSON array
    match = re.search(r'\[[\s\S]*\]', stripped)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def _wait_for_response(timeout: int = 120) -> tuple[list[dict], str]:
    """Wait for the manager to finish processing and return actions + reasoning."""
    start = time.time()
    last_content = ""

    while time.time() - start < timeout:
        state = detect_state()
        if state == "waiting_input":
            # Manager is done — capture the response
            pane = capture(lines=40)
            actions = _extract_actions_from_pane(pane)
            return actions, pane
        time.sleep(2)

    # Timeout
    pane = capture(lines=40)
    return [], pane


def run(webhook_port: int = 8080, use_webhooks: bool = False,
        batch_window: float = 5.0, github_secret: str = "",
        linear_secret: str = "", slack_signing_secret: str = ""):
    """Main event loop with persistent manager session."""

    log.info("Modastack starting (event-driven, persistent manager)")

    bus = get_bus()

    # Start the persistent manager session
    if not start_or_resume():
        log.error("Failed to start manager session")
        return

    # Start webhook server if configured
    if use_webhooks:
        start_server(port=webhook_port, github_secret=github_secret,
                     linear_secret=linear_secret,
                     slack_signing_secret=slack_signing_secret)

    # Start Slack Socket Mode
    slack_thread = start_socket_mode()

    # Start pollers
    exclude = []
    if use_webhooks:
        exclude.append("linear")
    if slack_thread:
        exclude.append("slack")
    start_pollers(exclude=exclude)

    log.info(f"Listening for events (batch window: {batch_window}s)")
    tick_count = 0

    while True:
        # Check if manager session is alive
        if not is_alive():
            log.warning("Manager session died — restarting")
            start_or_resume()

        # Wait for events
        bus.wait(timeout=batch_window)
        events = bus.drain()
        if not events:
            continue

        # Immediately post "Thinking..." for Slack messages
        for e in events:
            if e["source"] == "slack" and e.get("type", "").startswith("slack."):
                ch_id = e.get("data", {}).get("channel_id", "")
                if ch_id:
                    asyncio.run(post_thinking_placeholder(ch_id))

        tick_count += 1
        event_types = ", ".join(set(e["type"] for e in events))
        log.info(f"Batch #{tick_count}: {len(events)} events — {event_types}")

        # Wait for manager to be ready (not mid-thought from a previous batch)
        for _ in range(30):
            if detect_state() == "waiting_input":
                break
            time.sleep(2)

        # Inject events into the manager session
        event_text = _format_events(events)
        inject(event_text)

        # Wait for the manager to process and respond
        actions, reasoning = _wait_for_response(timeout=120)

        # Execute any structured actions the manager output
        summary = {"executed": 0, "skipped": 0, "errors": 0}
        if actions:
            summary = asyncio.run(execute_actions(actions))

        # Log
        DECISIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "events": len(events),
            "event_types": list(set(e["type"] for e in events)),
            "actions": actions,
            "outcome": summary,
        }
        with open(DECISIONS_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")

        if actions:
            log.info(f"Manager actions: {', '.join(a.get('type', '?') for a in actions)}")
        if summary.get("executed", 0) > 0 or summary.get("errors", 0) > 0:
            log.info(f"Result: {json.dumps(summary)}")
