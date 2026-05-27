"""FastAPI dashboard app with interactive terminals and pause/resume."""

import asyncio
import fcntl
import json
import logging
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading
from pathlib import Path

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from modastack.tmux import TMUX, has_session, is_paused, pause_session, resume_session
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


# ---------------------------------------------------------------------------
# Pause / Resume
# ---------------------------------------------------------------------------

@app.post("/api/sessions/{name}/pause")
async def api_pause(name: str):
    pause_session(name)
    return {"ok": True, "paused": True}


@app.post("/api/sessions/{name}/resume")
async def api_resume(name: str):
    resume_session(name)
    return {"ok": True, "paused": False}


# ---------------------------------------------------------------------------
# WebSocket Terminal — PTY bridge to tmux attach
# ---------------------------------------------------------------------------

@app.websocket("/ws/terminal/{session_name}")
async def ws_terminal(ws: WebSocket, session_name: str):
    await ws.accept()

    if not has_session(session_name):
        await ws.send_bytes(f"\r\nSession '{session_name}' not found.\r\n".encode())
        await ws.close()
        return

    master_fd, slave_fd = pty.openpty()

    proc = subprocess.Popen(
        [TMUX, "attach-session", "-t", session_name],
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        preexec_fn=os.setsid,
    )
    os.close(slave_fd)

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def _reader():
        try:
            while True:
                r, _, _ = select.select([master_fd], [], [], 1.0)
                if r:
                    chunk = os.read(master_fd, 4096)
                    if not chunk:
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
                if proc.poll() is not None:
                    break
        except OSError:
            pass
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()

    async def _sender():
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                await ws.send_bytes(chunk)
        except (WebSocketDisconnect, RuntimeError):
            pass

    sender_task = asyncio.create_task(_sender())

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            text = msg.get("text")
            if text:
                try:
                    obj = json.loads(text)
                    if obj.get("type") == "resize":
                        winsize = struct.pack(
                            "HHHH", obj["rows"], obj["cols"], 0, 0
                        )
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                        continue
                except (json.JSONDecodeError, KeyError):
                    pass
                os.write(master_fd, text.encode())
            elif msg.get("bytes"):
                os.write(master_fd, msg["bytes"])
    except WebSocketDisconnect:
        pass
    finally:
        sender_task.cancel()
        try:
            os.kill(proc.pid, signal.SIGTERM)
            proc.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.kill(proc.pid, signal.SIGKILL)
                proc.wait(timeout=1)
            except Exception:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass


def run_dashboard(port: int = 8095) -> None:
    import uvicorn

    log.info(f"Starting dashboard on http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
