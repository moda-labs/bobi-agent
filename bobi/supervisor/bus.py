"""Shared bus-publish seam for the sidecar's two halves (telemetry + admin).

Both the outbound telemetry layer and the inbound admin listener publish
bubble-signed events; this is the ONE wrapper they share so the private-API
publish path (`bobi.events.publish._post_topic`) has a single owner.
"""

from __future__ import annotations

from pathlib import Path


def default_publish(topic: str, source: str, data: dict,
                    project_root: Path | None) -> bool:
    """Bubble-signed publish via the public event publisher. `_post_topic` posts
    to /events/<topic>; a URL path that already carries the source prefix
    (e.g. "fleet/...") routes onto exactly that single topic (same trick as
    publish_inbox / publish_reply)."""
    from bobi.events.publish import _post_topic
    return _post_topic(topic, source, data, project_root)
