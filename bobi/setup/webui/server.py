"""The `bobi setup` web server — FastAPI on 127.0.0.1, foreground.

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
  dedicated `/api/credential` POST and go straight to run/.env.

`build_app` is pure (state + project in, app out) so it's driven directly
by Starlette's TestClient with an injected fake `stream_fn` — no network,
no CLI. `serve()` is the socket→uvicorn foreground launcher.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import AsyncIterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import yaml

# Imported at module level (not inside build_app) so that, under
# `from __future__ import annotations`, FastAPI can resolve the string
# annotations on the route handlers against this module's globals.
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from bobi import paths
from bobi.setup.state import STAGE_ORDER, SetupState, Stage
from bobi.webui_common.launcher import serve_local
from bobi.webui_common.security import (
    WEBUI_TOKEN_HEADER,
    install_security,
)
from bobi.webui_common.static import mount_static, serve_index

STATIC_DIR = Path(__file__).parent / "static"
NONCE_HEADER = WEBUI_TOKEN_HEADER


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
        "source_dir": state.source_dir,
        "chat": state.chat,
        "ingress": {
            "mode": state.ingress.mode,
            "url": state.ingress.url,
            "verified": state.ingress.verified,
            "verified_at": state.ingress.verified_at,
            "error": state.ingress.error,
        },
        "phase": state.phase,
        "spec": {
            "goal": spec.goal,
            "roles": spec.roles,
            "workflows": spec.workflows,
            "workflows_confirmed": spec.workflows_confirmed,
            "autonomous": spec.autonomous,
            "autonomous_confirmed": spec.autonomous_confirmed,
            "services": spec.services,
            # User-added MCP connections — names/command/args/url only (never
            # secret VALUES), so the UI can repopulate the edit form.
            "mcp_servers": spec.mcp_servers,
            "readiness": {s: spec.readiness_for(s).value
                          for s in ("goal", "roles", "autonomous", "services")},
        },
        "summary": state.summary,
        "messages": state.messages,
        "credentials_saved": state.credentials_saved,
        "advance_blocker": state.advance_blocker(),
        "validated": state.validated,
        "installed": state.installed,
        "finished": state.finished,
    }


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _validate_public_event_server_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return "use a public https:// event server URL"
    return None


def _probe_event_server(url: str) -> tuple[bool, str]:
    health_url = url.rstrip("/") + "/health"
    req = UrlRequest(health_url, headers={"accept": "application/json"})
    try:
        with urlopen(req, timeout=8) as resp:
            if not (200 <= resp.status < 300):
                return False, f"/health returned HTTP {resp.status}"
            try:
                payload = json.loads(resp.read(4096).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False, "/health did not return Bobi JSON"
            if (isinstance(payload, dict)
                    and payload.get("status") == "ok"
                    and payload.get("auth") == "hmac"):
                return True, ""
            return False, "/health did not look like a Bobi event server"
    except HTTPError as e:
        return False, f"/health returned HTTP {e.code}"
    except URLError as e:
        return False, f"could not reach /health: {e.reason}"
    except TimeoutError:
        return False, "timed out reaching /health"
    except OSError as e:
        return False, f"could not reach /health: {e}"


def _persist_ingress_env(project: Path, state: SetupState) -> None:
    from bobi.setup import actions
    env = actions.read_env(project)
    if state.ingress.mode == "local":
        env.pop("BOBI_EVENT_SERVER", None)
        os.environ.pop("BOBI_EVENT_SERVER", None)
    elif state.ingress.url:
        env["BOBI_EVENT_SERVER"] = state.ingress.url
        os.environ["BOBI_EVENT_SERVER"] = state.ingress.url
    actions.write_env(project, env)


# --- app -----------------------------------------------------------------

def build_app(state: SetupState, project: Path, *, nonce: str,
              model: str | None = None, stream_fn=None,
              home_root: Path | None = None, base_path: str = "",
              on_finish=None):
    """Construct the FastAPI app. `stream_fn` overrides the LLM source
    (tests inject a fake). `home_root` overrides the Bobi home for tests.
    `base_path` is the mount prefix when hosted as a sub-app of the unified
    web app (e.g. "/setup") — the SPA prefixes its /api and /static URLs
    with it. Empty (the standalone `bobi setup` server) changes nothing.
    `on_finish` is the unified app's launch hook: called after Finish marks
    the state complete; its dict return is merged into the finish response
    (e.g. {"launched": True, "redirect": ...}); an exception surfaces as
    `launch_error` without unwinding the finish. None (standalone) keeps
    today's behavior: finish just marks done and shows the start command."""
    app = FastAPI()
    app.state.stream_fn = stream_fn
    app.state.model = model
    # The machine-wide library of Bobi Agent slots. Editable sources default to
    # <home>/agents/<name>/src; setup scans this root.
    home = (home_root or paths.home_dir()).resolve()
    library = home / "agents"

    def _within_home(raw: str, default: Path) -> tuple[Path, bool]:
        """Resolve a user-supplied path and confine it to the home tree — the
        single source of truth for the folder-picker / scan security boundary.
        Relative paths re-base under home (consistent across endpoints). Returns
        (path, ok); ok is False when the resolved path escaped home, so each
        caller picks its own policy (reject vs. fall back)."""
        if not raw:
            return default, True
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = home / p
        p = p.resolve()
        return p, (p == home or home in p.parents)

    def _load_machine_config() -> dict:
        cfg_path = paths.ensure_global_config()
        try:
            return yaml.safe_load(cfg_path.read_text()) or {}
        except Exception:
            return {}

    def _write_machine_config(raw: dict) -> None:
        cfg_path = paths.ensure_global_config()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(yaml.dump(raw, default_flow_style=False))

    def source_roots() -> list[Path]:
        """Configured team-source scan roots, always including the library."""
        roots: list[Path] = []

        def add(path: Path) -> None:
            try:
                resolved = path.resolve()
            except OSError:
                return
            if resolved not in roots:
                roots.append(resolved)

        add(library)
        configured = _load_machine_config().get("sources", [])
        if isinstance(configured, list):
            for item in configured:
                target, ok = _within_home(str(item), library)
                if ok:
                    add(target)
        return roots

    install_security(
        app,
        secret=nonce,
        header_name=WEBUI_TOKEN_HEADER,
        error_message="bad or missing nonce",
    )
    serve_index(app, STATIC_DIR / "index.html",
                {"{{NONCE}}": nonce, "{{BASE}}": base_path})
    mount_static(app, STATIC_DIR)

    # --- state (deterministic) -----------------------------------------
    @app.get("/api/state")
    def get_state() -> dict:
        return serialize_state(state)

    # The harness backing the wizard (and the installed team): which agent
    # runs it, and whether it's authenticated. The welcome screen reads this
    # to show "which agent runs your harness" + a log-in prompt; the Re-check
    # button re-polls it after the user runs `claude auth login`.
    @app.get("/api/harness")
    def get_harness() -> dict:
        from bobi.setup import harness
        return harness.harness_status(app.state.model).to_dict()

    # A cheap liveness check for the client heartbeat: if this stops
    # answering (Ctrl-C, closed terminal, crash), the page knows the setup
    # server is gone and freezes itself instead of looking live.
    @app.get("/api/ping")
    def ping() -> dict:
        return {"ok": True}

    def visible_teams() -> list[dict]:
        # Every editable team source in the library, plus this session's team.
        # A team can be authored anywhere the user points /api/start (the chosen
        # location is persisted in state.source_dir). Surface this session's team
        # even when it lives outside the default library, deduped by path — else a
        # team the user explicitly placed elsewhere is invisible on the home screen.
        from bobi.setup import open_mode
        from bobi.setup.actions import team_source_dir
        teams = []
        seen = set()
        for root in source_roots():
            for t in open_mode.list_teams_in(root):
                if t["path"] not in seen:
                    seen.add(t["path"])
                    teams.append(t)
        if state.source_dir:
            src = team_source_dir(project, state)
            teams += [t for t in open_mode.list_teams_in(src)
                      if t["path"] not in seen]
        return teams

    # --- intro: create / modify-existing / from-registry + a location --
    @app.get("/api/intro")
    def intro() -> dict:
        # Create defaults the team source into the library; Modify defaults to
        # scanning the same library, but the user can point the scan elsewhere
        # (another source tree, a thumb drive, wherever) via /api/teams. Install
        # always targets the selected Bobi Agent's run/package directory; a
        # source outside the default library copies in like a registry team.
        default_source = paths.agent_source_dir(state.team_name or "new-agent")
        return {"teams": visible_teams(),
                "default_location": str(default_source),
                "scan_dir": str(library),
                "source_roots": [str(p) for p in source_roots()]}

    @app.get("/api/source-roots")
    def get_source_roots() -> dict:
        return {"source_roots": [str(p) for p in source_roots()]}

    @app.post("/api/source-roots")
    def add_source_root(payload: dict) -> JSONResponse:
        target, ok = _within_home(str(payload.get("dir") or ""), library)
        if not ok:
            return JSONResponse(
                {"error": "pick a folder inside your home directory"},
                status_code=400)
        raw = _load_machine_config()
        configured = raw.get("sources", [])
        if not isinstance(configured, list):
            configured = []
        existing = {str(p) for p in source_roots()}
        if str(target) not in existing:
            configured.append(str(target))
        raw["sources"] = configured
        _write_machine_config(raw)
        return JSONResponse({"source_roots": [str(p) for p in source_roots()]})

    @app.get("/api/teams")
    def teams(request: Request) -> JSONResponse:
        # Scan a user-chosen directory for editable team sources (Modify's
        # "which folder holds your teams?"). Accepts an absolute path or one
        # relative to home; confined to the home tree, like the picker.
        from bobi.setup import open_mode
        target, ok = _within_home(request.query_params.get("dir") or "", library)
        if not ok:
            return JSONResponse(
                {"error": "pick a folder inside your home directory"},
                status_code=400)
        return JSONResponse({"dir": str(target),
                             "teams": open_mode.list_teams_in(target)})

    # Internal/test packs that shouldn't surface as user-facing templates.
    _HIDDEN_TEMPLATES = {"dogfood-content-review"}

    @app.get("/api/registry")
    def registry_teams() -> dict:
        # Network-backed and lazy — only fetched to populate the intro's
        # template list, so the intro screen never blocks on it.
        from bobi.setup import open_mode
        teams = [t for t in open_mode.list_registry_teams(project)
                 if t.get("name") not in _HIDDEN_TEMPLATES]
        return {"teams": teams}

    @app.get("/api/browse")
    def browse(request: Request) -> JSONResponse:
        # A home-scoped directory lister for the location picker: a native OS
        # folder dialog isn't reachable from a localhost page, so we walk the
        # tree server-side. Rooted at the user's home (the library and most dev
        # repos live there); confined to it so the localhost page can't list
        # the whole filesystem. Paths are absolute. Anything outside home can
        # still be typed into the location field directly.
        # Best-effort create so the library is navigable on day one; never let
        # a read-only home or a file already named `agents` turn a GET
        # into a 500 — just fall back to listing home.
        try:
            library.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        default = library if library.is_dir() else home
        here, ok = _within_home(request.query_params.get("path") or "", default)
        if not ok:
            here = home
        if not here.is_dir():
            return JSONResponse({"error": "not a directory"}, status_code=404)
        try:
            dirs = sorted(d.name for d in here.iterdir()
                          if d.is_dir() and not d.name.startswith("."))
        except OSError:
            dirs = []
        parent = str(here.parent) if here != home else None
        return JSONResponse({"path": str(here), "parent": parent, "dirs": dirs})

    @app.post("/api/rename")
    def rename(payload: dict) -> JSONResponse:
        from bobi.setup.actions import team_source_dir
        from bobi.setup.authoring import slug
        new = slug(payload.get("name", ""))
        if not new:
            return JSONResponse({"error": "give the team a name"},
                                status_code=400)
        old = state.team_name
        # When the working source folder is named after the team (modify and
        # registry default to <location>/<team-name>), rename the folder on disk
        # to match so it reflects the new name. Create's "bobi/" folder isn't
        # team-named, so it's left as the user chose it. The hosted web app's
        # from-scratch flow starts in a placeholder slot
        # agents/<old>/src; treat that canonical source as team-named too so a
        # rename moves the editable source out of the placeholder slot.
        if old and state.source_dir and Path(state.source_dir).name == old:
            src = team_source_dir(project, state)
            dest = src.parent / new
            if src.resolve() != dest.resolve():
                if dest.exists():
                    return JSONResponse(
                        {"error": f"a folder named '{new}' already exists there"},
                        status_code=409)
                if src.is_dir():
                    src.rename(dest)
                rel = Path(state.source_dir)
                state.source_dir = (str(rel.parent / new)
                                    if str(rel.parent) not in (".", "") else new)
                # the source tree moved — any prior validation is stale
                state.validated = False
                state.validated_hash = ""
        elif on_finish is not None and state.source_dir:
            src = team_source_dir(project, state)
            try:
                rel_src = src.resolve().relative_to(library.resolve())
                slot = rel_src.parts[0] if rel_src.parts[1:] == ("src",) else ""
            except (IndexError, ValueError):
                slot = ""
            old_default = library / (old or slot) / "src"
            new_slot = library / new
            new_default = new_slot / "src"
            if src.resolve() == old_default.resolve():
                if src.resolve() != new_default.resolve() and new_slot.exists():
                    return JSONResponse(
                        {"error": f"a team named '{new}' already exists"},
                        status_code=409)
                if src.is_dir() and src.resolve() != new_default.resolve():
                    new_default.parent.mkdir(parents=True, exist_ok=True)
                    src.rename(new_default)
                    try:
                        src.parent.rmdir()
                    except OSError:
                        pass
                state.source_dir = str(new_default)
                # the source tree moved — any prior validation is stale
                state.validated = False
                state.validated_hash = ""
        state.team_name = new
        state.save(project)
        return JSONResponse(serialize_state(state))

    @app.post("/api/start")
    def start(payload: dict) -> JSONResponse:
        from bobi.setup import open_mode
        from bobi.setup.authoring import slug
        mode = payload.get("mode", "create")
        if mode not in ("create", "open", "registry"):
            return JSONResponse(
                {"error": "mode must be create, open, or registry"},
                status_code=400)
        location = (payload.get("location") or "").strip()
        team = (payload.get("team") or "").strip()
        if mode == "registry" and not team:
            return JSONResponse({"error": "pick a team to download"},
                                status_code=400)
        if location:
            loc = Path(location).expanduser()
            abs_loc = (loc if loc.is_absolute() else home / loc).resolve()
        elif mode == "registry":
            # A template defaults into its own library slot - the same
            # agents/<name>/src shape the home scan reads, so the finished
            # team shows on the hub without the UI doing path math. A name
            # that slugs to nothing (all punctuation / non-ASCII) has no
            # slot to default into.
            if not slug(team):
                return JSONResponse(
                    {"error": "choose a location for the team"},
                    status_code=400)
            abs_loc = (library / slug(team) / "src").resolve()
        else:
            return JSONResponse({"error": "choose a location for the team"},
                                status_code=400)
        run_root = project.resolve()
        if abs_loc == run_root or run_root in abs_loc.parents:
            return JSONResponse({"error": "pick a source location outside run/"},
                                status_code=400)
        # Validate and materialize the source first; session state is mutated
        # only after everything succeeded, so a rejected or failed start can't
        # leave the session pointing at a team it never opened.
        if mode == "open":
            # The UI sends the team's source path (from a scan of whatever
            # folder the user chose), not just a name — teams can live anywhere
            # now, so a name alone is ambiguous.
            team_path = (payload.get("team_path") or payload.get("team") or "").strip()
            src = Path(team_path).expanduser()
            if not src.is_absolute():
                src = project / src
            src = src.resolve()
            if not open_mode.is_team(src):
                return JSONResponse({"error": "that folder isn't a team "
                                     "(no agent.yaml)"}, status_code=400)
            # Forking/importing into a NEW location must not clobber a DIFFERENT
            # team already living there — copy_into merges (copytree
            # dirs_exist_ok) and would corrupt it. Opening a team in place sends
            # location == team_path (src == abs_loc), which is allowed.
            if abs_loc != src and abs_loc.exists():
                return JSONResponse(
                    {"error": f"a team already exists at {abs_loc} — rename or "
                     "remove it first, or choose another location."},
                    status_code=409)
            try:
                open_mode.copy_into(src, abs_loc)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
        elif mode == "registry":
            # Don't merge a template over a team that already lives at the target
            # (fetch_into → copy_into uses copytree dirs_exist_ok). An existing
            # but EMPTY directory is fine — the canonical slot src/ may already
            # have been created by the slot scaffolding. Also don't nest one
            # inside a direct-root team (agent.yaml right at the parent) - the
            # scanner would list both as editable teams.
            def _occupied(d: Path) -> bool:
                try:
                    return d.is_dir() and any(d.iterdir())
                except OSError:
                    return False
            if abs_loc.is_file() or _occupied(abs_loc) or open_mode.is_team(abs_loc.parent):
                return JSONResponse(
                    {"error": f"a team already exists at {abs_loc} — open it from "
                     "the hub, or remove it first to start from this template."},
                    status_code=409)
            try:
                open_mode.fetch_into(project, team, abs_loc)
            except Exception as e:
                # Anything now in the target is this request's partial copy -
                # remove it, else the leftover blocks the slot with a baffling
                # 409 forever.
                import shutil
                shutil.rmtree(abs_loc, ignore_errors=True)
                return JSONResponse({"error": f"couldn't download '{team}': {e}"},
                                    status_code=502)
        else:
            name = (payload.get("name") or "").strip()
            if open_mode.is_team(abs_loc):
                return JSONResponse(
                    {"error": f"a team already exists at {abs_loc} — open it "
                     "from the hub, or choose another source directory."},
                    status_code=409)
        state.source_dir = str(abs_loc)
        state.finished = False   # starting/opening a team begins a fresh session
        # Both modify-local and from-registry land in the same non-lossy
        # edit-in-place authoring path; only create authors from scratch.
        state.mode = "create" if mode == "create" else "open"
        if mode == "create":
            state.team_name = slug(name) if name else ""
        else:
            open_mode.reverse_fill(state, abs_loc)
        state.stage = Stage.DESIGN
        state.save(project)
        return JSONResponse(serialize_state(state))

    # --- conversation turn (streaming) ---------------------------------
    @app.post("/api/message")
    async def message(request: Request) -> StreamingResponse:
        body = await request.json()
        text = (body.get("text") or "").strip()

        def _record(user_text, reply):
            state.messages.append({"role": "user", "content": user_text})
            state.messages.append({"role": "assistant", "content": reply})
            state.save(project)

        async def _propose_test(user_text: str, hit: dict):
            """First turn: launch the server, list its tools, and PROPOSE a safe
            read-only tool to call — the user confirms before anything runs."""
            from bobi.setup import mcp_probe
            if hit.get("none"):
                reply = ("There are no MCP connections set up yet to test. Add "
                         "one with “add a connection,” then ask me to test it.")
                yield reply
                _record(user_text, reply)
                return
            if hit.get("ambiguous"):
                reply = ("Which connection should I test? You have: "
                         + ", ".join(hit.get("candidates") or []) + ".")
                yield reply
                _record(user_text, reply)
                return
            key = hit["key"]
            entry = (state.spec.mcp_servers or {}).get(key) or {}
            label = entry.get("label") or key
            yield (f"Starting {label} and listing its tools (first run can take "
                   "a moment)…\n\n")
            result = await mcp_probe.probe(entry, project)   # list only, no call
            if not result.get("ok"):
                reply = f"✗ Couldn’t start {label}: {result.get('error')}"
                if result.get("stderr"):
                    reply += f"\n\nServer output:\n{result['stderr'][:600]}"
                state.pending_test = {}
                yield reply
                _record(user_text, reply)
                return
            tools = result.get("tools") or []
            proposed = result.get("suggested")
            state.pending_test = {"key": key, "proposed": proposed, "tools": tools}
            state.save(project)
            shown = ", ".join(tools[:10]) + (" …" if len(tools) > 10 else "")
            if proposed:
                reply = (f"{label} is up — {len(tools)} tools available.\n\n"
                         f"To verify the connection end-to-end I'll call "
                         f"{proposed} (read-only, no arguments). Reply “yes” "
                         f"to run it, name another tool, or say no.\n\n"
                         f"Tools: {shown}")
            else:
                reply = (f"{label} is up — {len(tools)} tools available, but I "
                         f"couldn’t spot a clearly safe read-only one to call. "
                         f"Name a tool to try (no arguments will be sent): {shown}")
            yield reply
            _record(user_text, reply)

        async def _resolve_pending(user_text: str, decision: dict):
            """Second turn: the user confirmed (or named a tool / declined).
            Run the chosen tool and report — this is the real connection test."""
            from bobi.setup import mcp_probe
            pending = state.pending_test or {}
            if decision["action"] == "cancel":
                state.pending_test = {}
                reply = "Okay — skipped the test. Ask again whenever you’re ready."
                yield reply
                _record(user_text, reply)
                return
            if decision["action"] == "refuse_write":
                # User named a tool that looks like it writes/changes data — never
                # run it as a connection test. Keep the proposal open.
                reply = (f"{decision.get('tool')} looks like it writes or changes "
                         f"data, so I won’t call it as a test. Pick a read-only "
                         f"tool, or reply “yes” to run the proposed one.")
                yield reply
                _record(user_text, reply)
                return
            tool = decision.get("tool")
            if not tool:
                reply = ("Name a tool to call (no arguments will be sent): "
                         + ", ".join(pending.get("tools") or []))
                yield reply
                _record(user_text, reply)
                return
            key = pending.get("key")
            entry = (state.spec.mcp_servers or {}).get(key)
            state.pending_test = {}
            # The connection may have been edited or removed between the proposal
            # and now — don't test a stale/empty key.
            if not isinstance(entry, dict) or not entry:
                reply = ("That connection isn’t there anymore — it may have been "
                         "removed or changed. Ask me to test it again.")
                yield reply
                _record(user_text, reply)
                return
            label = entry.get("label") or key
            yield f"Calling {tool} on {label}…\n\n"
            result = await mcp_probe.probe(entry, project, call_name=tool)
            # Persist ONLY coarse status — never raw error/stderr text, which can
            # carry secrets and is served to the browser via /api/state.
            entry["last_test"] = {"ok": bool(result.get("ok")),
                                  "live_ok": result.get("live_ok"),
                                  "called": tool}
            state.spec.mcp_servers[key] = entry
            if not result.get("ok"):
                reply = f"✗ Couldn’t start {label}: {result.get('error')}"
                if result.get("stderr"):
                    reply += f"\n\nServer output:\n{result['stderr'][:600]}"
            elif result.get("live_ok"):
                out = (result.get("output") or "").strip()
                snippet = f"\n\nResponse: {out}" if out else ""
                reply = (f"✓ Called {tool} on {label} — it worked. The "
                         f"connection is live.{snippet}")
            else:
                reply = (f"⚠ {label} starts, but calling {tool} failed: "
                         f"{result.get('live_error')}\n\nThat usually means "
                         f"credentials aren’t set or aren’t valid yet — add them "
                         f"with “edit” on the connection, then re-test.")
            yield reply
            _record(user_text, reply)

        async def gen() -> AsyncIterator[str]:
            if not text:
                yield _sse("error", {"message": "empty message"})
                return
            # The digestion brain runs on the Claude Code CLI. A missing CLI is
            # a cheap, reliable signal (no keychain probe), so block early with
            # a clear message — real path only; an injected stream_fn (tests)
            # doesn't touch the CLI. Auth can't be detected reliably enough to
            # pre-block on (Mac stores it in the keychain, and "present" isn't
            # "valid"), so we never gate on it up front; we only probe it on
            # failure to enrich the error. That keeps the keychain call off
            # every happy turn and never blocks a working harness.
            import shutil
            from bobi.setup import harness
            real_harness = app.state.stream_fn is None
            if real_harness and shutil.which("claude") is None:
                yield _sse("error", {"message":
                    f"The {harness.AGENT_NAME} CLI isn't installed — setup's "
                    "brain runs on it. Install it "
                    "(https://claude.com/claude-code), then retry."})
                yield _sse("state", serialize_state(state))
                return
            from bobi.setup import digestion
            from bobi.setup.actions import redact_secrets
            # Scrub secrets at the trust boundary and tell the user. digest_turn
            # re-redacts (idempotent) so it's safe regardless of caller.
            clean, redacted = redact_secrets(text)
            if redacted:
                yield _sse("redacted", {"count": redacted})
            from bobi.setup import mcp_probe
            # A proposed test awaiting confirmation takes priority: a "yes" / tool
            # name / "no" resolves it; anything else drops it and falls through.
            if state.pending_test:
                decision = mcp_probe.match_test_confirmation(
                    clean, state.pending_test)
                if decision["action"] != "none":
                    async for chunk in _resolve_pending(clean, decision):
                        yield _sse("delta", {"text": chunk})
                    yield _sse("state", serialize_state(state))
                    return
                state.pending_test = {}   # user changed the subject
                state.save(project)
            # "test the connection" → propose a tool (the no-tools design brain
            # can't reach the team's servers, so we handle it here).
            hit = mcp_probe.match_connection_test(clean, state.spec.mcp_servers)
            if hit.get("intent"):
                async for chunk in _propose_test(clean, hit):
                    yield _sse("delta", {"text": chunk})
                yield _sse("state", serialize_state(state))
                return
            try:
                async for chunk in digestion.digest_turn(
                        state, project, clean, model=app.state.model,
                        cwd=str(project), stream_fn=app.state.stream_fn):
                    yield _sse("delta", {"text": chunk})
            except Exception as e:  # surface, don't kill the stream silently
                # Redact before surfacing: a CLI/transport error string can echo
                # a path or key prefix, and it lands in the SSE stream + history.
                safe_err = redact_secrets(str(e))[0]
                # Probe auth only now (the rare failure path). If the harness
                # looks unauthenticated, the failure is almost certainly the
                # login — give the actionable hint instead of the raw error.
                hs = harness.harness_status(app.state.model) if real_harness else None
                if hs is not None and not hs.authenticated:
                    yield _sse("error", {"message":
                        f"Can't reach the agent harness — you may not be logged "
                        f"into {hs.agent}. Run `{hs.login_command}` in your "
                        f"terminal, then retry. (Details: {safe_err})"})
                else:
                    yield _sse("error", {"message": safe_err})
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
        from bobi.setup import services
        connected = services.venn_connected_names(project)
        # The real Venn catalog (live from the `venn` CLI when a key is present)
        # classifies each service as venn-backed vs custom — fetched once here.
        venn_catalog = services.live_venn_catalog(project)
        cards = services.cards_for(state.spec.services, project,
                                   connected=connected, catalog=venn_catalog)
        # User-defined custom MCP connections supersede their service card (a
        # service first guessed as "custom" becomes an MCP row once configured).
        # Match by CANONICAL key so a placeholder guessed as 'substack' is also
        # superseded by an MCP added as 'substack-mcp' — otherwise the divergent
        # slug leaves both a "needs connect" placeholder and the MCP row.
        user_mcp = state.spec.mcp_servers or {}
        mcp_canon = {services.canonical_service_key(k) for k in user_mcp}
        cards = [c for c in cards
                 if services.canonical_service_key(c["key"]) not in mcp_canon]
        for key, cfg in user_mcp.items():
            if isinstance(cfg, dict):
                cards.append(services.user_mcp_card(key, cfg, project))
        # The connector catalog (every known connector, for on-demand setup like
        # Slack as a chat channel) is env-aware too, so a just-saved token reads
        # as connected.
        catalog = services.cards_for(list(services.CATALOG.keys()), project,
                                     connected=connected, catalog=venn_catalog)
        # Whether a VENN_API_KEY is present — drives the panel's Venn row state.
        from bobi.setup import actions
        return {"cards": cards, "catalog": catalog,
                "venn_configured": bool(actions.venn_key(project))}

    # --- ingress: how public webhooks reach the event server ------------
    @app.post("/api/ingress/verify")
    def ingress_verify(payload: dict) -> JSONResponse:
        from datetime import datetime, timezone
        from bobi.config import DEFAULT_EVENT_SERVER
        mode = (payload.get("mode") or state.ingress.mode or "local").strip()
        url = (payload.get("url") or state.ingress.url or "").strip().rstrip("/")
        if mode == "bobi_cloud":
            url = DEFAULT_EVENT_SERVER
        if mode == "local":
            state.ingress.mode = "local"
            state.ingress.url = ""
            state.ingress.verified = True
            state.ingress.verified_at = datetime.now(timezone.utc).isoformat()
            state.ingress.error = ""
            _persist_ingress_env(project, state)
            state.save(project)
            return JSONResponse({"ok": True, "state": serialize_state(state)})
        if mode not in ("quick_tunnel", "bobi_cloud", "custom_worker"):
            return JSONResponse({"ok": False, "error": "unknown ingress mode"},
                                status_code=400)
        if not url:
            return JSONResponse({"ok": False, "error": "event server URL required"},
                                status_code=400)
        err = _validate_public_event_server_url(url)
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        ok, error = _probe_event_server(url)
        if ok:
            state.ingress.mode = mode
            state.ingress.url = url
            state.ingress.verified = True
            state.ingress.verified_at = datetime.now(timezone.utc).isoformat()
            state.ingress.error = ""
            _persist_ingress_env(project, state)
            state.save(project)
        return JSONResponse({"ok": ok, "error": error,
                             "state": serialize_state(state)},
                            status_code=200 if ok else 502)

    # --- automate (suggester + commit) ---------------------------------
    @app.post("/api/automate/suggest")
    async def automate_suggest() -> dict:
        from bobi.setup import automate
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

    # --- panel edits: role / automation / connection (deterministic) ----
    def _role_status(role: dict) -> str:
        """A role is 'complete' only once all four interview dimensions are
        filled; otherwise it's still in progress. Mirrors the digestion bar."""
        has_systems = bool(role.get("systems"))
        return "complete" if (role.get("responsibility")
                              and role.get("good_looks_like")
                              and has_systems and role.get("triggers")) else "in_progress"

    @app.post("/api/role/update")
    def role_update(payload: dict) -> JSONResponse:
        idx, fields = payload.get("index"), payload.get("fields")
        roles = state.spec.roles
        if (not isinstance(idx, int) or not (0 <= idx < len(roles))
                or not isinstance(fields, dict)):
            return JSONResponse({"error": "bad index or fields"}, status_code=400)
        role = dict(roles[idx]) if isinstance(roles[idx], dict) else {}
        for k in ("name", "responsibility", "good_looks_like", "triggers"):
            if isinstance(fields.get(k), str):
                role[k] = fields[k].strip()
        if "systems" in fields:
            sysv = fields["systems"]
            if isinstance(sysv, list):
                role["systems"] = [str(s).strip() for s in sysv if str(s).strip()]
            elif isinstance(sysv, str):
                role["systems"] = [t.strip() for t in sysv.split(",") if t.strip()]
        role["status"] = _role_status(role)
        roles[idx] = role
        # If every role is now complete, the slot reads "enough"; else step back.
        all_complete = bool(roles) and all(
            (r.get("status") if isinstance(r, dict) else "") == "complete"
            for r in roles)
        state.spec.readiness["roles"] = "enough" if all_complete else "thin"
        state.validated = False
        state.save(project)
        return JSONResponse(serialize_state(state))

    @app.post("/api/automation/update")
    def automation_update(payload: dict) -> JSONResponse:
        idx, fields = payload.get("index"), payload.get("fields")
        items = state.spec.autonomous
        if (not isinstance(idx, int) or not (0 <= idx < len(items))
                or not isinstance(fields, dict)):
            return JSONResponse({"error": "bad index or fields"}, status_code=400)
        item = dict(items[idx]) if isinstance(items[idx], dict) else {}
        for k in ("description", "cadence", "role", "command"):
            if isinstance(fields.get(k), str):
                item[k] = fields[k].strip()
        if fields.get("leash") in ("notify", "ask", "act"):
            item["leash"] = fields["leash"]
        if fields.get("trigger") in ("schedule", "event"):
            item["trigger"] = fields["trigger"]
        items[idx] = item
        state.spec.autonomous_confirmed = True
        state.validated = False
        state.save(project)
        return JSONResponse(serialize_state(state))

    @app.get("/api/workflow/yaml")
    def workflow_yaml(request: Request) -> JSONResponse:
        # Read-only preview of the YAML a spec workflow will be codified as
        # at build — shown in the Workflows card's dark-slab popup. Editing
        # happens through the conversation, not here.
        from bobi.setup import authoring
        try:
            idx = int(request.query_params.get("index", ""))
        except ValueError:
            return JSONResponse({"error": "bad index"}, status_code=400)
        wfs = state.spec.workflows
        if not (0 <= idx < len(wfs)) or not isinstance(wfs[idx], dict):
            return JSONResponse({"error": "no such workflow"}, status_code=404)
        wf = wfs[idx]
        path = f"workflows/{authoring.workflow_slug(wf)}.yaml"
        return JSONResponse({"path": path,
                             "yaml": authoring.build_workflow_yaml(state, wf)})

    @app.post("/api/service/remove")
    def service_remove(payload: dict) -> JSONResponse:
        from bobi.setup import services
        key = (payload.get("service_key") or "").strip().lower()
        if not key:
            return JSONResponse({"error": "service_key required"}, status_code=400)
        kept = []
        for s in state.spec.services:
            name = (s.get("name") if isinstance(s, dict) else str(s)) or ""
            rk = services.resolve(name).key if name.strip() else ""
            if name.strip().lower() == key or rk == key:
                continue
            kept.append(s)
        state.spec.services = kept
        # A user-defined MCP is also rendered from spec.mcp_servers (independent
        # of services), so dropping only the service leaves its row behind —
        # remove the matching mcp_servers entry too.
        if state.spec.mcp_servers:
            state.spec.mcp_servers = {
                k: v for k, v in state.spec.mcp_servers.items()
                if k.strip().lower() != key}
        # Drop a pending connection-test that targets the removed connection.
        if (state.pending_test or {}).get("key", "").strip().lower() == key:
            state.pending_test = {}
        state.validated = False
        state.save(project)
        return JSONResponse(serialize_state(state))

    @app.post("/api/mcp/detect")
    def mcp_detect_folder(payload: dict) -> JSONResponse:
        """Inspect a local folder and infer a stdio MCP launch recipe — command,
        args, and the env vars it needs — to prefill the add-connection form.
        Pure read-only static analysis: nothing is executed or installed, and no
        secret VALUES are read (only var names are surfaced)."""
        from bobi.setup import mcp_detect
        path = (payload.get("path") or "").strip()
        if not path:
            return JSONResponse({"ok": False, "error": "give a folder path"},
                                status_code=400)
        # Confine the scan to the home tree — same boundary as the folder picker
        # (/api/browse, /api/teams). The detector reads README/source/.env.example
        # under the path, so don't let it probe arbitrary filesystem locations.
        target, ok = _within_home(mcp_detect._clean_path_input(path), home)
        if not ok:
            return JSONResponse(
                {"ok": False, "error": "pick a folder inside your home directory"},
                status_code=400)
        result = mcp_detect.detect(str(target))
        return JSONResponse(result,
                            status_code=200 if result.get("ok") else 400)

    @app.post("/api/mcp/add")
    def mcp_add(payload: dict) -> JSONResponse:
        """Add a custom MCP connection — remote or local (the Claude-style
        connector form).

        - **Remote (http)**: name + remote URL. Auth is api_key (Bearer header)
          or none (public). Any key goes straight to .env as a `${VAR}` ref.
        - **Local (stdio)**: name + command (+ optional args + env var names).
          Each declared env var is captured to .env as a `${VAR}` ref and
          authored as `env: {VAR: ${VAR}}`; the command line never carries an
          inline secret.

        Persists to spec.mcp_servers and as a team service so it shows as a row;
        authored into agent.yaml mcp_servers: at build. (OAuth-authed MCPs
        aren't supported yet — a follow-up.)"""
        import re
        import shlex
        from bobi.setup import actions
        name = (payload.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "give the connection a name"},
                                status_code=400)
        key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "mcp"
        prefix = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "MCP"

        url = (payload.get("url") or "").strip()
        command = (payload.get("command") or "").strip()
        # stdio when explicitly chosen, or when a command (and no URL) is given.
        is_stdio = (payload.get("transport") == "stdio"
                    or (command and not url))

        if is_stdio:
            if not command:
                return JSONResponse(
                    {"error": "a command is required for a local MCP server"},
                    status_code=400)
            # args: accept a list or a single shell-style string.
            raw_args = payload.get("args")
            if isinstance(raw_args, str):
                try:
                    args = shlex.split(raw_args)
                except ValueError as e:
                    return JSONResponse({"error": f"bad args: {e}"},
                                        status_code=400)
            else:
                args = [str(a) for a in (raw_args or [])]
            # env: a list of {name, value?} (or bare names). Capture any value to
            # .env; record the var name so it's authored as a `${VAR}` ref.
            env_vars: list[str] = []
            try:
                for item in payload.get("env") or []:
                    if isinstance(item, dict):
                        var = (item.get("name") or "").strip()
                        val = item.get("value") or ""
                    else:
                        var, val = str(item).strip(), ""
                    if not var:
                        continue
                    if not re.match(r"^[A-Z][A-Z0-9_]*$", var):
                        return JSONResponse(
                            {"error": f"env var '{var}' must be "
                                      "UPPER_SNAKE_CASE"}, status_code=400)
                    if var not in env_vars:
                        env_vars.append(var)
                    if val:
                        actions.save_credential(state, project, var, name, "",
                                                prompt_fn=lambda *_: val)
            except actions.ActionError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            entry: dict = {"type": "stdio", "command": command, "args": args,
                           "env_vars": env_vars, "auth": "stdio", "label": name}
        else:
            if not (url.startswith("http://") or url.startswith("https://")):
                return JSONResponse(
                    {"error": "a remote server URL (https://…) or a local "
                              "command is required"},
                    status_code=400)
            # API key is the only supported auth; no key means a public server.
            auth = payload.get("auth") or "api_key"
            if auth not in ("none", "api_key"):
                return JSONResponse({"error": "auth must be none or api_key"},
                                    status_code=400)
            entry = {"url": url, "type": "http", "auth": auth, "label": name}
            try:
                if auth == "api_key":
                    var = f"{prefix}_API_KEY"
                    entry["secret_var"] = var
                    api_key = payload.get("api_key", "")
                    if api_key:
                        actions.save_credential(state, project, var, name, "",
                                                prompt_fn=lambda *_: api_key)
            except actions.ActionError as e:
                return JSONResponse({"error": str(e)}, status_code=400)

        # `replaces` is the key of the connection being edited — drop its old
        # entry so an edit (which may rename → re-key) overwrites in place rather
        # than leaving a stale duplicate.
        replaces = (payload.get("replaces") or "").strip().lower()
        mcps = dict(state.spec.mcp_servers or {})
        if replaces and replaces != key:
            mcps.pop(replaces, None)
        mcps[key] = entry
        state.spec.mcp_servers = mcps
        # Replace any pre-existing service that's really THIS connection (a bare
        # placeholder like 'substack' when the MCP is 'substack-mcp', or the row
        # being edited) with a single row keyed by the MCP, so we never show both
        # a placeholder and the MCP. Match canonically; keep unrelated services.
        from bobi.setup.services import canonical_service_key
        new_canon = canonical_service_key(key)

        def _svc_name(s):
            return ((s.get("name") if isinstance(s, dict) else str(s)) or "")

        kept = [s for s in state.spec.services
                if canonical_service_key(_svc_name(s)) != new_canon
                and _svc_name(s).strip().lower() != replaces]
        kept.append({"name": key})
        state.spec.services = kept
        # Editing a connection invalidates any pending test against it (and a
        # re-key would orphan it) — drop it so we never test a stale config.
        if (state.pending_test or {}).get("key") in {key, replaces}:
            state.pending_test = {}
        state.validated = False
        state.save(project)
        return JSONResponse({"ok": True, "state": serialize_state(state)})

    # --- Venn: verify key, list the account's MCPs, reconcile team picks --
    def _venn_servers_payload(key: str) -> dict:
        """Verify the key and return the FULL set of services available to this
        Venn account (not just connected ones) as plain names. A bad/unreachable
        key raises VennError → caller renders the modal's error state."""
        from bobi.venn import list_servers_verified
        servers = list_servers_verified(key)
        return {"ok": True, "servers": sorted(
            {s.server_name for s in servers if s.server_name})}

    @app.get("/api/venn/servers")
    def venn_servers() -> JSONResponse:
        """List the services available via the SAVED Venn key (re-opening an
        already-connected modal). ok:false with a message if it won't verify."""
        from bobi.setup.actions import venn_key
        from bobi.venn import VennError
        key = venn_key(project)
        if not key:
            return JSONResponse({"ok": False,
                                 "error": "No Venn API key saved yet."})
        try:
            return JSONResponse(_venn_servers_payload(key))
        except VennError as e:
            return JSONResponse({"ok": False, "error": str(e)})

    @app.post("/api/venn/connect")
    def venn_connect(payload: dict) -> JSONResponse:
        """Verify a PASTED key against Venn, and only persist it on success — so
        a bad key never flips Venn to 'connected'. Returns the available
        services on success, or ok:false + error for the modal's error state."""
        from bobi.setup import actions
        from bobi.venn import VennError
        key = (payload.get("key") or "").strip()
        if not key:
            return JSONResponse({"ok": False, "error": "Paste your Venn key."})
        try:
            data = _venn_servers_payload(key)
        except VennError as e:
            return JSONResponse({"ok": False, "error": str(e)})
        try:
            actions.save_credential(state, project, "VENN_API_KEY", "venn", "",
                                    prompt_fn=lambda *_: key)
        except actions.ActionError as e:
            return JSONResponse({"ok": False, "error": str(e)})
        data["state"] = serialize_state(state)
        return JSONResponse(data)

    @app.post("/api/venn/apply")
    def venn_apply(payload: dict) -> JSONResponse:
        """Reconcile the team's Venn services to exactly the toggled-on set. A
        Venn service is on the team iff its toggle is on: `servers` are the
        on-toggles, `available` is the full picker universe. Add on-toggles not
        present; remove available services that are off (untouched: non-Venn
        services and anything outside the picker universe). Idempotent."""
        from bobi.setup import services
        on = payload.get("servers")
        if not isinstance(on, list):
            return JSONResponse({"error": "servers must be a list"},
                                status_code=400)
        desired = {(str(s) or "").strip().lower() for s in on if str(s).strip()}
        universe = {(str(s) or "").strip().lower()
                    for s in (payload.get("available") or []) if str(s).strip()}
        universe |= desired   # a toggled-on name is part of the universe too
        kept, added, removed = [], [], []
        for s in state.spec.services:
            name = (s.get("name") if isinstance(s, dict) else str(s)) or ""
            nl = name.strip().lower()
            # Only reconcile VENN-backed services. Venn's catalog can include
            # names that resolve native here (slack/github/linear) — those must
            # never be removed by the Venn picker even if left untoggled.
            is_venn = services.resolve(name).kind == "venn" if name.strip() else False
            if is_venn and nl in universe and nl not in desired:
                removed.append(name)          # a Venn service toggled OFF
                continue
            kept.append(s)
        have = {((s.get("name") if isinstance(s, dict) else str(s)) or "")
                .strip().lower() for s in kept}
        for raw in on:
            name = (str(raw) or "").strip()
            if name and name.lower() not in have:
                kept.append({"name": name})
                have.add(name.lower())
                added.append(name)
        if added or removed:
            state.spec.services = kept
            state.validated = False
            state.save(project)
        return JSONResponse({"added": added, "removed": removed,
                             "state": serialize_state(state)})

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

    # --- slack finalization (the post-install next-steps screen) --------
    def _env_value(var: str) -> str:
        # Same precedence as runtime resolution (config.load_dotenv and
        # venn_key): an exported environment variable wins over .env.
        import os
        from bobi.setup import actions
        return os.environ.get(var) or actions.read_env(project).get(var, "")

    @app.post("/api/slack/channel")
    def slack_channel(payload: dict) -> JSONResponse:
        """Save the team's dedicated Slack channel as SLACK_CHANNELS in
        run/.env (the `channels:` scoping knob in agent.yaml resolves it).
        Accepts a channel ID (C…/G…) directly, or a #name resolved live via
        the saved bot token."""
        from bobi.setup import actions
        raw = (payload.get("channel") or "").strip()
        if not raw:
            return JSONResponse({"error": "enter a channel ID (C…) or #name"},
                                status_code=400)
        token = _env_value("SLACK_BOT_TOKEN")
        if token:
            # One code path: resolve_channel_id owns the ID-vs-name decision
            # (a literal C…/G…/D… ID passes through without a network call).
            from bobi.slack import resolve_channel_id
            try:
                value = resolve_channel_id(token, raw)
            except Exception as e:
                return JSONResponse(
                    {"error": actions.redact_secrets(str(e))[0]},
                    status_code=502)
        else:
            # No token yet: only a literal channel ID can be trusted verbatim
            # (same shape rule resolve_channel_id uses); a name needs the
            # token to look up — a bare word saved as-is would silently scope
            # the adapter to a channel that doesn't exist.
            import re as _re
            if raw.startswith("#") or not _re.fullmatch(r"[CGD][A-Z0-9]{6,}",
                                                        raw):
                return JSONResponse(
                    {"error": "save the Slack bot token first so the name "
                     "can be looked up — or paste the channel ID (C…)"},
                    status_code=400)
            value = raw
        try:
            actions.save_credential(state, project, "SLACK_CHANNELS", "slack",
                                    "", prompt_fn=lambda *_: value)
        except actions.ActionError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "channel": value,
                             "state": serialize_state(state)})

    @app.post("/api/slack/test")
    def slack_test() -> JSONResponse:
        """Post a test message to the saved channel — proves token + channel
        + membership end-to-end, from the finalize-Slack step."""
        from bobi.setup import actions
        token = _env_value("SLACK_BOT_TOKEN")
        if not token:
            return JSONResponse({"error": "no Slack bot token saved yet"},
                                status_code=400)
        channel = _env_value("SLACK_CHANNELS").split(",")[0].strip()
        if not channel:
            return JSONResponse({"error": "save a channel first"},
                                status_code=400)
        from bobi.slack import post_slack_message
        team = state.team_name or "your bobi team"
        try:
            post_slack_message(
                token, channel,
                f"Test message from bobi setup — {team} can post here. "
                "You're wired up.")
        except Exception as e:
            return JSONResponse({"error": actions.redact_secrets(str(e))[0]},
                                status_code=502)
        return JSONResponse({"ok": True, "channel": channel})

    @app.post("/api/shutdown")
    def shutdown() -> dict:
        """End the setup session: the page shows a static goodbye and the
        server process exits once this response is sent. The uvicorn server
        is attached by serve_local; absent (tests), this is a no-op."""
        state.save(project)
        server = getattr(app.state, "uvicorn_server", None)
        if server is not None:
            server.should_exit = True
        return {"ok": True}

    @app.get("/api/credential/value")
    def credential_value(request: Request) -> JSONResponse:
        # Copy-to-clipboard support: returns a saved credential value to the
        # local page so it can be copied without being shown. Loopback + nonce
        # only; the value already lives in plaintext in .env on this machine.
        import os
        from bobi.setup import actions
        var = request.query_params.get("var", "")
        val = actions.read_env(project).get(var) or os.environ.get(var, "")
        if not val:
            return JSONResponse({"error": "not set"}, status_code=404)
        return JSONResponse({"value": val})

    @app.post("/api/credential")
    def credential(payload: dict) -> JSONResponse:
        from bobi.setup import actions
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
            # The one hard floor, enforced here too (not just /api/advance)
            # so a direct build call can't author a placeholder team.
            if not state.spec.goal.strip():
                yield _sse("error", {"message": "tell bobi what the team "
                           "should do — the goal is still empty"})
                yield _sse("state", serialize_state(state))
                return
            from bobi.setup import authoring
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
        from bobi.setup import actions
        return actions.team_source_dir(project, state).resolve()

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

    @app.post("/api/reveal")
    def reveal() -> JSONResponse:
        # Open the team's source folder in the OS file manager. Safe because
        # the server is loopback-bound and nonce-guarded — it reveals a folder
        # on the same machine the user is running setup on.
        import subprocess
        import sys
        target = _pack_dir()
        if not target.is_dir():
            return JSONResponse({"error": "no folder yet"}, status_code=404)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            elif sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"ok": True, "path": str(target)})

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
        from bobi.setup import actions
        result = actions.validate_team(state, project)
        return {**result, "state": serialize_state(state)}

    @app.post("/api/install")
    def install() -> JSONResponse:
        from bobi.setup import actions
        try:
            result = actions.install_team(state, project)
        except actions.ActionError as e:
            return JSONResponse({"error": str(e),
                                 "state": serialize_state(state)},
                                status_code=409)
        return JSONResponse({**result, "state": serialize_state(state)})

    @app.post("/api/preflight")
    def preflight() -> dict:
        from bobi.setup import actions
        result = actions.run_preflight(project)
        return {"ok": result.ok, "report": result.format()}

    # --- finish / homepage ---------------------------------------------
    @app.post("/api/finish")
    def finish() -> dict:
        # Mark the current setup complete but keep the server alive: after
        # Finish the page goes to the homepage (a re-entrant hub) where the
        # user can open and edit any team. The process ends when they stop it.
        state.finished = True
        state.save(project)
        result = serialize_state(state)
        if on_finish is not None:
            # Hosted mode: launch the installed team and send the browser
            # back to the unified app. A launch failure never unwinds the
            # finish — the team is installed either way.
            try:
                result.update(on_finish() or {})
            except Exception as e:  # noqa: BLE001 — surfaced to the UI
                result["launch_error"] = str(e)
        return result

    @app.get("/api/home")
    def home_teams() -> dict:
        # The homepage's team list - same visibility rules as the intro, so a
        # team never shows on one screen and not the other.
        # NB: don't name this `home` — that shadows the `home` Path in this
        # scope and breaks every endpoint that closes over it (e.g. browse).
        return {"teams": visible_teams(),
                "library": str(library)}

    return app


# --- foreground launcher -------------------------------------------------

def serve(project: Path, *, model: str | None = None,
          resume: bool = False, open_browser: bool = True) -> int:
    """Run the setup web UI in the foreground until setup finishes or the
    user interrupts. Binds 127.0.0.1:0, hands the socket to uvicorn."""
    state = None
    if resume:
        state = SetupState.load(project)
        if state is None or state.finished:
            print("No setup in progress to resume — run `bobi setup <name>`.")
            return 1
    if state is None:
        SetupState.clear(project)
        state = SetupState()

    # The server stays alive after Finish (the page transitions to the team
    # hub, a re-entrant editor), so there's no finish-triggered shutdown — it
    # runs until the user interrupts it.
    rc = serve_local(
        lambda nonce: build_app(state, project, nonce=nonce, model=model),
        open_browser=open_browser,
        label="bobi setup",
        announce=lambda url:
            f"\n  bobi setup is running at {url}\n  (Ctrl-C to stop)\n",
    )

    if state.finished:
        SetupState.clear(project)
    return rc
