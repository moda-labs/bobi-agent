"""Webhook receiver — lightweight HTTP server for GitHub, Linear, Slack events.

Runs in a background thread. Each source has its own endpoint:
  POST /webhooks/github  — GitHub webhook events
  POST /webhooks/linear  — Linear webhook events
  POST /webhooks/slack   — Slack Events API

Adding a new webhook source: add a handler function and register the route.
"""

import hashlib
import hmac
import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from .bus import get_bus

log = logging.getLogger(__name__)


def _github_issue_state(labels: list[str], gh_state: str) -> str:
    """Map GitHub Issue labels to workflow state names."""
    label_map = {
        "status:in-progress": "In Progress",
        "status:blocked": "Blocked",
        "status:in-review": "In Review",
        "status:todo": "Todo",
    }
    for label, state in label_map.items():
        if label in labels:
            return state
    return "Done" if gh_state == "closed" else "Todo"


def _resolve_linear_repo(prefix: str) -> str:
    """Map a Linear project prefix (e.g. 'AGD') to the repo path.

    Returns the repo path string, or empty string if not configured.
    """
    from modastack.config import GlobalConfig, RepoConfig
    config = GlobalConfig.load()
    for repo_path in config.repos:
        try:
            rc = RepoConfig.from_file(repo_path)
            if rc.linear_project == prefix:
                return str(rc.path)
        except FileNotFoundError:
            continue
    return ""


def _normalize_linear_action(action: str, data: dict) -> str:
    """Map Linear webhook action to a normalized task.* action.

    Linear uses 'create'/'update'/'remove'. We normalize to match
    GitHub Issues conventions so workflows can trigger on either source.
    """
    if action == "create":
        return "created"
    if action == "remove":
        return "closed"
    if action == "update":
        # Detect assignment: Linear sends action=update with an assignee
        if data.get("assignee"):
            return "assigned"
        state_name = data.get("state", {}).get("name", "")
        if state_name == "Done":
            return "closed"
        return "updated"
    return action


