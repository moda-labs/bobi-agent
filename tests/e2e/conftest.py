"""End-to-end browser tests for the bobbi setup web UI (Playwright).

Like jobtack's e2e suite, but adapted to bobbi's stack: instead of a Vercel
preview, each test boots the real FastAPI app in-process on a loopback port
with a **fake LLM source** (no Claude, no network), so the whole create flow
— Design conversation, Automate, Connect, Chat, the collapsed build, Done,
the file inspector — is deterministic and fast. Playwright drives a real
Chromium through the actual nonce + Host-guard security path.

Skips cleanly when Playwright isn't installed (the unit job doesn't need it).
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import urllib.request

import pytest

pytest.importorskip("playwright.sync_api")

import uvicorn  # noqa: E402

from modastack.setup.state import SetupState  # noqa: E402
from modastack.setup.webui import server  # noqa: E402

NONCE = "e2e-nonce"


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
        if "BOBBI-SPEC" in system_prompt:                       # digestion
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
            yield reply + "\n===BOBBI-SPEC===\n" + json.dumps(payload)
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


class _Bobbi:
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


@pytest.fixture
def bobbi(tmp_path):
    """Boot the setup server with a fake LLM on a free loopback port; yield a
    _Bobbi handle. Torn down after the test."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "workspace").mkdir()
    subprocess.run(["git", "init"], cwd=project, capture_output=True)

    # A stand-in home so the ~/bobbi-agents library and the folder picker stay
    # off the real filesystem. A couple of real subfolders give the picker
    # (now rooted at home) something to browse — dotfiles are hidden.
    home = tmp_path / "home"
    (home / "bobbi-agents").mkdir(parents=True)
    (home / "projects").mkdir()

    app = server.build_app(SetupState(), project, nonce=NONCE,
                           stream_fn=_fake_llm(), home_root=home)

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

    handle = _Bobbi(f"{base}/?n={NONCE}", home, project, srv, thread)
    yield handle
    handle.stop()


@pytest.fixture
def bobbi_url(bobbi):
    """The page URL alone — what most tests need."""
    return bobbi.url
