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
    """
    async def fn(*, system_prompt, user_prompt, model, cwd):
        if "BOBBI-SPEC" in system_prompt:                       # digestion
            reply = ("Got it — a team that triages incoming GitHub issues and "
                     "routes each to the right owner.")
            payload = {
                "deltas": {
                    "goal": "Triage incoming GitHub issues and route each to an owner.",
                    "roles": [{"name": "triager",
                               "responsibility": "classify and route new issues"}],
                    "services": [{"name": "github"}],
                },
                "summary": "A GitHub issue-triage team.",
                "readiness": {"goal": "enough", "roles": "thin",
                              "autonomous": "empty", "services": "thin"},
            }
            yield reply + "\n===BOBBI-SPEC===\n" + json.dumps(payload)
        elif "proactive behaviors" in system_prompt:            # automate suggester
            yield json.dumps([{"description": "Flag stale PRs idle 48h",
                               "leash": "notify", "cadence": "1d",
                               "rationale": "keeps reviews moving"}])
        else:                                                   # file authoring
            yield "# Generated\n\nYou triage incoming GitHub issues.\n"
    return fn


@pytest.fixture
def bobbi_url(tmp_path):
    """Boot the setup server with a fake LLM on a free loopback port; yield
    the page URL (nonce in the query string). Torn down after the test."""
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init"], cwd=project, capture_output=True)

    app = server.build_app(SetupState(), project, nonce=NONCE,
                           stream_fn=_fake_llm())

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

    yield f"{base}/?n={NONCE}"

    srv.should_exit = True
    thread.join(timeout=5)
