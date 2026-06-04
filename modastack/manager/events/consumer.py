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
from modastack.manager.events.slack_responder import _markdown_to_slack
from modastack.manager.session import (
    ManagerSession, set_default_session,
    start_or_resume, is_alive, detect_state,
)

log = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 30
DRAIN_INTERVAL = 2
PID_PATH = GLOBAL_CONFIG_DIR / "modastack.pid"


def _cleanup_pid():
    PID_PATH.unlink(missing_ok=True)


def _post_dm(token: str, channel: str, text: str) -> None:
    """Post a manager text response to the configured Slack DM channel."""
    if not text.strip():
        return
    text = _markdown_to_slack(text)
    payload = json.dumps({"channel": channel, "text": text}).encode()
    try:
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if result.get("ok"):
            log.info(f"Slack reply sent to {channel}")
        else:
            log.warning(f"Slack DM error: {result.get('error')}")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log.warning(f"Slack DM failed: {e}")


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
    from modastack.manager.session import inject, detect_state, set_response_callback

    log.info("Drain loop waiting for manager to finish startup")
    if not _wait_for_manager():
        log.error("Manager never became ready — drain loop exiting")
        return
    log.info("Manager ready — drain loop active")

    config = GlobalConfig.load()
    dm_channel = config.slack_dm_channel
    dm_token = config.slack_bot_token
    if dm_channel and dm_token:
        set_response_callback(lambda t: _post_dm(dm_token, dm_channel, t))
        log.info(f"Streaming all manager output to Slack DM {dm_channel}")

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
            inject(text)


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

    # Create the manager session for the first registered repo (or cwd)
    repo_path = config.repos[0] if config.repos else Path.cwd()
    session = ManagerSession(repo_path=repo_path)
    set_default_session(session)

    if not session.start_or_resume():
        log.error("Failed to start manager session")
        return

    log.info("Manager session started")

    from modastack.workflow.triggers import WorkflowDispatcher
    dispatcher = WorkflowDispatcher()
    dispatcher.load_all_workflows()
    log.info(f"Loaded {len(dispatcher.workflows)} workflow(s)")

    event_client = None
    if config.event_server_url and config.event_server_api_key:
        from .event_client import EventServerClient
        event_client = EventServerClient(
            server_url=config.event_server_url,
            deployment_id=config.event_server_deployment_id,
            api_key=config.event_server_api_key,
        )
        event_client.start()
        log.info(f"Event client started -> {config.event_server_url}")
        atexit.register(event_client.stop)
    else:
        log.warning("No event server configured — running without webhook events")

    # Start drain loop
    drain_thread = threading.Thread(target=_drain_loop, daemon=True, name="drain-loop")
    drain_thread.start()

    # Start background monitor scheduler (polls to fill webhook gaps,
    # injecting synthetic events onto the same queue webhooks use).
    from modastack.monitors.scheduler import MonitorScheduler
    monitor_scheduler = MonitorScheduler()
    monitor_scheduler.start()

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
