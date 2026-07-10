"""Shared security middleware for Bobi's local web UIs."""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

WEBUI_TOKEN_HEADER = "x-bobi-webui-token"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
# A hosted deployment (the fleet admin webapp - private bobi-deploy) serves the
# same `build_app` behind an auth gate + reverse proxy, so its inbound `Host` is
# a public domain the loopback default rejects. The operator names that host(s)
# here, comma-separated. Unset in the local product, so local behavior (loopback
# only) is unchanged. The DNS-rebinding defense stays meaningful for the hosted
# origin too - it just admits the configured public host(s) instead of nothing.
ALLOWED_HOSTS_ENV = "BOBI_WEBUI_ALLOWED_HOSTS"


def _host_without_port(raw: str) -> str:
    if raw.startswith("["):
        return raw.split("]", 1)[0] + "]"
    return raw.rsplit(":", 1)[0]


def _configured_allowed_hosts() -> set[str]:
    """Loopback defaults plus any bare hostnames in ``BOBI_WEBUI_ALLOWED_HOSTS``.

    Hostnames are case-insensitive, so entries are lowercased (the guard
    lowercases the request Host to match); use bare hostnames without a port or
    scheme (the request Host is port-stripped before comparison)."""
    hosts = {h.lower() for h in ALLOWED_HOSTS}
    raw = os.environ.get(ALLOWED_HOSTS_ENV, "")
    hosts.update(h.strip().lower() for h in raw.split(",") if h.strip())
    return hosts


def install_security(
    app: FastAPI,
    *,
    secret: str,
    header_name: str = WEBUI_TOKEN_HEADER,
    error_message: str = "bad or missing token",
    allowed_hosts: set[str] | None = None,
) -> None:
    """Install Host protection and `/api` secret-header checks.

    Hosts default to loopback plus ``BOBI_WEBUI_ALLOWED_HOSTS`` (see module
    docstring on that env var); pass ``allowed_hosts`` to set them explicitly
    (direct/test callers). Read once at install time - the env is fixed for the
    process lifetime."""
    source = allowed_hosts if allowed_hosts is not None else _configured_allowed_hosts()
    allowed = {h.lower() for h in source}

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        host = _host_without_port(request.headers.get("host") or "")
        if host and host.lower() not in allowed:
            return JSONResponse({"error": "host not allowed"}, status_code=403)
        # Strip the mount prefix so the check also fires when this app is
        # mounted as a sub-app (e.g. setup under /setup in the unified app):
        # there url.path is the FULL outer path and a bare startswith("/api")
        # would silently skip the token check.
        path = request.url.path
        root = request.scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root):]
        if path.startswith("/api"):
            if request.headers.get(header_name) != secret:
                return JSONResponse({"error": error_message}, status_code=403)
        return await call_next(request)
