"""Tests for the `modastack ui` agent dashboard server — security, the agent
cards, and the blocking chat endpoint. Driven by Starlette's TestClient with
injected `registry_fn`/`deliver_fn`: no live team, no event server."""

import inspect

import pytest
from fastapi.testclient import TestClient

from modastack.agentui import server
from modastack.sdk import SessionEntry

TOKEN = "test-token-123"


def _entry(name, role="engineer", **kw):
    return SessionEntry(name=name, role=role, **kw)


def _registry(*entries):
    return lambda: list(entries)


def _testclient(app):
    # The Host guard only allows loopback; TestClient defaults to "testserver".
    return TestClient(app, base_url="http://127.0.0.1")


def _client(project, *, entries=(), deliver_fn=None, manager_name="", **kw):
    app = server.build_app(project, token=TOKEN, registry_fn=_registry(*entries),
                           deliver_fn=deliver_fn, manager_name=manager_name, **kw)
    c = _testclient(app)
    c.headers.update({server.TOKEN_HEADER: TOKEN})
    return c


@pytest.fixture
def project(tmp_path):
    return tmp_path


# --- security ------------------------------------------------------------

class TestSecurity:
    def test_api_requires_token(self, project):
        app = server.build_app(project, token=TOKEN, registry_fn=_registry())
        c = _testclient(app)
        assert c.get("/api/agents").status_code == 403
        c.headers.update({server.TOKEN_HEADER: "wrong"})
        assert c.get("/api/agents").status_code == 403
        c.headers.update({server.TOKEN_HEADER: TOKEN})
        assert c.get("/api/agents").status_code == 200

    def test_page_is_open_and_embeds_token(self, project):
        app = server.build_app(project, token=TOKEN, registry_fn=_registry())
        c = _testclient(app)
        r = c.get("/")            # no token header
        assert r.status_code == 200
        assert TOKEN in r.text
        assert "{{TOKEN}}" not in r.text

    def test_ping_is_guarded(self, project):
        app = server.build_app(project, token=TOKEN, registry_fn=_registry())
        c = _testclient(app)
        assert c.get("/api/ping").status_code == 403
        c.headers.update({server.TOKEN_HEADER: TOKEN})
        assert c.get("/api/ping").json() == {"ok": True}

    def test_host_guard_rejects_foreign_host(self, project):
        app = server.build_app(project, token=TOKEN, registry_fn=_registry())
        c = TestClient(app, base_url="http://evil.example.com")
        c.headers.update({server.TOKEN_HEADER: TOKEN})
        assert c.get("/api/agents").status_code == 403

    def test_static_path_traversal_404(self, project):
        c = _client(project)
        assert c.get("/static/../server.py").status_code == 404

    def test_static_serves_assets(self, project):
        c = _client(project)
        r = c.get("/static/app.js")
        assert r.status_code == 200
        assert "text/javascript" in r.headers["content-type"]


# --- agents (the cards) --------------------------------------------------

class TestAgents:
    def test_lists_manager_and_workers(self, project):
        mgr = _entry("moda-manager-proj", role="manager")
        w1 = _entry("eng-1-impl", role="engineer", title="Fix auth")
        w2 = _entry("eng-2-review", role="engineer")
        c = _client(project, entries=(mgr, w1, w2),
                    manager_name="moda-manager-proj")
        agents = c.get("/api/agents").json()["agents"]
        assert [a["name"] for a in agents] == \
            ["moda-manager-proj", "eng-1-impl", "eng-2-review"]
        flags = {a["name"]: a["is_manager"] for a in agents}
        assert flags == {"moda-manager-proj": True,
                         "eng-1-impl": False, "eng-2-review": False}

    def test_card_shape(self, project):
        e = _entry("eng-1", role="engineer", title="t", phase="implement",
                   status="running", model="claude-sonnet-4-6",
                   total_cost_usd=0.123456)
        c = _client(project, entries=(e,))
        card = c.get("/api/agents").json()["agents"][0]
        for k in ("name", "role", "title", "phase", "status", "model",
                  "total_cost_usd", "started_at", "last_activity", "is_manager"):
            assert k in card
        assert card["total_cost_usd"] == 0.1235   # rounded to 4dp

    def test_agent_detail_and_404(self, project):
        e = _entry("eng-1")
        c = _client(project, entries=(e,))
        assert c.get("/api/agents/eng-1").json()["name"] == "eng-1"
        assert c.get("/api/agents/nope").status_code == 404


