"""FastAPI dashboard — view layer for modastack."""

import asyncio
import json
import logging
import time
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
        "engineers": data.get_sessions(),
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


@app.get("/api/log")
async def api_log(limit: int = Query(50, ge=1, le=200), session: str = Query("moda-manager")):
    return {"turns": data.get_conversation_log(limit=limit, session=session)}


@app.post("/api/message")
async def api_send_message(request: Request):
    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        return {"ok": False, "error": "empty message"}
    from modastack.manager.session import inject, is_alive
    if not is_alive():
        return {"ok": False, "error": "manager not running"}
    inject(text)
    return {"ok": True}


@app.post("/api/consult")
async def api_consult(request: Request):
    body = await request.json()
    question = body.get("question", "").strip()
    timeout = body.get("timeout", 300)
    source = body.get("source", "engineer")
    correlation_id = body.get("correlation_id", "")

    if not question:
        return {"ok": False, "error": "empty question"}

    from modastack.manager.session import is_alive
    if not is_alive():
        return {"ok": False, "error": "manager not running"}

    result = await asyncio.to_thread(
        _do_consult, question, timeout, source, correlation_id
    )
    return result


def _do_consult(question, timeout, source, correlation_id):
    from modastack.manager.session import inject, read_last_response, last_inject_error

    _log_consultation(correlation_id, source, question)

    ok = inject(
        f"[CONSULTATION] {question}",
        timeout=timeout,
        wait_for_ready=timeout,
    )
    if not ok:
        return {"ok": False, "error": f"inject failed: {last_inject_error()}"}

    response = read_last_response() or ""
    return {"ok": True, "response": response, "correlation_id": correlation_id}


def _log_consultation(correlation_id: str, source: str, question: str):
    events_log = Path.home() / ".modastack" / "manager" / "events.jsonl"
    events_log.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "type": "consultation.request",
        "source": source,
        "data": {
            "correlation_id": correlation_id,
            "question": question[:500],
        },
    }
    with open(events_log, "a") as f:
        f.write(json.dumps(entry) + "\n")


@app.post("/api/engineers/{issue_id}/message")
async def api_send_engineer_message(issue_id: str, request: Request):
    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        return {"ok": False, "error": "empty message"}
    from modastack.subagent import inject_message
    if inject_message(issue_id, text):
        return {"ok": True}
    return {"ok": False, "error": "agent not running"}


@app.get("/api/workflow/{issue_id}")
async def api_workflow_progress(issue_id: str):
    progress = data.get_workflow_progress(issue_id)
    if not progress:
        return {"progress": None}
    return {"progress": progress}


def run_dashboard(port: int = 8095) -> None:
    import uvicorn

    log.info(f"Starting dashboard on http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
