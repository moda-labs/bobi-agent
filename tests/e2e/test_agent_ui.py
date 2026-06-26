"""E2E: drive the bobi agent UI (`bobi ui`) in a real browser.

Like the setup e2e, but for the running-team dashboard: boots
``agentui.server.build_app`` in-process on a loopback port with a **fake
registry + a canned deliver** (no Claude, no event server, no Fly), so the
roster → click → chat → markdown → persisted-history flow is deterministic.
Playwright drives real Chromium through the actual token + Host-guard path.

Skips cleanly when Playwright isn't installed (the unit job doesn't need it).
"""

from __future__ import annotations

import socket
import threading
import time
import types
import urllib.request

import pytest

pytest.importorskip("playwright.sync_api")

import uvicorn  # noqa: E402
from playwright.sync_api import expect  # noqa: E402

from bobi.agentui import server as ui_server  # noqa: E402
from bobi.sdk import SessionEntry  # noqa: E402

TOKEN = "e2e-token"
# A reply that exercises the markdown renderer (bold, list, inline code).
REPLY = "Here's the plan:\n\n- **Triage** the issue\n- Add `tests`\n\nThen ship."


def _entries():
    return [
        SessionEntry(name="bobi-manager-acme", role="manager",
                     title="Coordinating the team", status="running",
                     model="claude-opus-4-8", total_cost_usd=0.5),
        SessionEntry(name="eng-1-impl", role="engineer",
                     title="Add OAuth rotation", status="running",
                     model="claude-sonnet-4-6"),
    ]


def _deliver(to, text, sender="", wait=False, timeout=300):
    return True, REPLY


@pytest.fixture
def agent_ui(tmp_path):
    """Boot the agent UI with a fake registry + deliver on a free loopback port."""
    project = tmp_path / "project"
    project.mkdir()
    app = ui_server.build_app(project, token=TOKEN, registry_fn=_entries,
                              deliver_fn=_deliver, manager_name="bobi-manager-acme")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    srv.install_signal_handlers = lambda: None       # off the main thread
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            urllib.request.urlopen(base + "/", timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)

    yield types.SimpleNamespace(url=base + "/", project=project)
    srv.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def agent_ui_url(agent_ui):
    return agent_ui.url


def _open_manager(page, url):
    page.goto(url)
    expect(page.locator(".card")).to_have_count(2)
    page.locator(".card", has_text="bobi-manager-acme").click()
    expect(page.locator("#chat-name")).to_have_text("bobi-manager-acme")


# --- tests ---------------------------------------------------------------

def test_roster_lists_agents(page, agent_ui_url):
    page.goto(agent_ui_url)
    expect(page.locator("#subtitle")).to_have_text("2 agents")
    expect(page.locator(".card")).to_have_count(2)
    # the manager card carries the MGR badge
    mgr = page.locator(".card", has_text="bobi-manager-acme")
    expect(mgr.locator(".badge")).to_have_text("mgr")
    # before selecting anything, the main pane shows the placeholder
    expect(page.locator("#placeholder")).to_be_visible()
    expect(page.locator("#chat")).to_be_hidden()


def test_click_opens_chat_and_renders_markdown(page, agent_ui_url):
    _open_manager(page, agent_ui_url)
    page.fill("#input", "what's the plan?")
    page.click("#send")
    body = page.locator(".msg.agent .body")
    # markdown rendered, not literal: bold, list item, inline code all present
    expect(body.locator("strong")).to_have_text("Triage")
    expect(body.locator("li")).to_have_count(2)
    expect(body.locator("code")).to_have_text("tests")
    # and the literal asterisks are gone
    expect(body).not_to_contain_text("**Triage**")


def test_history_persists_across_reload(page, agent_ui_url):
    _open_manager(page, agent_ui_url)
    page.fill("#input", "show me the plan")
    page.click("#send")
    expect(page.locator(".msg.agent .body strong")).to_have_text("Triage")

    # A full reload drops the browser's in-memory history; selecting the agent
    # must reload it from disk (webui-chat.jsonl).
    page.reload()
    page.locator(".card", has_text="bobi-manager-acme").click()
    expect(page.locator(".msg")).to_have_count(2)
    expect(page.locator(".msg.user .body")).to_have_text("show me the plan")
    expect(page.locator(".msg.agent .body strong")).to_have_text("Triage")
