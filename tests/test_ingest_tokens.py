"""Unit tests for events/ingest_tokens - the signed token-management client.

Focus: the bubble-credential guard (no bubble means no signed call can
succeed, so fail fast without touching the network), request signing, and
faithful surfacing of server rejections. The end-to-end path against a real
server lives in tests/integration/test_event_server.py::TestIngestTokens.
"""

import json
from unittest.mock import patch

import pytest

from bobi import http as pooled
from bobi.config import save_bubble_state
from bobi.events import ingest_tokens as it


def _project(tmp_path):
    (tmp_path / ".bobi").mkdir()
    return tmp_path


class _Resp:
    def __init__(self, status_code, body, *, text=None):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body) if text is None else text

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def test_no_bubble_fails_fast_without_network(tmp_path):
    project = _project(tmp_path)

    calls = []

    def _record(*a, **k):
        calls.append((a, k))
        return _Resp(200, {})

    with patch.object(pooled, "request", side_effect=_record):
        with pytest.raises(it.IngestTokenError, match="bubble"):
            it.create_token("alert/firing", project_path=project)

    assert calls == [], "must not call a server that can only 403 an unsigned request"


def test_create_signs_and_posts(tmp_path):
    project = _project(tmp_path)
    save_bubble_state(project, "bub_test", "bkey_test")

    captured = {}

    def _fake_request(method, url, *, content=None, headers=None, timeout=None):
        captured.update(method=method, url=url, content=content, headers=headers)
        return _Resp(201, {"id": "tok1", "topic": "alert/firing",
                           "token": "ingt_abc", "created_at": "now"})

    with patch.object(pooled, "request", side_effect=_fake_request):
        minted = it.create_token("alert/firing", name="oncall", project_path=project)

    assert minted["token"] == "ingt_abc"
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/ingest-tokens")
    assert json.loads(captured["content"]) == {"name": "oncall", "topic": "alert/firing"}
    hdr_lower = {k.lower() for k in captured["headers"]}
    assert "x-moda-signature" in hdr_lower and "x-moda-bubble" in hdr_lower


def test_list_and_revoke_sign_empty_bodies(tmp_path):
    project = _project(tmp_path)
    save_bubble_state(project, "bub_test", "bkey_test")

    calls = []

    def _fake_request(method, url, *, content=None, headers=None, timeout=None):
        calls.append((method, url, content, headers))
        if method == "GET":
            return _Resp(200, {"tokens": [{"id": "tok1", "topic": "alert/firing"}]})
        return _Resp(200, {"ok": True})

    with patch.object(pooled, "request", side_effect=_fake_request):
        tokens = it.list_tokens(project_path=project)
        it.revoke_token("tok1", project_path=project)

    assert tokens == [{"id": "tok1", "topic": "alert/firing"}]
    get_call, delete_call = calls
    assert get_call[0] == "GET" and get_call[2] is None
    assert delete_call[0] == "DELETE" and delete_call[1].endswith("/ingest-tokens/tok1")
    # Both are signed even with empty bodies - the signature covers method+path.
    for call in calls:
        assert "x-moda-signature" in {k.lower() for k in call[3]}


def test_revoke_quotes_token_id_into_signed_path(tmp_path):
    """The signature covers the exact wire path, so ids are percent-encoded
    before signing - otherwise httpx re-encodes the URL and the server's HMAC
    check fails with an opaque 403."""
    project = _project(tmp_path)
    save_bubble_state(project, "bub_test", "bkey_test")

    captured = {}

    def _fake_request(method, url, *, content=None, headers=None, timeout=None):
        captured.update(method=method, url=url)
        return _Resp(200, {"ok": True})

    with patch.object(pooled, "request", side_effect=_fake_request):
        it.revoke_token("tok 1/../x", project_path=project)

    assert captured["url"].endswith("/ingest-tokens/tok%201%2F..%2Fx")


def test_server_rejection_surfaces_reason(tmp_path):
    project = _project(tmp_path)
    save_bubble_state(project, "bub_test", "bkey_test")

    with patch.object(pooled, "request",
                      return_value=_Resp(400, {"error": "topic must use source/type form"})):
        with pytest.raises(it.IngestTokenError, match="source/type"):
            it.create_token("nope", project_path=project)


def test_server_rejection_preserves_plain_text_error(tmp_path):
    project = _project(tmp_path)
    save_bubble_state(project, "bub_test", "bkey_test")

    with patch.object(pooled, "request",
                      return_value=_Resp(502, ValueError("not JSON"),
                                         text="bad gateway")):
        with pytest.raises(it.IngestTokenError, match="bad gateway"):
            it.create_token("alert/firing", project_path=project)
