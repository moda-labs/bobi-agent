"""End-to-end Slack Socket Mode transport tests for Lane A (#808).

The real local event server connects to a loopback Slack REST stub and a TLS
WebSocket stub. The WebSocket certificate is signed by a per-test CA trusted
only through NODE_EXTRA_CA_CERTS. No live Slack credentials are involved.
"""

import json
import os
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from bobi.config import save_bubble_state
from bobi.events.server import _find_event_server_dir, _post_register, ensure_running
from bobi.events.signing import signed_request

APP_ID = "A_SOCKET_TEST"
APP_TOKEN = "xapp-test-socket-secret"
BOT_ID = "B_SOCKET_TEST"
BOT_TOKEN = "xoxb-test-bot-secret"
BOT_USER_ID = "U_SOCKET_BOT"
CHANNEL_ID = "C_SOCKET_TEST"
DM_CHANNEL_ID = "D_SOCKET_TEST"
HUMAN_USER_ID = "U_SOCKET_HUMAN"
TEAM_ID = "T_SOCKET_TEST"
TOPIC = f"slack:{TEAM_ID}:app:{APP_ID}"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _generate_tls_material(base):
    ca_key = base / "ca.key"
    ca_cert = base / "ca.pem"
    server_key = base / "server.key"
    server_csr = base / "server.csr"
    server_cert = base / "server.pem"
    server_ext = base / "server.ext"
    server_ext.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=DNS:localhost,IP:127.0.0.1\n"
    )

    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-days", "2", "-sha256", "-subj", "/CN=Bobi Test CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout", str(ca_key), "-out", str(ca_cert),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes",
            "-sha256", "-subj", "/CN=localhost",
            "-keyout", str(server_key), "-out", str(server_csr),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "openssl", "x509", "-req", "-in", str(server_csr),
            "-CA", str(ca_cert), "-CAkey", str(ca_key), "-CAcreateserial",
            "-days", "2", "-sha256", "-extfile", str(server_ext),
            "-out", str(server_cert),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return ca_cert, server_cert, server_key


SOCKET_STUB_JS = r"""
const fs = require("fs");
const http = require("http");
const https = require("https");
const { WebSocketServer } = require("ws");

const socketPort = Number(process.argv[2]);
const controlPort = Number(process.argv[3]);
const certPath = process.argv[4];
const keyPath = process.argv[5];
const appId = process.argv[6];

let activeSocket = null;
let nextConnection = 0;
const connections = [];
const acknowledgements = [];

const tlsServer = https.createServer({
  cert: fs.readFileSync(certPath),
  key: fs.readFileSync(keyPath),
});
const wss = new WebSocketServer({ noServer: true });

tlsServer.on("upgrade", (request, socket, head) => {
  wss.handleUpgrade(request, socket, head, (ws) => {
    wss.emit("connection", ws, request);
  });
});

wss.on("connection", (ws, request) => {
  const connection = ++nextConnection;
  activeSocket = ws;
  connections.push({ connection, path: request.url });
  const pingTimer = setInterval(() => {
    if (ws.readyState === 1) ws.ping();
  }, 500);
  ws.on("close", () => {
    clearInterval(pingTimer);
    if (activeSocket === ws) activeSocket = null;
  });
  ws.on("message", (raw) => {
    let message;
    try { message = JSON.parse(String(raw)); } catch { return; }
    if (typeof message.envelope_id === "string") {
      acknowledgements.push({
        connection,
        envelope_id: message.envelope_id,
        frame: String(raw),
      });
    }
  });
  ws.send(JSON.stringify({
    type: "hello",
    num_connections: 1,
    connection_info: { app_id: appId },
    debug_info: { host: "localhost" },
  }));
});

function respond(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

const control = http.createServer((req, res) => {
  let body = "";
  req.on("data", (chunk) => { body += chunk; });
  req.on("end", () => {
    if (req.method === "GET" && req.url === "/state") {
      return respond(res, 200, {
        connected: Boolean(activeSocket && activeSocket.readyState === 1),
        connections,
        acknowledgements,
      });
    }
    if (req.method === "POST" && req.url === "/frame") {
      if (!activeSocket || activeSocket.readyState !== 1) {
        return respond(res, 409, { error: "no active socket" });
      }
      let frame;
      try { frame = JSON.parse(body); } catch {
        return respond(res, 400, { error: "invalid frame" });
      }
      activeSocket.send(JSON.stringify(frame));
      return respond(res, 202, { ok: true });
    }
    return respond(res, 404, { error: "unknown route" });
  });
});

tlsServer.listen(socketPort, "127.0.0.1");
control.listen(controlPort, "127.0.0.1", () => console.log("stub ready"));
"""


