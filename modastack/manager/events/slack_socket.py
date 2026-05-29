"""Slack Socket Mode client — real-time events without webhooks.

Connects to Slack via WebSocket. No public URL needed.
Receives events and pushes them to the event bus.
Filters channel messages to only those in threads Modabot participated in.
"""

import json
import logging
import os
import threading
import time

import httpx
import websocket

from modastack.config import GlobalConfig
from .bus import get_bus

log = logging.getLogger(__name__)


def _get_tokens() -> tuple[str, str]:
    """Get (app_token, bot_token) from global config."""
    config = GlobalConfig.load()
    return config.slack_app_token, config.slack_bot_token


def _get_bot_user_id(bot_token: str) -> str:
    """Get the bot's own user ID."""
    with httpx.Client() as c:
        r = c.post("https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {bot_token}"})
        if r.status_code == 200 and r.json().get("ok"):
            return r.json()["user_id"]
    return ""


def _resolve_user(bot_token: str, user_id: str, cache: dict) -> str:
    """Resolve a user ID to a display name, with caching."""
    if user_id in cache:
        return cache[user_id]
    with httpx.Client() as c:
        r = c.get("https://slack.com/api/users.info",
                   headers={"Authorization": f"Bearer {bot_token}"},
                   params={"user": user_id})
        if r.status_code == 200 and r.json().get("ok"):
            name = r.json()["user"].get("real_name", user_id)
            cache[user_id] = name
            return name
    cache[user_id] = user_id
    return user_id


def _open_socket_url(app_token: str) -> str:
    """Get a WebSocket URL from Slack's apps.connections.open API."""
    with httpx.Client() as c:
        r = c.post("https://slack.com/api/apps.connections.open",
                    headers={"Authorization": f"Bearer {app_token}"})
        data = r.json()
        if data.get("ok"):
            return data["url"]
        else:
            log.error(f"Socket Mode connection failed: {data.get('error')}")
            return ""


def _run_socket(app_token: str, bot_token: str):
    """Main socket loop — connect, receive events, push to bus."""
    bus = get_bus()
    bot_user_id = _get_bot_user_id(bot_token)
    user_cache = {}
    # Track threads modabot has participated in
    our_threads: set[str] = set()

    while True:
        url = _open_socket_url(app_token)
        if not url:
            log.warning("Failed to get Socket Mode URL, retrying in 10s")
            time.sleep(10)
            continue

        log.info("Slack Socket Mode connected")

        def on_message(ws, raw):
            try:
                envelope = json.loads(raw)
            except json.JSONDecodeError:
                return

            # Acknowledge immediately
            envelope_id = envelope.get("envelope_id")
            if envelope_id:
                ws.send(json.dumps({"envelope_id": envelope_id}))

            payload = envelope.get("payload", {})
            event = payload.get("event", {})
            event_type = event.get("type", "")

            # Skip bot's own messages
            if event.get("user") == bot_user_id or event.get("bot_id"):
                # But track threads we participate in
                thread_ts = event.get("thread_ts") or event.get("ts", "")
                channel = event.get("channel", "")
                if thread_ts and channel:
                    our_threads.add(f"{channel}:{thread_ts}")
                return

            user_name = _resolve_user(bot_token, event.get("user", ""), user_cache)
            channel = event.get("channel", "")
            text = event.get("text", "")[:500]
            thread_ts = event.get("thread_ts", "")

            # Inject directly into the manager session — no event bus hop.
            # The Stop hook handles posting the response back to Slack.
            is_human_msg = False

            if event_type == "app_mention":
                is_human_msg = True
            elif event_type == "message":
                channel_type = event.get("channel_type", "")
                if channel_type == "im":
                    is_human_msg = True
                elif thread_ts and f"{channel}:{thread_ts}" in our_threads:
                    is_human_msg = True

            if is_human_msg and text:
                from modastack.tmux import send_text as tmux_send
                from modastack.manager.session import SESSION_NAME
                # Set marker so the Stop hook knows to relay the response to Slack
                marker = os.path.expanduser("~/.modastack/manager/slack_reply_pending")
                os.makedirs(os.path.dirname(marker), exist_ok=True)
                with open(marker, "w") as f:
                    f.write(event.get("ts", ""))
                tmux_send(SESSION_NAME, f"{user_name}: {text}", verify=False)
                log.info(f"Slack → manager: {user_name}: {text[:80]}")

                # Also push to bus so workflow approval nodes can match
                bus.push(f"slack.{'dm' if event.get('channel_type') == 'im' else 'mention'}", "slack", {
                    "channel_id": channel,
                    "from": user_name,
                    "from_id": event.get("user", ""),
                    "text": text,
                    "ts": event.get("ts", ""),
                    "thread_ts": thread_ts,
                })

        def on_error(ws, error):
            log.warning(f"Socket Mode error: {error}")

        def on_close(ws, code, msg):
            log.info(f"Socket Mode closed ({code}), reconnecting...")

        ws = websocket.WebSocketApp(
            url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ws.run_forever(ping_interval=30, ping_timeout=10)
        time.sleep(2)  # Brief pause before reconnect


def start_socket_mode() -> threading.Thread | None:
    """Start Slack Socket Mode in a background thread. Returns None if not configured."""
    app_token, bot_token = _get_tokens()
    if not app_token or not bot_token:
        log.info("Slack Socket Mode not configured (missing app_token or bot_token)")
        return None

    thread = threading.Thread(target=_run_socket, args=(app_token, bot_token),
                              daemon=True, name="slack-socket")
    thread.start()
    log.info("Slack Socket Mode started")
    return thread
