"""FastAPI dashboard app."""

import logging
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import data

log = logging.getLogger(__name__)

app = FastAPI(title="modastack dashboard")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/status")
async def api_status():
    return {
        "manager": data.get_manager_status(),
        "sessions": data.get_sessions(),
    }


@app.get("/api/events")
async def api_events(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    source: str | None = Query(None),
    type: str | None = Query(None),
):
    events, total = data.read_events(
        limit=limit, offset=offset, source=source, type_filter=type
    )
    return {"events": events, "total": total}


@app.get("/api/decisions")
async def api_decisions(limit: int = Query(10, ge=1, le=100)):
    return {"decisions": data.read_decisions(limit=limit)}


@app.get("/api/sources")
async def api_sources():
    return {"sources": data.get_event_sources()}


def run_dashboard(port: int = 8095) -> None:
    import uvicorn

    log.info(f"Starting dashboard on http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
