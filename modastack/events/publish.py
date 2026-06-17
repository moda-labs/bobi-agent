"""Post synthetic events to the event server's generic topic endpoint.

Used by lifecycle emits (subagent), monitor check verdicts, and the
`modastack` CLI. Lives in events/ so library code never has to import
the CLI module to publish an event.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from modastack import http as pooled

log = logging.getLogger(__name__)

# Resolved event-server URL per project root. Loading config re-reads .env
# and agent.yaml; the URL can't change while the process runs.
_es_url_cache: dict[str, str] = {}

DEFAULT_URL = "http://localhost:8080"


def _event_server_url(project_path: Path) -> str:
    key = str(project_path)
    url = _es_url_cache.get(key)
    if url is None:
        try:
            from modastack.config import Config
            url = Config.load(project_path).event_server_url or DEFAULT_URL
        except Exception:
            url = DEFAULT_URL
        _es_url_cache[key] = url
    return url


def _post_topic(topic: str, source: str, data: dict,
                project_path: Path | None) -> bool:
    """POST a v2 event to ``/events/{topic}`` with the given source.

    The server builds the event's routing topics from the URL ``topic`` and
    the body ``source`` (``createTopicEvent`` in event-server/src/core.ts).
    """
    if project_path is None:
        from modastack.paths import modastack_root
        project_path = modastack_root()

    es_url = _event_server_url(project_path)

    try:
        resp = pooled.post(
            f"{es_url}/events/{topic}",
            json={"source": source, "payload": data},
            timeout=10.0,
        )
        resp.json()
        return True
    except (httpx.HTTPError, OSError, TimeoutError, ValueError) as e:
        log.warning(f"Failed to post event {source}/{topic}: {e}")
        return False


def post_event(event_type: str, data: dict,
               project_path: Path | None = None) -> bool:
    """Post a synthetic event. Returns True when the server accepted it.

    ``event_type`` is "source/type" (e.g. "agent/session.started");
    a bare type defaults the source to "monitor". The source is stripped to
    the body and the bare type becomes the topic path, so the server routes
    onto both the bare type and the source-qualified topic.
    """
    if "/" in event_type:
        source, etype = event_type.split("/", 1)
    else:
        source, etype = "monitor", event_type

    return _post_topic(etype, source, data, project_path)


def publish_inbox(session: str, payload: dict,
                  project_path: Path | None = None) -> bool:
    """Publish an inter-agent message to a session's ``inbox/<session>`` topic.

    Posts to ``/events/inbox/<session>`` with ``source="inbox"``. Because the
    URL topic path already carries the ``inbox/`` source prefix, the server's
    ``createTopicEvent`` emits exactly ``["inbox/<session>"]`` as the routing
    topics — byte-identical to the subscription key a session registers
    (``inbox/<session>``). This is the integration seam for comms-v1: publish
    key == subscribe key. See tests/test_inbox.py and
    event-server/test/core.spec.ts.
    """
    return _post_topic(f"inbox/{session}", "inbox", payload, project_path)


def publish_reply(reply_to: str, corr_id: str, response: str,
                  project_path: Path | None = None) -> bool:
    """Publish a correlated reply for a blocking ``deliver(wait=True)`` caller.

    The blocking sender subscribed to a transient ``reply/<uuid>`` topic and
    passed it as ``reply_to`` on the request. Posting to ``/events/<reply_to>``
    makes the server emit exactly that topic as the routing key (the URL path
    is the subscription key — same trick as ``publish_inbox``), so the reply
    reaches the waiting sender, which matches it on ``corr_id``. See
    ``inbox.Inbox.respond`` (target side) and ``inbox._await_reply`` (sender).
    """
    return _post_topic(reply_to, "reply",
                       {"corr_id": corr_id, "response": response}, project_path)
