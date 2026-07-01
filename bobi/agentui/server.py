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

import json
import os
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

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
CHAT_HISTORY_LIMIT = 200


# --- chat history persistence -------------------------------------------
# Each web-UI exchange is appended to `webui-chat.jsonl` beside the session's
# state, so the transcript survives a refresh or switching agents (the
# browser's in-memory copy does not). This is the web-UI conversation
# specifically — not the agent's full event/tool transcript.

def _safe_name(name: str) -> bool:
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name


def _chat_log_path(project: Path, name: str) -> Path:
    from bobi import paths
    return paths.sessions_dir(project) / name / "webui-chat.jsonl"


def read_chat(project: Path, name: str, limit: int = CHAT_HISTORY_LIMIT) -> list[dict]:
    """Load the persisted web-UI conversation for an agent (oldest→newest)."""
    path = _chat_log_path(project, name)
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        role, text = obj.get("role"), obj.get("text", "")
        if role in ("user", "agent") and text:
            out.append({"role": role, "text": text})
    return out[-limit:]


def append_chat(project: Path, name: str, role: str, text: str) -> None:
    path = _chat_log_path(project, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"role": role, "text": text, "ts": time.time()}) + "\n")


# --- Claude transcript replay -------------------------------------------
# The UI's durable source of truth is the Claude Code JSONL transcript. The
# webui-chat log above is only a local fallback for cases where a transcript
# cannot be resolved yet.

def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return ""


def _claude_projects_dirs() -> list[Path]:
    dirs = []
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if cfg:
        dirs.append(Path(cfg) / "projects")
    dirs.append(Path.home() / ".claude" / "projects")

    seen = set()
    out = []
    for d in dirs:
        key = str(d)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _transcript_path(session_id: str) -> Path | None:
    if not session_id:
        return None
    for projects in _claude_projects_dirs():
        if not projects.exists():
            continue
        for project_dir in projects.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / f"{session_id}.jsonl"
            if candidate.exists():
                return candidate
    return None


def read_transcript_messages(session_id: str,
                             limit: int = CHAT_HISTORY_LIMIT) -> list[dict]:
    path = _transcript_path(session_id)
    if not path:
        return []

    out = []
    for line in path.read_text().splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        msg_type = obj.get("type", "")
        if msg_type not in ("human", "user", "assistant"):
            continue
        text = _extract_text(obj.get("message", {}).get("content", "")).strip()
        if not text:
            continue
        role = "agent" if msg_type == "assistant" else "user"
        out.append({"role": role, "text": text})
    return out[-limit:]


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
