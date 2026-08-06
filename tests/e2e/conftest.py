"""End-to-end browser tests for the bobi setup web UI (Playwright).

Like jobtack's e2e suite, but adapted to bobi's stack: instead of a Vercel
preview, each test boots the real FastAPI app in-process on a loopback port
with a **fake LLM source** (no Claude, no network), so the whole create flow
— Design conversation, Automate, Connect, Chat, the collapsed build, Done,
the file inspector — is deterministic and fast. Playwright drives a real
Chromium through the actual nonce + Host-guard security path.

Skips cleanly when Playwright isn't installed (the unit job doesn't need it).

`bobi_app` boots the OTHER local web UI the same way. It is here only so the
shared top bar can be asserted on both surfaces at once; the web app's own
coverage is a separate ticket.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import quote

import pytest

pytest.importorskip("playwright.sync_api")

import uvicorn  # noqa: E402

from bobi.setup.state import SetupState  # noqa: E402
from bobi.setup.webui import server  # noqa: E402

NONCE = "e2e-nonce"
APP_TOKEN = "e2e-app-token"
APP_AGENT = "topbar-e2e"


def _fake_llm():
    """Scripted stream source: digestion → reply + spec, suggester → ideas,
    authoring → markdown. Matches what the prompts ask for so the UI advances
    exactly as it would with a real model, minus the latency and nondeterminism.

    Digestion is keyword-driven over the WHOLE assembled context (which carries
    the recent messages), so triggers are cumulative across the conversation —
    e.g. once the user mentions automating something, the automations slot stays
    "enough" on later turns. This lets e2e drive the five-slot Finish gate.
    """
    async def fn(*, system_prompt, user_prompt, model, cwd):
        if "BOBI-SPEC" in system_prompt:                       # digestion
            ctx = (user_prompt or "").lower()
            deltas = {
                "goal": "Triage incoming GitHub issues and route each to an owner.",
                "roles": [{"name": "triager",
                           "responsibility": "classify and route new issues"}],
                "services": [{"name": "github"}],
            }
            readiness = {"goal": "enough", "roles": "enough",
                         "autonomous": "empty", "services": "enough"}
            payload = {"deltas": deltas, "summary": "A GitHub issue-triage team.",
                       "readiness": readiness,
                       "suggestions": ["Also post a daily digest",
                                       "Flag urgent issues first"]}
            # Venn-backed services appear only once the user implies them.
            if "email" in ctx or "calendar" in ctx:
                deltas["services"] += [{"name": "email"}, {"name": "calendar"}]
            # Workflows settle once the user describes (or declines) a flow.
            if "workflow" in ctx or "lifecycle" in ctx:
                deltas["workflows"] = [{
                    "name": "issue-lifecycle",
                    "description": "Triage each new issue and open a PR.",
                    "trigger": "a new GitHub issue lands",
                    "steps": [
                        {"name": "triage", "role": "triager",
                         "prompt": "Classify the issue.", "hitl": False},
                        {"name": "fix", "role": "triager",
                         "prompt": "Implement a fix.", "hitl": True},
                    ]}]
                payload["workflows_confirmed"] = True
            # Automations settle once the user weighs in on proactive behavior.
            if any(k in ctx for k in ("automat", "proactive", "stale",
                                      "on its own", "nothing")):
                deltas["autonomous"] = [{"description": "Flag stale PRs",
                                         "leash": "notify", "cadence": "1d"}]
                readiness["autonomous"] = "enough"
                payload["autonomous_confirmed"] = True
            # Chat interface settles when they say how they'll reach the team.
            if "slack" in ctx:
                deltas["chat"] = "slack"
            elif any(k in ctx for k in ("cli", "terminal", "command line")):
                deltas["chat"] = "cli"
            reply = ("Got it — a GitHub issue-triage team that routes each issue "
                     "to the right owner.")
            yield reply + "\n===BOBI-SPEC===\n" + json.dumps(payload)
        elif "proactive behaviors" in system_prompt:            # automate suggester
            yield json.dumps([{"description": "Flag stale PRs idle 48h",
                               "leash": "notify", "cadence": "1d",
                               "rationale": "keeps reviews moving"}])
        else:                                                   # file authoring
            yield "# Generated\n\nYou triage incoming GitHub issues.\n"
    return fn


@pytest.fixture(autouse=True)
def _isolate_env():
    """Credential capture writes straight to os.environ (so the running setup
    session picks it up). The e2e server runs in-process, so without this those
    writes would leak across tests — a saved GITHUB_TOKEN would make a later
    test see GitHub as already connected. Snapshot and restore around each test.
    """
    import os
    snap = dict(os.environ)
    for v in ("GITHUB_TOKEN", "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET",
              "LINEAR_API_KEY", "VENN_API_KEY"):
        os.environ.pop(v, None)
    yield
    os.environ.clear()
    os.environ.update(snap)


class _Bobi:
    """A booted setup server: the page URL, its home/project dirs, and a stop()
    the test can call to simulate the server dying mid-session."""
    def __init__(self, url, home, project, srv, thread):
        self.url = url
        self.home = home
        self.project = project
        self._srv = srv
        self._thread = thread

    def stop(self):
        self._srv.should_exit = True
        self._thread.join(timeout=5)


def _serve(app):
    """Run `app` under uvicorn on a free loopback port in a daemon thread.

    Returns (base_url, server, thread) once the port answers. Shared by both
    surfaces so the setup UI and the web app boot the same way.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    srv.install_signal_handlers = lambda: None      # we're off the main thread
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(200):                            # wait until it answers
        try:
            urllib.request.urlopen(base + "/", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)
    return base, srv, thread


