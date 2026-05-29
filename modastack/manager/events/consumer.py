"""Event consumer — thin orchestrator.

Starts three independent components:
1. Manager session (Claude Code in tmux)
2. Event client (WebSocket to centralized event server)
3. Slack Socket Mode (direct DM injection)

Then watches the manager session and restarts it if it dies.
"""

import logging
import time

import truststore
truststore.inject_into_ssl()

from modastack.config import GlobalConfig
from modastack.manager.session import start_or_resume, is_alive

log = logging.getLogger(__name__)

HEALTH_CHECK_INTERVAL = 30


def run(**kwargs):
    """Start modastack: manager session + event client + Slack."""

    log.info("Modastack starting")

    config = GlobalConfig.load()

    # Start the manager session (non-blocking — Claude boots in the background)
    if not start_or_resume():
        log.error("Failed to start manager session")
        return

    log.info("Manager session started")

    # Start event server client
    if config.event_server_url and config.event_server_api_key:
        from .event_client import start_event_client
        start_event_client(
            server_url=config.event_server_url,
            deployment_id=config.event_server_deployment_id,
            api_key=config.event_server_api_key,
        )
        log.info(f"Event client started → {config.event_server_url}")
    else:
        log.warning("No event server configured — running without webhook events")

    # Start Slack Socket Mode
    from .slack_socket import start_socket_mode
    slack_thread = start_socket_mode()

    log.info("Modastack running")

    # Health check loop — restart manager if it dies
    while True:
        time.sleep(HEALTH_CHECK_INTERVAL)
        if not is_alive():
            log.warning("Manager session died — restarting")
            start_or_resume()
