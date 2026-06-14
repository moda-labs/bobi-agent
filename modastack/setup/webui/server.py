"""The `bobbi setup` web server — FastAPI on 127.0.0.1, foreground.

Design (from the implementation handoff):
- **Deterministic routes are sync `def`** — FastAPI runs them in a thread
  pool, so blocking work (validate, install, preflight, Venn checks) never
  stalls the event loop.
- **Streaming routes are `async def`** returning SSE — the digestion turn
  and the Build pour stream tokens to the browser.
- **Security**: loopback bind only, a per-launch **nonce** every `/api`
  call must present, and a **Host guard** (mitigates DNS rebinding). The
  page is served with the nonce embedded; the browser echoes it back as a
  header. Secrets never enter the LLM loop — credential values arrive on a
  dedicated `/api/credential` POST and go straight to `.env`.

`build_app` is pure (state + project in, app out) so it's driven directly
by Starlette's TestClient with an injected fake `stream_fn` — no network,
no CLI. `serve()` is the socket→uvicorn foreground launcher.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import AsyncIterator

# Imported at module level (not inside build_app) so that, under
# `from __future__ import annotations`, FastAPI can resolve the string
# annotations on the route handlers against this module's globals.
from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, JSONResponse, Response,
                               StreamingResponse)

from modastack.setup.state import STAGE_ORDER, SetupState, Stage

STATIC_DIR = Path(__file__).parent / "static"
NONCE_HEADER = "x-bobbi-nonce"
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


# --- serialization -------------------------------------------------------

def serialize_state(state: SetupState) -> dict:
    """The wizard's view of the world for the UI — stage, spec, readiness,
    the advance blocker, and the terminal flags."""
    spec = state.spec
    return {
        "stage": state.stage.value,
        "stages": [s.value for s in STAGE_ORDER],
        "mode": state.mode,
        "team_name": state.team_name,
        "chat": state.chat,
        "spec": {
            "goal": spec.goal,
            "roles": spec.roles,
            "autonomous": spec.autonomous,
            "autonomous_confirmed": spec.autonomous_confirmed,
            "services": spec.services,
            "readiness": {s: spec.readiness_for(s).value
                          for s in ("goal", "roles", "autonomous", "services")},
        },
        "summary": state.summary,
        "messages": state.messages,
        "advance_blocker": state.advance_blocker(),
        "validated": state.validated,
        "installed": state.installed,
        "finished": state.finished,
    }


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# --- app -----------------------------------------------------------------

def build_app(state: SetupState, project: Path, *, nonce: str,
              model: str | None = None, stream_fn=None,
              on_finish=None):
    """Construct the FastAPI app. `stream_fn` overrides the LLM source
    (tests inject a fake); `on_finish` is called when setup completes."""
    app = FastAPI()
    app.state.stream_fn = stream_fn
    app.state.model = model

    # --- security middleware: Host guard + nonce on /api ---------------
    @app.middleware("http")
    async def _guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host and host not in _ALLOWED_HOSTS:
            return JSONResponse({"error": "host not allowed"}, status_code=403)
        if request.url.path.startswith("/api"):
            if request.headers.get(NONCE_HEADER) != nonce:
                return JSONResponse({"error": "bad or missing nonce"},
                                    status_code=403)
        return await call_next(request)

    # --- page ----------------------------------------------------------
    @app.get("/")
    def index() -> Response:
        html = (STATIC_DIR / "index.html").read_text()
        # The page bootstraps the nonce from a meta tag the JS reads back.
        html = html.replace("{{NONCE}}", nonce)
        return Response(html, media_type="text/html")

    # static assets (css/js) — no nonce needed, same-origin only
    @app.get("/static/{name}")
    def static_asset(name: str) -> Response:
        target = (STATIC_DIR / name).resolve()
        if not target.is_file() or STATIC_DIR.resolve() not in target.parents:
            return JSONResponse({"error": "not found"}, status_code=404)
        types = {".css": "text/css", ".js": "text/javascript",
                 ".svg": "image/svg+xml"}
        return FileResponse(target,
                            media_type=types.get(target.suffix, "text/plain"))

    # --- state (deterministic) -----------------------------------------
    @app.get("/api/state")
    def get_state() -> dict:
        return serialize_state(state)

    # --- conversation turn (streaming) ---------------------------------
    @app.post("/api/message")
    async def message(request: Request) -> StreamingResponse:
        body = await request.json()
        text = (body.get("text") or "").strip()

        async def gen() -> AsyncIterator[str]:
            if not text:
                yield _sse("error", {"message": "empty message"})
                return
            from modastack.setup import digestion
            from modastack.setup.actions import redact_secrets
            # Scrub secrets at the trust boundary and tell the user. digest_turn
            # re-redacts (idempotent) so it's safe regardless of caller.
            clean, redacted = redact_secrets(text)
            if redacted:
                yield _sse("redacted", {"count": redacted})
            try:
                async for chunk in digestion.digest_turn(
                        state, project, clean, model=app.state.model,
                        cwd=str(project), stream_fn=app.state.stream_fn):
                    yield _sse("delta", {"text": chunk})
            except Exception as e:  # surface, don't kill the stream silently
                yield _sse("error", {"message": str(e)})
            yield _sse("state", serialize_state(state))

        return StreamingResponse(gen(), media_type="text/event-stream")

    # --- advance / navigate (deterministic) ----------------------------
    @app.post("/api/advance")
    def advance(payload: dict) -> JSONResponse:
        try:
            to = Stage(payload.get("to", ""))
        except ValueError:
            return JSONResponse({"error": "unknown stage"}, status_code=400)
        reason = state.can_advance(to)
        if reason:
            return JSONResponse({"error": reason}, status_code=409)
        state.stage = to
        state.save(project)
        return JSONResponse(serialize_state(state))

    # --- connect cards (deterministic; may hit Venn → threadpooled) ----
    @app.get("/api/connect")
    def connect() -> dict:
        from modastack.setup import services
        connected = services.venn_connected_names(project)
        cards = services.cards_for(state.spec.services, project,
                                   connected=connected)
        return {"cards": cards, "catalog": services.catalog_cards()}

    # --- automate (suggester + commit) ---------------------------------
    @app.post("/api/automate/suggest")
    async def automate_suggest() -> dict:
        from modastack.setup import automate
        suggestions = await automate.suggest(
            state, model=app.state.model, cwd=str(project),
            stream_fn=app.state.stream_fn)
        return {"suggestions": suggestions}

    @app.post("/api/automate")
    def automate_commit(payload: dict) -> dict:
        # The user's picks become the autonomous slot; committing is an
        # explicit confirmation even when the list is empty ("nothing").
        behaviors = payload.get("behaviors")
        state.spec.autonomous = behaviors if isinstance(behaviors, list) else []
        state.spec.autonomous_confirmed = True
        state.spec.readiness["autonomous"] = "enough"
        state.save(project)
        return serialize_state(state)

    # --- chat (how you talk to the team) -------------------------------
    @app.post("/api/chat")
    def set_chat(payload: dict) -> JSONResponse:
        channel = payload.get("channel", "")
        if channel not in ("cli", "slack", "telegram"):
            return JSONResponse({"error": "channel must be cli, slack, or "
                                 "telegram"}, status_code=400)
        state.chat = channel
        state.save(project)
        return JSONResponse(serialize_state(state))

    @app.post("/api/credential")
    def credential(payload: dict) -> JSONResponse:
        from modastack.setup import actions
        value = payload.get("value", "")
        try:
            result = actions.save_credential(
                state, project, payload.get("var_name", ""),
                payload.get("service", ""), payload.get("instructions", ""),
                prompt_fn=lambda *_: value)
        except actions.ActionError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(result)

    # --- build pour (streaming) ----------------------------------------
    @app.post("/api/build")
    async def build() -> StreamingResponse:
        async def gen() -> AsyncIterator[str]:
            from modastack.setup import authoring
            try:
                async for event in authoring.author_pack(
                        state, project, model=app.state.model,
                        stream_fn=app.state.stream_fn):
                    yield _sse(event["type"], event)
            except Exception as e:
                yield _sse("error", {"message": str(e)})
            yield _sse("state", serialize_state(state))

        return StreamingResponse(gen(), media_type="text/event-stream")

    # --- review: browse / edit the authored pack source ----------------
    def _pack_dir() -> Path:
        return (project / "agents" / state.team_name).resolve()

    def _safe_target(rel: str) -> Path | None:
        pack = _pack_dir()
        target = (pack / rel).resolve()
        if target == pack or pack not in target.parents:
            return None
        return target

    @app.get("/api/files")
    def files() -> dict:
        pack = _pack_dir()
        if not pack.is_dir():
            return {"files": []}
        rels = sorted(p.relative_to(pack).as_posix()
                      for p in pack.rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts)
        return {"files": rels}

    @app.get("/api/file")
    def read_file(path: str) -> JSONResponse:
        target = _safe_target(path)
        if target is None or not target.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"path": path, "content": target.read_text()})

    @app.post("/api/file")
    def write_file(payload: dict) -> JSONResponse:
        target = _safe_target(payload.get("path", ""))
        if target is None:
            return JSONResponse({"error": "path outside the pack"},
                                status_code=400)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload.get("content", ""))
        # An edit invalidates the freeze — Install must re-validate.
        state.validated = False
        state.validated_hash = ""
        state.save(project)
        return JSONResponse({"path": payload.get("path"),
                             "state": serialize_state(state)})

    # --- validate / install / preflight (deterministic) ----------------
    @app.post("/api/validate")
    def validate() -> dict:
        from modastack.setup import actions
        result = actions.validate_team(state, project)
        return {**result, "state": serialize_state(state)}

    @app.post("/api/install")
    def install() -> JSONResponse:
        from modastack.setup import actions
        try:
            result = actions.install_team(state, project)
        except actions.ActionError as e:
            return JSONResponse({"error": str(e),
                                 "state": serialize_state(state)},
                                status_code=409)
        return JSONResponse({**result, "state": serialize_state(state)})

    @app.post("/api/preflight")
    def preflight() -> dict:
        from modastack.setup import actions
        result = actions.run_preflight(project)
        return {"ok": result.ok, "report": result.format()}

    # --- finish --------------------------------------------------------
    @app.post("/api/finish")
    def finish() -> dict:
        state.finished = True
        state.save(project)
        if on_finish:
            on_finish()
        return serialize_state(state)

    return app


# --- foreground launcher -------------------------------------------------

def serve(project: Path, *, model: str | None = None,
          resume: bool = False, open_browser: bool = True) -> int:
    """Run the setup web UI in the foreground until setup finishes or the
    user interrupts. Binds 127.0.0.1:0, hands the socket to uvicorn."""
    import secrets
    import threading
    import webbrowser

    import uvicorn

    state = None
    if resume:
        state = SetupState.load(project)
        if state is None or state.finished:
            print("No setup in progress to resume — run `bobbi setup`.")
            return 1
    if state is None:
        SetupState.clear(project)
        state = SetupState()

    nonce = secrets.token_urlsafe(24)

    # Bind our own loopback socket first so we know the port before serving.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/?n={nonce}"

    server_holder: dict = {}

    def _on_finish() -> None:
        srv = server_holder.get("server")
        if srv is not None:
            srv.should_exit = True

    app = build_app(state, project, nonce=nonce, model=model,
                    on_finish=_on_finish)
    config = uvicorn.Config(app, log_level="warning")
    server = uvicorn.Server(config)
    server_holder["server"] = server

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    print(f"\n  bobbi setup is running at {url}\n  (Ctrl-C to stop)\n")

    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()

    if state.finished:
        SetupState.clear(project)
        return 0
    return 0
