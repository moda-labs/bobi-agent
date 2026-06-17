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
                headers={"User-Agent": "modastack"},
            )
    return _client


def post(url: str, *, json: dict | None = None, content: bytes | None = None,
         headers: dict | None = None, timeout: float | None = None) -> httpx.Response:
    """POST with connection pooling and bounded concurrency."""
    kwargs: dict = {}
    if json is not None:
        kwargs["json"] = json
    if content is not None:
        kwargs["content"] = content
    if headers:
        kwargs["headers"] = headers
    if timeout is not None:
        kwargs["timeout"] = timeout
    return client().post(url, **kwargs)


def get(url: str, *, headers: dict | None = None,
        timeout: float | None = None) -> httpx.Response:
    """GET with connection pooling and bounded concurrency."""
    kwargs: dict = {}
    if headers:
        kwargs["headers"] = headers
    if timeout is not None:
        kwargs["timeout"] = timeout
    return client().get(url, **kwargs)


def put(url: str, *, json: dict | None = None, content: bytes | None = None,
        headers: dict | None = None, timeout: float | None = None) -> httpx.Response:
    """PUT with connection pooling and bounded concurrency."""
    kwargs: dict = {}
    if json is not None:
        kwargs["json"] = json
    if content is not None:
        kwargs["content"] = content
    if headers:
        kwargs["headers"] = headers
    if timeout is not None:
        kwargs["timeout"] = timeout
    return client().put(url, **kwargs)


def delete(url: str, *, headers: dict | None = None,
           timeout: float | None = None) -> httpx.Response:
    """DELETE with connection pooling and bounded concurrency."""
    kwargs: dict = {}
    if headers:
        kwargs["headers"] = headers
    if timeout is not None:
        kwargs["timeout"] = timeout
    return client().delete(url, **kwargs)


def request(method: str, url: str, *, json: dict | None = None,
            content: bytes | None = None, headers: dict | None = None,
            timeout: float | None = None) -> httpx.Response:
    """Generic request with connection pooling."""
    kwargs: dict = {}
    if json is not None:
        kwargs["json"] = json
    if content is not None:
        kwargs["content"] = content
    if headers:
        kwargs["headers"] = headers
    if timeout is not None:
        kwargs["timeout"] = timeout
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
