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
from .bus import get_bus

log = logging.getLogger(__name__)


def _poll_workers(interval: int = 5):
    """Poll ALL tmux worker sessions for state changes.

    Discovers sessions by scanning tmux ls, not just state.json.
    This catches sessions the manager spawned directly via bash.
    """
    bus = get_bus()
    last_states = {}

    while True:
        try:
            # Discover all non-manager tmux sessions
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True,
            )
            session_names = []
            if result.returncode == 0:
                session_names = [
                    s.strip() for s in result.stdout.strip().splitlines()
                    if s.strip() and s.strip() != "moda-manager"
                ]

            for session_name in session_names:
                # Derive issue ID from session name
                iid = session_name.upper().replace("WORKER-", "").replace("MODA-", "")

                # Check session state by capturing pane
                pane_result = subprocess.run(
                    ["tmux", "capture-pane", "-t", session_name, "-p", "-S", "-5"],
                    capture_output=True, text=True,
                )
                pane_lines = [l for l in (pane_result.stdout or "").splitlines() if l.strip()]

                sess_state = "working"
                for line in reversed(pane_lines[-5:]):
                    if "❯" in line and "bypass" not in line:
                        if any("bypass" in l or "⏵⏵" in l for l in pane_lines[-3:]):
                            sess_state = "waiting_input"
                        break

                state_key = f"{iid}:{sess_state}"
                if state_key != last_states.get(iid):
                    last_states[iid] = state_key
                    bus.push(f"worker.{sess_state}", "worker", {
                        "issue_id": iid,
                        "session_name": session_name,
                        "session_state": sess_state,
                        "alive": True,
                    })

            # Detect sessions that disappeared
            current_ids = {s.upper().replace("WORKER-", "").replace("MODA-", "") for s in session_names}
            for old_id in list(last_states.keys()):
                if old_id not in current_ids:
                    bus.push("worker.exited", "worker", {
                        "issue_id": old_id,
                        "session_state": "exited",
                        "alive": False,
                    })
                    del last_states[old_id]

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


def _poll_orphans(interval: int = 60):
    """Detect orphaned issues — In Progress on Linear but no tmux session running.

    This catches cases where an engineer session died (restart, crash, stall kill)
    but the Linear ticket is still In Progress. Pushes an event so the manager
    can decide whether to respawn or ask the human.
    """
    import truststore
    truststore.inject_into_ssl()
    from modastack.scanner import scan_linear_all_active

    bus = get_bus()
    alerted = set()

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
                api_key = creds.get("linear_api_key")
                if not api_key:
                    continue

                issues_by_state = asyncio.run(scan_linear_all_active(api_key, rc))

                for issue in issues_by_state.get("In Progress", []):
                    iid = issue["identifier"]
                    # Check if there's a tmux session for this issue
                    session_name = f"moda-{iid.lower()}"
                    has_session = subprocess.run(
                        ["tmux", "has-session", "-t", session_name],
                        capture_output=True,
                    ).returncode == 0

                    # Also check the older naming format
                    if not has_session:
                        alt_name = iid.lower()
                        has_session = subprocess.run(
                            ["tmux", "has-session", "-t", alt_name],
                            capture_output=True,
                        ).returncode == 0

                    if not has_session and iid not in alerted:
                        alerted.add(iid)
                        labels = [l["name"] for l in issue.get("labels", {}).get("nodes", [])]
                        bus.push("orphan.detected", "system", {
                            "issue_id": iid,
                            "linear_id": issue["id"],
                            "title": issue["title"],
                            "state": "In Progress",
                            "labels": labels,
                            "repo": str(rc.path),
                            "project": rc.linear_project,
                            "reason": "Issue is In Progress but no engineer session is running.",
                        })
                        log.info(f"Orphan detected: {iid} — In Progress, no session")

                    elif has_session and iid in alerted:
                        alerted.discard(iid)

        except Exception as e:
            log.error(f"Orphan poller error: {e}")

        time.sleep(interval)


# Registry of pollers — each runs in its own thread
POLLERS = {
    "workers": (_poll_workers, 5),
    "linear": (_poll_linear, 30),
    "slack": (_poll_slack, 10),
    "orphans": (_poll_orphans, 60),
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
