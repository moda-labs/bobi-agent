"""Bubble request signing — the Python side of the auth-v1 HMAC scheme.

Every authenticated request to the event server (generic publish + join
registration) carries `x-moda-*` headers with an HMAC-SHA256 over a canonical
string. This MUST stay byte-for-byte identical to the verifier in
``event-server/src/core.ts`` (``bubbleCanonicalString`` / ``verifyBubbleSignature``):

    canonical = f"{timestamp}\n{nonce}\n{METHOD}\n{path}\n{body}"

- ``timestamp`` is epoch SECONDS (integer string) — Slack's verifier uses
  seconds, and the server's ±300s replay window assumes it.
- ``nonce`` is per-request random; it is signed now so the wire format is
  forward-compatible with server-side replay dedup (deferred follow-up).
- ``path`` is the exact request path on the wire (pathname, plus query when
  present) — sign what you send, never a recomputed form.
- ``body`` is the exact transmitted bytes. Callers serialize ONCE with
  :func:`serialize_body` and send those bytes via httpx ``content=`` (never
  ``json=``, which re-serializes and would break the signature).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import httpx

from bobi import http as pooled

ALGO = "hmac-sha256"


def serialize_body(payload: dict) -> str:
    """Canonical JSON serialization shared by signer and request body.

    Compact + key-sorted so the bytes are deterministic. The server signs over
    the raw request bytes it receives, so the only requirement is that the
    client signs exactly what it transmits — this guarantees that.
    """
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def canonical_string(timestamp: str, nonce: str, method: str,
                     path: str, body: str) -> str:
    return f"{timestamp}\n{nonce}\n{method.upper()}\n{path}\n{body}"


def sign_headers(bubble_id: str, bubble_key: str, method: str,
                 path: str, body: str) -> dict[str, str]:
    """Build the `x-moda-*` signing headers for a request."""
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    msg = canonical_string(timestamp, nonce, method, path, body)
    sig = hmac.new(bubble_key.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "x-moda-bubble": bubble_id,
        "x-moda-algo": ALGO,
        "x-moda-timestamp": timestamp,
        "x-moda-nonce": nonce,
        "x-moda-signature": sig,
    }


def signed_request(base_url: str, method: str, path: str, payload: dict | None,
                   bubble_id: str, bubble_key: str, *, timeout: float,
                   extra_headers: dict[str, str] | None = None) -> httpx.Response:
    """Send one bubble-signed request to the event server.

    The single client-side transport for the scheme above: serializes
    ``payload`` ONCE with :func:`serialize_body` and signs the exact
    transmitted bytes and path (query string included). ``payload=None``
    sends no body (GET/DELETE) and signs the empty string. When ``bubble_key``
    is empty the request goes out unsigned (the ``/deployments`` mint flow).
    Transport and HTTP errors propagate; callers own the failure semantics
    (raise vs best-effort).
    """
    method = method.upper()
    # Fail loudly on combinations that would sign bytes the wire never
    # carries - the server would 403 with no hint at the real cause.
    if method not in ("GET", "POST", "DELETE"):
        raise ValueError(f"signed_request supports GET/POST/DELETE, got {method}")
    if method != "POST" and payload is not None:
        raise ValueError(
            f"a {method} body is never transmitted, so it cannot be signed")

    body = serialize_body(payload) if payload is not None else ""
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    if bubble_key:
        headers.update(sign_headers(bubble_id, bubble_key, method, path, body))
    return pooled.request(method, f"{base_url}{path}", content=body or None,
                          headers=headers, timeout=timeout)
