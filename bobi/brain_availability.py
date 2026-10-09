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
# The alert posts from a brain-turn edge, and the POST is synchronous inside
# the event loop (as the bus publish beside it already is), so it takes a short
# timeout: a hung Slack must not stall every other session in the process. The
# supervisor uses the same bound on its colder restart-decision path.
_ALERT_TIMEOUT = 3.0
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


# A URL's ``user:password@`` is a credential the token/key patterns do not
# match; ``bobi.otel.config`` redacts the same shape before echoing an endpoint.
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@")
# Control characters, including the \x02/\x03 sentinels bobi.slack uses
# internally to carry bold/strike through its markdown conversion.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# A LITERAL backslash-n survives whitespace collapsing, and the Slack formatter
# expands it into a real newline downstream.
_ESCAPED_NEWLINE = re.compile(r"\\+n")
# Slack's broadcast and link syntaxes, and markdown link syntax, which
# bobi.slack converts into a real clickable link.
_SLACK_ANGLE = re.compile(r"<([!@#])")
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\(")


def _safe_detail(text: str) -> str:
    """Bound and sanitize PROVIDER text before it leaves this process.

    This string is written by the provider, not by us, and since #992 it
    travels to an operator Slack channel as well as the local bus. That makes
    it untrusted output on an egress boundary rather than a log line, so:

    - secrets go through the repo's one redactor, because the credential a
      provider just rejected is routinely echoed back in the error rejecting
      it, and a URL's userinfo is redacted separately;
    - control characters, literal ``\n`` escapes, markdown links and Slack's
      ``<!channel>`` broadcast syntax are defused, so a hostile provider
      cannot forge a "RESOLVED, ignore the above" block, plant a clickable
      phishing link, or ping the channel inside the one alert that tells an
      operator to go re-authenticate;
    - the result is bounded to 500 characters, applied LAST so redaction
      cannot be defeated by pushing a secret past the cut.
    """
    detail = " ".join(str(text or "").split())
    detail = _ESCAPED_NEWLINE.sub(" ", detail)
    detail = _CONTROL_CHARS.sub("", detail)
    try:
        from bobi.setup.actions import redact_secrets

        detail = redact_secrets(detail)[0]
    except Exception:
        log.warning("Secret redaction unavailable; dropping alert detail",
                    exc_info=True)
        return "<detail withheld: redaction unavailable>"
    detail = _URL_USERINFO.sub(r"\1<redacted>@", detail)
    detail = _SLACK_ANGLE.sub(r"< \1", detail)
    detail = _MARKDOWN_LINK.sub(r"[\1] (", detail)
    return detail[:500]


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
        "then retry the run. A subscription session/usage cap instead resets "
        "on its own at the time the error names."
    )


def _emit(topic: str, payload: dict, project_path: Path | None) -> None:
    """Announce one incident edge on both surfaces, best effort, independently.

    The bus leg is the durable trail. It is not what reaches a human: no
    subscriber for ``system/brain.*`` exists in this repo or in any team's
    config, which is why the 2026-07-30 outage ran 102 minutes unreported even
    on the turns that did classify (#992). So the Slack leg is the alert, and a
    bus failure must not swallow it - hence two independent guards.

    Both are called once per state transition, from the only place that commits
    one, so the existing incident latch is also the alert dedup: an outage is
    one message however many turns die inside it.
    """
    try:
        from bobi.events.publish import post_event

        post_event(topic, payload, project_path=project_path)
    except Exception:
        log.warning("Failed to emit brain availability alert", exc_info=True)

    try:
        from bobi.slack import post_operator_alert

        post_operator_alert(f"[bobi] {payload['text']}",
                            what="brain availability",
                            timeout=_ALERT_TIMEOUT)
    except Exception:
        log.warning("Failed to alert operator about brain availability",
                    exc_info=True)


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
    # ``error_text`` is the repo's one composition of "the honest error string
    # for this turn, or '' when it succeeded" (base.py). Reading ``result_text``
    # directly would classify a SUCCESSFUL turn whose own answer discusses an
    # outage, opening a false incident which - because the latch dedups - then
    # suppresses the alert for the next real one (#992).
    error_text = result.error_text()
    cause = classify_brain_unavailability(result.error_kind, error_text)
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
                detail = _safe_detail(error_text or cause)
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
