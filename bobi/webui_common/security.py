"""Shared security middleware for Bobi's local web UIs."""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

WEBUI_TOKEN_HEADER = "x-bobi-webui-token"
LEGACY_SETUP_TOKEN_HEADER = "x-bobi-nonce"
LEGACY_AGENTUI_TOKEN_HEADER = "x-bobi-ui-token"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _host_without_port(raw: str) -> str:
    if raw.startswith("["):
        return raw.split("]", 1)[0] + "]"
    return raw.rsplit(":", 1)[0]


def _accepted_headers(header_name: str,
                      legacy_header_names: Iterable[str]) -> tuple[str, ...]:
    seen = []
    for name in (header_name, *legacy_header_names):
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def install_security(
    app: FastAPI,
    *,
    secret: str,
    header_name: str = WEBUI_TOKEN_HEADER,
    legacy_header_names: Iterable[str] = (),
    error_message: str = "bad or missing token",
) -> None:
    """Install loopback Host protection and `/api` secret-header checks."""
    header_names = _accepted_headers(header_name, legacy_header_names)

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        host = _host_without_port(request.headers.get("host") or "")
        if host and host not in ALLOWED_HOSTS:
            return JSONResponse({"error": "host not allowed"}, status_code=403)
        if request.url.path.startswith("/api"):
            if not any(request.headers.get(name) == secret for name in header_names):
                return JSONResponse({"error": error_message}, status_code=403)
        return await call_next(request)
