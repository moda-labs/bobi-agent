"""Local event server daemon — FastAPI app with WebSocket subscriptions.

Mirrors the Cloudflare Workers event server API so the same
``EventServerClient`` code works against either backend. Receives
webhooks (GitHub, Linear, Slack), routes events to subscribed
deployments via in-memory buffers, and streams them over WebSocket.

Run standalone:
    python -m modastack.manager.events.event_server --port 8080

Or start as a daemon via the CLI:
    modastack event-server start
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory state (replaces KV + Durable Objects)
# ---------------------------------------------------------------------------


@dataclass
class Deployment:
    id: str
    name: str
    api_key: str
    subscriptions: list[str]
    next_seq: int = 1
    event_buffer: deque = field(default_factory=lambda: deque(maxlen=10000))
    websockets: set = field(default_factory=set)


_deployments: dict[str, Deployment] = {}
_api_key_index: dict[str, str] = {}
_subscription_index: dict[str, set[str]] = {}
_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="modastack-event-server", docs_url=None, redoc_url=None)

# Secrets are set at startup via app.state
app.state.webhook_secret = ""
app.state.slack_signing_secret = ""


def _get_webhook_secret() -> str:
    return getattr(app.state, "webhook_secret", "") or ""


def _get_slack_signing_secret() -> str:
    return getattr(app.state, "slack_signing_secret", "") or ""


# ---------------------------------------------------------------------------
# Event routing
# ---------------------------------------------------------------------------


async def _route_event(event_data: dict) -> int:
    """Route a normalized event to all subscribed deployments.

    Returns the number of deployments the event was delivered to.
    """
    keys: list[str] = []
    if event_data.get("repo"):
        keys.append(event_data["repo"])
    if event_data.get("team_key"):
        keys.append(f"linear:{event_data['team_key']}")
    if event_data.get("workspace"):
        keys.append(f"slack:{event_data['workspace']}")

    deployment_ids: set[str] = set()
    async with _lock:
        for key in keys:
            deployment_ids.update(_subscription_index.get(key, set()))

    delivered = 0
    for dep_id in deployment_ids:
        dep = _deployments.get(dep_id)
        if not dep:
            continue

        # Each deployment gets its own seq
        event_copy = dict(event_data)
        event_copy["seq"] = dep.next_seq
        dep.next_seq += 1
        dep.event_buffer.append(event_copy)
        delivered += 1

        msg = json.dumps({"type": "event", "data": event_copy})
        dead: set = set()
        for ws in dep.websockets:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        dep.websockets -= dead

    return delivered


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok", "mode": "local", "deployments": len(_deployments)}


# ---------------------------------------------------------------------------
# Webhook routes
# ---------------------------------------------------------------------------


@app.post("/webhooks/github")
async def github_webhook(request: Request):
    body_bytes = await request.body()
    if not body_bytes:
        raise HTTPException(400, "empty body")

    # Signature verification
    secret = _get_webhook_secret()
    if secret:
        sig_header = request.headers.get("x-hub-signature-256", "")
        if not sig_header:
            raise HTTPException(401, "missing signature")
        expected = hmac.new(
            secret.encode(), body_bytes, hashlib.sha256,
        ).hexdigest()
        received = sig_header.removeprefix("sha256=")
        if not hmac.compare_digest(expected, received):
            raise HTTPException(401, "invalid signature")

    try:
        payload = json.loads(body_bytes)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(400, "invalid JSON")

    event_header = request.headers.get("x-github-event", "unknown")
    delivery_id = request.headers.get("x-github-delivery", str(uuid.uuid4()))
    repo_full_name = (payload.get("repository") or {}).get("full_name", "")
    installation_id = (payload.get("installation") or {}).get("id")

    if not repo_full_name:
        raise HTTPException(400, "no repository in payload")

    normalized = {
        "id": delivery_id,
        "source": "github",
        "type": f"github.{event_header}",
        "repo": repo_full_name,
        "installation_id": installation_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "payload": payload,
    }

    delivered = await _route_event(normalized)
    return {"delivered_to": delivered}


@app.post("/webhooks/linear")
async def linear_webhook(request: Request):
    body_bytes = await request.body()
    if not body_bytes:
        raise HTTPException(400, "empty body")

    try:
        payload = json.loads(body_bytes)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(400, "invalid JSON")

    action = payload.get("action", "unknown")
    data_type = payload.get("type", "unknown")
    team_key = ((payload.get("data") or {}).get("team") or {}).get("key", "")

    normalized = {
        "id": str(uuid.uuid4()),
        "source": "linear",
        "type": f"linear.{data_type}.{action}",
        "team_key": team_key,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "payload": payload,
    }

    delivered = await _route_event(normalized)
    return {"delivered_to": delivered}


@app.post("/webhooks/slack")
async def slack_webhook(request: Request):
    body_bytes = await request.body()
    if not body_bytes:
        raise HTTPException(400, "empty body")

    # Reject retries
    if request.headers.get("x-slack-retry-num"):
        return JSONResponse({"ok": True})

    try:
        payload = json.loads(body_bytes)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(400, "invalid JSON")

    # URL verification challenge
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}

    # Signature verification
    slack_secret = _get_slack_signing_secret()
    if slack_secret:
        timestamp = request.headers.get("x-slack-request-timestamp", "")
        signature = request.headers.get("x-slack-signature", "")
        if not timestamp or not signature:
            raise HTTPException(401, "missing signature headers")
        try:
            age = abs(time.time() - int(timestamp))
        except (ValueError, TypeError):
            raise HTTPException(401, "invalid timestamp")
        if age > 300:
            raise HTTPException(401, "request too old")
        sig_base = f"v0:{timestamp}:{body_bytes.decode()}"
        expected = "v0=" + hmac.new(
            slack_secret.encode(), sig_base.encode(), hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(401, "invalid signature")

    # Only process event_callback
    if payload.get("type") != "event_callback":
        return JSONResponse({"ok": True})

    event = payload.get("event") or {}
    if not event:
        return JSONResponse({"ok": True})

    # Skip bot messages and subtypes
    if event.get("bot_id") or event.get("subtype"):
        return JSONResponse({"ok": True})

    event_type = event.get("type", "")
    channel_type = event.get("channel_type", "")
    thread_ts = event.get("thread_ts", "")

    if event_type == "app_mention":
        slack_event_type = "slack.mention"
    elif channel_type == "im":
        slack_event_type = "slack.dm"
    elif thread_ts:
        slack_event_type = "slack.thread_reply"
    else:
        return JSONResponse({"ok": True})

    team_id = payload.get("team_id", "")
    normalized = {
        "id": payload.get("event_id", str(uuid.uuid4())),
        "source": "slack",
        "type": slack_event_type,
        "workspace": team_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "payload": {
            "user_id": event.get("user", ""),
            "channel": event.get("channel", ""),
            "channel_type": channel_type,
            "text": (event.get("text", "") or "")[:4000],
            "ts": event.get("ts", ""),
            "thread_ts": thread_ts,
        },
    }

    delivered = await _route_event(normalized)
    return {"delivered_to": delivered}


# ---------------------------------------------------------------------------
# Deployment management
# ---------------------------------------------------------------------------


@app.post("/deployments", status_code=201)
async def register_deployment(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")

    name = body.get("name")
    subscriptions = body.get("subscriptions")

    if not name or not subscriptions:
        raise HTTPException(400, "name and subscriptions[] required")

    deployment_id = str(uuid.uuid4())
    api_key = f"moda_{uuid.uuid4().hex}"

    dep = Deployment(
        id=deployment_id,
        name=name,
        api_key=api_key,
        subscriptions=list(subscriptions),
    )

    async with _lock:
        _deployments[deployment_id] = dep
        _api_key_index[api_key] = deployment_id
        for sub in subscriptions:
            _subscription_index.setdefault(sub, set()).add(deployment_id)

    return {"deployment_id": deployment_id, "api_key": api_key}


def _auth_deployment(deployment_id: str, request_or_token: Request | str) -> Deployment:
    """Authenticate and return the deployment, or raise 401/403."""
    if isinstance(request_or_token, str):
        api_key = request_or_token
    else:
        auth = request_or_token.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(401, "unauthorized")
        api_key = auth[7:]

    dep_id = _api_key_index.get(api_key)
    if not dep_id or dep_id != deployment_id:
        raise HTTPException(403, "invalid API key")

    dep = _deployments.get(dep_id)
    if not dep:
        raise HTTPException(404, "deployment not found")

    return dep


@app.put("/deployments/{deployment_id}/subscriptions")
async def update_subscriptions(deployment_id: str, request: Request):
    dep = _auth_deployment(deployment_id, request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")

    new_subs = body.get("add")
    if not new_subs:
        raise HTTPException(400, "add[] required")

    added = 0
    async with _lock:
        for sub in new_subs:
            if sub not in dep.subscriptions:
                dep.subscriptions.append(sub)
                added += 1
            _subscription_index.setdefault(sub, set()).add(deployment_id)

    return {"subscriptions": dep.subscriptions, "added": added}


# ---------------------------------------------------------------------------
# WebSocket subscription
# ---------------------------------------------------------------------------


@app.websocket("/deployments/{deployment_id}/subscribe")
async def subscribe_ws(websocket: WebSocket, deployment_id: str):
    # Auth: Bearer token from header or query param
    auth = websocket.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
    else:
        token = websocket.query_params.get("token", "")

    if not token:
        await websocket.close(code=4001, reason="unauthorized")
        return

    dep_id = _api_key_index.get(token)
    if not dep_id or dep_id != deployment_id:
        await websocket.close(code=4003, reason="invalid API key")
        return

    dep = _deployments.get(dep_id)
    if not dep:
        await websocket.close(code=4004, reason="deployment not found")
        return

    await websocket.accept()

    # Replay events after last_seen
    last_seen_str = websocket.query_params.get("last_seen", "0")
    try:
        last_seen = int(last_seen_str)
    except (ValueError, TypeError):
        last_seen = 0

    if last_seen > 0:
        for stored in dep.event_buffer:
            if stored.get("seq", 0) > last_seen:
                try:
                    await websocket.send_text(
                        json.dumps({"type": "replay", "data": stored})
                    )
                except Exception:
                    return

    # Send connected message
    try:
        await websocket.send_text(json.dumps({
            "type": "connected",
            "deployment_id": deployment_id,
            "next_seq": dep.next_seq,
        }))
    except Exception:
        return

    dep.websockets.add(websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if msg.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
            elif msg.get("type") == "ack":
                pass  # Acknowledged — could track cursor per-client
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        dep.websockets.discard(websocket)


# ---------------------------------------------------------------------------
# Daemon helpers (called from consumer.py and CLI)
# ---------------------------------------------------------------------------


def ensure_running(port: int, webhook_secret: str = "",
                   slack_signing_secret: str = "",
                   repo_path: "Path | None" = None) -> None:
    """Start the event server daemon if not already running."""
    import urllib.request

    try:
        req = urllib.request.Request(f"http://localhost:{port}/health")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            if data.get("status") == "ok":
                log.info(f"Event server already running on port {port}")
                return
    except Exception:
        pass

    import subprocess
    import sys
    from modastack.sdk import get_repo_root

    rp = repo_path or get_repo_root()
    if rp is None:
        raise RuntimeError("repo_path required for event server")
    state = rp / ".modastack" / "state"
    state.mkdir(parents=True, exist_ok=True)
    log_file = state / "event-server.log"
    pid_file = state / "event-server.pid"

    env = dict(os.environ)
    env["MODASTACK_ES_PORT"] = str(port)
    env["MODASTACK_ES_WEBHOOK_SECRET"] = webhook_secret
    env["MODASTACK_ES_SLACK_SIGNING_SECRET"] = slack_signing_secret

    with open(log_file, "a") as lf:
        proc = subprocess.Popen(
            [sys.executable, "-m", "modastack.manager.events.event_server",
             "--port", str(port)],
            stdout=lf, stderr=lf,
            env=env, start_new_session=True,
        )

    pid_file.write_text(str(proc.pid))

    # Wait for healthy
    for _ in range(30):
        time.sleep(0.5)
        try:
            req = urllib.request.Request(f"http://localhost:{port}/health")
            with urllib.request.urlopen(req, timeout=2):
                log.info(f"Event server started on port {port} (pid {proc.pid})")
                return
        except Exception:
            continue
    log.error("Event server failed to start within 15 seconds")


def register(base_url: str, name: str,
             subscriptions: list[str]) -> tuple[str, str]:
    """Register a deployment with the event server.

    Returns (deployment_id, api_key).
    """
    import urllib.request

    data = json.dumps({"name": name, "subscriptions": subscriptions}).encode()
    req = urllib.request.Request(
        f"{base_url}/deployments",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        result = json.loads(resp.read())
    return result["deployment_id"], result["api_key"]


def run_server(port: int, webhook_secret: str = "",
               slack_signing_secret: str = "") -> None:
    """Foreground entry point for the event server daemon."""
    import uvicorn

    app.state.webhook_secret = (
        webhook_secret or os.environ.get("MODASTACK_ES_WEBHOOK_SECRET", "")
    )
    app.state.slack_signing_secret = (
        slack_signing_secret
        or os.environ.get("MODASTACK_ES_SLACK_SIGNING_SECRET", "")
    )
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


# ---------------------------------------------------------------------------
# __main__ support: python -m modastack.manager.events.event_server --port 8080
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="modastack local event server")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    run_server(args.port)
