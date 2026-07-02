"""The unified web app server — machine-scoped over every installed agent.

Unlike the per-team `agentui` server (bound to one project at build time),
this app resolves the target agent from the request path on every call, so
one server serves the whole `$BOBI_HOME/agents/` tree. That per-request
resolution is deliberate: it is the same seam a hosted multi-tenant
deployment would resolve a tenant through (#525).

Endpoint shape follows the #526 nouns: `/api/agents/{name}` is an installed
Bobi Agent (a dashboard card); sessions inside one agent are its subagents
(added by the chat routes).

Service-core calls run through a root binder (see `_RootBinder`): the
service selects a runtime via the process environment (`BOBI_ROOT`), so
calls for different agents are serialized while calls for the same agent
run concurrently.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from bobi import paths
from bobi.chat_history import read_chat, read_transcript_messages, safe_name
from bobi.webui_common.security import (
    LEGACY_AGENTUI_TOKEN_HEADER,
    LEGACY_SETUP_TOKEN_HEADER,
    WEBUI_TOKEN_HEADER,
    install_security,
)
from bobi.webui_common.static import mount_static, serve_index

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_CHAT_TIMEOUT = 300


class _RootBinder:
    """Serialize service-core calls across *different* runtime roots.

    The service core selects a runtime by mutating the process environment
    (BOBI_ROOT), so two threadpooled requests must never interleave binds to
    different roots. Calls under the SAME root run concurrently (a blocking
    chat must not lock out the roster poll beside it); a call for another
    root waits until the current root's in-flight calls drain."""

    def __init__(self):
        self._cond = threading.Condition()
        self._root: Path | None = None
        self._active = 0

    @contextmanager
    def bound(self, root: Path):
        with self._cond:
            while self._active and self._root != root:
                self._cond.wait()
            self._root = root
            self._active += 1
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                if not self._active:
                    self._cond.notify_all()


_binder = _RootBinder()


def _first_paragraph(md: str) -> str:
    """First prose paragraph of an agent.md — the card description."""
    para: list[str] = []
    for line in md.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            if para:
                break
            continue
        para.append(s)
    return " ".join(para)


def _describe(agent_dir: Path) -> str:
    try:
        return _first_paragraph((agent_dir / "agent.md").read_text())[:160]
    except OSError:
        return ""


def _manager_pid(root: Path) -> int:
    """The manager pid when alive, else 0. A pure filesystem+signal check —
    no runtime bind, so the dashboard read path never touches BOBI_ROOT."""
    import os

    pid_path = paths.manager_pid_path(root)
    if not pid_path.exists():
        return 0
    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        return 0
    if pid <= 0:
        return 0
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return 0
    except PermissionError:
        pass  # exists, owned by someone else — still alive
    return pid


def agent_card(name: str) -> dict:
    """One dashboard card: an installed agent slot and its runtime state."""
    root = paths.agent_run_root(name)
    pid = _manager_pid(root)
    return {
        "name": name,
        "installed": True,
        "running": bool(pid),
        "pid": pid,
        "description": _describe(paths.package_dir(root)),
    }


def design_card(name: str) -> dict:
    """A source-only slot (designed, never installed) — dashboard shows it so
    the library and the runtime roster share one home."""
    return {
        "name": name,
        "installed": False,
        "running": False,
        "pid": 0,
        "description": _describe(paths.agent_source_dir(name)),
    }


