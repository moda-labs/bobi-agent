"""Generic event infrastructure — client, server, drain loop, subscriptions.

This package is transport-agnostic. Any agent can use it to subscribe
to event topics and receive events via the drain loop.
"""

from bobi.events.client import (
    EventServerClient,
    event_queue,
    format_event_for_manager,
)
from bobi.events.drain import drain_loop
from bobi.events.reactor import AutoDispatchRule, EventReactor
from bobi.events.server import ensure_running, register
from bobi.events.subscriptions import discover_subscriptions

__all__ = [
    "AutoDispatchRule",
    "EventReactor",
    "EventServerClient",
    "event_queue",
    "format_event_for_manager",
    "drain_loop",
    "ensure_running",
    "register",
    "discover_subscriptions",
]
