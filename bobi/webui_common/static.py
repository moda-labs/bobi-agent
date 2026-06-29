"""Shared index and static-file routes for Bobi's local web UIs."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response

from bobi.webui_common import resolve_static_asset

NO_STORE_HEADERS = {"Cache-Control": "no-store, max-age=0"}
MEDIA_TYPES = {
    ".css": "text/css",
    ".js": "text/javascript",
    ".svg": "image/svg+xml",
}


def serve_index(app: FastAPI, html_path: Path,
                substitutions: dict[str, str]) -> None:
    @app.get("/")
    def index() -> Response:
        html = html_path.read_text()
        for placeholder, value in substitutions.items():
            html = html.replace(placeholder, value)
        return Response(html, media_type="text/html", headers=NO_STORE_HEADERS)


def mount_static(app: FastAPI, static_dir: Path) -> None:
    @app.get("/static/{name:path}")
    def static_asset(name: str) -> Response:
        target = resolve_static_asset(static_dir, name)
        if target is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(
            target,
            media_type=MEDIA_TYPES.get(target.suffix, "text/plain"),
            headers=NO_STORE_HEADERS,
        )
