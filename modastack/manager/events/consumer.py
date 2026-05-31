"""Event consumer — thin orchestrator.

Three components:
1. Manager session (Claude Code via Agent SDK)
2. Event client (WebSocket to event server → pushes to queue)
3. Drain loop (batches queued events → injects into manager)

The manager handles all response routing (Slack replies, etc.) using
its own tools. The consumer never touches transport-specific logic.
"""

import json
import logging
import os
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path

import truststore
truststore.inject_into_ssl()

from modastack.config import GlobalConfig, GLOBAL_CONFIG_DIR
from modastack.manager.session import start_or_resume, is_alive, detect_state

log = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 30
DRAIN_INTERVAL = 2
PID_PATH = GLOBAL_CONFIG_DIR / "modastack.pid"


def _cleanup_pid():
    PID_PATH.unlink(missing_ok=True)


def _notify_slack(config: GlobalConfig, text: str) -> None:
    """Post a notification to the configured Slack DM channel."""
    token = config.slack_bot_token
    channel = config.slack_dm_channel
    if not token or not channel:
        return
    try:
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=json.dumps({"channel": channel, "text": text}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except (urllib.error.URLError, OSError, TimeoutError):
        pass


def _drain_loop():
    """Drain the event queue and inject batched events into the manager."""
    from .event_client import event_queue, format_event_for_manager
    from modastack.manager.session import inject, detect_state

    while True:
        event = event_queue.get()

        time.sleep(DRAIN_INTERVAL)
        batch = [event]
        while not event_queue.empty():
            batch.append(event_queue.get_nowait())

        lines = [format_event_for_manager(e) for e in batch]
        text = "\n\n".join(lines)

        if detect_state() != "waiting_input":
            log.warning(f"Manager not idle — dropping {len(batch)} event(s)")
            continue

        log.info(f"Injecting {len(batch)} event(s)")
        inject(text)


def run(**kwargs):
    """Start modastack: manager session + event client + drain loop."""
    import atexit
    import signal

    log.info("Modastack starting")
    PID_PATH.write_text(str(os.getpid()))
    atexit.register(_cleanup_pid)

    def _handle_term(signum, frame):
        log.info("Received SIGTERM — shutting down")
        _cleanup_pid()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_term)

    config = GlobalConfig.load()

    if not start_or_resume():
        log.error("Failed to start manager session")
        return

    log.info("Manager session started")

    # Load workflow dispatcher for feed_event (approval nodes in active runs)
    from modastack.workflow.triggers import WorkflowDispatcher
    dispatcher = WorkflowDispatcher()
    dispatcher.load_all_workflows()
    log.info(f"Loaded {len(dispatcher.workflows)} workflow(s)")
    dispatcher.cleanup_stale_runs()

    if config.event_server_url and config.event_server_api_key:
        from .event_client import start_event_client
        start_event_client(
            server_url=config.event_server_url,
            deployment_id=config.event_server_deployment_id,
            api_key=config.event_server_api_key,
            on_event=dispatcher.feed_event,
        )
        log.info(f"Event client started -> {config.event_server_url}")
    else:
        log.warning("No event server configured — running without webhook events")

    # Start drain loop
    drain_thread = threading.Thread(target=_drain_loop, daemon=True, name="drain-loop")
    drain_thread.start()

    # Start dashboard in background
    from dashboard.app import run_dashboard
    dashboard_thread = threading.Thread(
        target=run_dashboard, kwargs={"port": 8095},
        daemon=True, name="dashboard",
    )
    dashboard_thread.start()
    log.info("Dashboard started on http://localhost:8095")

    log.info("Modastack running")
    _notify_slack(config, "Modastack started.")

    while True:
        time.sleep(HEALTH_CHECK_INTERVAL)
        if not is_alive():
            log.warning("Manager session died — restarting")
            start_or_resume()
