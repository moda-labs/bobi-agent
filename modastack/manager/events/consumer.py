"""Event consumer — append-only event log with manager-side checkpointing.

Events are appended to a log file with monotonic batch IDs. The manager
tracks its own read offset via a checkpoint file. Duplicate triggers are
harmless — the manager reads from its checkpoint and skips already-processed
batches. The consumer periodically truncates batches before the checkpoint.
"""

import json
import logging
import subprocess
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
from modastack.manager.session import start_or_resume, inject, detect_state, is_alive, wait_until_ready, read_last_response

log = logging.getLogger(__name__)

EVENTS_DIR = Path.home() / ".modastack" / "manager"
PENDING_EVENTS_PATH = EVENTS_DIR / "pending_events.md"
CHECKPOINT_PATH = EVENTS_DIR / "events_checkpoint"
BATCH_COUNTER_PATH = EVENTS_DIR / "batch_counter"
DECISIONS_LOG = EVENTS_DIR / "decisions.jsonl"


def _next_batch_id() -> int:
    """Get and increment the persistent batch counter."""
    BATCH_COUNTER_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        n = int(BATCH_COUNTER_PATH.read_text().strip()) if BATCH_COUNTER_PATH.exists() else 0
    except (ValueError, OSError):
        n = 0
    n += 1
    BATCH_COUNTER_PATH.write_text(str(n))
    return n


def _read_checkpoint() -> int:
    """Read the manager's last-processed batch ID."""
    try:
        return int(CHECKPOINT_PATH.read_text().strip()) if CHECKPOINT_PATH.exists() else 0
    except (ValueError, OSError):
        return 0


def _format_batch(batch_id: int, events: list[dict]) -> str:
    """Format events into a markdown batch section with an ID marker."""
    lines = [
        f"<!-- batch:{batch_id} -->",
        f"# Batch {batch_id} — {len(events)} events at {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]

    for e in events:
        data = e.get("data", {})
        detail = data.get("text", "") or data.get("title", "") or data.get("body", "") or ""
        if len(detail) > 300:
            detail = detail[:300] + "..."

        lines.append(f"## {e['source']}/{e['type']}")

        for key in ("issue_id", "task_id", "from", "channel_id", "repo",
                     "state", "phase", "spec_pr", "current_version",
                     "new_version", "changelog"):
            if data.get(key):
                lines.append(f"- {key}: {data[key]}")
        if data.get("labels"):
            lines.append(f"- labels: {', '.join(data['labels'])}")
        if data.get("pr_url") or data.get("url"):
            lines.append(f"- url: {data.get('pr_url') or data.get('url')}")
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


def _append_batch(batch_id: int, events: list[dict]) -> None:
    """Append a batch to the events log file."""
    content = _format_batch(batch_id, events)
    PENDING_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PENDING_EVENTS_PATH, "a") as f:
        f.write(content + "\n\n")


def _truncate_processed() -> None:
    """Remove batches that the manager has already checkpointed."""
    checkpoint = _read_checkpoint()
    if checkpoint == 0 or not PENDING_EVENTS_PATH.exists():
        return

    try:
        content = PENDING_EVENTS_PATH.read_text()
    except OSError:
        return

    # Find the position after the last checkpointed batch
    marker = f"<!-- batch:{checkpoint} -->"
    idx = content.find(marker)
    if idx == -1:
        return

    # Find the start of the next batch after the checkpoint
    next_batch = content.find("<!-- batch:", idx + len(marker))
    if next_batch == -1:
        # Everything has been processed — clear the file
        PENDING_EVENTS_PATH.write_text("")
    else:
        PENDING_EVENTS_PATH.write_text(content[next_batch:])


def _has_unread_events() -> bool:
    """Check if there are batches after the manager's checkpoint."""
    if not PENDING_EVENTS_PATH.exists():
        return False
    try:
        content = PENDING_EVENTS_PATH.read_text()
    except OSError:
        return False
    if not content.strip():
        return False

    checkpoint = _read_checkpoint()
    # Check if any batch marker has an ID > checkpoint
    import re
    batch_ids = [int(m) for m in re.findall(r'<!-- batch:(\d+) -->', content)]
    return any(bid > checkpoint for bid in batch_ids)


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


_RELAY_SKIP_EVENTS = {
    "system.update_available",
    "worker.waiting_input",
    "worker.working",
    "worker.exited",
    "task.unlabeled",
    "task.updated",
}


def _summarize_events_for_relay(events: list[dict]) -> str:
    """One-line summary of user-facing events for the chat relay.

    Filters out internal state-tracking events that are noise in Slack.
    """
    parts = []
    for e in events:
        etype = e.get("type", "")
        if etype in _RELAY_SKIP_EVENTS:
            continue
        data = e.get("data", {})
        detail = data.get("text", "") or data.get("title", "") or ""
        if detail:
            parts.append(f"{etype}: {detail[:100]}")
        else:
            iid = data.get("issue_id", "")
            parts.append(f"{etype}" + (f" #{iid}" if iid else ""))
    return "\n".join(parts)


