"""Bubble signing — unit tests + cross-language parity vector.

The HMAC canonicalization here MUST stay byte-for-byte identical to the
verifier in event-server/src/core.ts. The GOLDEN vector below is asserted in
BOTH this file and event-server/test/core.spec.ts ("bubble signature parity
vector") — if either side drifts (separator handling, key sort, METHOD casing,
path/query inclusion), one of the two suites fails instead of producing silent
403s only visible against a live server.
"""

import hashlib
import hmac

from bobi.events.signing import (
    ALGO,
    canonical_string,
    serialize_body,
    sign_headers,
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
