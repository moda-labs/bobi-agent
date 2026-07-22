"""Live Slack Socket Mode smoke for the Phase 3 operator surface (#809).

This test starts the real local event server, registers real bot and app
credentials through the production signed Python path, waits for the socket to
connect, and receives one operator-generated mention through a subscribed
deployment. It is skipped unless both Slack credentials are explicitly set.

Run with output visible so the mention instruction can be followed:

    pytest tests/integration/test_slack_socket_live.py -m live -s
"""

import json
import os
import signal
import socket
import time
from types import SimpleNamespace

import httpx
import pytest

from bobi.config import Config, save_bubble_state
from bobi.events.server import (
    _post_register,
    ensure_running,
    register_slack_workspaces,
)
from bobi.events.signing import signed_request

pytestmark = [
    pytest.mark.live,
    pytest.mark.timeout(180),
    pytest.mark.skipif(
        not (
            os.environ.get("SLACK_APP_TOKEN")
            and os.environ.get("SLACK_BOT_TOKEN")
        ),
        reason="live Slack Socket Mode not configured "
               "(SLACK_APP_TOKEN, SLACK_BOT_TOKEN)",
    ),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _stop_event_server(project) -> None:
    pid_file = project / "state" / "event-server.pid"
    if not pid_file.exists():
        return
    try:
        os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
    except (ProcessLookupError, ValueError):
        pass


def _slack_identity(bot_token: str) -> tuple[str, str, str]:
    auth = httpx.post(
        "https://slack.com/api/auth.test",
        headers={"Authorization": f"Bearer {bot_token}"},
        timeout=15,
    ).json()
    assert auth.get("ok"), (
        "SLACK_BOT_TOKEN rejected by auth.test: "
        f"{auth.get('error', 'unknown error')}"
    )
    team_id = str(auth.get("team_id") or "")
    bot_id = str(auth.get("bot_id") or "")
    bot_user_id = str(auth.get("user_id") or "")
    assert team_id and bot_id and bot_user_id, (
        "Slack auth.test did not return team, bot, and user identities"
    )

    bot = httpx.get(
        "https://slack.com/api/bots.info",
        params={"bot": bot_id},
        headers={"Authorization": f"Bearer {bot_token}"},
        timeout=15,
    ).json()
    assert bot.get("ok"), (
        "SLACK_BOT_TOKEN rejected by bots.info: "
        f"{bot.get('error', 'unknown error')}"
    )
    app_id = str((bot.get("bot") or {}).get("app_id") or "")
    assert app_id, "Slack bots.info did not return an app id"
    return team_id, bot_user_id, app_id


def _wait_connected(es_url: str, app_id: str, timeout: float = 45) -> dict:
    deadline = time.monotonic() + timeout
    last_entry = None
    while time.monotonic() < deadline:
        health = httpx.get(f"{es_url}/health", timeout=5).json()
        entries = {
            entry.get("application_id"): entry
            for entry in health.get("slack_socket", [])
            if isinstance(entry, dict)
        }
        last_entry = entries.get(app_id)
        if last_entry and last_entry.get("state") == "connected":
            return last_entry
        if last_entry and last_entry.get("state") == "fatal":
            pytest.fail(
                "Slack Socket Mode entered fatal state: "
                f"{last_entry.get('fatal_reason', 'unknown reason')}"
            )
        time.sleep(0.5)
    raise AssertionError(
        f"Slack Socket Mode did not connect for {app_id}: {last_entry}"
    )


@pytest.fixture(scope="module")
def live_socket(tmp_path_factory):
    bot_token = os.environ["SLACK_BOT_TOKEN"]
    team_id, bot_user_id, app_id = _slack_identity(bot_token)

    base = tmp_path_factory.mktemp("slack-socket-live")
    project = base / "run"
    (project / "package").mkdir(parents=True)
    (project / "state").mkdir(parents=True)
    port = _free_port()
    es_url = f"http://localhost:{port}"
    (project / "package" / "agent.yaml").write_text(
        "entry_point: manager\n"
        f"event_server: {es_url}\n"
        "services:\n"
        "  - name: slack\n"
        "    events: true\n"
        "    credentials:\n"
        "      bot_token: ${SLACK_BOT_TOKEN}\n"
        "      app_token: ${SLACK_APP_TOKEN}\n"
    )

    old_api_override = os.environ.pop("BOBI_ES_SLACK_API_URL", None)
    deployment = None
    ws = None
    try:
        status = ensure_running(
            port, bind="127.0.0.1", project_path=project,
        )
        assert status in ("started", "connected")

        bubble = _post_register(
            es_url, "slack-socket-live-bootstrap", ["_bootstrap"],
        )
        save_bubble_state(project, bubble["bubble_id"], bubble["bubble_key"])

        cfg = Config.load(project)
        registered = register_slack_workspaces(
            es_url, cfg, bubble["bubble_id"], bubble["bubble_key"],
        )
        assert registered == [team_id], "signed Slack registration failed"
        _wait_connected(es_url, app_id)

        topic = f"slack:{team_id}:app:{app_id}"
        response = signed_request(
            es_url,
            "POST",
            "/deployments",
            {"name": "slack-socket-live", "subscriptions": [topic]},
            bubble["bubble_id"],
            bubble["bubble_key"],
            timeout=10,
        )
        assert response.status_code == 201, response.text
        deployment = response.json()

        import websocket

        ws = websocket.create_connection(
            f"ws://{es_url.removeprefix('http://')}"
            f"/deployments/{deployment['deployment_id']}/subscribe",
            header=[f"Authorization: Bearer {deployment['api_key']}"],
            timeout=10,
        )
        hello = json.loads(ws.recv())
        assert hello["type"] == "connected"

        yield SimpleNamespace(
            app_id=app_id,
            bot_user_id=bot_user_id,
            deployment=deployment,
            es_url=es_url,
            team_id=team_id,
            topic=topic,
            ws=ws,
        )
    finally:
        if ws is not None:
            ws.close()
        if deployment is not None:
            try:
                httpx.delete(
                    f"{es_url}/deployments/{deployment['deployment_id']}",
                    headers={
                        "Authorization": f"Bearer {deployment['api_key']}"
                    },
                    timeout=5,
                )
            except httpx.HTTPError:
                pass
        _stop_event_server(project)
        if old_api_override is not None:
            os.environ["BOBI_ES_SLACK_API_URL"] = old_api_override


def test_real_mention_arrives_over_socket_mode(live_socket):
    import websocket

    nonce = f"bobi-socket-live-{time.time_ns()}"
    print(
        "\nIn Slack, mention the configured bot "
        f"(<@{live_socket.bot_user_id}>) and include this exact nonce: {nonce}",
        flush=True,
    )

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        live_socket.ws.settimeout(min(2, max(0.1, deadline - time.monotonic())))
        try:
            message = json.loads(live_socket.ws.recv())
        except websocket.WebSocketTimeoutException:
            continue
        if message.get("type") not in ("event", "replay"):
            continue
        event = message["data"]
        if nonce not in str(event.get("text") or ""):
            continue

        assert event["source"] == "slack"
        assert event["type"] == "slack.mention"
        assert event["fields"]["api_app_id"] == live_socket.app_id
        assert live_socket.topic in event["topics"]
        return

    raise AssertionError(
        "No matching Slack mention arrived before the 120-second timeout"
    )
