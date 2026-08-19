"""Durable, deduplicated operator alerts for unavailable brain accounts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from bobi.brain.base import (
    ERROR_KIND_AUTHENTICATION,
    ERROR_KIND_CREDITS_EXHAUSTED,
    TurnResult,
    classify_brain_unavailability,
)
from bobi.fsutil import atomic_write_json, file_lock

log = logging.getLogger(__name__)

STATE_FILE = "brain-availability.json"
_ALERT_TOPICS = {
    ERROR_KIND_AUTHENTICATION: "system/brain.auth.failed",
    ERROR_KIND_CREDITS_EXHAUSTED: "system/brain.credits.exhausted",
}


def _state_path(project_path: Path | None) -> Path:
    from bobi import paths

    return paths.state_path(project_path) / STATE_FILE


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"incidents": {}}
    incidents = data.get("incidents") if isinstance(data, dict) else None
    return {"incidents": incidents if isinstance(incidents, dict) else {}}


def _account_boundary(provider: str) -> str:
    provider = str(provider or "unknown").lower()
    if provider == "anthropic":
        source = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    elif provider == "openai":
        source = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    elif provider == "gateway":
        source = os.environ.get("BOBI_GATEWAY_BASE_URL") or "configured-gateway"
    else:
        source = provider
    digest = hashlib.sha256(source.encode("utf-8", "replace")).hexdigest()[:12]
    return f"{provider}:{digest}"


def _agent_name(project_path: Path | None) -> str:
    try:
        from bobi import paths

        return paths.agent_name_for_root(project_path)
    except Exception:
        return Path(project_path).name if project_path is not None else "unknown"


def _safe_detail(text: str) -> str:
    detail = " ".join(str(text or "").split())[:500]
    return re.sub(
        r"(?i)\b(api[_ -]?key|authorization|token)\s*[:=]\s*\S+",
        r"\1=<redacted>",
        detail,
    )


def _remedy(cause: str, provider: str, agent: str) -> str:
    if cause == ERROR_KIND_AUTHENTICATION:
        return (
            f"Run `bobi agent {agent} login-bootstrap` to re-authenticate the "
            f"{provider} account, then retry the run."
        )
    if provider == "openai":
        return (
            "Purchase more Codex credits, increase the workspace usage limit, "
            "or ask the workspace owner/admin to do so."
        )
    return (
        f"Top up the {provider} account or increase its subscription/quota, "
        "then retry the run."
    )


def _emit(topic: str, payload: dict, project_path: Path | None) -> None:
    try:
        from bobi.events.publish import post_event

        post_event(topic, payload, project_path=project_path)
    except Exception:
        log.warning("Failed to emit brain availability alert", exc_info=True)


def observe_brain_turn(
    result: TurnResult,
    *,
    session: str,
    provider: str,
    project_path: Path | None = None,
) -> None:
    """Record an unavailable/recovered transition and emit its alert once.

    The state transition is committed under a cross-process lock before the
    best-effort publish, so concurrent scheduled ticks cannot duplicate an
    incident. A failed publish is not retried: alerting must never affect the
    brain result or turn lifecycle that triggered it.
    """
    cause = classify_brain_unavailability(
        result.error_kind,
        result.error_message or result.result_text,
    )
    succeeded = not (result.is_error or result.error_kind)
    if not cause and not succeeded:
        return

    provider = str(provider or "unknown")
    boundary = _account_boundary(provider)
    incident_key = f"{boundary}:{cause}" if cause else ""
    event: tuple[str, dict] | None = None
    now = time.time()

    try:
        state_path = _state_path(project_path)
        with file_lock(state_path):
            state = _load_state(state_path)
            incidents = state["incidents"]
            if cause:
                if incident_key in incidents:
                    return
                agent = _agent_name(project_path)
                detail = _safe_detail(
                    result.error_message or result.result_text or cause
                )
                incidents[incident_key] = {
                    "account_boundary": boundary,
                    "cause": cause,
                    "opened_at": now,
                    "provider": provider,
                }
                atomic_write_json(state_path, state, indent=None, sort_keys=True)
                remedy = _remedy(cause, provider, agent)
                event = (_ALERT_TOPICS[cause], {
                    "agent": agent,
                    "session": session,
                    "provider": provider,
                    "account_boundary": boundary,
                    "cause": cause,
                    "error": detail,
                    "remedy": remedy,
                    "text": (
                        f"Brain unavailable for agent '{agent}' ({provider}, "
                        f"{cause}): {detail}. {remedy}"
                    ),
                })
            else:
                recovered = [
                    (key, incident)
                    for key, incident in incidents.items()
                    if incident.get("account_boundary") == boundary
                ]
                if not recovered:
                    return
                for key, _ in recovered:
                    incidents.pop(key, None)
                atomic_write_json(state_path, state, indent=None, sort_keys=True)
                causes = sorted({item["cause"] for _, item in recovered})
                opened_at = min(float(item.get("opened_at") or now) for _, item in recovered)
                agent = _agent_name(project_path)
                event = ("system/brain.recovered", {
                    "agent": agent,
                    "session": session,
                    "provider": provider,
                    "account_boundary": boundary,
                    "recovered_causes": causes,
                    "outage_seconds": round(max(0.0, now - opened_at), 1),
                    "text": (
                        f"Brain access recovered for agent '{agent}' ({provider}); "
                        f"cleared: {', '.join(causes)}."
                    ),
                })
    except Exception:
        log.warning("Failed to persist brain availability state", exc_info=True)
        return

    if event is not None:
        _emit(event[0], event[1], project_path)
