"""Pollers — fallback event producers for sources without webhooks.

Each poller runs in a background thread, polls periodically, and pushes
events to the bus when something changes. Used for:
- Worker tmux sessions (no webhook possible)
- Linear/GitHub/Slack when webhooks aren't configured (fallback mode)

Adding a new poller: write a function that polls and pushes to the bus,
then register it in POLLERS.
"""

import asyncio
import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

from modastack.config import GlobalConfig, RepoConfig
from modastack.session import detect_state, capture, session_exists
from modastack.state import StateStore
from .bus import get_bus

log = logging.getLogger(__name__)


def _poll_workers(interval: int = 5):
    """Poll tmux worker sessions for state changes."""
    bus = get_bus()
    last_states = {}

    while True:
        try:
            store = StateStore()
            for agent in store.all_agents():
                iid = agent.issue_id
                alive = session_exists(iid)
                sess = detect_state(iid) if alive else {"state": "exited"}
                state_key = f"{iid}:{sess['state']}"

                if state_key != last_states.get(iid):
                    last_states[iid] = state_key
                    bus.push(f"worker.{sess['state']}", "worker", {
                        "issue_id": iid,
                        "title": agent.title,
                        "phase": agent.last_phase,
                        "session_state": sess["state"],
                        "alive": alive,
                        "idle_minutes": int((time.time() - agent.last_activity_at) / 60),
                        "question": sess.get("question", ""),
                        "options": sess.get("options", []),
                    })
        except Exception as e:
            log.error(f"Worker poller error: {e}")

        time.sleep(interval)


def _poll_linear(interval: int = 30):
    """Poll Linear for issue changes. Fallback when webhooks aren't set up."""
    import truststore
    truststore.inject_into_ssl()
    from modastack.scanner import scan_linear_all_active

    bus = get_bus()
    last_states = {}

    while True:
        try:
            global_config = GlobalConfig.load()
            for repo_path in global_config.repos:
                if not repo_path.exists():
                    continue
                try:
                    rc = RepoConfig.from_file(repo_path)
                except FileNotFoundError:
                    continue
                creds = rc.get_credentials()
                api_key = creds.get("linear_api_key") or global_config.linear_api_key
                if not api_key:
                    continue

                issues_by_state = asyncio.run(scan_linear_all_active(api_key, rc))
                for state_name, issues in issues_by_state.items():
                    for issue in issues:
                        iid = issue["identifier"]
                        labels = [l["name"] for l in issue.get("labels", {}).get("nodes", [])]
                        comments = issue.get("comments", {}).get("nodes", [])
                        latest_comment = comments[-1].get("body", "")[:50] if comments else ""
                        state_key = f"{iid}:{state_name}:{latest_comment}"

                        if state_key != last_states.get(iid):
                            last_states[iid] = state_key

                            event_type = "linear.issue.updated"
                            if iid not in last_states or state_name == "Todo":
                                event_type = "linear.issue.created" if state_name == "Todo" else "linear.issue.updated"

                            bus.push(event_type, "linear", {
                                "issue_id": iid,
                                "linear_id": issue["id"],
                                "title": issue["title"],
                                "description": (issue.get("description") or "")[:500],
                                "state": state_name,
                                "labels": labels,
                                "repo": str(rc.path),
                                "project": rc.linear_project,
                                "recent_comments": [
                                    {"author": c.get("user", {}).get("name", ""), "body": c.get("body", "")[:300]}
                                    for c in comments[-3:]
                                ],
                            })
        except Exception as e:
            log.error(f"Linear poller error: {e}")

        time.sleep(interval)


def _poll_slack(interval: int = 10):
    """Poll Slack DMs. Fallback when Events API isn't set up."""
    import httpx

    bus = get_bus()
    last_seen = {}
    token = ""

    while True:
        try:
            if not token:
                token = GlobalConfig.load().slack_bot_token
            if not token:
                time.sleep(interval)
                continue

            # Synchronous httpx for thread safety
            import httpx as _httpx
            with _httpx.Client() as client:
                headers = {"Authorization": f"Bearer {token}"}

                # Get bot user ID
                auth = client.post("https://slack.com/api/auth.test", headers=headers).json()
                if not auth.get("ok"):
                    time.sleep(interval)
                    continue
                bot_user_id = auth["user_id"]

                # List DM conversations
                convos = client.get("https://slack.com/api/conversations.list",
                    headers=headers, params={"types": "im", "limit": 20}).json().get("channels", [])

                for convo in convos:
                    ch_id = convo["id"]
                    oldest = last_seen.get(ch_id, "0")
                    hist = client.get("https://slack.com/api/conversations.history",
                        headers=headers, params={"channel": ch_id, "oldest": oldest, "limit": 10}).json()

                    for msg in hist.get("messages", []):
                        if msg.get("user") == bot_user_id or msg.get("bot_id"):
                            continue

                        user_name = msg.get("user", "unknown")
                        user_info = client.get("https://slack.com/api/users.info",
                            headers=headers, params={"user": msg["user"]}).json()
                        if user_info.get("ok"):
                            user_name = user_info["user"].get("real_name", user_name)

                        bus.push("slack.message", "slack", {
                            "channel_id": ch_id,
                            "from": user_name,
                            "from_id": msg.get("user", ""),
                            "text": msg.get("text", "")[:500],
                            "ts": msg.get("ts", ""),
                        })

                    if hist.get("messages"):
                        last_seen[ch_id] = max(m["ts"] for m in hist["messages"])

        except Exception as e:
            log.error(f"Slack poller error: {e}")

        time.sleep(interval)


# Registry of pollers — each runs in its own thread
POLLERS = {
    "workers": (_poll_workers, 5),
    "linear": (_poll_linear, 30),
    "slack": (_poll_slack, 10),
}


def start_pollers(exclude: list[str] = None) -> list[threading.Thread]:
    """Start all pollers in background threads.

    exclude: list of poller names to skip (e.g., ["linear"] if using webhooks).
    """
    exclude = exclude or []
    threads = []
    for name, (fn, interval) in POLLERS.items():
        if name in exclude:
            log.info(f"Poller '{name}' excluded (using webhooks)")
            continue
        t = threading.Thread(target=fn, args=(interval,), daemon=True, name=f"poller-{name}")
        t.start()
        threads.append(t)
        log.info(f"Started poller: {name} (every {interval}s)")
    return threads
