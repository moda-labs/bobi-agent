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


def _wait_for_manager(timeout: int = 300) -> bool:
    """Block until the manager is in waiting_input state."""
    from modastack.manager.session import detect_state
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if detect_state() == "waiting_input":
            return True
        time.sleep(2)
    return False


def _drain_loop():
    """Drain the event queue and inject batched events into the manager."""
    from .event_client import event_queue, format_event_for_manager
    from .slack_responder import SlackResponder
    from modastack.manager.session import inject, detect_state, read_last_response

    responder = SlackResponder()

    log.info("Drain loop waiting for manager to finish startup")
    if not _wait_for_manager():
        log.error("Manager never became ready — drain loop exiting")
        return
    log.info("Manager ready — drain loop active")

    while True:
        event = event_queue.get()

        time.sleep(DRAIN_INTERVAL)
        batch = [event]
        while not event_queue.empty():
            batch.append(event_queue.get_nowait())

        slack_events = [e for e in batch if e.get("source") == "slack"]
        other_events = [e for e in batch if e.get("source") != "slack"]

        for group, is_slack in [(other_events, False), (slack_events, True)]:
            if not group:
                continue

            lines = [format_event_for_manager(e) for e in group]
            text = "\n\n".join(lines)

            if detect_state() != "waiting_input":
                log.info(f"Manager busy — waiting before injecting {len(group)} event(s)")
                if not _wait_for_manager():
                    log.warning(f"Manager not ready after wait — dropping {len(group)} event(s)")
                    continue

            log.info(f"Injecting {len(group)} event(s)")
            ok = inject(text)

            if ok and is_slack:
                response = read_last_response() or ""
                responder.handle(group, response)


def _kill_stale_instances():
    """Kill any running modastack start processes besides ourselves."""
    import subprocess as sp
    import signal as sig
    my_pid = os.getpid()

    if PID_PATH.exists():
        try:
            old_pid = int(PID_PATH.read_text().strip())
            if old_pid != my_pid:
                os.kill(old_pid, sig.SIGTERM)
                log.info(f"Killed stale instance from PID file (pid {old_pid})")
        except (ProcessLookupError, ValueError, PermissionError):
            pass

    try:
        result = sp.run(
            ["pgrep", "-f", "modastack.*start"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            try:
                pid = int(line.strip())
                if pid != my_pid:
                    os.kill(pid, sig.SIGTERM)
                    log.info(f"Killed orphaned modastack process {pid}")
            except (ProcessLookupError, ValueError, PermissionError):
                pass
    except (FileNotFoundError, sp.TimeoutExpired):
        pass


def run(**kwargs):
    """Start modastack: manager session + event client + drain loop."""
    import atexit
    import signal

    log.info("Modastack starting")
    _kill_stale_instances()
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

    event_client = None
    if config.event_server_url and config.event_server_api_key:
        from .event_client import EventServerClient
        event_client = EventServerClient(
            server_url=config.event_server_url,
            deployment_id=config.event_server_deployment_id,
            api_key=config.event_server_api_key,
            on_event=dispatcher.feed_event,
        )
        event_client.start()
        log.info(f"Event client started -> {config.event_server_url}")
        atexit.register(event_client.stop)
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
