"""WebSocket client for the centralized event server.

Connects outbound to the Cloudflare Worker, receives webhook events
(GitHub, Linear, Slack) with automatic catch-up on missed events after downtime.
Pushes normalized events to a thread-safe queue for the consumer to drain.
"""

import json
import logging
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from queue import SimpleQueue

import certifi
import websocket

log = logging.getLogger(__name__)

def _state_path(name: str) -> Path:
    from modastack.sdk import get_repo_root
    root = get_repo_root()
    if not root:
        raise RuntimeError("repo root not set — call set_repo_root() first")
    d = root / ".modastack" / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d / name

# Normalized events land here for the consumer to drain.
event_queue: SimpleQueue = SimpleQueue()


def _load_cursor() -> int:
    try:
        if _state_path("cursor.json").exists():
            data = json.loads(_state_path("cursor.json").read_text())
            return data.get("last_seen", 0)
    except (json.JSONDecodeError, OSError):
        pass
    return 0


def _save_cursor(seq: int) -> None:
    _state_path("cursor.json").write_text(json.dumps({"last_seen": seq}))


def _log_event(event: dict) -> None:
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "type": event.get("type", ""),
        "source": event.get("source", ""),
        "data": event.get("data", {}),
    }
    with open(_state_path("events.jsonl"), "a") as f:
        f.write(json.dumps(entry) + "\n")


_slack_user_cache: dict[str, str] = {}


