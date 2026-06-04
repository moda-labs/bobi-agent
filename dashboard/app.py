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
    manager, engineers = await asyncio.gather(
        asyncio.to_thread(data.get_manager_status),
        asyncio.to_thread(data.get_sessions),
    )
    return {"manager": manager, "engineers": engineers}


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
async def api_log(limit: int = Query(50, ge=1, le=200), session: str = Query("")):
    if not session:
        session = data._get_manager_session_name()
    return {"turns": data.get_conversation_log(limit=limit, session=session)}


@app.get("/api/logs")
async def api_logs(limit: int = Query(200, ge=1, le=2000)):
    """Tail of the modastack process log (~/.modastack/modastack.log)."""
    lines = await asyncio.to_thread(data.read_modastack_log, limit)
    return {"lines": lines}


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


@app.post("/api/event")
async def api_post_event(request: Request):
    """Enqueue a synthetic event onto the same queue webhooks use.

    Used by out-of-band check processes (`modastack spawn --non-interactive
    --post-event ...`) to report findings without touching the manager's
    conversation — the drain loop routes it like any other event.
    """
    body = await request.json()
    etype = (body.get("type") or "").strip()
    if not etype:
        return {"ok": False, "error": "missing event type"}

    from modastack.manager.events.event_client import event_queue
    event = {
        "type": etype,
        "source": (body.get("source") or "monitor").strip(),
        "data": body.get("data") or {},
    }
    event_queue.put(event)
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
    from modastack.manager.session import inject_capture, last_inject_error

    _log_consultation(correlation_id, source, question)

    ok, response = inject_capture(
        f"[CONSULTATION] {question}",
        timeout=timeout,
        wait_for_ready=timeout,
    )
    if not ok:
        return {"ok": False, "error": f"inject failed: {last_inject_error()}"}

    return {"ok": True, "response": response or "", "correlation_id": correlation_id}


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

    # Direct injection only works if the engineer sub-agent is running in
    # this process (legacy fire-and-forget path). The supervised executor
    # runs phases to completion without a live inbox, so fall back to
    # relaying the feedback through the manager, who coordinates engineers.
    from modastack.subagent import inject_message
    if inject_message(issue_id, text):
        return {"ok": True, "delivery": "direct"}

    from modastack.manager.session import inject, is_alive
    if not is_alive():
        return {"ok": False, "error": "manager not running and no live engineer session"}

    title = ""
    entry = data.get_registry().get(f"eng-{issue_id.lower()}") or _engineer_entry(issue_id)
    if entry:
        title = entry.title
    relay = (
        f"[ENGINEER FEEDBACK] For engineer working on #{issue_id}"
        + (f" ({title})" if title else "")
        + f": {text}\n\nRelay this to the engineer or act on it as appropriate."
    )
    ok = await asyncio.to_thread(inject, relay, timeout=120, wait_for_ready=120)
    if ok:
        return {"ok": True, "delivery": "manager"}
    return {"ok": False, "error": "could not deliver — manager busy or unavailable"}


def _engineer_entry(issue_id: str):
    """Find the most recent registry entry for an issue across phases."""
    registry = data.get_registry()
    matches = [
        e for e in registry.get_by_role("engineer")
        if e.issue_id.lower() == issue_id.lower()
    ]
    if not matches:
        return None
    return max(matches, key=lambda e: e.last_activity)


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
