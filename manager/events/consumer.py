"""Event consumer — writes events to a file, triggers the manager to read them.

Instead of injecting long text into tmux (unreliable paste buffer), we:
1. Write events to ~/.modastack/manager/pending_events.md
2. Inject a short trigger: "New events — read .modastack/manager/pending_events.md"
3. The manager reads the file and acts directly (curl, tmux, etc.)
4. No executor needed — the manager handles everything via tools
"""

import json
import logging
import threading
import time
from pathlib import Path

import truststore
truststore.inject_into_ssl()

from .bus import get_bus
from .pollers import start_pollers, _poll_version
from .slack_socket import start_socket_mode
from .webhook_server import start_server
from modastack.config import GlobalConfig
from manager.session import start_or_resume, inject, detect_state, is_alive

log = logging.getLogger(__name__)

PENDING_EVENTS_PATH = Path.home() / ".modastack" / "manager" / "pending_events.md"
DECISIONS_LOG = Path.home() / ".modastack" / "manager" / "decisions.jsonl"


def _write_events_file(events: list[dict]) -> None:
    """Write events to a file the manager can read."""
    lines = [f"# {len(events)} new events at {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]

    for e in events:
        data = e.get("data", {})
        detail = data.get("text", "") or data.get("title", "") or data.get("body", "") or ""
        if len(detail) > 300:
            detail = detail[:300] + "..."

        line = f"## {e['source']}/{e['type']}"
        lines.append(line)

        if data.get("issue_id"):
            lines.append(f"- issue_id: {data['issue_id']}")
        if data.get("task_id"):
            lines.append(f"- task_id: {data['task_id']}")
        if data.get("from"):
            lines.append(f"- from: {data['from']}")
        if data.get("channel_id"):
            lines.append(f"- channel_id: {data['channel_id']}")
        if data.get("repo"):
            lines.append(f"- repo: {data['repo']}")
        if data.get("state"):
            lines.append(f"- state: {data['state']}")
        if data.get("labels"):
            lines.append(f"- labels: {', '.join(data['labels'])}")
        if data.get("phase"):
            lines.append(f"- phase: {data['phase']}")
        if data.get("spec_pr"):
            lines.append(f"- spec_pr: {data['spec_pr']}")
        if data.get("pr_url") or data.get("url"):
            lines.append(f"- url: {data.get('pr_url') or data.get('url')}")
        if data.get("current_version"):
            lines.append(f"- current_version: {data['current_version']}")
        if data.get("new_version"):
            lines.append(f"- new_version: {data['new_version']}")
        if data.get("changelog"):
            lines.append(f"- changelog: {data['changelog']}")
        if detail:
            lines.append(f"- detail: {detail}")

        lines.append("")

    PENDING_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_EVENTS_PATH.write_text("\n".join(lines))


def _log_batch(events: list[dict]) -> None:
    """Log the batch to decisions.jsonl."""
    DECISIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "events": len(events),
        "event_types": list(set(e["type"] for e in events)),
    }
    with open(DECISIONS_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _ensure_repos():
    """Check that all registered repos exist on disk. Log warnings for missing ones."""
    config = GlobalConfig.load()
    for repo_path in config.repos:
        path = repo_path if isinstance(repo_path, Path) else Path(repo_path)
        if not path.exists():
            log.warning(f"Registered repo not found on disk: {path}")


def run(webhook_port: int = 8080, use_webhooks: bool = False,
        batch_window: float = 5.0, github_secret: str = "",
        linear_secret: str = "", slack_signing_secret: str = ""):
    """Main event loop with persistent manager session."""

    log.info("Modastack starting (event-driven, persistent manager)")

    _ensure_repos()

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
        exclude.append("tasks")
    if slack_thread:
        exclude.append("slack")
    start_pollers(exclude=exclude)

    # Run an immediate version check on startup (doesn't wait for the 1h poller)
    def _one_shot_version_check():
        try:
            _poll_version(interval=0)
        except Exception as e:
            log.debug(f"Startup version check failed: {e}")
    threading.Thread(target=_one_shot_version_check, daemon=True).start()

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

        tick_count += 1
        event_types = ", ".join(set(e["type"] for e in events))
        log.info(f"Batch #{tick_count}: {len(events)} events — {event_types}")

        # Write events to file (always, even if manager is busy)
        _write_events_file(events)

        # Only inject trigger if manager is waiting for input.
        # If busy, it will see queued messages when it finishes.
        state = detect_state()
        if state == "working":
            log.info(f"Batch #{tick_count}: manager is busy — events written, trigger deferred")
            _log_batch(events)
            continue

        # Wait briefly for manager to be ready if in unknown state
        if state != "waiting_input":
            for _ in range(30):
                state = detect_state()
                if state in ("waiting_input", "working"):
                    break
                time.sleep(2)

        if state == "working":
            log.info(f"Batch #{tick_count}: manager started working — trigger deferred")
            _log_batch(events)
            continue

        # Inject once — no retry flooding
        trigger = f"New events. Read {PENDING_EVENTS_PATH} and act on them."
        inject(trigger)

        # Log
        _log_batch(events)

        log.info(f"Batch #{tick_count} delivered to manager")
