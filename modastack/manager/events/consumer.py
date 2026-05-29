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
from modastack.history import context_for_events, start_background_indexer, index as index_history
from modastack.workflow.triggers import WorkflowDispatcher
from modastack.manager.session import start_or_resume, inject, detect_state, is_alive, wait_until_ready

log = logging.getLogger(__name__)

PENDING_EVENTS_PATH = Path.home() / ".modastack" / "manager" / "pending_events.md"
DECISIONS_LOG = Path.home() / ".modastack" / "manager" / "decisions.jsonl"


def _format_events(events: list[dict]) -> str:
    """Format events into markdown sections."""
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

    try:
        history_ctx = context_for_events(events)
        if history_ctx:
            lines.append(history_ctx)
    except Exception as e:
        log.debug(f"History context lookup failed: {e}")

    return "\n".join(lines)


def _write_events_file(events: list[dict], append: bool = False) -> None:
    """Write events to a file the manager can read.

    When append=True, adds to the existing file so deferred events
    accumulate instead of being overwritten.
    """
    content = _format_events(events)
    PENDING_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)

    if append and PENDING_EVENTS_PATH.exists():
        with open(PENDING_EVENTS_PATH, "a") as f:
            f.write("\n\n" + content)
    else:
        PENDING_EVENTS_PATH.write_text(content)


def _clear_events_file() -> None:
    """Remove the pending events file after successful delivery."""
    try:
        PENDING_EVENTS_PATH.unlink(missing_ok=True)
    except Exception:
        pass


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

    # Initial history index + background re-indexer
    try:
        stats = index_history()
        log.info(f"History: {stats['total_conversations']} conversations, {stats['total_messages']} messages indexed")
    except Exception as e:
        log.warning(f"Initial history index failed: {e}")
    start_background_indexer(interval=120)

    # Load workflow definitions (repo-local > user > defaults)
    dispatcher = WorkflowDispatcher()
    dispatcher.load_all_workflows()

    bus = get_bus()

    # Start the persistent manager session
    if not start_or_resume():
        log.error("Failed to start manager session")
        return

    # Wait for the manager pane to be accessible and idle before starting
    # pollers — prevents the "can't find pane" race on startup
    if wait_until_ready(timeout=60):
        log.info("Manager session verified idle and ready")
    else:
        log.warning("Manager not idle after 60s — proceeding (may miss first batch)")

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
    manager_working_since: float | None = None  # watchdog: when manager entered "working"
    MANAGER_BUSY_TIMEOUT = 120  # 2 min — force-inject if manager stuck working

    while True:
        # Check if manager session is alive
        if not is_alive():
            log.warning("Manager session died — restarting")
            start_or_resume()
            manager_working_since = None

        # Wait for events
        bus.wait(timeout=batch_window)
        events = bus.drain()
        if not events:
            # Even with no new events, check if we have deferred events
            # and the manager is now free
            if PENDING_EVENTS_PATH.exists() and PENDING_EVENTS_PATH.stat().st_size > 0:
                state = detect_state()
                if state == "waiting_input":
                    log.info("Manager now idle — delivering deferred events")
                    trigger = f"New events. Read {PENDING_EVENTS_PATH} and act on them."
                    if inject(trigger):
                        _clear_events_file()
                        manager_working_since = None
                        log.info("Deferred events delivered to manager")
            continue

        tick_count += 1
        event_types = ", ".join(set(e["type"] for e in events))
        log.info(f"Batch #{tick_count}: {len(events)} events — {event_types}")

        # Dispatch to workflow engine first; feed all events to running approvals
        for event in events:
            dispatcher.dispatch(event)
            dispatcher.feed_event(event)

        # Unhandled events fall through to the manager LLM
        unhandled = [e for e in events if not dispatcher.was_dispatched(e)]
        if not unhandled:
            _log_batch(events)
            log.info(f"Batch #{tick_count}: all events handled by workflows")
            continue

        # Check manager state
        state = detect_state()

        if state == "working":
            # Track how long manager has been working
            if manager_working_since is None:
                manager_working_since = time.monotonic()

            busy_secs = time.monotonic() - manager_working_since

            if busy_secs > MANAGER_BUSY_TIMEOUT:
                # Manager stuck — force-inject (better to interrupt than lose events)
                log.warning(
                    f"Batch #{tick_count}: manager busy for {int(busy_secs)}s — "
                    f"force-injecting (timeout {MANAGER_BUSY_TIMEOUT}s exceeded)"
                )
                _write_events_file(unhandled, append=True)
                trigger = f"New events. Read {PENDING_EVENTS_PATH} and act on them."
                if inject(trigger):
                    _clear_events_file()
                    manager_working_since = None
                _log_batch(events)
                continue

            # Normal deferral — append so earlier events aren't lost
            _write_events_file(unhandled, append=True)
            log.info(f"Batch #{tick_count}: manager is busy ({int(busy_secs)}s) — events appended, trigger deferred")
            _log_batch(events)
            continue
        else:
            manager_working_since = None

        # Wait briefly for manager to be ready if in unknown state
        if state != "waiting_input":
            for _ in range(30):
                state = detect_state()
                if state in ("waiting_input", "working"):
                    break
                time.sleep(2)

        if state == "working":
            _write_events_file(unhandled, append=True)
            log.info(f"Batch #{tick_count}: manager started working — events appended, trigger deferred")
            manager_working_since = time.monotonic()
            _log_batch(events)
            continue

        # Manager is ready — write events and inject
        _write_events_file(unhandled, append=True)
        trigger = f"New events. Read {PENDING_EVENTS_PATH} and act on them."
        if not inject(trigger):
            log.warning(f"Batch #{tick_count}: injection failed — events preserved for retry")
            _log_batch(events)
            continue

        _clear_events_file()
        _log_batch(events)
        log.info(f"Batch #{tick_count} delivered to manager")
