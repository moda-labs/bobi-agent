"""Pollers — fallback event producers for sources without webhooks.

Each poller runs in a background thread, polls periodically, and pushes
events to the bus when something changes. Used for:
- Worker tmux sessions (no webhook possible)
- Task tracker (GitHub Issues or Linear) when webhooks aren't configured
- Slack when Socket Mode isn't configured

Adding a new poller: write a function that polls and pushes to the bus,
then register it in POLLERS.
"""

import asyncio
import hashlib
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from modastack.config import GlobalConfig, RepoConfig
from .bus import get_bus

log = logging.getLogger(__name__)


STALL_THRESHOLD_SECS = 300   # 5 min — emit worker.stalled
STUCK_THRESHOLD_SECS = 600   # 10 min — emit worker.stuck

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _find_handoff(issue_id: str) -> dict:
    """Find and parse the handoff file for an issue across all registered repos.

    Returns the YAML frontmatter as a dict, or {} if not found.
    """
    import yaml
    global_config = GlobalConfig.load()
    iid_lower = issue_id.lower()

    for repo_path in global_config.repos:
        handoff_path = repo_path / "worktrees" / iid_lower / ".modastack" / "handoff.md"
        if handoff_path.exists():
            try:
                content = handoff_path.read_text()
                if content.startswith("---"):
                    end = content.index("---", 3)
                    return yaml.safe_load(content[3:end]) or {}
            except Exception:
                pass
    return {}


def _poll_workers(interval: int = 5):
    """Poll ALL tmux worker sessions for state changes.

    Discovers sessions by scanning tmux ls, not just state.json.
    Uses detect_state from session.py for canonical state detection,
    tracks output hashes to detect stalls, and reads handoff files
    to include the current phase in events for auto-routing.
    """
    from modastack.session import detect_state as detect_session_state

    bus = get_bus()
    last_states: dict[str, str] = {}
    heartbeats: dict[str, dict] = {}

    while True:
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True,
            )
            session_names = []
            if result.returncode == 0:
                session_names = [
                    s.strip() for s in result.stdout.strip().splitlines()
                    if s.strip()
                    and s.strip().startswith("moda-")
                    and s.strip() != "moda-manager"
                ]

            for session_name in session_names:
                iid = session_name.upper().replace("WORKER-", "").replace("MODA-", "")

                state_info = detect_session_state(iid)
                sess_state = state_info["state"]

                # Process liveness: tmux session exists but claude process died
                if sess_state == "exited":
                    bus.push("worker.process_dead", "worker", {
                        "issue_id": iid,
                        "session_name": session_name,
                        "reason": "tmux session exists but claude process is not running",
                    })
                    last_states.pop(iid, None)
                    heartbeats.pop(iid, None)
                    continue

                # Emit state change events
                state_key = f"{iid}:{sess_state}"
                if state_key != last_states.get(iid):
                    last_states[iid] = state_key
                    event_data = {
                        "issue_id": iid,
                        "session_name": session_name,
                        "session_state": sess_state,
                        "alive": True,
                    }
                    if sess_state == "permission_blocked":
                        event_data["prompt_line"] = state_info.get("prompt_line", "")

                    # When idle, read the handoff to include phase info
                    if sess_state == "waiting_input":
                        handoff = _find_handoff(iid)
                        if handoff:
                            event_data["phase"] = handoff.get("phase", "")
                            event_data["title"] = handoff.get("title", "")
                            if handoff.get("spec_pr"):
                                event_data["spec_pr"] = handoff["spec_pr"]

                    bus.push(f"worker.{sess_state}", "worker", event_data)

                # Heartbeat: hash pane output to detect stalls
                pane_result = subprocess.run(
                    ["tmux", "capture-pane", "-t", session_name, "-p", "-S", "-30"],
                    capture_output=True, text=True,
                )
                pane_content = _strip_ansi(pane_result.stdout or "")
                content_hash = hashlib.md5(pane_content.encode()).hexdigest()

                now = time.monotonic()
                hb = heartbeats.get(iid)
                if hb is None or hb["hash"] != content_hash:
                    heartbeats[iid] = {
                        "hash": content_hash,
                        "last_change": now,
                        "alerted_stall": False,
                        "alerted_stuck": False,
                    }
                elif sess_state not in ("waiting_input", "permission_blocked"):
                    idle_secs = now - hb["last_change"]
                    snippet = pane_content.strip().splitlines()[-3:] if pane_content.strip() else []

                    if idle_secs > STUCK_THRESHOLD_SECS and not hb["alerted_stuck"]:
                        hb["alerted_stuck"] = True
                        bus.push("worker.stuck", "worker", {
                            "issue_id": iid,
                            "session_name": session_name,
                            "idle_seconds": int(idle_secs),
                            "last_output_snippet": "\n".join(snippet),
                        })
                    elif idle_secs > STALL_THRESHOLD_SECS and not hb["alerted_stall"]:
                        hb["alerted_stall"] = True
                        bus.push("worker.stalled", "worker", {
                            "issue_id": iid,
                            "session_name": session_name,
                            "idle_seconds": int(idle_secs),
                            "last_output_snippet": "\n".join(snippet),
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
                    heartbeats.pop(old_id, None)

        except Exception as e:
            log.error(f"Worker poller error: {e}")

        time.sleep(interval)


def _scan_repo_tasks(rc: RepoConfig) -> dict[str, list[dict]]:
    """Scan tasks for a repo using the configured task tracker."""
    if rc.task_tracking == "github-issues":
        from modastack.github_issues import scan_github_issues
        return scan_github_issues(rc)
    elif rc.task_tracking == "linear":
        import truststore
        truststore.inject_into_ssl()
        from modastack.scanner import scan_linear_all_active
        creds = rc.get_credentials()
        api_key = creds.get("linear_api_key")
        if not api_key:
            return {}
        return asyncio.run(scan_linear_all_active(api_key, rc))
    return {}


def _poll_tasks(interval: int = 30):
    """Poll the task tracker for issue changes. Works with both GitHub Issues and Linear."""
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

                issues_by_state = _scan_repo_tasks(rc)
                source = rc.task_tracking  # "github-issues" or "linear"

                for state_name, issues in issues_by_state.items():
                    for issue in issues:
                        iid = issue["identifier"]
                        labels = [l["name"] for l in issue.get("labels", {}).get("nodes", [])]
                        comments = issue.get("comments", {}).get("nodes", [])
                        latest_comment = comments[-1].get("body", "")[:50] if comments else ""
                        state_key = f"{iid}:{state_name}:{latest_comment}"

                        if state_key != last_states.get(iid):
                            last_states[iid] = state_key

                            event_type = "task.updated"
                            if iid not in last_states or state_name == "Todo":
                                event_type = "task.created" if state_name == "Todo" else "task.updated"

                            bus.push(event_type, source, {
                                "issue_id": iid,
                                "task_id": issue["id"],
                                "title": issue["title"],
                                "description": (issue.get("description") or "")[:500],
                                "state": state_name,
                                "labels": labels,
                                "repo": str(rc.path),
                                "project": rc.project,
                                "recent_comments": [
                                    {"author": c.get("user", {}).get("name", ""), "body": c.get("body", "")[:300]}
                                    for c in comments[-3:]
                                ],
                            })
        except Exception as e:
            log.error(f"Task poller error: {e}")

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
    """Detect orphaned issues — In Progress in the task tracker but no tmux session running.

    This catches cases where an engineer session died (restart, crash, stall kill)
    but the task is still In Progress. Pushes an event so the manager
    can decide whether to respawn or ask the human.
    """
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

                issues_by_state = _scan_repo_tasks(rc)

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
                            "task_id": issue["id"],
                            "title": issue["title"],
                            "state": "In Progress",
                            "labels": labels,
                            "repo": str(rc.path),
                            "project": rc.project,
                            "reason": "Task is In Progress but no engineer session is running.",
                        })
                        log.info(f"Orphan detected: {iid} — In Progress, no session")

                    elif has_session and iid in alerted:
                        alerted.discard(iid)

        except Exception as e:
            log.error(f"Orphan poller error: {e}")

        time.sleep(interval)


