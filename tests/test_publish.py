"""Unit tests for events/publish — the synthetic-event POST path.

Focus: the bubble-credential guard. An unsigned publish is rejected (403)
unconditionally — namespacing is not authentication — so when no bubble has
been minted yet (the cold-start / --fresh window before the bubble lands),
_post_topic must NOT waste a doomed round-trip. Lifecycle emits
(session.started / session.failed) routinely fire in that window; the old
code POSTed unsigned, logged two WARNINGs, and got a 403. See the
investigation: prod manager.log lines 34867-35008.
"""

from unittest.mock import patch

from bobi import http as pooled
from bobi.config import save_bubble_state
from bobi.events import publish as pub


def _project(tmp_path):
    (tmp_path / ".bobi").mkdir()
    return tmp_path


def test_post_topic_skips_doomed_publish_when_no_bubble(tmp_path, monkeypatch):
    """No bubble credential → return False WITHOUT hitting the network."""
    project = _project(tmp_path)
    monkeypatch.setattr(pub, "_event_server_url", lambda p: "http://localhost:8080")

    calls = []
    with patch.object(pooled, "request",
                      side_effect=lambda *a, **k: calls.append((a, k))):
        ok = pub._post_topic("session.started", "agent", {"x": 1}, project)

    assert ok is False
    assert calls == [], "must not POST an unsigned event that is guaranteed to 403"


def test_post_topic_signs_and_posts_when_bubble_present(tmp_path, monkeypatch):
    """With a bubble, _post_topic signs and POSTs (happy path still works)."""
    project = _project(tmp_path)
    save_bubble_state(project, "bub_test", "bkey_test")
    monkeypatch.setattr(pub, "_event_server_url", lambda p: "http://localhost:8080")

    class _Resp:
        status_code = 200
        text = "{}"

        @staticmethod
        def json():
            return {}

    captured = {}

    def _fake_request(method, url, content=None, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _Resp()

    with patch.object(pooled, "request", side_effect=_fake_request):
        ok = pub._post_topic("session.started", "agent", {"x": 1}, project)

    assert ok is True
    assert captured["url"].endswith("/events/session.started")
    # Signing headers must be attached for the server to route within the bubble.
    hdr_lower = {k.lower() for k in captured["headers"]}
    assert any("bubble" in h or "signature" in h or "x-moda" in h for h in hdr_lower), \
        f"expected bubble-signing headers, got {sorted(captured['headers'])}"
