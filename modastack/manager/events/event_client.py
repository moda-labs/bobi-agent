"""Backward compat — real code lives in modastack.events.client."""
from modastack.events.client import *  # noqa: F401,F403
from modastack.events.client import (  # noqa: F401 — explicit re-exports for type checkers
    EventServerClient,
    event_queue,
    format_event_for_manager,
    start_event_client,
    _normalize_event,
    _normalize_github,
    _normalize_linear,
    _normalize_slack,
    _should_filter,
    _load_cursor,
    _save_cursor,
    _log_event,
    _resolve_slack_user,
    _format_requester,
    _state_path,
    _slack_user_cache,
)