def _resolve_slack_user(bot_token: str, user_id: str) -> str:
    if not user_id or not bot_token:
        return user_id
    if user_id in _slack_user_cache:
        return _slack_user_cache[user_id]
    try:
        req = urllib.request.Request(
            f"https://slack.com/api/users.info?user={user_id}",
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                name = data["user"].get("real_name", user_id)
                _slack_user_cache[user_id] = name
                return name
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        pass
    _slack_user_cache[user_id] = user_id
    return user_id


def format_event_for_manager(event: dict) -> str:
    """Format a normalized event as a concise message for the manager."""
    etype = event.get("type", "unknown")
    source = event.get("source", "")
    data = event.get("data", {})

    lines = [f"Event: {source}/{etype}"]
    for key in ("issue_id", "pr_number", "title", "repo", "from", "user_id",
                "state", "branch", "conclusion", "text", "ref",
                "channel", "workspace", "thread_ts",
                "phase", "duration", "summary", "error"):
        val = data.get(key)
        if val:
            lines.append(f"  {key}: {val}")
    if data.get("labels"):
        lines.append(f"  labels: {', '.join(data['labels'])}")
    if data.get("url") or data.get("pr_url"):
        lines.append(f"  url: {data.get('url') or data.get('pr_url')}")
    if data.get("requested_by"):
        lines.append(f"  requested_by: {_format_requester(data['requested_by'])}")

    return "\n".join(lines)


def _format_requester(requester: dict) -> str:
    """Render a `requested_by` block into a one-line human-readable note.

    Gives the manager enough to route async results (e.g. a spawned-work
    completion) back to the originating Slack user and thread.
    """
    if not isinstance(requester, dict):
        return str(requester)
    name = requester.get("from") or requester.get("user_id") or "unknown"
    parts = [name]
    if requester.get("user_id") and requester.get("from"):
        parts.append(f"(user {requester['user_id']})")
    if requester.get("channel"):
        parts.append(f"in channel {requester['channel']}")
    if requester.get("thread_ts"):
        parts.append(f"thread {requester['thread_ts']}")
    return " ".join(parts)


def _should_filter(event: dict) -> bool:
    """Drop events that don't match the repo's project filter (if configured)."""
    if event.get("source") != "linear":
        return False
    from modastack.sdk import get_repo_root
    root = get_repo_root()
    if not root:
        return False
    try:
        from modastack.config import RepoConfig
        rc = RepoConfig.from_file(root)
        if rc.linear_project:
            event_project = event.get("data", {}).get("project", "")
            if event_project and event_project != rc.linear_project:
                log.debug(f"Filtered Linear event: project={event_project}, want={rc.linear_project}")
                return True
    except Exception:
        pass
    return False


def _normalize_event(event_data: dict) -> dict | None:
    """Convert a central server event into the local format."""
    payload = event_data.get("payload", {})
    source = event_data.get("source", "unknown")
    event_type = event_data.get("type", "unknown")

    if source == "github":
        return _normalize_github(event_type, payload)
    elif source == "linear":
        return _normalize_linear(event_type, payload)
    elif source == "slack":
        return _normalize_slack(event_type, payload, event_data.get("workspace", ""))
    return None


def _normalize_github(event_type: str, payload: dict) -> dict | None:
    action = payload.get("action", "")
    repo = (payload.get("repository") or {}).get("full_name", "")
    # The webhook `sender` is the human who triggered the action — surface it
    # as `from` so issue/PR/push events have a consistent originator the
    # manager can attribute work to (best-effort; empty for bot/system events).
    sender = (payload.get("sender") or {}).get("login", "")

    if event_type == "github.issues":
        issue = payload.get("issue", {})
        return {
            "type": f"task.{action}", "source": "github",
            "data": {
                "issue_id": str(issue.get("number", "")),
                "title": issue.get("title", ""),
                "body": issue.get("body", ""),
                "labels": [l["name"] for l in issue.get("labels", [])],
                "assignees": [a["login"] for a in issue.get("assignees", [])],
                "state": issue.get("state", ""),
                "repo": repo,
                "from": sender,
                "url": issue.get("html_url", ""),
            },
        }

    if event_type == "github.issue_comment":
        issue = payload.get("issue", {})
        comment = payload.get("comment", {})
        return {
            "type": f"comment.{action}", "source": "github",
            "data": {
                "issue_id": str(issue.get("number", "")),
                "title": issue.get("title", ""),
                "repo": repo,
                "from": comment.get("user", {}).get("login", ""),
                "text": comment.get("body", ""),
                "url": comment.get("html_url", ""),
            },
        }

    if event_type == "github.pull_request":
        pr = payload.get("pull_request", {})
        return {
            "type": f"pr.{action}", "source": "github",
            "data": {
                "pr_number": pr.get("number"),
                "title": pr.get("title", ""),
                "repo": repo,
                "branch": pr.get("head", {}).get("ref", ""),
                "state": pr.get("state", ""),
                "merged": pr.get("merged", False),
                # Actor who triggered the PR action; falls back to the PR author.
                "from": sender or (pr.get("user") or {}).get("login", ""),
                "pr_url": pr.get("html_url", ""),
            },
        }

    if event_type == "github.pull_request_review":
        pr = payload.get("pull_request", {})
        review = payload.get("review", {})
        return {
            "type": f"review.{action}", "source": "github",
            "data": {
                "pr_number": pr.get("number"),
                "title": pr.get("title", ""),
                "repo": repo,
                "from": review.get("user", {}).get("login", ""),
                "state": review.get("state", ""),
                "text": review.get("body", ""),
                "pr_url": pr.get("html_url", ""),
            },
        }

    if event_type == "github.pull_request_review_comment":
        pr = payload.get("pull_request", {})
        comment = payload.get("comment", {})
        return {
            "type": f"review_comment.{action}", "source": "github",
            "data": {
                "pr_number": pr.get("number"),
                "repo": repo,
                "from": comment.get("user", {}).get("login", ""),
                "text": comment.get("body", ""),
                "path": comment.get("path", ""),
                "url": comment.get("html_url", ""),
            },
        }

    if event_type == "github.check_run":
        check = payload.get("check_run", {})
        return {
            "type": f"ci.{check.get('conclusion', action)}", "source": "github",
            "data": {
                "name": check.get("name", ""),
                "repo": repo,
                "status": check.get("status", ""),
                "conclusion": check.get("conclusion", ""),
                "branch": check.get("check_suite", {}).get("head_branch", ""),
                "url": check.get("html_url", ""),
            },
        }

    if event_type == "github.workflow_run":
        run = payload.get("workflow_run", {})
        return {
            "type": f"ci.workflow_{action}", "source": "github",
            "data": {
                "name": run.get("name", ""),
                "repo": repo,
                "status": run.get("status", ""),
                "conclusion": run.get("conclusion", ""),
                "branch": run.get("head_branch", ""),
                "url": run.get("html_url", ""),
            },
        }

    if event_type in ("github.push", "github.create", "github.delete"):
        return {
            "type": event_type.replace("github.", "git."), "source": "github",
            "data": {
                "ref": payload.get("ref", ""),
                "repo": repo,
                "from": sender,
                "sender": sender,
            },
        }

    return {
        "type": event_type, "source": "github",
        "data": {"repo": repo, "action": action},
    }


def _normalize_linear(event_type: str, payload: dict) -> dict | None:
    data = payload.get("data", {})
    actor = (data.get("assignee") or {}) or (data.get("creator") or {})
    project = data.get("project") or {}
    normalized = {
        "type": event_type, "source": "linear",
        "data": {
            "issue_id": data.get("identifier", ""),
            "linear_id": data.get("id", ""),
            "title": data.get("title", ""),
            "state": (data.get("state", {}) or {}).get("name", ""),
            "team_key": (data.get("team", {}) or {}).get("key", ""),
            "from": actor.get("name", "") or actor.get("displayName", ""),
        },
    }
    if project.get("name"):
        normalized["data"]["project"] = project["name"]
    return normalized


def _normalize_slack(event_type: str, payload: dict, workspace: str) -> dict | None:
    user_id = payload.get("user_id", "")
    token = ""
    try:
        from modastack.config import LocalConfig
        from modastack.sdk import get_repo_root
        root = get_repo_root()
        if root:
            token = LocalConfig.load(root).slack_bot_token
    except Exception:
        pass
    user_name = _resolve_slack_user(token, user_id)
    text = payload.get("text", "")
    if event_type == "slack.mention":
        text = re.sub(r'^<@U\w+>\s*', '', text).strip()

    return {
        "type": event_type, "source": "slack",
        "data": {
            "from": user_name,
            # Stable Slack identity (Uxxxx) the manager keys on — survives
            # display-name changes. Already resolved above; no longer discarded.
            "user_id": user_id,
            "text": text,
            "channel": payload.get("channel", ""),
            "channel_type": payload.get("channel_type", ""),
            "workspace": workspace,
            "ts": payload.get("ts", ""),
            "thread_ts": payload.get("thread_ts", ""),
        },
    }


class EventServerClient:
    """WebSocket client that connects to the centralized event server.

    Normalized events are pushed to `event_queue` for the consumer to drain.
    The WebSocket callback never blocks on inject or Slack replies.
    """

    def __init__(self, server_url: str, deployment_id: str, api_key: str,
                 on_event: callable = None):
        self.server_url = server_url.rstrip("/")
        self.deployment_id = deployment_id
        self.api_key = api_key
        self.on_event = on_event
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reconnect_delay = 1

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="event-client")
        self._thread.start()
        log.info(f"Event client connecting to {self.server_url}")
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        if self._ws:
            self._ws.close()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._connect()
            except Exception as e:
                log.warning(f"Event client error: {e}")

            if self._stop.is_set():
                break

            delay = min(self._reconnect_delay, 60)
            log.info(f"Event client reconnecting in {delay}s")
            self._stop.wait(timeout=delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    def _connect(self) -> None:
        last_seen = _load_cursor()
        ws_url = (
            f"{self.server_url.replace('https://', 'wss://').replace('http://', 'ws://')}"
            f"/deployments/{self.deployment_id}/subscribe?last_seen={last_seen}"
        )

        def on_message(ws, message):
            try:
                msg = json.loads(message)
            except json.JSONDecodeError:
                return

            msg_type = msg.get("type")

            if msg_type == "connected":
                log.info(f"Event client connected (next_seq: {msg.get('next_seq')})")
                self._reconnect_delay = 1
                return

            if msg_type == "pong":
                return

            if msg_type in ("event", "replay"):
                data = msg.get("data", {})
                seq = data.get("seq", 0)

                normalized = _normalize_event(data)
                if normalized and not _should_filter(normalized):
                    _log_event(normalized)
                    event_queue.put(normalized)
                    log.info(f"Event queued: {normalized['source']}/{normalized['type']}")

                    if self.on_event:
                        self.on_event(normalized)

                if seq > 0:
                    _save_cursor(seq)
                    ws.send(json.dumps({"type": "ack", "seq": seq}))

        def on_error(ws, error):
            log.warning(f"Event client WebSocket error: {error}")

        def on_close(ws, close_status, close_msg):
            log.info(f"Event client disconnected: {close_status} {close_msg}")

        self._ws = websocket.WebSocketApp(
            ws_url,
            header={"Authorization": f"Bearer {self.api_key}"},
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._ws.run_forever(ping_interval=30, ping_timeout=10, sslopt={"context": ssl_context})


def start_event_client(server_url: str, deployment_id: str, api_key: str,
                       on_event: callable = None) -> threading.Thread:
    client = EventServerClient(server_url, deployment_id, api_key, on_event=on_event)
    return client.start()
