"""The `bobi agent <name> ui` web server — a minimal dashboard for a *running* team.

Shows one card per active agent session (manager + workers) and lets you open
a chat panel to talk to any of them directly. The chat is **blocking
request/response**: a message is delivered via :func:`bobi.inbox.deliver`
with ``wait=True`` and the agent's full reply comes back as one block — there's
no token streaming surface to expose.

Two run modes share one FastAPI app (only the bind + token source differ):

- **local** (`bobi agent <name> ui`): binds ``127.0.0.1:0`` in the foreground, mints a
  per-launch token, opens a browser. The operator's machine is the trust
  boundary, exactly like `bobi setup`.
- **container** (:func:`start_in_thread`, started by the manager when
  ``BOBI_UI`` is set): binds the Fly private 6PN address (``::``) in a
  daemon thread so an operator reaches it with ``fly proxy 8080:8080 -a <app>``.
  The box has no public ingress (the generated fly.toml has no ``[http_service]``),
  so 6PN reachability is the boundary; the token is defense-in-depth.

In both modes the browser talks to *localhost* (loopback locally, or the
forwarded port through `fly proxy`), so the same loopback Host guard + token
check from the setup server applies unchanged.

`build_app` is pure (project + injected `registry_fn`/`deliver_fn` in, app out)
so Starlette's TestClient drives it with no live team and no event server.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from bobi.chat_history import (
    CHAT_HISTORY_LIMIT,
    append_chat,
    read_chat,
    read_transcript_messages,
    safe_name as _safe_name,
)
from bobi.webui_common.launcher import serve_container, serve_local
from bobi.webui_common.security import (
    LEGACY_SETUP_TOKEN_HEADER,
    WEBUI_TOKEN_HEADER,
    install_security,
)
from bobi.webui_common.static import mount_static, serve_index

STATIC_DIR = Path(__file__).parent / "static"
TOKEN_HEADER = "x-bobi-ui-token"
DEFAULT_CHAT_TIMEOUT = 300


# --- serialization -------------------------------------------------------

def serialize_card(entry, *, manager_name: str = "") -> dict:
    """An agent session's view for a card. Mirrors the fields a `SessionEntry`
    (`bobi.sdk.SessionEntry`) exposes; `is_manager` flags the entry-point
    session so the UI can badge it."""
    return {
        "name": entry.name,
        "role": entry.role,
        "title": entry.title,
        "phase": entry.phase,
        "project": entry.project,
        "status": entry.status,
        "model": entry.model,
        "provider": entry.provider,
        "total_cost_usd": round(entry.total_cost_usd or 0.0, 4),
        "run_key": entry.run_key,
        "started_at": entry.started_at,
        "last_activity": entry.last_activity,
        "is_manager": bool(manager_name) and entry.name == manager_name,
    }


def ordered_agents(entries, *, manager_name: str = "") -> list:
    return sorted(entries, key=lambda e: (0 if manager_name and e.name == manager_name else 1,
                                         e.started_at or 0))


def _resolve_manager_name(project: Path) -> str:
    """Best-effort entry-point session name matching `cli._manager_session_name`.

    Never raises; an empty string just
    means no card gets the manager badge."""
    try:
        from bobi import paths
        from bobi.config import Config
        role = Config.load(project).entry_point or "manager"
        agent_name = paths.agent_name_for_root(project)
    except Exception:
        role = "manager"
        agent_name = project.name
    return f"bobi-{agent_name}-{role}"


# --- app -----------------------------------------------------------------

def build_app(project: Path, *, token: str, registry_fn=None, deliver_fn=None,
              manager_name: str | None = None,
              chat_timeout: int = DEFAULT_CHAT_TIMEOUT) -> FastAPI:
    """Construct the FastAPI app.

    `registry_fn` returns the list of active `SessionEntry` (defaults to the
    on-disk registry); `deliver_fn` sends a message and blocks for the reply
    (defaults to `inbox.deliver`). Tests inject both so no live team is needed.
    """
    app = FastAPI()

    def _active():
        if registry_fn is not None:
            return registry_fn()
        from bobi.sdk import get_registry
        return get_registry().list_active()

    def _deliver(*args, **kwargs):
        if deliver_fn is not None:
            return deliver_fn(*args, **kwargs)
        from bobi.inbox import deliver
        return deliver(*args, **kwargs)

    mgr = manager_name if manager_name is not None else _resolve_manager_name(project)

    install_security(
        app,
        secret=token,
        header_name=WEBUI_TOKEN_HEADER,
        legacy_header_names=(TOKEN_HEADER, LEGACY_SETUP_TOKEN_HEADER),
        error_message="bad or missing token",
    )
    serve_index(app, STATIC_DIR / "index.html", {"{{TOKEN}}": token})
    mount_static(app, STATIC_DIR)

    # --- liveness ------------------------------------------------------
    @app.get("/api/ping")
    def ping() -> dict:
        return {"ok": True}

    # --- agents (the cards) --------------------------------------------
    @app.get("/api/agents")
    def agents() -> dict:
        return {"agents": [serialize_card(e, manager_name=mgr)
                           for e in ordered_agents(_active(), manager_name=mgr)]}

    @app.get("/api/agents/{name}")
    def agent_detail(name: str) -> JSONResponse:
        for e in _active():
            if e.name == name:
                return JSONResponse(serialize_card(e, manager_name=mgr))
        return JSONResponse({"error": "unknown agent"}, status_code=404)

    @app.get("/api/agents/{name}/messages")
    def agent_messages(name: str) -> JSONResponse:
        if not _safe_name(name):
            return JSONResponse({"error": "bad name"}, status_code=404)
        entry = next((e for e in _active() if e.name == name), None)
        if not entry:
            return JSONResponse({"error": "unknown agent"}, status_code=404)

        session_id = entry.session_id
        if not session_id:
            try:
                from bobi.sdk import load_session_id
                session_id = load_session_id(name)
            except Exception:
                session_id = ""

        messages = read_transcript_messages(session_id)
        if not messages:
            messages = read_chat(project, name)
        return JSONResponse({"messages": messages})

    # --- chat (blocking request/response) ------------------------------
    # Sync `def` on purpose: FastAPI runs it in a threadpool, so the blocking
    # deliver(wait=True) never stalls the event loop or /api/agents polling.
    @app.post("/api/chat")
    def chat(payload: dict) -> JSONResponse:
        agent = (payload.get("agent") or "").strip()
        text = (payload.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)
        # Only address sessions that are actually live — never let the UI fan a
        # message at an arbitrary name.
        if agent not in {e.name for e in _active()}:
            return JSONResponse({"error": "unknown agent"}, status_code=404)
        ok, reply = _deliver(agent, text, sender="web-ui", wait=True,
                             timeout=chat_timeout)
        if ok:
            # Persist the exchange so the transcript survives refresh/switch.
            append_chat(project, agent, "user", text)
            append_chat(project, agent, "agent", reply)
            return JSONResponse({"reply": reply})
        # deliver returns a descriptive failure (session stopped, dead, timeout).
        return JSONResponse({"error": reply}, status_code=502)

    return app


# --- launchers -----------------------------------------------------------

def serve(project: Path, *, mode: str = "local", open_browser: bool = True,
          chat_timeout: int = DEFAULT_CHAT_TIMEOUT) -> int:
    """Run the UI in the foreground (the `bobi agent <name> ui` command). Binds
    ``127.0.0.1:0``, mints a per-launch token, hands the socket to uvicorn."""
    from bobi import paths

    try:
        agent_name = paths.agent_name_for_root(project)
    except Exception:
        agent_name = "<name>"
    return serve_local(
        lambda token: build_app(project, token=token,
                                chat_timeout=chat_timeout),
        open_browser=open_browser,
        label=f"bobi agent {agent_name} ui",
        announce=lambda url:
            f"\n  bobi agent {agent_name} ui is running at {url}\n"
            "  (Ctrl-C to stop)\n",
    )


def start_in_thread(project: Path, *, state_dir: Path,
                    chat_timeout: int = DEFAULT_CHAT_TIMEOUT) -> int:
    """Start the UI in a daemon thread inside the running manager (container
    mode). Binds the Fly 6PN address so `fly proxy` can reach it; writes the
    port and (if auto-generated) the token under ``state_dir``. Returns the
    bound port.

    IMPORTANT: binds ``::`` (or ``$BOBI_UI_HOST``) — NOT ``127.0.0.1``,
    which `fly proxy` (reaching the machine over private IPv6) can't reach.
    """
    return serve_container(
        lambda token: build_app(project, token=token,
                                chat_timeout=chat_timeout),
        state_dir=state_dir,
    )
