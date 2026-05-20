"""Slack messaging channel.

Polls for new DMs and @mentions. Posts messages on behalf of the manager.
Uses the Slack Web API (no webhooks, no socket mode — just HTTP polling).
"""

import json
import logging
import time
from pathlib import Path

import httpx

from dispatch.config import GlobalConfig, Credentials

log = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
STATE_PATH = Path.home() / ".dispatch" / "manager" / "slack_state.json"


def _get_token() -> str:
    """Get the Slack bot token from credentials."""
    creds = Credentials.load()
    for name in creds.list_names():
        entry = creds.get(name)
        token = entry.get("slack_bot_token", "")
        if token:
            return token
    global_config = GlobalConfig.load()
    return global_config.slack_bot_token or ""


def _load_state() -> dict:
    """Load last-seen timestamps per conversation."""
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


async def gather(config: dict) -> list[dict]:
    """Fetch new messages from Slack DMs and channels where the bot is mentioned."""
    token = _get_token()
    if not token:
        return []

    state = _load_state()
    items = []

    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {token}"}

        # Get bot's own user ID
        bot_resp = await client.post(f"{SLACK_API}/auth.test", headers=headers)
        if bot_resp.status_code != 200:
            return []
        bot_data = bot_resp.json()
        if not bot_data.get("ok"):
            log.warning(f"Slack auth failed: {bot_data.get('error')}")
            return []
        bot_user_id = bot_data["user_id"]

        # List DM conversations (im = direct messages with the bot)
        convos_resp = await client.get(
            f"{SLACK_API}/conversations.list",
            headers=headers,
            params={"types": "im", "limit": 20},
        )
        if convos_resp.status_code != 200:
            return items
        convos = convos_resp.json().get("channels", [])

        for convo in convos:
            channel_id = convo["id"]
            oldest = state.get(channel_id, "0")

            # Fetch messages newer than what we've seen
            hist_resp = await client.get(
                f"{SLACK_API}/conversations.history",
                headers=headers,
                params={"channel": channel_id, "oldest": oldest, "limit": 10},
            )
            if hist_resp.status_code != 200:
                continue
            messages = hist_resp.json().get("messages", [])

            for msg in messages:
                # Skip bot's own messages
                if msg.get("user") == bot_user_id or msg.get("bot_id"):
                    continue

                # Resolve username
                user_name = msg.get("user", "unknown")
                user_resp = await client.get(
                    f"{SLACK_API}/users.info",
                    headers=headers,
                    params={"user": msg["user"]},
                )
                if user_resp.status_code == 200 and user_resp.json().get("ok"):
                    user_name = user_resp.json()["user"].get("real_name", user_name)

                items.append({
                    "channel": "slack",
                    "type": "dm",
                    "from": user_name,
                    "from_id": msg.get("user", ""),
                    "text": msg.get("text", "")[:500],
                    "ts": msg.get("ts", ""),
                    "channel_id": channel_id,
                })

            # Update last-seen
            if messages:
                latest_ts = max(m["ts"] for m in messages)
                state[channel_id] = latest_ts

    _save_state(state)
    return items


def hash_key(items: list[dict]) -> str:
    """Change detection — new messages."""
    parts = [f"{i.get('ts', '')}:{i.get('text', '')[:30]}" for i in items]
    return "|".join(parts)


def format_context(items: list[dict]) -> str:
    """Format for manager prompt."""
    if not items:
        return ""
    lines = ["\n## Slack Messages"]
    for i in items:
        prefix = "DM" if i.get("type") == "dm" else f"#{i.get('channel_name', '?')}"
        lines.append(f"- 💬 [{prefix}] {i['from']}: {i['text'][:200]}")
        lines.append(f"  (reply with send_slack action, channel_id: {i.get('channel_id', '')})")
    return "\n".join(lines)
