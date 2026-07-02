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


class _SetupHost:
    """ASGI slot for the active onboarding session's setup app.

    The unified app hosts ONE onboarding at a time (an explicit MVP
    decision): POST /api/setup/open builds the standard setup app for a
    named slot and parks it here; /setup/* requests forward to it. With no
    active session, /setup/* redirects to the shell, which shows the
    create form."""

    def __init__(self):
        self.app = None
        self.name: str | None = None
        self.project: Path | None = None

    async def __call__(self, scope, receive, send):
        if self.app is not None:
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        await send({
            "type": "http.response.start",
            "status": 307,
            "headers": [(b"location", b"/#/setup")],
        })
        await send({"type": "http.response.body", "body": b""})


def _claude_available() -> bool:
    import shutil

    from bobi.sdk import get_cli_path

    return bool(shutil.which("claude")) or Path(get_cli_path()).exists()


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

    # --- onboarding (the setup app, hosted) -----------------------------

    setup_host = _SetupHost()
    app.mount("/setup", setup_host)

    @app.get("/api/setup/current")
    def setup_current() -> dict:
        return {"active": setup_host.app is not None, "name": setup_host.name}

    @app.post("/api/setup/open")
    def setup_open(payload: dict) -> JSONResponse:
        from bobi.setup import open_mode
        from bobi.setup.state import SetupState
        from bobi.setup.webui.server import build_app as build_setup_app

        name = (payload.get("name") or "").strip() or "new-agent"
        mode = (payload.get("mode") or "create").strip()
        if not safe_name(name):
            return JSONResponse({"error": "bad name"}, status_code=400)
        if mode not in ("create", "open"):
            return JSONResponse({"error": "mode must be create or open"},
                                status_code=400)
        if not _claude_available():
            return JSONResponse(
                {"error": "the Claude Code CLI is required for setup — "
                          "install it first (https://claude.com/claude-code)."},
                status_code=409)

        src = paths.agent_source_dir(name)
        if mode == "open" and not open_mode.is_team(src):
            return JSONResponse(
                {"error": f"'{name}' has no editable source at {src}"},
                status_code=404)

        project = paths.agent_run_root(name)
        project.mkdir(parents=True, exist_ok=True)
        paths.workspace_dir(project).mkdir(parents=True, exist_ok=True)

        # Resume an interrupted onboarding for this slot; else start fresh.
        state = SetupState.load(project)
        resumed = bool(state) and not state.finished
        if not resumed:
            SetupState.clear(project)
            state = SetupState(team_name=name)
            state.save(project)

        def on_finish() -> dict:
            # Finish returns to the home dashboard; the user starts the
            # team from its card there (launch stays a deliberate action).
            # Release the slot so /setup/ starts clean next time.
            setup_host.app = None
            setup_host.name = None
            setup_host.project = None
            return {"redirect": "/#/"}

        setup_host.app = build_setup_app(state, project, nonce=token,
                                         base_path="/setup",
                                         on_finish=on_finish)
        setup_host.name = name
        setup_host.project = project

        # Edit-an-existing-team entry: a fresh session deep-links the SPA
        # into open mode for the slot's source (the SPA drives /api/start,
        # which owns the copy/reverse-fill semantics). A resumed session is
        # already mid-flow — don't re-open over it.
        url = "/setup/"
        if mode == "open" and not resumed:
            from urllib.parse import quote

            url = f"/setup/?open={quote(str(src))}"
        return JSONResponse({"url": url, "name": name, "resumed": resumed})

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

    # Submit-then-poll chat: the POST returns a message id immediately and
    # the deliver runs in a background thread — no request is held open for
    # the agent's (up to minutes-long) reply, so the endpoint shape survives
    # proxies and load balancers (the #525 SaaS discipline). The reply
    # reaches the transcript via the messages poll; this job store carries
    # only status and errors.
    chat_jobs: dict[str, dict] = {}

    def _prune_jobs() -> None:
        if len(chat_jobs) <= 500:
            return
        for mid in [m for m, j in chat_jobs.items()
                    if j["status"] != "pending"][:250]:
            chat_jobs.pop(mid, None)

    @app.post("/api/agents/{name}/chat")
    def chat(name: str, payload: dict) -> JSONResponse:
        import uuid

        from bobi import service

        root = _resolve(name)
        if root is None:
            return JSONResponse({"error": "unknown agent"}, status_code=404)
        subagent = (payload.get("subagent") or "").strip()
        text = (payload.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)

        message_id = uuid.uuid4().hex
        _prune_jobs()
        chat_jobs[message_id] = {"status": "pending"}

        def work() -> None:
            with _binder.bound(root):
                try:
                    service.ask(root, subagent, text,
                                timeout=DEFAULT_CHAT_TIMEOUT)
                    chat_jobs[message_id] = {"status": "done"}
                except service.MessageDeliveryError as e:
                    chat_jobs[message_id] = {"status": "error",
                                             "error": str(e)}
                except Exception as e:  # noqa: BLE001 — job must resolve
                    chat_jobs[message_id] = {"status": "error",
                                             "error": str(e)}

        threading.Thread(target=work, daemon=True,
                         name=f"chat-{message_id[:8]}").start()
        return JSONResponse({"message_id": message_id})

    @app.get("/api/agents/{name}/chat/{message_id}")
    def chat_status(name: str, message_id: str) -> JSONResponse:
        job = chat_jobs.get(message_id)
        if job is None:
            return JSONResponse({"error": "unknown message"}, status_code=404)
        return JSONResponse(job)

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
