"""Bubble signing — unit tests + cross-language parity vector.

The HMAC canonicalization here MUST stay byte-for-byte identical to the
verifier in event-server/core/src/core.ts. The GOLDEN vector below is asserted in
BOTH this file and event-server/test/core.spec.ts ("bubble signature parity
vector") — if either side drifts (separator handling, key sort, METHOD casing,
path/query inclusion), one of the two suites fails instead of producing silent
403s only visible against a live server.
"""

import hashlib
import hmac
from unittest.mock import patch

import pytest

from bobi import http as pooled
from bobi.config import save_bubble_state
from bobi.events.signing import (
    ALGO,
    SignedJSONRequestError,
    canonical_string,
    checked_signed_json_request,
    serialize_body,
    sign_headers,
    signed_request,
)

# GOLDEN VECTOR — keep identical to core.spec.ts.
GOLDEN_KEY = "bkey_golden"
GOLDEN_TS = "1700000000"
GOLDEN_NONCE = "abc123"
GOLDEN_METHOD = "POST"
GOLDEN_PATH = "/events/inbox/manager"
GOLDEN_PAYLOAD = {"source": "inbox", "payload": {"text": "hi", "z": 1, "a": 2}}
GOLDEN_BODY = '{"payload":{"a":2,"text":"hi","z":1},"source":"inbox"}'
GOLDEN_CANON = (
    '1700000000\nabc123\nPOST\n/events/inbox/manager\n'
    '{"payload":{"a":2,"text":"hi","z":1},"source":"inbox"}'
)
GOLDEN_SIG = "81915dcbcceb5cfa052c2a17557962413517be26f0094dd62fe355cd6d0126d7"


def test_serialize_body_is_compact_and_key_sorted():
    assert serialize_body(GOLDEN_PAYLOAD) == GOLDEN_BODY


def test_canonical_string_matches_golden():
    assert canonical_string(GOLDEN_TS, GOLDEN_NONCE, GOLDEN_METHOD,
                            GOLDEN_PATH, GOLDEN_BODY) == GOLDEN_CANON


def test_golden_signature_matches():
    sig = hmac.new(GOLDEN_KEY.encode(), GOLDEN_CANON.encode(),
                   hashlib.sha256).hexdigest()
    assert sig == GOLDEN_SIG


def test_sign_headers_shape_and_signature():
    body = serialize_body(GOLDEN_PAYLOAD)
    headers = sign_headers("bub_x", GOLDEN_KEY, "POST", GOLDEN_PATH, body)
    assert headers["x-moda-bubble"] == "bub_x"
    assert headers["x-moda-algo"] == ALGO == "hmac-sha256"
    assert headers["x-moda-timestamp"].isdigit()      # epoch seconds
    assert len(headers["x-moda-nonce"]) >= 8
    # Recompute over the headers' own timestamp/nonce → must verify.
    canon = canonical_string(headers["x-moda-timestamp"], headers["x-moda-nonce"],
                             "POST", GOLDEN_PATH, body)
    expected = hmac.new(GOLDEN_KEY.encode(), canon.encode(),
                        hashlib.sha256).hexdigest()
    assert headers["x-moda-signature"] == expected


def test_method_is_uppercased_in_canonical():
    assert canonical_string("1", "n", "post", "/p", "b") == \
        canonical_string("1", "n", "POST", "/p", "b")


# --- signed_request: the shared transport every signed client path uses ---

class _Resp:
    status_code = 200