# --- chat (blocking request/response) ------------------------------------

class TestChat:
    def test_chat_calls_deliver_with_right_args(self, project):
        calls = []

        def fake_deliver(to, text, sender="", wait=False, timeout=300):
            calls.append((to, text, sender, wait, timeout))
            return True, "the agent reply"

        c = _client(project, entries=(_entry("eng-1"),),
                    deliver_fn=fake_deliver, chat_timeout=42)
        r = c.post("/api/chat", json={"agent": "eng-1", "text": "hi there"})
        assert r.status_code == 200
        assert r.json() == {"reply": "the agent reply"}
        assert calls == [("eng-1", "hi there", "web-ui", True, 42)]

    def test_chat_empty_text_400(self, project):
        c = _client(project, entries=(_entry("eng-1"),),
                    deliver_fn=lambda *a, **k: (True, "x"))
        assert c.post("/api/chat",
                      json={"agent": "eng-1", "text": "  "}).status_code == 400

    def test_chat_unknown_agent_404(self, project):
        seen = []
        c = _client(project, entries=(_entry("eng-1"),),
                    deliver_fn=lambda *a, **k: seen.append(a) or (True, "x"))
        r = c.post("/api/chat", json={"agent": "ghost", "text": "hi"})
        assert r.status_code == 404
        assert seen == []   # never delivered to a non-active session

    def test_chat_deliver_failure_502(self, project):
        c = _client(project, entries=(_entry("eng-1"),),
                    deliver_fn=lambda *a, **k: (False, "session 'eng-1' is stopped"))
        r = c.post("/api/chat", json={"agent": "eng-1", "text": "hi"})
        assert r.status_code == 502
        assert r.json() == {"error": "session 'eng-1' is stopped"}

    def test_chat_persists_and_messages_endpoint_replays(self, project):
        c = _client(project, entries=(_entry("eng-1"),),
                    deliver_fn=lambda *a, **k: (True, "a reply"))
        # nothing persisted yet
        assert c.get("/api/agents/eng-1/messages").json() == {"messages": []}
        c.post("/api/chat", json={"agent": "eng-1", "text": "hello"})
        c.post("/api/chat", json={"agent": "eng-1", "text": "again"})
        msgs = c.get("/api/agents/eng-1/messages").json()["messages"]
        assert msgs == [
            {"role": "user", "text": "hello"},
            {"role": "agent", "text": "a reply"},
            {"role": "user", "text": "again"},
            {"role": "agent", "text": "a reply"},
        ]
        # survives a brand-new client (i.e. a browser refresh)
        c2 = _client(project, entries=(_entry("eng-1"),))
        assert len(c2.get("/api/agents/eng-1/messages").json()["messages"]) == 4

    def test_failed_chat_not_persisted(self, project):
        c = _client(project, entries=(_entry("eng-1"),),
                    deliver_fn=lambda *a, **k: (False, "stopped"))
        c.post("/api/chat", json={"agent": "eng-1", "text": "hello"})
        assert c.get("/api/agents/eng-1/messages").json() == {"messages": []}

    def test_messages_rejects_unsafe_name(self, project):
        c = _client(project)
        assert c.get("/api/agents/..%2f..%2fetc/messages").status_code == 404

    def test_chat_handler_is_sync(self):
        # A sync def lets FastAPI threadpool the blocking deliver(wait=True) so
        # it never stalls the event loop. Guard against an accidental async def.
        routes = {r.path: r for r in server.build_app(
            __import__("pathlib").Path("."), token=TOKEN,
            registry_fn=_registry()).routes if hasattr(r, "path")}
        assert not inspect.iscoroutinefunction(routes["/api/chat"].endpoint)