def serialize_subagent(entry, *, manager_name: str = "") -> dict:
    """A session's card view. Mirrors the fields a `SessionEntry`
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


def ordered_subagents(entries, *, manager_name: str = "") -> list:
    return sorted(entries,
                  key=lambda e: (0 if manager_name and e.name == manager_name
                                 else 1, e.started_at or 0))


def _session_id_for(root: Path, session: str) -> str:
    """A session's Claude session id: the registry entry, else the on-disk
    <sessions>/<name>.id file — read directly so no runtime bind is needed."""
    try:
        return (paths.sessions_dir(root) / f"{session}.id").read_text().strip()
    except OSError:
        return ""


def dashboard_snapshot() -> dict:
    """Every agent slot on this machine: installed (with run state) first,
    then design-only sources."""
    installed = paths.list_agents()
    cards = [agent_card(name) for name in installed]

    agents_root = paths.agents_root()
    if agents_root.is_dir():
        for d in sorted(agents_root.iterdir()):
            if (d.is_dir() and d.name not in installed
                    and (d / "src" / "agent.yaml").is_file()):
                cards.append(design_card(d.name))
    return {"agents": cards, "home": str(paths.home_dir())}


def build_app(*, token: str) -> FastAPI:
    app = FastAPI()

    install_security(
        app,
        secret=token,
        header_name=WEBUI_TOKEN_HEADER,
        legacy_header_names=(LEGACY_AGENTUI_TOKEN_HEADER,
                             LEGACY_SETUP_TOKEN_HEADER),
        error_message="bad or missing token",
    )
    serve_index(app, STATIC_DIR / "index.html", {"{{TOKEN}}": token})
    mount_static(app, STATIC_DIR)

    @app.get("/api/ping")
    def ping() -> dict:
        return {"ok": True}

    @app.get("/api/dashboard")
    def dashboard() -> dict:
        return dashboard_snapshot()

    def _resolve(name: str) -> Path | None:
        try:
            return paths.resolve_root_for_agent(name)
        except RuntimeError:
            return None

    @app.get("/api/agents/{name}/status")
    def agent_status(name: str) -> JSONResponse:
        if _resolve(name) is None:
            return JSONResponse({"error": "unknown agent"}, status_code=404)
        return JSONResponse(agent_card(name))

    # Lifecycle actions are sync `def` on purpose: FastAPI runs them in a
    # threadpool so the (brief) spawn/stop work never stalls the event loop,
    # and the service lock serializes the BOBI_ROOT bind.
    @app.post("/api/agents/{name}/start")
    def start_agent(name: str) -> JSONResponse:
        from bobi import service

        root = _resolve(name)
        if root is None:
            return JSONResponse({"error": "unknown agent"}, status_code=404)
        with _binder.bound(root):
            try:
                result = service.spawn_team(root)
            except service.AlreadyRunning as e:
                return JSONResponse({"error": "already running", "pid": e.pid},
                                    status_code=409)
            except service.PreflightFailed as e:
                return JSONResponse(
                    {"error": "preflight failed",
                     "report": e.validation.format()},
                    status_code=409)
            except service.ServiceError as e:
                return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse({"ok": True, "pid": result.startup.pid})

    @app.post("/api/agents/{name}/stop")
    def stop_agent(name: str) -> JSONResponse:
        from bobi import service

        root = _resolve(name)
        if root is None:
            return JSONResponse({"error": "unknown agent"}, status_code=404)
        with _binder.bound(root):
            result = service.stop_team(root)
        return JSONResponse({
            "ok": result.stopped or result.killed or result.stale
                  or result.pid == 0,
            "stopped": result.stopped,
            "pid": result.pid,
            "still_running": result.still_running,
        })

    # --- subagents (sessions inside one agent) + chat -------------------

    @app.get("/api/agents/{name}/subagents")
    def subagents(name: str) -> JSONResponse:
        from bobi import service

        root = _resolve(name)
        if root is None:
            return JSONResponse({"error": "unknown agent"}, status_code=404)
        mgr = service.manager_session_name(root)
        with _binder.bound(root):
            entries = service.list_agents(root)
        return JSONResponse({
            "subagents": [serialize_subagent(e, manager_name=mgr)
                          for e in ordered_subagents(entries, manager_name=mgr)],
        })

    @app.get("/api/agents/{name}/subagents/{session}/messages")
    def subagent_messages(name: str, session: str) -> JSONResponse:
        root = _resolve(name)
        if root is None or not safe_name(session):
            return JSONResponse({"error": "unknown agent"}, status_code=404)
        # The durable source of truth is the Claude transcript; the web-UI
        # chat log is the fallback when no transcript resolves yet. Both are
        # explicit-path reads — no runtime bind.
        messages = read_transcript_messages(_session_id_for(root, session))
        if not messages:
            messages = read_chat(root, session)
        return JSONResponse({"messages": messages})

    # Blocking request/response chat (sync def → threadpool; the binder
    # allows same-root calls to proceed while this waits).
    @app.post("/api/agents/{name}/chat")
    def chat(name: str, payload: dict) -> JSONResponse:
        from bobi import service

        root = _resolve(name)
        if root is None:
            return JSONResponse({"error": "unknown agent"}, status_code=404)
        subagent = (payload.get("subagent") or "").strip()
        text = (payload.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)
        with _binder.bound(root):
            try:
                result = service.ask(root, subagent, text,
                                     timeout=DEFAULT_CHAT_TIMEOUT)
            except service.MessageDeliveryError as e:
                code = 404 if "unknown agent" in str(e) else 502
                return JSONResponse({"error": str(e)}, status_code=code)
        return JSONResponse({"reply": result.response})

    @app.post("/api/agents/{name}/restart")
    def restart_agent(name: str) -> JSONResponse:
        from bobi import service

        root = _resolve(name)
        if root is None:
            return JSONResponse({"error": "unknown agent"}, status_code=404)
        with _binder.bound(root):
            stop = service.stop_team(root)
            if stop.still_running:
                return JSONResponse(
                    {"error": "manager did not stop", "pid": stop.pid},
                    status_code=409)
            try:
                result = service.spawn_team(root)
            except service.ServiceError as e:
                return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse({"ok": True, "pid": result.startup.pid})

    return app