class _SocketStub:
    def __init__(self, base, cert_path, key_path):
        self.socket_port = _free_port()
        self.control_port = _free_port()
        self._script = base / "slack_socket_stub.cjs"
        self._script.write_text(SOCKET_STUB_JS)
        self._cert_path = cert_path
        self._key_path = key_path
        self._proc: subprocess.Popen | None = None

    def start(self):
        env = dict(os.environ)
        env["NODE_PATH"] = str(_find_event_server_dir() / "node_modules")
        self._proc = subprocess.Popen(
            [
                "node", str(self._script), str(self.socket_port),
                str(self.control_port), str(self._cert_path),
                str(self._key_path), APP_ID,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                self.state()
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("Slack Socket Mode stub did not start")

    def stop(self):
        if not self._proc:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5)

    def connection_url(self, ticket: int) -> str:
        return f"wss://127.0.0.1:{self.socket_port}/socket/{ticket}"

    def state(self) -> dict:
        return httpx.get(
            f"http://127.0.0.1:{self.control_port}/state", timeout=5
        ).json()

    def wait_connected(self, minimum_connections: int = 1, timeout: float = 20) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.state()
            if state["connected"] and len(state["connections"]) >= minimum_connections:
                return state
            time.sleep(0.1)
        raise AssertionError(f"socket never connected: {self.state()}")

    def send_frame(self, payload: dict):
        response = httpx.post(
            f"http://127.0.0.1:{self.control_port}/frame",
            json=payload,
            timeout=5,
        )
        assert response.status_code == 202, response.text

    def send_envelope(self, payload: dict, timeout: float = 10) -> dict:
        envelope_id = payload["envelope_id"]
        before = sum(
            ack["envelope_id"] == envelope_id
            for ack in self.state()["acknowledgements"]
        )
        self.send_frame(payload)
        deadline = time.time() + timeout
        while time.time() < deadline:
            matching = [
                ack for ack in self.state()["acknowledgements"]
                if ack["envelope_id"] == envelope_id
            ]
            if len(matching) > before:
                return matching[-1]
            time.sleep(0.05)
        raise AssertionError(
            f"no acknowledgement for {envelope_id}: {self.state()}"
        )

    def request_refresh(self):
        self.send_frame({"type": "disconnect", "reason": "refresh_requested"})


class _SlackRestStub:
    def __init__(self, socket_stub: _SocketStub):
        self.port = _free_port()
        self.calls: list[dict] = []
        self.connection_urls: list[str] = []
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _respond(self, payload, status: int = 200):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _record(self, method: str):
                with stub._lock:
                    stub.calls.append({
                        "method": method,
                        "path": self.path,
                        "authorization": self.headers.get("Authorization", ""),
                    })

            def do_GET(self):
                self._record("GET")
                if self.path == "/auth.test":
                    if self.headers.get("Authorization") != f"Bearer {BOT_TOKEN}":
                        return self._respond({"ok": False, "error": "invalid_auth"})
                    return self._respond({
                        "ok": True,
                        "team_id": TEAM_ID,
                        "bot_id": BOT_ID,
                        "user_id": BOT_USER_ID,
                    })
                return self._respond({"ok": False, "error": "unknown_method"}, 404)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                self._record("POST")
                if self.path == "/apps.connections.open":
                    if self.headers.get("Authorization") != f"Bearer {APP_TOKEN}":
                        return self._respond({"ok": False, "error": "invalid_auth"}, 401)
                    with stub._lock:
                        ticket = len(stub.connection_urls) + 1
                        url = socket_stub.connection_url(ticket)
                        stub.connection_urls.append(url)
                    return self._respond({"ok": True, "url": url})
                return self._respond({"ok": False, "error": "unknown_method"}, 404)

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def apps_open_count(self) -> int:
        with self._lock:
            return sum(
                call["method"] == "POST" and call["path"] == "/apps.connections.open"
                for call in self.calls
            )


def _event_payload(event_id: str, event: dict) -> dict:
    return {
        "type": "event_callback",
        "team_id": TEAM_ID,
        "api_app_id": APP_ID,
        "event_id": event_id,
        "event_time": 1_753_200_000,
        "event": event,
    }


def _envelope(envelope_id: str, event_id: str, event: dict, **extra) -> dict:
    return {
        "type": "events_api",
        "envelope_id": envelope_id,
        "accepts_response_payload": False,
        "payload": _event_payload(event_id, event),
        **extra,
    }


def _mention_event(text: str, ts: str) -> dict:
    return {
        "type": "app_mention",
        "user": HUMAN_USER_ID,
        "channel": CHANNEL_ID,
        "channel_type": "channel",
        "text": text,
        "ts": ts,
    }


def _dm_event(text: str, ts: str, **extra) -> dict:
    return {
        "type": "message",
        "user": HUMAN_USER_ID,
        "channel": DM_CHANNEL_ID,
        "channel_type": "im",
        "text": text,
        "ts": ts,
        **extra,
    }


def _stop_event_server(project):
    pid_file = project / "state" / "event-server.pid"
    if not pid_file.exists():
        return
    try:
        os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
    except (ProcessLookupError, ValueError):
        pass


def _wait_for_health(es_url: str, timeout: float = 20) -> dict:
    last = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = httpx.get(f"{es_url}/health", timeout=5)
        last = response.json()
        entries = last.get("slack_socket", [])
        if any(
            entry.get("application_id") == APP_ID and entry.get("state") == "connected"
            for entry in entries
        ):
            return last
        time.sleep(0.1)
    raise AssertionError(f"Slack socket health never became connected: {last}")


@pytest.fixture(scope="module")
def slack_socket_env(tmp_path_factory):
    base = tmp_path_factory.mktemp("slack-socket-mode")
    project = base / "run"
    (project / "package").mkdir(parents=True)
    (project / "state").mkdir(parents=True)
    ca_cert, server_cert, server_key = _generate_tls_material(base)

    socket_stub = _SocketStub(base, server_cert, server_key)
    rest_stub = _SlackRestStub(socket_stub)
    socket_stub.start()
    rest_stub.start()

    port = _free_port()
    es_url = f"http://localhost:{port}"
    (project / "package" / "agent.yaml").write_text(
        f"entry_point: manager\nevent_server: {es_url}\n"
    )

    try:
        status = ensure_running(
            port,
            bind="127.0.0.1",
            project_path=project,
            extra_env={
                "BOBI_ES_SLACK_API_URL": f"http://127.0.0.1:{rest_stub.port}/",
                "NODE_EXTRA_CA_CERTS": str(ca_cert),
                "NODE_TLS_REJECT_UNAUTHORIZED": "1",
            },
        )
        assert status in ("started", "connected")

        minted = _post_register(es_url, "slack-socket-bootstrap", ["_bootstrap"])
        save_bubble_state(project, minted["bubble_id"], minted["bubble_key"])

        registration = {
            "workspace_id": TEAM_ID,
            "bot_token": BOT_TOKEN,
            "bot_id": BOT_ID,
            "bot_user_id": BOT_USER_ID,
            "app_id": APP_ID,
            "app_token": APP_TOKEN,
            "signing_secret": "slack-signing-test-secret",
        }
        response = signed_request(
            es_url,
            "POST",
            "/slack/workspaces",
            registration,
            minted["bubble_id"],
            minted["bubble_key"],
            timeout=10,
        )
        assert response.status_code == 200, response.text
        assert APP_TOKEN not in response.text

        socket_stub.wait_connected()
        _wait_for_health(es_url)

        yield SimpleNamespace(
            project=project,
            es_url=es_url,
            bubble=minted,
            socket=socket_stub,
            rest=rest_stub,
        )
    finally:
        _stop_event_server(project)
        rest_stub.stop()
        socket_stub.stop()


def _subscribe(env, name: str):
    response = signed_request(
        env.es_url,
        "POST",
        "/deployments",
        {"name": name, "subscriptions": [TOPIC]},
        env.bubble["bubble_id"],
        env.bubble["bubble_key"],
        timeout=10,
    )
    assert response.status_code == 201, response.text
    deployment = response.json()

    import websocket

    ws = websocket.create_connection(
        f"ws://{env.es_url.removeprefix('http://')}"
        f"/deployments/{deployment['deployment_id']}/subscribe",
        header=[f"Authorization: Bearer {deployment['api_key']}"],
        timeout=10,
    )
    hello = json.loads(ws.recv())
    assert hello["type"] == "connected"
    return ws


def _recv_event(ws, timeout: float = 10) -> dict:
    import websocket

    deadline = time.time() + timeout
    while time.time() < deadline:
        ws.settimeout(min(1, max(0.1, deadline - time.time())))
        try:
            message = json.loads(ws.recv())
        except websocket.WebSocketTimeoutException:
            continue
        if message.get("type") in ("event", "replay"):
            return message["data"]
    raise AssertionError("subscribed deployment received no event")


def _assert_no_event(ws, timeout: float = 1.5):
    import websocket

    deadline = time.time() + timeout
    while time.time() < deadline:
        ws.settimeout(min(0.25, max(0.05, deadline - time.time())))
        try:
            message = json.loads(ws.recv())
        except websocket.WebSocketTimeoutException:
            continue
        if message.get("type") in ("event", "replay"):
            raise AssertionError(f"unexpected downstream event: {message['data']}")


class TestSlackSocketConnection:
    def test_health_is_connected_and_secret_free(self, slack_socket_env):
        env = slack_socket_env
        response = httpx.get(f"{env.es_url}/health", timeout=5)
        health_text = response.text
        health = response.json()
        entries = {
            entry["application_id"]: entry for entry in health["slack_socket"]
        }

        assert entries[APP_ID]["state"] == "connected"
        assert {
            "last_event_at",
            "delivered_event_count",
            "connect_count",
            "reconnect_count",
        } <= entries[APP_ID].keys()
        assert APP_TOKEN not in health_text
        assert BOT_TOKEN not in health_text
        assert "wss://" not in health_text
        assert env.rest.apps_open_count() == 1


class TestSlackSocketDelivery:
    def test_wire_ack_precedes_observed_mention_delivery(self, slack_socket_env):
        env = slack_socket_env
        ws = _subscribe(env, "slack-socket-mention")
        try:
            ack = env.socket.send_envelope(_envelope(
                "env-mention",
                "Ev-mention",
                _mention_event("<@U_SOCKET_BOT> deploy please", "1753200000.000100"),
            ))
            # send_envelope returns only after the WSS stub observes the wire
            # ack. The downstream read therefore observes the required order.
            event = _recv_event(ws)
        finally:
            ws.close()

        assert ack["frame"] == '{"envelope_id":"env-mention"}'
        assert event["id"] == "Ev-mention"
        assert event["source"] == "slack"
        assert event["type"] == "slack.mention"
        assert event["text"] == "<@U_SOCKET_BOT> deploy please"
        assert event["topics"] == [TOPIC, f"{TOPIC}:{CHANNEL_ID}"]
        assert event["conversation"] == (
            f"slack:{TEAM_ID}:channel:{CHANNEL_ID}:thread:1753200000.000100"
        )

    def test_dm_delivers_through_the_existing_slack_pipeline(self, slack_socket_env):
        env = slack_socket_env
        ws = _subscribe(env, "slack-socket-dm")
        try:
            env.socket.send_envelope(_envelope(
                "env-dm",
                "Ev-dm",
                _dm_event("private status", "1753200001.000100"),
            ))
            event = _recv_event(ws)
        finally:
            ws.close()

        assert event["id"] == "Ev-dm"
        assert event["type"] == "slack.dm"
        assert event["topics"] == [TOPIC]
        assert event["conversation"] == (
            f"slack:{TEAM_ID}:dm:{DM_CHANNEL_ID}:thread:1753200001.000100"
        )

    def test_refresh_uses_a_fresh_url_preserves_dedup_and_keeps_delivery(
        self, slack_socket_env
    ):
        env = slack_socket_env
        ws = _subscribe(env, "slack-socket-refresh")
        first = _envelope(
            "env-across-refresh",
            "Ev-across-refresh",
            _mention_event("<@U_SOCKET_BOT> before refresh", "1753200002.000100"),
        )
        try:
            first_ack = env.socket.send_envelope(first)
            assert _recv_event(ws)["id"] == "Ev-across-refresh"

            before_state = env.socket.state()
            before_connections = len(before_state["connections"])
            before_open_calls = env.rest.apps_open_count()
            old_path = before_state["connections"][-1]["path"]

            env.socket.request_refresh()
            reconnected = env.socket.wait_connected(before_connections + 1)
            deadline = time.time() + 10
            while env.rest.apps_open_count() <= before_open_calls and time.time() < deadline:
                time.sleep(0.05)

            assert env.rest.apps_open_count() == before_open_calls + 1
            assert reconnected["connections"][-1]["path"] != old_path
            assert env.rest.connection_urls[-1] != env.rest.connection_urls[-2]

            duplicate = {**first, "retry_attempt": 1, "retry_reason": "timeout"}
            duplicate_ack = env.socket.send_envelope(duplicate)
            assert duplicate_ack["connection"] != first_ack["connection"]
            _assert_no_event(ws)

            env.socket.send_envelope(_envelope(
                "env-after-refresh",
                "Ev-after-refresh",
                _mention_event("<@U_SOCKET_BOT> after refresh", "1753200003.000100"),
            ))
            assert _recv_event(ws)["id"] == "Ev-after-refresh"
        finally:
            ws.close()

    def test_self_authored_bot_message_is_acked_and_filtered(self, slack_socket_env):
        env = slack_socket_env
        ws = _subscribe(env, "slack-socket-self-filter")
        try:
            ack = env.socket.send_envelope(_envelope(
                "env-self-bot",
                "Ev-self-bot",
                _dm_event(
                    "message from ourselves",
                    "1753200004.000100",
                    bot_id=BOT_ID,
                    user=BOT_USER_ID,
                ),
            ))
            assert ack["envelope_id"] == "env-self-bot"
            _assert_no_event(ws)
        finally:
            ws.close()


class TestUnsignedSlackSocketRegistration:
    def test_unsigned_registration_cannot_start_or_repoint_a_socket(
        self, slack_socket_env
    ):
        env = slack_socket_env
        opens_before = env.rest.apps_open_count()
        connections_before = len(env.socket.state()["connections"])

        for registration in [
            {
                "workspace_id": TEAM_ID,
                "bot_token": "xoxb-unsigned-replacement",
                "bot_id": BOT_ID,
                "bot_user_id": BOT_USER_ID,
                "app_id": APP_ID,
                "app_token": "xapp-unsigned-replacement",
            },
            {
                "workspace_id": "T_UNSIGNED_OTHER",
                "bot_token": "xoxb-unsigned-other",
                "bot_id": "B_UNSIGNED_OTHER",
                "bot_user_id": "U_UNSIGNED_OTHER",
                "app_id": "A_UNSIGNED_OTHER",
                "app_token": "xapp-unsigned-other",
            },
        ]:
            response = httpx.post(
                f"{env.es_url}/slack/workspaces", json=registration, timeout=10
            )
            assert response.status_code == 200, response.text
            assert registration["app_token"] not in response.text

        time.sleep(0.5)
        assert env.rest.apps_open_count() == opens_before
        state = env.socket.state()
        assert len(state["connections"]) == connections_before
        assert state["connected"] is True

        health = httpx.get(f"{env.es_url}/health", timeout=5).json()
        entries = {
            entry["application_id"]: entry for entry in health["slack_socket"]
        }
        assert entries[APP_ID]["state"] == "connected"
        assert "A_UNSIGNED_OTHER" not in entries
