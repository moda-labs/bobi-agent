"""The unified web app server — machine-scoped over every installed agent.

Unlike the per-team `agentui` server (bound to one project at build time),
this app resolves the target agent from the request path on every call, so
one server serves the whole `$BOBI_HOME/agents/` tree. That per-request
resolution is deliberate: it is the same seam a hosted multi-tenant
deployment would resolve a tenant through (#525).

Endpoint shape follows the #526 nouns: `/api/agents/{name}` is an installed
Bobi Agent (a dashboard card); sessions inside one agent are its subagents
(added by the chat routes).

All service-core calls run under one process-wide lock: the service binds
the selected runtime via the process environment (`BOBI_ROOT`), so two
threadpooled requests must never interleave a bind.
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from bobi import paths
from bobi.webui_common.security import (
    LEGACY_AGENTUI_TOKEN_HEADER,
    LEGACY_SETUP_TOKEN_HEADER,
    WEBUI_TOKEN_HEADER,
    install_security,
)
from bobi.webui_common.static import mount_static, serve_index

STATIC_DIR = Path(__file__).parent / "static"

# The service core selects a runtime by mutating the process environment
# (paths.bind_root), so cross-agent calls are serialized process-wide.
_service_lock = threading.Lock()


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
    except (ProcessLookupError, PermissionError):
        return 0
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
        with _service_lock:
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
        with _service_lock:
            result = service.stop_team(root)
        return JSONResponse({
            "ok": result.stopped or result.killed or result.stale
                  or result.pid == 0,
            "stopped": result.stopped,
            "pid": result.pid,
            "still_running": result.still_running,
        })

    @app.post("/api/agents/{name}/restart")
    def restart_agent(name: str) -> JSONResponse:
        from bobi import service

        root = _resolve(name)
        if root is None:
            return JSONResponse({"error": "unknown agent"}, status_code=404)
        with _service_lock:
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
