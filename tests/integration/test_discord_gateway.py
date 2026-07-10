"""End-to-end integration tests for the Discord channel (#2).

Drives the REAL path with a stubbed Discord REST API (BOBI_ES_DISCORD_API_URL)
and a stubbed Gateway WebSocket server (a small Node script reusing the
event-server's `ws` dependency):

- the local event server's Gateway connection manager: GET /gateway/bot ->
  connect -> HELLO -> IDENTIFY -> READY, surfaced in /health,
- inbound MESSAGE_CREATE dispatch -> normalize -> grant-filtered delivery to
  a joined deployment's WebSocket,
- resume-first reconnect after a dropped socket (RESUME, not a fresh
  IDENTIFY, and delivery keeps working),
- the signed /discord/apps registration through the production client
  (register_discord_apps), which seeds both the bubble-scoped send credential
  and the discord resource grant,
- outbound `bobi reply` through the Discord REST API.

No live Discord credentials are involved anywhere.
"""

import json
import os
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from click.testing import CliRunner

from bobi.config import Config, ServiceConfig, save_bubble_state
from bobi.events.server import (
    _find_event_server_dir,
    _post_register,
    ensure_running,
    register_discord_apps,
)
from bobi.events.signing import signed_request

APP_ID = "111222333444555666"
BOT_USER = "999888777666555444"
BOT_TOKEN = "dc-gw-test-token"
CHANNEL = "888777666555444333"
CONV = f"discord:{APP_ID}:channel:{CHANNEL}"
MESSAGE_CONTENT_INTENT = 1 << 15
# A second app whose token the REST stub accepts on /applications/@me (so
# registration succeeds) but rejects with a 401 on /gateway/bot - the
# production bad-token signal that must park the connection as fatal.
BAD_APP_ID = "222333444555666777"
BAD_TOKEN = "dc-gw-revoked-token"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Minimal Discord Gateway stub. Node (with the event-server's `ws` package via
# NODE_PATH) because the Python test deps only ship a WebSocket client. The
# HTTP control plane lets the test inject dispatches and drop the socket.
GATEWAY_STUB_JS = """
const { WebSocketServer } = require("ws");
const http = require("http");

const wsPort = Number(process.argv[2]);
const controlPort = Number(process.argv[3]);
const BOT_USER = process.argv[4];
const APP_ID = process.argv[5];

let seq = 0;
let socket = null;
const state = { connected: false, identifies: [], resumes: [] };

const wss = new WebSocketServer({ port: wsPort, host: "127.0.0.1" });
wss.on("connection", (ws) => {
  socket = ws;
  state.connected = true;
  ws.on("close", () => {
    if (socket === ws) { socket = null; state.connected = false; }
  });
  ws.send(JSON.stringify({ op: 10, d: { heartbeat_interval: 500 } }));
  ws.on("message", (raw) => {
    const frame = JSON.parse(String(raw));
    if (frame.op === 1) ws.send(JSON.stringify({ op: 11 }));
    if (frame.op === 2) {
      state.identifies.push(frame.d);
      ws.send(JSON.stringify({ op: 0, t: "READY", s: ++seq, d: {
        session_id: "stub-session",
        resume_gateway_url: `ws://127.0.0.1:${wsPort}`,
        user: { id: BOT_USER },
        application: { id: APP_ID },
      }}));
    }
    if (frame.op === 6) {
      state.resumes.push(frame.d);
      ws.send(JSON.stringify({ op: 0, t: "RESUMED", s: ++seq, d: {} }));
    }
  });
});

const control = http.createServer((req, res) => {
  let body = "";
  req.on("data", (c) => { body += c; });
  req.on("end", () => {
    if (req.method === "POST" && req.url === "/dispatch") {
      if (!socket) { res.writeHead(409); return res.end("no connection"); }
      const { t, d } = JSON.parse(body);
      socket.send(JSON.stringify({ op: 0, t, d, s: ++seq }));
      res.writeHead(200); return res.end("ok");
    }
    if (req.method === "POST" && req.url === "/drop") {
      // A resumable close (non-1000, not a fatal code).
      if (socket) socket.close(4900, "stub drop");
      res.writeHead(200); return res.end("ok");
    }
    if (req.url === "/state") {
      res.writeHead(200, { "Content-Type": "application/json" });
      return res.end(JSON.stringify(state));
    }
    res.writeHead(404); res.end();
  });
});
control.listen(controlPort, "127.0.0.1", () => console.log("stub ready"));
"""


class _GatewayStub:
    """The Node Gateway stub process plus its HTTP control-plane client."""

    def __init__(self, tmp_path):
        self.ws_port = _free_port()
        self.control_port = _free_port()
        self._script = tmp_path / "discord_gateway_stub.cjs"
        self._script.write_text(GATEWAY_STUB_JS)
        self._proc: subprocess.Popen | None = None

    def start(self):
        es_dir = _find_event_server_dir()
        env = dict(os.environ)
        # The stub lives in tmp; resolve `ws` from the event-server install.
        env["NODE_PATH"] = str(es_dir / "node_modules")
        self._proc = subprocess.Popen(
            ["node", str(self._script), str(self.ws_port),
             str(self.control_port), BOT_USER, APP_ID],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                self.state()
                return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("gateway stub did not come up")

    def stop(self):
        if self._proc:
            self._proc.terminate()
            self._proc.wait(timeout=10)

    def state(self) -> dict:
        resp = httpx.get(
            f"http://127.0.0.1:{self.control_port}/state", timeout=5)
        return resp.json()

    def dispatch(self, t: str, d: dict):
        resp = httpx.post(
            f"http://127.0.0.1:{self.control_port}/dispatch",
            json={"t": t, "d": d}, timeout=5)
        assert resp.status_code == 200, resp.text

    def drop(self):
        assert httpx.post(
            f"http://127.0.0.1:{self.control_port}/drop",
            timeout=5).status_code == 200

    def wait_connected(self, timeout: float = 15) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.state()
            if state["connected"]:
                return state
            time.sleep(0.2)
        raise AssertionError(f"gateway never connected: {self.state()}")


class _RestStub:
    """Minimal Discord REST API stub recording every call it receives."""

    def __init__(self, gateway_ws_port: int):
        self.calls: list[dict] = []
        self.port = _free_port()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _respond(self, payload, status: int = 200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                stub.calls.append({
                    "method": "GET", "path": self.path,
                    "auth": self.headers.get("Authorization", ""),
                })
                bad = self.headers.get("Authorization", "") == f"Bot {BAD_TOKEN}"
                if self.path == "/applications/@me":
                    # The bad token still names ITS app (a revoked-after-
                    # registration token behaves this way in the stub: the
                    # registration passes, the Gateway bootstrap then 401s).
                    if bad:
                        return self._respond({"id": BAD_APP_ID, "name": "bobi-bad"})
                    return self._respond({"id": APP_ID, "name": "bobi-test"})
                if self.path == "/gateway/bot":
                    if bad:
                        return self._respond({"message": "401: Unauthorized"}, 401)
                    return self._respond({
                        "url": f"ws://127.0.0.1:{gateway_ws_port}",
                        "shards": 1,
                        "session_start_limit": {"total": 1000, "remaining": 999},
                    })
                return self._respond({"message": "unknown route"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = {"_raw": True}
                stub.calls.append({
                    "method": "POST", "path": self.path,
                    "auth": self.headers.get("Authorization", ""),
                    "body": body,
                })
                if self.path == f"/channels/{CHANNEL}/messages":
                    n = len(stub.sends())
                    return self._respond({"id": f"dmsg.{n}"})
                if self.path.endswith("/typing"):
                    self.send_response(204)
                    self.end_headers()
                    return None
                return self._respond({"message": "unknown route"}, 404)

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def sends(self) -> list[dict]:
        return [c for c in self.calls
                if c["path"] == f"/channels/{CHANNEL}/messages"
                and c["method"] == "POST"]


def _message_create(text: str, *, guild: bool = False,
                    mention_bot: bool = False, msg_id: str = "1300000000000000001") -> dict:
    d = {
        "id": msg_id,
        "channel_id": CHANNEL,
        "content": text,
        "author": {"id": "user-1", "username": "ada"},
        "mentions": [{"id": BOT_USER}] if mention_bot else [],
    }
    if guild:
        d["guild_id"] = "guild-1"
    return d


@pytest.fixture(scope="module")
def discord_env(tmp_path_factory):
    """A real local event server wired to stubbed Discord REST + Gateway, with
    a minted bubble and a signed app registration through the production
    client."""
    base = tmp_path_factory.mktemp("discord-gw")
    project = base / "run"
    (project / "package").mkdir(parents=True)
    (project / "state").mkdir(parents=True)

    gateway = _GatewayStub(base)
    gateway.start()
    rest = _RestStub(gateway.ws_port)
    rest.start()

    port = _free_port()
    es_url = f"http://localhost:{port}"
    (project / "package" / "agent.yaml").write_text(
        f"entry_point: manager\nevent_server: {es_url}\n")

    status = ensure_running(
        port,
        project_path=project,
        extra_env={
            "BOBI_ES_DISCORD_API_URL": f"http://127.0.0.1:{rest.port}/",
            "BOBI_ES_DISCORD_BOT_TOKEN": BOT_TOKEN,
            "BOBI_ES_DISCORD_APPLICATION_ID": APP_ID,
        },
    )
    assert status in ("started", "connected")

    minted = _post_register(es_url, "discord-bootstrap", ["_bootstrap"])
    save_bubble_state(project, minted["bubble_id"], minted["bubble_key"])

    # Production registration client: verifies the token against the (stubbed)
    # REST API, stores the send credential, writes the discord grant.
    cfg = Config(services=[ServiceConfig(name="discord", credentials={
        "bot_token": BOT_TOKEN, "application_id": APP_ID,
    })])
    registered = register_discord_apps(
        es_url, cfg, minted["bubble_id"], minted["bubble_key"])
    assert registered == [APP_ID]

    gateway.wait_connected()

    yield project, gateway, rest, es_url, minted

    gateway.stop()
    rest.stop()
    pid_file = project / "state" / "event-server.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass


@pytest.fixture
def discord(discord_env, monkeypatch):
    project, gateway, rest, es_url, bubble = discord_env
    monkeypatch.setenv("BOBI_ROOT", str(project))
    rest.calls.clear()
    return project, gateway, rest, es_url, bubble


def _subscribe_ws(es_url: str, deployment_id: str, api_key: str):
    import websocket

    ws = websocket.create_connection(
        f"ws://{es_url.removeprefix('http://')}"
        f"/deployments/{deployment_id}/subscribe",
        header=[f"Authorization: Bearer {api_key}"],
        timeout=10,
    )
    hello = json.loads(ws.recv())
    assert hello["type"] == "connected"
    return ws


def _recv_event(ws) -> dict:
    deadline = time.time() + 10
    while time.time() < deadline:
        msg = json.loads(ws.recv())
        if msg.get("type") in ("event", "replay"):
            return msg["data"]
    raise AssertionError("no event received")


class TestGatewayConnection:
    def test_identifies_without_the_privileged_message_content_intent(self, discord):
        _project, gateway, _rest, _es_url, _bubble = discord
        state = gateway.state()
        assert len(state["identifies"]) >= 1
        identify = state["identifies"][0]
        assert identify["token"] == BOT_TOKEN
        assert identify["shard"] == [0, 1]
        # MESSAGE_CONTENT is opt-in; identifying with it un-enabled would 4014.
        assert identify["intents"] & MESSAGE_CONTENT_INTENT == 0

    def test_health_surfaces_the_connection(self, discord):
        _project, _gateway, _rest, es_url, _bubble = discord
        health = httpx.get(f"{es_url}/health", timeout=5).json()
        entries = {e["application_id"]: e for e in health["discord_gateway"]}
        assert entries[APP_ID]["state"] == "connected"


class TestInboundDelivery:
    def test_dm_dispatch_delivers_to_granted_subscriber(self, discord):
        _project, gateway, _rest, es_url, bubble = discord
        # JOIN a deployment subscribed to the app's topic. Registration
        # succeeding proves the discord grant exists (#488 gate).
        resp = signed_request(
            es_url, "POST", "/deployments",
            {"name": "dc-inbound", "subscriptions": [f"discord:{APP_ID}"]},
            bubble["bubble_id"], bubble["bubble_key"], timeout=10,
        )
        assert resp.status_code == 201, resp.text
        dep = resp.json()

        ws = _subscribe_ws(es_url, dep["deployment_id"], dep["api_key"])
        try:
            gateway.dispatch("MESSAGE_CREATE", _message_create("hola bobi"))
            event = _recv_event(ws)
        finally:
            ws.close()

        assert event["type"] == "discord.dm"
        assert event["source"] == "discord"
        assert event["text"] == "hola bobi"
        assert event["conversation"] == f"discord:{APP_ID}:dm:{CHANNEL}"
        assert event["id"] == "1300000000000000001"

    def test_resume_after_drop_keeps_delivering(self, discord):
        _project, gateway, _rest, es_url, bubble = discord
        resp = signed_request(
            es_url, "POST", "/deployments",
            {"name": "dc-resume", "subscriptions": [f"discord:{APP_ID}"]},
            bubble["bubble_id"], bubble["bubble_key"], timeout=10,
        )
        assert resp.status_code == 201, resp.text
        dep = resp.json()

        resumes_before = len(gateway.state()["resumes"])
        gateway.drop()
        deadline = time.time() + 20
        while time.time() < deadline:
            state = gateway.state()
            if state["connected"] and len(state["resumes"]) > resumes_before:
                break
            time.sleep(0.2)
        state = gateway.state()
        # Resume-first: the reconnect must RESUME the session, never burn a
        # fresh IDENTIFY (budgeted at 1000/day).
        assert len(state["resumes"]) > resumes_before, state
        assert state["resumes"][-1]["session_id"] == "stub-session"

        ws = _subscribe_ws(es_url, dep["deployment_id"], dep["api_key"])
        try:
            gateway.dispatch("MESSAGE_CREATE", _message_create(
                "after resume", guild=True, mention_bot=True,
                msg_id="1300000000000000002"))
            event = _recv_event(ws)
        finally:
            ws.close()
        assert event["type"] == "discord.mention"
        assert event["conversation"] == f"discord:{APP_ID}:channel:{CHANNEL}"


class TestOutbound:
    def test_reply_sends_through_the_rest_api(self, discord):
        _project, _gateway, rest, _es_url, _bubble = discord

        from bobi.cli import main
        result = CliRunner().invoke(main, ["reply", CONV, "hola *back*"])
        assert result.exit_code == 0, result.output

        sends = rest.sends()
        assert len(sends) == 1
        assert sends[0]["auth"] == f"Bot {BOT_TOKEN}"
        assert sends[0]["body"] == {"content": "hola *back*"}


class TestFatalParking:
    # Runs last: it leaves a parked (fatal) connection in the shared server,
    # which is harmless to the good app but would be confusing mid-suite.
    def test_bad_token_parks_the_connection_as_fatal_in_health(self, discord):
        """A 401 from GET /gateway/bot is the production bad-token signal
        (the socket's 4004 close never happens when the bootstrap REST call
        already fails). The driver must park the connection as `fatal` -
        surfaced in /health, no backoff retry loop - and leave the healthy
        app's connection alone."""
        _project, _gateway, _rest, es_url, bubble = discord

        cfg = Config(services=[ServiceConfig(name="discord", credentials={
            "bot_token": BAD_TOKEN, "application_id": BAD_APP_ID,
        })])
        # Registration itself succeeds: the stub's /applications/@me accepts
        # the token (revoked-after-registration scenario).
        registered = register_discord_apps(
            es_url, cfg, bubble["bubble_id"], bubble["bubble_key"])
        assert registered == [BAD_APP_ID]

        entry = None
        deadline = time.time() + 15
        while time.time() < deadline:
            health = httpx.get(f"{es_url}/health", timeout=5).json()
            entries = {e["application_id"]: e
                       for e in health.get("discord_gateway", [])}
            entry = entries.get(BAD_APP_ID)
            if entry and entry["state"] == "fatal":
                break
            time.sleep(0.2)
        assert entry and entry["state"] == "fatal", entry
        assert "authentication failed" in entry["fatal_reason"]
        # The healthy app's connection is untouched.
        assert entries[APP_ID]["state"] == "connected"