class WebhookHandler(BaseHTTPRequestHandler):
    # Set by start_server
    github_secret: str = ""
    linear_secret: str = ""
    slack_signing_secret: str = ""

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            if self.path == "/webhooks/github":
                self._handle_github(body)
            elif self.path == "/webhooks/linear":
                self._handle_linear(body)
            elif self.path == "/webhooks/slack":
                self._handle_slack(body)
            else:
                self.send_response(404)
                self.end_headers()
                return
        except Exception as e:
            log.error(f"Webhook handler error: {e}")
            self.send_response(500)
            self.end_headers()
            return

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_github(self, body: bytes):
        # Verify signature if secret is configured
        if self.github_secret:
            sig = self.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(
                self.github_secret.encode(), body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                self.send_response(401)
                self.end_headers()
                return

        payload = json.loads(body)
        event_type = self.headers.get("X-GitHub-Event", "unknown")
        bus = get_bus()

        if event_type == "pull_request":
            action = payload.get("action", "")
            pr = payload.get("pull_request", {})
            bus.push(f"github.pr.{action}", "github", {
                "action": action,
                "repo": payload.get("repository", {}).get("full_name", ""),
                "number": pr.get("number"),
                "title": pr.get("title", ""),
                "branch": pr.get("head", {}).get("ref", ""),
                "state": pr.get("state", ""),
                "merged": pr.get("merged", False),
                "url": pr.get("html_url", ""),
                "author": pr.get("user", {}).get("login", ""),
            })

        elif event_type == "pull_request_review":
            review = payload.get("review", {})
            pr = payload.get("pull_request", {})
            bus.push("github.pr.review", "github", {
                "repo": payload.get("repository", {}).get("full_name", ""),
                "pr_number": pr.get("number"),
                "pr_title": pr.get("title", ""),
                "reviewer": review.get("user", {}).get("login", ""),
                "state": review.get("state", ""),  # approved, changes_requested, commented
                "body": review.get("body", ""),
            })

        elif event_type == "issues":
            action = payload.get("action", "")
            issue = payload.get("issue", {})
            label_names = [l["name"] for l in issue.get("labels", [])]
            assignee_logins = [a["login"] for a in issue.get("assignees", [])]
            if action in ("opened", "labeled", "unlabeled", "closed", "reopened", "assigned"):
                event_data = {
                    "action": action,
                    "issue_id": f"#{issue.get('number', '')}",
                    "task_id": str(issue.get("number", "")),
                    "title": issue.get("title", ""),
                    "state": _github_issue_state(label_names, issue.get("state", "")),
                    "labels": label_names,
                    "assignees": assignee_logins,
                    "repo": payload.get("repository", {}).get("full_name", ""),
                    "url": issue.get("html_url", ""),
                }
                if action == "assigned":
                    assignee = payload.get("assignee", {}).get("login", "")
                    event_data["assigned_to"] = assignee
                bus.push(f"task.{action}", "github-issues", event_data)

        elif event_type == "issue_comment":
            comment = payload.get("comment", {})
            issue = payload.get("issue", {})
            is_pr = "pull_request" in issue
            bus.push("github.comment" if is_pr else "task.comment", "github", {
                "repo": payload.get("repository", {}).get("full_name", ""),
                "issue_number": issue.get("number"),
                "issue_id": f"#{issue.get('number', '')}",
                "author": comment.get("user", {}).get("login", ""),
                "body": comment.get("body", "")[:500],
            })

        elif event_type == "ping":
            log.info(f"GitHub ping received: {payload.get('zen', '')}")

        self.send_response(200)
        self.end_headers()

    def _handle_linear(self, body: bytes):
        payload = json.loads(body)
        action = payload.get("action", "")
        event_type = payload.get("type", "")
        data = payload.get("data", {})
        bus = get_bus()

        issue_id = ""
        if event_type == "Issue":
            issue_id = data.get("identifier", "")
        elif event_type == "Comment":
            issue_id = data.get("issue", {}).get("identifier", "")

        # Resolve project prefix → repo path
        repo_path = ""
        if issue_id:
            prefix = issue_id.split("-")[0]
            repo_path = _resolve_linear_repo(prefix)
            if not repo_path:
                self.send_response(200)
                self.end_headers()
                return

        if event_type == "Issue":
            issue_id = data.get("identifier", "")
            assignee = data.get("assignee", {})
            state_name = data.get("state", {}).get("name", "")

            # Normalize to task.* events (same shape as GitHub Issues)
            normalized_action = _normalize_linear_action(action, data)
            event_data = {
                "action": normalized_action,
                "issue_id": issue_id,
                "task_id": data.get("id", ""),
                "linear_id": data.get("id", ""),
                "title": data.get("title", ""),
                "body": (data.get("description") or "")[:500],
                "state": state_name,
                "labels": [l.get("name", "") for l in data.get("labels", [])],
                "repo": repo_path,
            }
            if assignee:
                event_data["assigned_to"] = assignee.get("name", "")

            bus.push(f"task.{normalized_action}", "linear", event_data)

        elif event_type == "Comment":
            issue = data.get("issue", {})
            bus.push("linear.comment", "linear", {
                "issue_id": issue.get("identifier", ""),
                "linear_id": issue.get("id", ""),
                "author": data.get("user", {}).get("name", ""),
                "body": data.get("body", "")[:500],
                "repo": repo_path,
            })

        self.send_response(200)
        self.end_headers()

    def _handle_slack(self, body: bytes):
        payload = json.loads(body)

        # Slack URL verification challenge
        if payload.get("type") == "url_verification":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"challenge": payload["challenge"]}).encode())
            return

        bus = get_bus()

        if payload.get("type") == "event_callback":
            event = payload.get("event", {})
            event_type = event.get("type", "")

            if event_type == "message" and not event.get("bot_id"):
                bus.push("slack.message", "slack", {
                    "channel_id": event.get("channel", ""),
                    "user_id": event.get("user", ""),
                    "text": event.get("text", "")[:500],
                    "ts": event.get("ts", ""),
                })

        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        # Suppress default access logs
        pass


def start_server(port: int = 8080, github_secret: str = "",
                 linear_secret: str = "", slack_signing_secret: str = "") -> threading.Thread:
    """Start the webhook server in a background thread."""
    WebhookHandler.github_secret = github_secret
    WebhookHandler.linear_secret = linear_secret
    WebhookHandler.slack_signing_secret = slack_signing_secret

    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info(f"Webhook server listening on port {port}")
    return thread