@pytest.fixture
def bobi(tmp_path):
    """Boot the setup server with a fake LLM on a free loopback port; yield a
    _Bobi handle. Torn down after the test."""
    home = tmp_path / "home"
    os.environ["BOBI_HOME"] = str(home)

    project = home / "agents" / "setup-e2e" / "run"
    project.mkdir(parents=True)
    (project / "workspace").mkdir()
    subprocess.run(["git", "init"], cwd=project, capture_output=True)

    # A stand-in BOBI_HOME so the agent-source library and folder picker stay
    # off the real filesystem. A couple of real subfolders give the picker
    # something to browse - dotfiles are hidden.
    (home / "agents").mkdir(parents=True, exist_ok=True)
    (home / "projects").mkdir()

    app = server.build_app(SetupState(), project, nonce=NONCE,
                           stream_fn=_fake_llm(), home_root=home)
    base, srv, thread = _serve(app)

    handle = _Bobi(f"{base}/?n={NONCE}", home, project, srv, thread)
    yield handle
    handle.stop()


@pytest.fixture
def bobi_url(bobi):
    """The page URL alone — what most tests need."""
    return bobi.url


class _BobiApp:
    """A booted `bobi app` web UI: the dashboard URL, plus the route of the one
    installed agent (the web app's only view that fills the back slot)."""
    def __init__(self, url, agent, srv, thread):
        self.url = url
        self.agent = agent
        self.agent_url = f"{url}#/agents/{agent}"
        self._srv = srv
        self._thread = thread

    def stop(self):
        self._srv.should_exit = True
        self._thread.join(timeout=5)


# --- the web app (`bobi app`) ----------------------------------------------
#
# A different server over a different tree from the setup fixture above.
# `bobi.webapp.server.build_app` resolves the target agent from the request
# path on every call, so it serves a whole `$BOBI_HOME/agents/` tree rather
# than the one project dir setup is bound to. `bobi_install`
# (tests/conftest.py, inherited here by conftest scoping) seeds exactly that
# tree with one installed agent, but boots no server. The web app's e2e
# fixture is the composition of the two.

WEBAPP_TOKEN = "e2e-webapp-token"


def _free_port() -> int:
    """A loopback port nothing is listening on."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _WebApp:
    """A booted `bobi app` server over a seeded BOBI_HOME.

    Carries the page URL, the install it serves, and the one knob the agent
    page's state tri-state needs: a manager that is really there.
    """

    def __init__(self, base, install, srv, thread):
        self.base = base
        self.install = install
        self.agent = install.agent_name
        self._srv = srv
        self._thread = thread
        self._procs = []

    # The SPA is hash-routed, so every route below is the same page load.
    @property
    def url(self) -> str:
        return self.base + "/#/"

    def agent_url(self, name: str | None = None) -> str:
        return self.base + "/#/agents/" + quote(name or self.agent, safe="")

    def run_manager(self, *, responsive: bool = True) -> int:
        """Make this agent read as `running` (or, wedged, `not_responding`).

        `webapp/health.py` asks two real questions: is the recorded pid alive,
        and does the manager's health endpoint answer. So this writes a pidfile
        for a process that really is alive and, for the wedged case, a port
        file for a port that really is closed. Nothing is stubbed: the state
        comes out of the same fold the strip renders in production.
        """
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"])
        self._procs.append(proc)
        state = self.install.state_dir
        (state / "manager.pid").write_text(str(proc.pid))
        port_file = state / "manager-health.port"
        if responsive:
            # No port file at all: `probe()` returns "never asked", which is
            # RUNNING on the pidfile view. A manager without a health server
            # must not read as wedged.
            port_file.unlink(missing_ok=True)
        else:
            port_file.write_text(str(_free_port()))
        return proc.pid

    def stop(self):
        """Kill the server (and any stand-in manager). Idempotent: a test
        that stops the server to watch the page notice is torn down again."""
        while self._procs:
            proc = self._procs.pop()
            proc.kill()
            proc.wait(timeout=5)
        self._srv.should_exit = True
        self._thread.join(timeout=5)


@pytest.fixture
def bobi_app(tmp_path):
    """Boot the `bobi app` web UI - the OTHER surface sharing chrome.css -
    against an isolated BOBI_HOME holding one installed agent.

    Only the shared top bar is under test from here; the web app's own
    coverage is a separate ticket. The setup server takes its home as an
    explicit `home_root`, so both fixtures can be alive at once without
    fighting over BOBI_HOME (which only this surface reads per request).
    """
    from bobi.webapp import server as app_server

    home = tmp_path / "apphome"
    pkg = home / "agents" / APP_AGENT / "run" / "package"
    pkg.mkdir(parents=True)
    (pkg / "agent.yaml").write_text(
        f"agent: {APP_AGENT}\nversion: 0.1.0\nentry_point: lead\n")
    (pkg / "agent.md").write_text(f"# {APP_AGENT}\n\nWatch the repo.\n")
    os.environ["BOBI_HOME"] = str(home)

    base, srv, thread = _serve(app_server.build_app(token=APP_TOKEN))
    handle = _BobiApp(base + "/", APP_AGENT, srv, thread)
    yield handle
    handle.stop()


@pytest.fixture
def webapp(bobi_install):
    """Boot the web app over `bobi_install`'s seeded home on a free loopback
    port; yield a _WebApp handle. Torn down after the test."""
    from bobi.webapp import server as webapp_server

    app = webapp_server.build_app(token=WEBAPP_TOKEN)

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning")
    srv = uvicorn.Server(config)
    srv.install_signal_handlers = lambda: None      # we're off the main thread
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(200):                            # wait until it answers
        try:
            urllib.request.urlopen(base + "/", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)

    handle = _WebApp(base, bobi_install, srv, thread)
    yield handle
    handle.stop()
