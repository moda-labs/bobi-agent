"""Event consumer — thin orchestrator.

Starts two independent components:
1. Manager session (Claude Code via Agent SDK)
2. Event client (WebSocket to centralized event server)

Slack events flow through the event server like GitHub and Linear.
The event client handles Slack reply-back to the originating channel/thread.
"""

import logging
import os
import time
from pathlib import Path

import truststore
truststore.inject_into_ssl()

from modastack.config import GlobalConfig, GLOBAL_CONFIG_DIR
from modastack.manager.session import start_or_resume, is_alive

log = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 30
PID_PATH = GLOBAL_CONFIG_DIR / "modastack.pid"


def run(**kwargs):
    """Start modastack: manager session + event client."""

    log.info("Modastack starting")
    PID_PATH.write_text(str(os.getpid()))

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

    # Start dashboard in background
    import threading
    from dashboard.app import run_dashboard
    dashboard_thread = threading.Thread(
        target=run_dashboard, kwargs={"port": 8095},
        daemon=True, name="dashboard",
    )
    dashboard_thread.start()
    log.info("Dashboard started on http://localhost:8095")

    log.info("Modastack running")

    from .event_client import _post_slack_reply
    if config.slack_dm_channel:
        _post_slack_reply(config.slack_dm_channel, "Modastack started.")

    while True:
        time.sleep(HEALTH_CHECK_INTERVAL)
        if not is_alive():
            log.warning("Manager session died — restarting")
            start_or_resume()
