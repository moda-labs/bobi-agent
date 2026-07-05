"""Post synthetic events to the event server's generic topic endpoint.

Used by lifecycle emits (subagent), monitor check verdicts, and the
`bobi` CLI. Lives in events/ so library code never has to import
the CLI module to publish an event.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

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
            from bobi.config import Config
            url = Config.load(project_path).event_server_url or DEFAULT_URL
        except Exception:
            url = DEFAULT_URL
        _es_url_cache[key] = url
    return url


def bubble_context(project_path: Path | None) -> tuple[str, str, str]:
    """Resolve (event_server_url, bubble_id, bubble_key) for this instance.

    Shared by every signed client path (publish, channel gateway). The
    credentials are empty strings when no bubble has been minted yet; callers
    own that failure mode (skip vs raise).
    """
    if project_path is None:
        from bobi.paths import bobi_root
        project_path = bobi_root()
    else:
        project_path = Path(project_path)

    from bobi.config import load_bubble_state

    bubble = load_bubble_state(project_path)
    return (_event_server_url(project_path),
            bubble.get("bubble_id", ""), bubble.get("bubble_key", ""))


def _post_topic(topic: str, source: str, data: dict,
                project_path: Path | None) -> bool:
    """POST a v2 event to ``/events/{topic}`` with the given source.

    The server builds the event's routing topics from the URL ``topic`` and
    the body ``source`` (``createTopicEvent`` in event-server/src/core.ts).

    The publish is signed with the instance's bubble key so the server routes
    it within the bubble. An unsigned publish is rejected (403) — namespacing
    is not authentication. Any in-instance process can sign (the key lives in
    bubble.json); a missing bubble means the instance isn't started.
    """
    from bobi.events.signing import signed_request

    es_url, bubble_id, bubble_key = bubble_context(project_path)
    if not (bubble_id and bubble_key):
        # No bubble credential yet. An unsigned publish is rejected (403)
        # unconditionally — namespacing is not authentication — so a POST here
        # can only ever 403; skip the doomed round-trip. This is the normal
        # cold-start / `--fresh` window before the bubble is minted, when
        # best-effort lifecycle emits (session.started/failed) may fire first.
        # Drop quietly at debug; the next session registration mints the bubble.
        log.debug("No bubble credential yet — skipping publish to %s", topic)
        return False

    try:
        resp = signed_request(
            es_url, "POST", f"/events/{topic}",
            {"source": source, "payload": data},
            bubble_id, bubble_key, timeout=10.0,
        )
        # A 403 (e.g. the server forgot the bubble after a restart, or
        # bubble.json is stale) returns a JSON error body — do NOT treat that
        # as success and silently drop the event. The next session
        # registration re-mints the bubble; a transient publish failure is
        # surfaced, not swallowed.
        if resp.status_code >= 400:
            log.warning("Publish to %s rejected (%d): %s",
                        topic, resp.status_code, resp.text[:200])
            return False
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
