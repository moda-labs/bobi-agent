"""Post synthetic events to the event server's generic topic endpoint.

Used by lifecycle emits (subagent), monitor check verdicts, and the
`modastack` CLI. Lives in events/ so library code never has to import
the CLI module to publish an event.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

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
        from modastack.sdk import get_project_root
        project_path = get_project_root() or Path.cwd().resolve()

    es_url = _event_server_url(project_path)

    payload = json.dumps({"source": source, "payload": data}).encode()
    try:
        req = urllib.request.Request(
            f"{es_url}/events/{etype}",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "modastack"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.loads(resp.read())
        return True
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as e:
        log.warning(f"Failed to post event {event_type}: {e}")
        return False