def _capture(captured):
    """Record any pooled.request call and return a canned 200."""
    def _call(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return _Resp()
    return _call


def test_signed_request_posts_canonical_bytes_with_verifiable_signature():
    captured: dict = {}
    with patch.object(pooled, "request", side_effect=_capture(captured)):
        resp = signed_request("http://es:8080", "POST", GOLDEN_PATH,
                              GOLDEN_PAYLOAD, "bub_x", GOLDEN_KEY, timeout=7.0)

    assert resp.status_code == 200
    assert captured["method"] == "POST"
    assert captured["url"] == f"http://es:8080{GOLDEN_PATH}"
    assert captured["content"] == GOLDEN_BODY  # serialized ONCE, canonical form
    assert captured["timeout"] == 7.0
    headers = captured["headers"]
    assert headers["Content-Type"] == "application/json"
    # The signature must verify over the exact transmitted bytes.
    canon = canonical_string(headers["x-moda-timestamp"],
                             headers["x-moda-nonce"], "POST", GOLDEN_PATH,
                             captured["content"])
    expected = hmac.new(GOLDEN_KEY.encode(), canon.encode(),
                        hashlib.sha256).hexdigest()
    assert headers["x-moda-signature"] == expected


def test_signed_request_get_signs_empty_body_and_query_path():
    path = "/channels/history?conversation=slack%3AT1%3Achannel%3AC1&limit=5"
    captured: dict = {}
    with patch.object(pooled, "request", side_effect=_capture(captured)):
        signed_request("http://es:8080", "GET", path, None,
                       "bub_x", GOLDEN_KEY, timeout=30.0)

    assert captured["method"] == "GET"
    assert captured["url"] == f"http://es:8080{path}"
    assert captured["content"] is None  # a GET never carries a body
    headers = captured["headers"]
    # GET signs the empty body and the exact path including the query string.
    canon = canonical_string(headers["x-moda-timestamp"],
                             headers["x-moda-nonce"], "GET", path, "")
    expected = hmac.new(GOLDEN_KEY.encode(), canon.encode(),
                        hashlib.sha256).hexdigest()
    assert headers["x-moda-signature"] == expected


def test_signed_request_unsigned_when_no_bubble_key():
    """The /deployments MINT flow sends unsigned; no x-moda-* headers leak."""
    captured: dict = {}
    with patch.object(pooled, "request", side_effect=_capture(captured)):
        signed_request("http://es:8080", "POST", "/deployments",
                       {"name": "s"}, "", "", timeout=5.0)

    assert not any(h.startswith("x-moda") for h in captured["headers"])
    assert captured["headers"]["Content-Type"] == "application/json"


def test_signed_request_merges_extra_headers():
    captured: dict = {}
    with patch.object(pooled, "request", side_effect=_capture(captured)):
        signed_request("http://es:8080", "POST", "/__test/resource-grants",
                       {"grants": []}, "bub_x", GOLDEN_KEY, timeout=5.0,
                       extra_headers={"x-moda-test-secret": "shh"})

    headers = captured["headers"]
    assert headers["x-moda-test-secret"] == "shh"
    assert "x-moda-signature" in headers  # extra headers never displace signing


def test_signed_request_delete_signs_empty_body():
    path = "/ingest-tokens/tok1"
    captured: dict = {}
    with patch.object(pooled, "request", side_effect=_capture(captured)):
        signed_request("http://es:8080", "DELETE", path, None,
                       "bub_x", GOLDEN_KEY, timeout=10.0)

    assert captured["method"] == "DELETE"
    assert captured["url"] == f"http://es:8080{path}"
    assert captured["content"] is None  # a DELETE never carries a body
    headers = captured["headers"]
    canon = canonical_string(headers["x-moda-timestamp"],
                             headers["x-moda-nonce"], "DELETE", path, "")
    expected = hmac.new(GOLDEN_KEY.encode(), canon.encode(),
                        hashlib.sha256).hexdigest()
    assert headers["x-moda-signature"] == expected


def test_signed_request_rejects_unsignable_combinations():
    """Combinations whose signed bytes would never reach the wire fail loudly
    at the caller instead of as an opaque server-side 403."""
    with pytest.raises(ValueError, match="GET/POST/DELETE"):
        signed_request("http://es:8080", "PUT", "/deployments/d1", None,
                       "bub_x", GOLDEN_KEY, timeout=5.0)
    with pytest.raises(ValueError, match="cannot be signed"):
        signed_request("http://es:8080", "GET", "/channels/history",
                       {"limit": 5}, "bub_x", GOLDEN_KEY, timeout=5.0)
    with pytest.raises(ValueError, match="cannot be signed"):
        signed_request("http://es:8080", "DELETE", "/ingest-tokens/tok1",
                       {"id": "tok1"}, "bub_x", GOLDEN_KEY, timeout=5.0)


# --- checked_signed_json_request: shared signed JSON response envelope ---

class _JSONResp:
    def __init__(self, status_code, body, *, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _project_with_bubble(tmp_path):
    (tmp_path / ".bobi").mkdir()
    save_bubble_state(tmp_path, "bub_x", GOLDEN_KEY)
    return tmp_path


def test_checked_signed_json_request_returns_dict(tmp_path):
    project = _project_with_bubble(tmp_path)

    with patch.object(pooled, "request",
                      return_value=_JSONResp(200, {"ok": True, "value": 1})):
        data = checked_signed_json_request(
            project, "GET", "/channels/history", None, timeout=10.0)

    assert data == {"ok": True, "value": 1}


def test_checked_signed_json_request_fails_fast_without_bubble(tmp_path):
    (tmp_path / ".bobi").mkdir()
    calls = []

    def _record(*args, **kwargs):
        calls.append((args, kwargs))
        return _JSONResp(200, {"ok": True})

    with patch.object(pooled, "request", side_effect=_record):
        with pytest.raises(SignedJSONRequestError) as excinfo:
            checked_signed_json_request(
                tmp_path, "GET", "/channels/history", None, timeout=10.0)

    assert excinfo.value.kind == "missing_credentials"
    assert calls == []


def test_checked_signed_json_request_wraps_unreachable(tmp_path):
    project = _project_with_bubble(tmp_path)

    with patch.object(pooled, "request", side_effect=OSError("offline")):
        with pytest.raises(SignedJSONRequestError) as excinfo:
            checked_signed_json_request(
                project, "GET", "/channels/history", None, timeout=10.0)

    assert excinfo.value.kind == "unreachable"
    assert "offline" in str(excinfo.value)


def test_checked_signed_json_request_rejects_http_error_with_server_detail(tmp_path):
    project = _project_with_bubble(tmp_path)

    with patch.object(pooled, "request",
                      return_value=_JSONResp(
                          400, {"error": "bad topic", "detail": "use source/type"})):
        with pytest.raises(SignedJSONRequestError) as excinfo:
            checked_signed_json_request(
                project, "POST", "/ingest-tokens", {"topic": "x"}, timeout=10.0)

    assert excinfo.value.kind == "rejected"
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "bad topic: use source/type"


def test_checked_signed_json_request_preserves_non_json_error_text(tmp_path):
    project = _project_with_bubble(tmp_path)

    with patch.object(pooled, "request",
                      return_value=_JSONResp(502, ValueError("not JSON"),
                                             text="bad gateway")):
        with pytest.raises(SignedJSONRequestError) as excinfo:
            checked_signed_json_request(
                project, "POST", "/ingest-tokens", {"topic": "x"}, timeout=10.0)

    assert excinfo.value.kind == "rejected"
    assert excinfo.value.status_code == 502
    assert excinfo.value.detail == ""
    assert excinfo.value.response_text == "bad gateway"


def test_checked_signed_json_request_rejects_non_json_success(tmp_path):
    project = _project_with_bubble(tmp_path)

    with patch.object(pooled, "request",
                      return_value=_JSONResp(200, ValueError("not JSON"),
                                             text="not json")):
        with pytest.raises(SignedJSONRequestError) as excinfo:
            checked_signed_json_request(
                project, "POST", "/channels/send", {}, timeout=10.0)

    assert excinfo.value.kind == "invalid_response"
    assert "JSON object" in excinfo.value.detail
    assert excinfo.value.response_text == "not json"


def test_checked_signed_json_request_treats_non_object_http_error_as_rejection(tmp_path):
    project = _project_with_bubble(tmp_path)

    with patch.object(pooled, "request",
                      return_value=_JSONResp(500, ["server exploded"],
                                             text='["server exploded"]')):
        with pytest.raises(SignedJSONRequestError) as excinfo:
            checked_signed_json_request(
                project, "POST", "/channels/send", {}, timeout=10.0)

    assert excinfo.value.kind == "rejected"
    assert excinfo.value.status_code == 500
    assert excinfo.value.response_text == '["server exploded"]'


def test_checked_signed_json_request_rejects_ok_false_envelope(tmp_path):
    project = _project_with_bubble(tmp_path)

    with patch.object(pooled, "request",
                      return_value=_JSONResp(200, {"ok": False, "error": "no token"})):
        with pytest.raises(SignedJSONRequestError) as excinfo:
            checked_signed_json_request(
                project, "POST", "/channels/send", {}, timeout=10.0)

    assert excinfo.value.kind == "rejected"
    assert excinfo.value.status_code == 200
    assert excinfo.value.detail == "no token"


def test_checked_signed_json_request_rejects_non_dict_json(tmp_path):
    project = _project_with_bubble(tmp_path)

    with patch.object(pooled, "request",
                      return_value=_JSONResp(200, [{"ok": True}], text='[{"ok":true}]')):
        with pytest.raises(SignedJSONRequestError) as excinfo:
            checked_signed_json_request(
                project, "GET", "/channels/history", None, timeout=10.0)

    assert excinfo.value.kind == "invalid_response"
    assert "JSON object" in excinfo.value.detail
