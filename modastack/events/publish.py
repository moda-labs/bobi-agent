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


def post_event(event_type: str, data: dict,
               project_path: Path | None = None) -> bool:
    """Post a synthetic event. Returns True when the server accepted it.

    ``event_type`` is "source/type" (e.g. "agent/session.started");
    a bare type defaults the source to "monitor".
    """
    if "/" in event_type:
        source, etype = event_type.split("/", 1)
    else:
        source, etype = "monitor", event_type

    if project_path is None:
        from modastack.paths import modastack_root
        project_path = modastack_root()

    es_url = _event_server_url(project_path)

    try:
        resp = pooled.post(
            f"{es_url}/events/{etype}",
            json={"source": source, "payload": data},
            timeout=10.0,
        )
        resp.json()
        return True
    except (httpx.HTTPError, OSError, TimeoutError, ValueError) as e:
        log.warning(f"Failed to post event {event_type}: {e}")
        return False
