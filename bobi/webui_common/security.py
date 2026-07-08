"""Shared security middleware for Bobi's local web UIs."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

WEBUI_TOKEN_HEADER = "x-bobi-webui-token"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _host_without_port(raw: str) -> str:
    if raw.startswith("["):
        return raw.split("]", 1)[0] + "]"
    return raw.rsplit(":", 1)[0]


def install_security(
    app: FastAPI,
    *,
    secret: str,
    header_name: str = WEBUI_TOKEN_HEADER,
    error_message: str = "bad or missing token",
) -> None:
    """Install loopback Host protection and `/api` secret-header checks."""

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        host = _host_without_port(request.headers.get("host") or "")
        if host and host not in ALLOWED_HOSTS:
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
