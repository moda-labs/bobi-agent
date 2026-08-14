"""Shared HTTP client with connection pooling.

All framework code that makes outbound HTTP requests should use this
module instead of raw ``urllib.request``.  The pooled ``httpx.Client``
reuses TCP connections across calls to the same host, avoiding
ephemeral port exhaustion on macOS where the default range is only
~16K ports and TIME_WAIT sockets linger for 60 seconds.

Thread safety: ``httpx.Client`` is thread-safe.  The module-level
client is lazily created on first use and shared across all threads.
"""

from __future__ import annotations

import logging
import threading

import httpx

log = logging.getLogger(__name__)

_client: httpx.Client | None = None
_lock = threading.Lock()

# Bounded concurrency prevents a burst of monitors from opening
# hundreds of sockets simultaneously.
_LIMITS = httpx.Limits(
    max_connections=20,
    max_keepalive_connections=10,
    keepalive_expiry=30,
)

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def client() -> httpx.Client:
    """Return the shared, long-lived ``httpx.Client``.

    The client pools TCP connections per host and respects HTTP
    keep-alive, so repeated calls to the same endpoint reuse the
    same socket instead of churning ephemeral ports.
    """
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            _client = httpx.Client(
                timeout=_TIMEOUT,
                limits=_LIMITS,
                follow_redirects=True,
                headers={"User-Agent": "bobi"},
            )
    return _client


def post(url: str, *, json: dict | None = None, content: bytes | None = None,
         headers: dict | None = None, timeout: float | None = None,
         follow_redirects: bool = True) -> httpx.Response:
    """POST with connection pooling and bounded concurrency.

    ``follow_redirects`` defaults to the shared client's ``True``, so every
    existing caller is unaffected. Pass ``False`` when the request carries a
    credential in a CUSTOM header: httpx strips only ``Authorization`` and
    ``Cookie`` on a cross-origin redirect, so a vendor key travels verbatim to
    whatever host the redirect names.
    """
    return request("POST", url, json=json, content=content, headers=headers,
                   timeout=timeout, follow_redirects=follow_redirects)


def get(url: str, *, headers: dict | None = None,
        timeout: float | None = None) -> httpx.Response:
    """GET with connection pooling and bounded concurrency."""
    return request("GET", url, headers=headers, timeout=timeout)


def put(url: str, *, json: dict | None = None, content: bytes | None = None,
        headers: dict | None = None, timeout: float | None = None) -> httpx.Response:
    """PUT with connection pooling and bounded concurrency."""
    return request("PUT", url, json=json, content=content, headers=headers,
                   timeout=timeout)


def delete(url: str, *, headers: dict | None = None,
           timeout: float | None = None) -> httpx.Response:
    """DELETE with connection pooling and bounded concurrency."""
    return request("DELETE", url, headers=headers, timeout=timeout)


def request(method: str, url: str, *, json: dict | None = None,
            content: bytes | None = None, headers: dict | None = None,
            timeout: float | None = None,
            follow_redirects: bool = True) -> httpx.Response:
    """Generic request with connection pooling.

    The single place the optional kwargs are filtered — an argument left None
    is omitted so the shared client's own default applies. Every verb helper
    above delegates here; ``client().post(url, ...)`` is exactly
    ``client().request("POST", url, ...)`` per httpx.

    ``follow_redirects`` is threaded through rather than dropped: ``post``
    exposes it so a caller sending a credential in a CUSTOM header can refuse
    redirects, and collapsing that into this helper must not lose it.
    """
    kwargs: dict = {}
    if json is not None:
        kwargs["json"] = json
    if content is not None:
        kwargs["content"] = content
    if headers:
        kwargs["headers"] = headers
    if timeout is not None:
        kwargs["timeout"] = timeout
    if not follow_redirects:
        kwargs["follow_redirects"] = False
    return client().request(method, url, **kwargs)


def close() -> None:
    """Close the shared client and release all pooled connections.

    Called during graceful shutdown.  Safe to call multiple times or
    when the client was never created.
    """
    global _client
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:
                pass
            _client = None