def _inject_trigger() -> None:
    """Send the standard trigger to the manager."""
    trigger = (
        f"New events. Read {PENDING_EVENTS_PATH} and process only batches "
        f"after your checkpoint in {CHECKPOINT_PATH}. "
        f"When done, write the last batch number you processed to {CHECKPOINT_PATH}."
    )
    inject(trigger)


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

    # Emit restart event with changelog so the manager can update Slack
    try:
        repo_root = str(Path(__file__).resolve().parent.parent.parent)
        result = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True, text=True, timeout=5, cwd=repo_root,
        )
        changelog = result.stdout.strip() if result.returncode == 0 else ""
        version = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=repo_root,
        ).stdout.strip()
        bus.push("system.restarted", "system", {
            "version": version,
            "changelog": changelog,
            "text": f"Modastack restarted (now at {version}). Recent changes:\n{changelog}",
        })
    except Exception as e:
        log.debug(f"Failed to emit restart event: {e}")

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

    # Initialize chat relay adapter (mirrors manager I/O to Slack etc.)
    from modastack.relay import build_adapter
    relay = build_adapter()
    last_relayed_response_ts: float = 0  # avoid duplicate relay of same response

    log.info(f"Listening for events (batch window: {batch_window}s)")
    tick_count = 0
    manager_working_since: float | None = None
    last_trigger_at: float = 0  # monotonic time of last trigger
    MANAGER_BUSY_TIMEOUT = 120
    TRIGGER_MIN_INTERVAL = 5  # minimum seconds between triggers
    TRUNCATE_INTERVAL = 60  # truncate processed batches every 60s
    last_truncate: float = 0

    while True:
        # Check if manager session is alive
        if not is_alive():
            log.warning("Manager session died — restarting")
            start_or_resume()
            manager_working_since = None

        # Wait for events
        bus.wait(timeout=batch_window)
        events = bus.drain()

        # Periodic truncation of processed batches
        now = time.monotonic()
        if now - last_truncate > TRUNCATE_INTERVAL:
            last_truncate = now
            _truncate_processed()

        state = detect_state()

        # Relay manager output when it finishes a turn
        if state == "waiting_input":
            response = read_last_response()
            if response:
                from modastack.manager.session import _read_last_activity
                last = _read_last_activity()
                resp_ts = last.get("ts", 0) if last else 0
                if resp_ts > last_relayed_response_ts:
                    last_relayed_response_ts = resp_ts
                    try:
                        relay.send(response, role="assistant")
                    except Exception as e:
                        log.warning(f"Relay output failed: {e}")

        if not events:
            # If there are unread events and manager is idle, trigger
            if _has_unread_events() and state == "waiting_input":
                if now - last_trigger_at > TRIGGER_MIN_INTERVAL:
                    log.info("Manager idle with unread events — triggering")
                    _inject_trigger()
                    last_trigger_at = time.monotonic()
            if state == "waiting_input":
                manager_working_since = None
            continue

        tick_count += 1
        event_types = ", ".join(set(e["type"] for e in events))
        log.info(f"Batch #{tick_count}: {len(events)} events — {event_types}")

        # Dispatch to workflow engine first
        for event in events:
            dispatcher.dispatch(event)
            dispatcher.feed_event(event)

        unhandled = [e for e in events if not dispatcher.was_dispatched(e)]
        if not unhandled:
            _log_batch(events)
            log.info(f"Batch #{tick_count}: all events handled by workflows")
            continue

        # Always append events to the log — never lost
        batch_id = _next_batch_id()
        _append_batch(batch_id, unhandled)
        _log_batch(events)
        log.info(f"Batch #{tick_count} appended as batch:{batch_id}")

        # Relay input to chat
        try:
            summary = _summarize_events_for_relay(unhandled)
            if summary:
                relay.send(summary, role="user")
        except Exception as e:
            log.warning(f"Relay input failed: {e}")

        if state == "working":
            if manager_working_since is None:
                manager_working_since = time.monotonic()

            busy_secs = time.monotonic() - manager_working_since

            if busy_secs > MANAGER_BUSY_TIMEOUT:
                log.warning(
                    f"Batch #{tick_count}: manager busy for {int(busy_secs)}s — force-triggering"
                )
                _inject_trigger()
                last_trigger_at = time.monotonic()
                manager_working_since = None
            else:
                log.info(f"Batch #{tick_count}: manager busy ({int(busy_secs)}s) — events queued, trigger deferred")
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
            log.info(f"Batch #{tick_count}: manager started working — trigger deferred")
            manager_working_since = time.monotonic()
            continue

        # Manager is idle — trigger it
        if time.monotonic() - last_trigger_at > TRIGGER_MIN_INTERVAL:
            _inject_trigger()
            last_trigger_at = time.monotonic()
            log.info(f"Batch #{tick_count} trigger sent to manager")
        else:
            log.info(f"Batch #{tick_count}: events queued, trigger recently sent")
