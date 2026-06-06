"""Generic event infrastructure — client, server, drain loop, subscriptions.

This package is transport-agnostic. Any agent can use it to subscribe
to event topics and receive events via the drain loop.
"""

from modastack.events.client import (
    EventServerClient,
    event_queue,
    format_event_for_manager,
    start_event_client,
)
from modastack.events.drain import drain_loop
from modastack.events.server import ensure_running, register
from modastack.events.subscriptions import discover_subscriptions

__all__ = [
    "EventServerClient",
    "event_queue",
    "format_event_for_manager",
    "start_event_client",
    "drain_loop",
    "ensure_running",
    "register",
    "discover_subscriptions",
]
