"""Event consumer — thin orchestrator.

Starts three independent components:
1. Manager session (Claude Code via Agent SDK)
2. Event client (WebSocket to centralized event server)
3. Slack Socket Mode (direct DM injection)

Then watches the manager session and restarts it if it dies.
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
    """Start modastack: manager session + event client + Slack."""

    log.info("Modastack starting")
    PID_PATH.write_text(str(os.getpid()))

    config = GlobalConfig.load()

    if not start_or_resume():
        log.error("Failed to start manager session")
        return

    log.info("Manager session started")

    # Start workflow dispatcher
    from modastack.workflow.triggers import WorkflowDispatcher
    dispatcher = WorkflowDispatcher()
    dispatcher.load_all_workflows()
    log.info(f"Workflow dispatcher loaded {len(dispatcher.workflows)} workflow(s)")

    def _on_event(event: dict):
        dispatched = dispatcher.dispatch(event)
        if dispatched:
            log.info(f"Workflow dispatched for {event.get('type')}: "
                     f"{event.get('data', {}).get('issue_id', '')}")
        dispatcher.feed_event(event)

    if config.event_server_url and config.event_server_api_key:
        from .event_client import start_event_client
        start_event_client(
            server_url=config.event_server_url,
            deployment_id=config.event_server_deployment_id,
            api_key=config.event_server_api_key,
            on_event=_on_event,
        )
        log.info(f"Event client started -> {config.event_server_url}")
    else:
        log.warning("No event server configured — running without webhook events")

    from .slack_socket import start_socket_mode
    slack_thread = start_socket_mode()

    log.info("Modastack running")

    while True:
        time.sleep(HEALTH_CHECK_INTERVAL)
        if not is_alive():
            log.warning("Manager session died — restarting")
            start_or_resume()
