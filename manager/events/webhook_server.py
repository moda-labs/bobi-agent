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

        elif event_type == "issue_comment":
            comment = payload.get("comment", {})
            bus.push("github.comment", "github", {
                "repo": payload.get("repository", {}).get("full_name", ""),
                "issue_number": payload.get("issue", {}).get("number"),
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

        # Filter to configured projects only
        issue_id = ""
        if event_type == "Issue":
            issue_id = data.get("identifier", "")
        elif event_type == "Comment":
            issue_id = data.get("issue", {}).get("identifier", "")

        if issue_id:
            prefix = issue_id.split("-")[0]
            from modastack.config import GlobalConfig
            configured_projects = {
                entry.linear_project
                for entry in GlobalConfig.load().repos
                if entry.linear_project
            }
            if prefix not in configured_projects:
                self.send_response(200)
                self.end_headers()
                return

        if event_type == "Issue":
            issue_id = data.get("identifier", "")
            bus.push(f"linear.issue.{action}", "linear", {
                "action": action,
                "issue_id": issue_id,
                "linear_id": data.get("id", ""),
                "title": data.get("title", ""),
                "state": data.get("state", {}).get("name", ""),
                "labels": [l.get("name", "") for l in data.get("labels", [])],
            })

        elif event_type == "Comment":
            issue = data.get("issue", {})
            bus.push("linear.comment", "linear", {
                "issue_id": issue.get("identifier", ""),
                "linear_id": issue.get("id", ""),
                "author": data.get("user", {}).get("name", ""),
                "body": data.get("body", "")[:500],
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