def _get_modastack_root() -> Path:
    return Path(__file__).parent.parent.parent


def _extract_changelog_entries(changelog_text: str, from_version: str, to_version: str) -> str:
    """Extract changelog entries between two versions."""
    lines = changelog_text.splitlines()
    capturing = False
    entries = []

    for line in lines:
        if line.startswith("## ") and to_version in line:
            capturing = True
            continue
        if line.startswith("## ") and from_version in line:
            break
        if capturing and line.strip():
            entries.append(line)

    return "\n".join(entries)


def _check_version(bus, root: Path, last_announced: str) -> str:
    """Check for a new version on origin/main. Returns the announced version (or last_announced)."""
    result = subprocess.run(
        ["git", "fetch", "origin", "main", "--quiet"],
        cwd=root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.debug(f"Version check: fetch failed — {result.stderr.strip()}")
        return last_announced

    result = subprocess.run(
        ["git", "show", "origin/main:VERSION"],
        cwd=root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return last_announced
    remote_version = result.stdout.strip()

    version_file = root / "VERSION"
    local_version = version_file.read_text().strip() if version_file.exists() else "0.0.0"

    if remote_version == local_version or remote_version == last_announced:
        return last_announced

    changelog = ""
    result = subprocess.run(
        ["git", "show", "origin/main:CHANGELOG.md"],
        cwd=root, capture_output=True, text=True,
    )
    if result.returncode == 0:
        changelog = _extract_changelog_entries(
            result.stdout, local_version, remote_version
        )

    bus.push("system.update_available", "system", {
        "current_version": local_version,
        "new_version": remote_version,
        "changelog": changelog,
    })
    log.info(f"Update available: {local_version} → {remote_version}")
    return remote_version


def _poll_version(interval: int = 3600):
    """Check for new versions on origin/main.

    Pass interval=0 for a one-shot check (used at startup).
    """
    bus = get_bus()
    last_announced = ""
    root = _get_modastack_root()

    while True:
        try:
            last_announced = _check_version(bus, root, last_announced)
        except Exception as e:
            log.error(f"Version poller error: {e}")

        if interval == 0:
            return
        time.sleep(interval)


# Registry of pollers — each runs in its own thread
POLLERS = {
    "workers": (_poll_workers, 5),
    "tasks": (_poll_tasks, 30),
    "slack": (_poll_slack, 10),
    "orphans": (_poll_orphans, 60),
    "version": (_poll_version, 3600),
}


def start_pollers(exclude: list[str] = None) -> list[threading.Thread]:
    """Start all pollers in background threads.

    exclude: list of poller names to skip (e.g., ["tasks"] if using webhooks).
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
