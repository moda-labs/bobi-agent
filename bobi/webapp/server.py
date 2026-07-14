"""The unified web app server — machine-scoped over every installed agent.

Unlike the per-team `agentui` server (bound to one project at build time),
this app resolves the target agent from the request path on every call, so
one server serves the whole `$BOBI_HOME/agents/` tree. That per-request
resolution is deliberate: it is the same seam a hosted multi-tenant
deployment would resolve a tenant through (#525).

Endpoint shape follows the #526 nouns: `/api/agents/{name}` is an installed
Bobi Agent (a dashboard card); sessions inside one agent are its subagents
(added by the chat routes).

Handlers speak only to a `TeamRuntime` (see `runtime.py`, #690 stage 1):
this module owns HTTP mapping (routes, status codes, input validation) and
the hosted-onboarding surface, which is local-only product. The app binds
`LocalRuntime` at build time; no handler binds a runtime root, so requests
for different agents run concurrently (spawns serialize on a runtime-owned
lock around the one remaining process-global, the brain pin - see
`LocalRuntime._spawn_lock`).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from bobi import paths
from bobi.chat_history import safe_name
from bobi.webapp.runtime import (
    LocalRuntime,
    TeamAlreadyRunning,
    TeamDidNotStop,
    TeamLifecycleError,
    TeamPreflightFailed,
    TeamRuntime,
    UnknownTeam,
)
from bobi.webui_common.security import (
    WEBUI_TOKEN_HEADER,
    install_security,
)
from bobi.webui_common.static import mount_static, serve_index

STATIC_DIR = Path(__file__).parent / "static"


class _SetupHost:
    """ASGI router for hosted onboarding sessions.

    Each named slot gets its own setup app at `/setup/<slot>/...`, so two
    browser tabs can create or edit different teams concurrently."""

    def __init__(self):
        self.sessions: dict[str, object] = {}

    async def __call__(self, scope, receive, send):
        name = self._session_name(scope)
        app = self.sessions.get(name or "")
        if app is not None and name is not None:
            root = (scope.get("root_path") or "").rstrip("/")
            prefix = f"{root}/{quote(name)}"
            child_scope = dict(scope)
            child_scope["root_path"] = prefix
            await app(child_scope, receive, send)
            return
        if scope["type"] != "http":
            return
        await send({
            "type": "http.response.start",
            "status": 307,
            "headers": [(b"location", b"/#/setup")],
        })
        await send({"type": "http.response.body", "body": b""})

    def _session_name(self, scope) -> str | None:
        root = (scope.get("root_path") or "").rstrip("/")
        path = scope.get("path") or ""
        if root and path.startswith(root):
            path = path[len(root):]
        first = path.lstrip("/").split("/", 1)[0]
        return unquote(first) if safe_name(unquote(first)) else None

    def put(self, name: str, app) -> None:
        self.sessions[name] = app

    def release(self, name: str) -> None:
        self.sessions.pop(name, None)

    def names(self) -> list[str]:
        return sorted(self.sessions)


def _claude_available() -> bool:
    import shutil

    from bobi.sdk import get_cli_path

    return bool(shutil.which("claude")) or Path(get_cli_path()).exists()


def build_app(*, token: str, runtime: TeamRuntime | None = None) -> FastAPI:
    rt = runtime if runtime is not None else LocalRuntime()
    app = FastAPI()

    install_security(
        app,
        secret=token,
        header_name=WEBUI_TOKEN_HEADER,
        error_message="bad or missing token",
    )
    serve_index(app, STATIC_DIR / "index.html", {"{{TOKEN}}": token})
    mount_static(app, STATIC_DIR)

    # One HTTP mapping per runtime error type, app-wide, so every endpoint
    # (and any added later) translates the TeamRuntime error surface the
    # same way instead of growing per-route except-ladders.
    @app.exception_handler(UnknownTeam)
    def _unknown_team(request, exc) -> JSONResponse:
        return JSONResponse({"error": "unknown agent"}, status_code=404)

    @app.exception_handler(TeamAlreadyRunning)
    def _already_running(request, exc) -> JSONResponse:
        return JSONResponse({"error": "already running", "pid": exc.pid},
                            status_code=409)

    @app.exception_handler(TeamPreflightFailed)
    def _preflight_failed(request, exc) -> JSONResponse:
        return JSONResponse({"error": "preflight failed", "report": exc.report},
                            status_code=409)

    @app.exception_handler(TeamDidNotStop)
    def _did_not_stop(request, exc) -> JSONResponse:
        return JSONResponse({"error": "manager did not stop", "pid": exc.pid},
                            status_code=409)

    @app.exception_handler(TeamLifecycleError)
    def _lifecycle_error(request, exc) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.get("/api/ping")
    def ping() -> dict:
        return {"ok": True}

    @app.get("/api/dashboard")
    def dashboard() -> dict:
        return rt.dashboard()

    # Observability: spend read from existing per-session cost (#733). No
    # new emitters; the runtime folds each team's session state files.
    @app.get("/api/fleet/spend")
    def fleet_spend() -> dict:
        return rt.fleet_spend()

    @app.get("/api/agents/{name}/spend")
    def agent_spend(name: str) -> JSONResponse:
        return JSONResponse(rt.spend_summary(name))

    # System health (#733 vertical 2): manager liveness + session statuses;
    # a hosted runtime adds reachability and the sidecar's lifecycle trail.
    @app.get("/api/agents/{name}/health")
    def agent_health(name: str) -> JSONResponse:
        return JSONResponse(rt.health_summary(name))

    # Session logs (#733 vertical 3): the full session history with honest
    # terminal outcomes; transcripts drill in via the messages route below.
    @app.get("/api/agents/{name}/sessions")
    def agent_sessions(name: str) -> JSONResponse:
        return JSONResponse(rt.session_log(name))

    @app.get("/api/agents/{name}/status")
    def agent_status(name: str) -> JSONResponse:
        return JSONResponse(rt.team_status(name))

    # Lifecycle actions are sync `def` on purpose: FastAPI runs them in a
    # threadpool so the (brief) spawn/stop work never stalls the event loop.
    @app.post("/api/agents/{name}/start")
    def start_agent(name: str) -> JSONResponse:
        return JSONResponse(rt.start_team(name))

    @app.post("/api/agents/{name}/stop")
    def stop_agent(name: str) -> JSONResponse:
        return JSONResponse(rt.stop_team(name))

    @app.post("/api/agents/{name}/restart")
    def restart_agent(name: str) -> JSONResponse:
        return JSONResponse(rt.restart_team(name))

    # --- onboarding (the setup app, hosted) -----------------------------

    setup_host = _SetupHost()
    app.mount("/setup", setup_host)

    @app.get("/api/setup/current")
    def setup_current() -> dict:
        names = setup_host.names()
        return {"active": bool(names), "name": names[0] if names else None,
                "sessions": names}

    @app.post("/api/setup/open")
    def setup_open(payload: dict) -> JSONResponse:
        from bobi.setup import open_mode
        from bobi.setup.state import SetupState
        from bobi.setup.webui.server import build_app as build_setup_app

        name = (payload.get("name") or "").strip() or "new-agent"
        mode = (payload.get("mode") or "create").strip()
        model = (payload.get("model") or "").strip() or None
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
            # Tolerate a nested source (an older flow could land a template
            # in a src/ subfolder): a single team child counts as the source.
            nested = open_mode.list_teams_in(src)
            if len(nested) == 1:
                src = Path(nested[0]["path"])
            else:
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
            setup_host.release(name)
            # The slot was opened under a placeholder name but the team got
            # its real name during setup (template pick, auto-name, rename).
            # A slot IS its team (#526: agents/<name>/), so move the whole
            # slot dir to match. Nothing is running yet (finish no longer
            # launches) and the session is released, so the move is safe.
            final = (state.team_name or "").strip()
            if final and final != name:
                import shutil

                old_dir = paths.agent_dir(name)
                new_dir = paths.agent_dir(final)
                if safe_name(final) and old_dir.is_dir():
                    if not new_dir.exists():
                        shutil.move(str(old_dir), str(new_dir))
                    elif (paths.agent_source_dir(final).is_dir()
                          and Path(state.source_dir or "").resolve()
                          == paths.agent_source_dir(final).resolve()):
                        old_run = old_dir / "run"
                        new_run = new_dir / "run"
                        if old_run.is_dir() and not new_run.exists():
                            shutil.move(str(old_run), str(new_run))
                        try:
                            old_dir.rmdir()
                        except OSError:
                            pass
            return {"redirect": "/#/"}

        setup_base = f"/setup/{quote(name)}"
        setup_host.put(
            name,
            build_setup_app(state, project, nonce=token,
                            base_path=setup_base, model=model,
                            on_finish=on_finish),
        )

        # Edit-an-existing-team entry: a fresh session deep-links the SPA
        # into open mode for the slot's source (the SPA drives /api/start,
        # which owns the copy/reverse-fill semantics). A resumed session is
        # already mid-flow — don't re-open over it.
        url = f"{setup_base}/"
        if mode == "open" and not resumed:
            url = f"{setup_base}/?open={quote(str(src))}"
        return JSONResponse({"url": url, "name": name, "resumed": resumed})

    # --- subagents (sessions inside one agent) + chat -------------------

    @app.get("/api/agents/{name}/subagents")
    def subagents(name: str) -> JSONResponse:
        return JSONResponse({"subagents": rt.subagents(name)})

    @app.get("/api/agents/{name}/subagents/{session}/messages")
    def subagent_messages(name: str, session: str) -> JSONResponse:
        if not safe_name(session):
            return JSONResponse({"error": "unknown agent"}, status_code=404)
        return JSONResponse({"messages": rt.messages(name, session)})

    # Submit-then-poll chat: the POST returns a message id immediately and
    # the deliver runs in the background — no request is held open for the
    # agent's (up to minutes-long) reply, so the endpoint shape survives
    # proxies and load balancers (the #525 SaaS discipline). The reply
    # reaches the transcript via the messages poll; the job carries only
    # status and errors.
    @app.post("/api/agents/{name}/chat")
    def chat(name: str, payload: dict) -> JSONResponse:
        subagent = (payload.get("subagent") or "").strip()
        text = (payload.get("text") or "").strip()
        if not text:
            # Unknown team still wins over bad input (the historical order):
            # the status probe raises UnknownTeam -> 404 before the 400.
            rt.team_status(name)
            return JSONResponse({"error": "empty message"}, status_code=400)
        return JSONResponse({"message_id": rt.chat_submit(name, subagent, text)})

    @app.get("/api/agents/{name}/chat/{message_id}")
    def chat_status(name: str, message_id: str) -> JSONResponse:
        job = rt.chat_job(name, message_id)
        if job is None:
            return JSONResponse({"error": "unknown message"}, status_code=404)
        return JSONResponse(job)

    return app
