"""Tests for webhook server handlers."""

import json
from io import BytesIO
from unittest.mock import patch, MagicMock

from modastack.manager.events.webhook_server import (
    _github_issue_state, _normalize_linear_action, WebhookHandler,
)


class TestGithubIssueState:

    def test_in_progress_label(self):
        assert _github_issue_state(["status:in-progress", "bug"], "open") == "In Progress"

    def test_blocked_label(self):
        assert _github_issue_state(["status:blocked"], "open") == "Blocked"

    def test_in_review_label(self):
        assert _github_issue_state(["status:in-review"], "open") == "In Review"

    def test_todo_label(self):
        assert _github_issue_state(["status:todo"], "open") == "Todo"

    def test_no_status_label_open(self):
        assert _github_issue_state(["bug", "agent"], "open") == "Todo"

    def test_no_status_label_closed(self):
        assert _github_issue_state(["bug"], "closed") == "Done"

    def test_empty_labels_open(self):
        assert _github_issue_state([], "open") == "Todo"

    def test_priority_first_match(self):
        """First matching label wins."""
        result = _github_issue_state(["status:in-progress", "status:blocked"], "open")
        assert result == "In Progress"


class _FakeRequest:
    """Minimal fake HTTP request for testing WebhookHandler."""

    def __init__(self, path, body, headers=None):
        self.path = path
        self.body = body
        self.headers = headers or {}
        self.response_code = None
        self.response_headers = {}
        self.response_body = b""

    def makefile(self, *args, **kwargs):
        return BytesIO(self.body)


def _make_handler(path, body, headers=None):
    """Create a WebhookHandler with mocked internals for testing."""
    handler = WebhookHandler.__new__(WebhookHandler)
    handler.path = path
    handler.headers = headers or {}
    handler.headers.setdefault("Content-Length", str(len(body)))
    handler.rfile = BytesIO(body)
    handler.wfile = BytesIO()
    handler._response_code = None

    def send_response(code):
        handler._response_code = code
    handler.send_response = send_response
    handler.end_headers = lambda: None
    handler.send_header = lambda k, v: None

    return handler


class TestGithubWebhook:

    @patch("modastack.manager.events.webhook_server.get_bus")
    def test_pr_opened(self, mock_get_bus):
        bus = MagicMock()
        mock_get_bus.return_value = bus

        payload = {
            "action": "opened",
            "pull_request": {
                "number": 42,
                "title": "Add feature",
                "head": {"ref": "feature-branch"},
                "state": "open",
                "merged": False,
                "html_url": "https://github.com/org/repo/pull/42",
                "user": {"login": "dev"},
            },
            "repository": {"full_name": "org/repo"},
        }
        body = json.dumps(payload).encode()
        handler = _make_handler("/webhooks/github", body, {"X-GitHub-Event": "pull_request"})
        handler.github_secret = ""
        handler._handle_github(body)

        bus.push.assert_called_once()
        call_args = bus.push.call_args
        assert call_args[0][0] == "github.pr.opened"
        assert call_args[0][1] == "github"
        assert call_args[0][2]["number"] == 42
        assert call_args[0][2]["branch"] == "feature-branch"

    @patch("modastack.manager.events.webhook_server.get_bus")
    def test_pr_review(self, mock_get_bus):
        bus = MagicMock()
        mock_get_bus.return_value = bus

        payload = {
            "review": {"user": {"login": "reviewer"}, "state": "changes_requested", "body": "Fix this"},
            "pull_request": {"number": 10, "title": "PR title"},
            "repository": {"full_name": "org/repo"},
        }
        body = json.dumps(payload).encode()
        handler = _make_handler("/webhooks/github", body, {"X-GitHub-Event": "pull_request_review"})
        handler.github_secret = ""
        handler._handle_github(body)

        bus.push.assert_called_once()
        data = bus.push.call_args[0][2]
        assert data["reviewer"] == "reviewer"
        assert data["state"] == "changes_requested"

    @patch("modastack.manager.events.webhook_server.get_bus")
    def test_issue_opened(self, mock_get_bus):
        bus = MagicMock()
        mock_get_bus.return_value = bus

        payload = {
            "action": "opened",
            "issue": {
                "number": 5,
                "title": "Bug report",
                "state": "open",
                "labels": [{"name": "agent"}, {"name": "status:todo"}],
                "assignees": [],
                "html_url": "https://github.com/org/repo/issues/5",
            },
            "repository": {"full_name": "org/repo"},
        }
        body = json.dumps(payload).encode()
        handler = _make_handler("/webhooks/github", body, {"X-GitHub-Event": "issues"})
        handler.github_secret = ""
        handler._handle_github(body)

        bus.push.assert_called_once()
        assert bus.push.call_args[0][0] == "task.opened"
        data = bus.push.call_args[0][2]
        assert data["state"] == "Todo"
        assert "agent" in data["labels"]

    @patch("modastack.manager.events.webhook_server.get_bus")
    def test_issue_comment_on_pr(self, mock_get_bus):
        bus = MagicMock()
        mock_get_bus.return_value = bus

        payload = {
            "comment": {"user": {"login": "dev"}, "body": "Looks good"},
            "issue": {"number": 10, "pull_request": {}},
            "repository": {"full_name": "org/repo"},
        }
        body = json.dumps(payload).encode()
        handler = _make_handler("/webhooks/github", body, {"X-GitHub-Event": "issue_comment"})
        handler.github_secret = ""
        handler._handle_github(body)

        assert bus.push.call_args[0][0] == "github.comment"

    @patch("modastack.manager.events.webhook_server.get_bus")
    def test_issue_comment_on_issue(self, mock_get_bus):
        bus = MagicMock()
        mock_get_bus.return_value = bus

        payload = {
            "comment": {"user": {"login": "dev"}, "body": "Info"},
            "issue": {"number": 5},
            "repository": {"full_name": "org/repo"},
        }
        body = json.dumps(payload).encode()
        handler = _make_handler("/webhooks/github", body, {"X-GitHub-Event": "issue_comment"})
        handler.github_secret = ""
        handler._handle_github(body)

        assert bus.push.call_args[0][0] == "task.comment"

    @patch("modastack.manager.events.webhook_server.get_bus")
    def test_ping_event(self, mock_get_bus):
        bus = MagicMock()
        mock_get_bus.return_value = bus

        payload = {"zen": "Keep it simple"}
        body = json.dumps(payload).encode()
        handler = _make_handler("/webhooks/github", body, {"X-GitHub-Event": "ping"})
        handler.github_secret = ""
        handler._handle_github(body)

        bus.push.assert_not_called()
        assert handler._response_code == 200


class TestSlackWebhook:

    @patch("modastack.manager.events.webhook_server.get_bus")
    def test_url_verification(self, mock_get_bus):
        payload = {"type": "url_verification", "challenge": "test-challenge-123"}
        body = json.dumps(payload).encode()
        handler = _make_handler("/webhooks/slack", body)
        handler.slack_signing_secret = ""
        handler._handle_slack(body)

        assert handler._response_code == 200
        response = handler.wfile.getvalue()
        assert b"test-challenge-123" in response

    @patch("modastack.manager.events.webhook_server.get_bus")
    def test_message_event(self, mock_get_bus):
        bus = MagicMock()
        mock_get_bus.return_value = bus

        payload = {
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel": "C123",
                "user": "U456",
                "text": "Hello bot",
                "ts": "1234567890.123456",
            },
        }
        body = json.dumps(payload).encode()
        handler = _make_handler("/webhooks/slack", body)
        handler.slack_signing_secret = ""
        handler._handle_slack(body)

        bus.push.assert_called_once()
        data = bus.push.call_args[0][2]
        assert data["channel_id"] == "C123"
        assert data["text"] == "Hello bot"

    @patch("modastack.manager.events.webhook_server.get_bus")
    def test_ignores_bot_messages(self, mock_get_bus):
        bus = MagicMock()
        mock_get_bus.return_value = bus

        payload = {
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel": "C123",
                "bot_id": "B789",
                "text": "Bot reply",
                "ts": "123",
            },
        }
        body = json.dumps(payload).encode()
        handler = _make_handler("/webhooks/slack", body)
        handler.slack_signing_secret = ""
        handler._handle_slack(body)

        bus.push.assert_not_called()


class TestNormalizeLinearAction:

    def test_create(self):
        assert _normalize_linear_action("create", {}) == "created"

    def test_remove(self):
        assert _normalize_linear_action("remove", {}) == "closed"

    def test_update_with_assignee(self):
        data = {"assignee": {"id": "user-1", "name": "Zach"}}
        assert _normalize_linear_action("update", data) == "assigned"

    def test_update_to_done(self):
        data = {"state": {"name": "Done"}}
        assert _normalize_linear_action("update", data) == "closed"

    def test_update_generic(self):
        data = {"state": {"name": "In Progress"}}
        assert _normalize_linear_action("update", data) == "updated"

    def test_update_no_assignee_no_state(self):
        assert _normalize_linear_action("update", {}) == "updated"


class TestLinearWebhook:

    @patch("modastack.manager.events.webhook_server._resolve_linear_repo", return_value="/home/ubuntu/dev/myrepo")
    @patch("modastack.manager.events.webhook_server.get_bus")
    def test_issue_assigned(self, mock_get_bus, mock_resolve):
        bus = MagicMock()
        mock_get_bus.return_value = bus

        payload = {
            "action": "update",
            "type": "Issue",
            "data": {
                "identifier": "AGD-42",
                "id": "uuid-1",
                "title": "Add rate limiting",
                "description": "We need rate limiting",
                "state": {"name": "In Progress"},
                "labels": [{"name": "agent"}],
                "assignee": {"id": "user-1", "name": "Zach"},
            },
        }
        body = json.dumps(payload).encode()
        handler = _make_handler("/webhooks/linear", body)
        handler.linear_secret = ""
        handler._handle_linear(body)

        bus.push.assert_called_once()
        event_type = bus.push.call_args[0][0]
        assert event_type == "task.assigned"
        data = bus.push.call_args[0][2]
        assert data["issue_id"] == "AGD-42"
        assert data["repo"] == "/home/ubuntu/dev/myrepo"
        assert data["assigned_to"] == "Zach"

    @patch("modastack.manager.events.webhook_server._resolve_linear_repo", return_value="/home/ubuntu/dev/myrepo")
    @patch("modastack.manager.events.webhook_server.get_bus")
    def test_issue_created(self, mock_get_bus, mock_resolve):
        bus = MagicMock()
        mock_get_bus.return_value = bus

        payload = {
            "action": "create",
            "type": "Issue",
            "data": {
                "identifier": "AGD-43",
                "id": "uuid-2",
                "title": "New feature",
                "state": {"name": "Todo"},
                "labels": [],
            },
        }
        body = json.dumps(payload).encode()
        handler = _make_handler("/webhooks/linear", body)
        handler.linear_secret = ""
        handler._handle_linear(body)

        assert bus.push.call_args[0][0] == "task.created"

    @patch("modastack.manager.events.webhook_server._resolve_linear_repo", return_value="")
    @patch("modastack.manager.events.webhook_server.get_bus")
    def test_unconfigured_project_ignored(self, mock_get_bus, mock_resolve):
        bus = MagicMock()
        mock_get_bus.return_value = bus

        payload = {
            "action": "update",
            "type": "Issue",
            "data": {"identifier": "UNKNOWN-1", "id": "x", "title": "Test", "state": {}, "labels": []},
        }
        body = json.dumps(payload).encode()
        handler = _make_handler("/webhooks/linear", body)
        handler.linear_secret = ""
        handler._handle_linear(body)

        bus.push.assert_not_called()

    @patch("modastack.manager.events.webhook_server._resolve_linear_repo", return_value="/repo")
    @patch("modastack.manager.events.webhook_server.get_bus")
    def test_comment_includes_repo(self, mock_get_bus, mock_resolve):
        bus = MagicMock()
        mock_get_bus.return_value = bus

        payload = {
            "action": "create",
            "type": "Comment",
            "data": {
                "issue": {"identifier": "AGD-42", "id": "uuid-1"},
                "user": {"name": "Zach"},
                "body": "Looks good",
            },
        }
        body = json.dumps(payload).encode()
        handler = _make_handler("/webhooks/linear", body)
        handler.linear_secret = ""
        handler._handle_linear(body)

        data = bus.push.call_args[0][2]
        assert data["repo"] == "/repo"


class TestHealthEndpoint:

    def test_health_returns_ok(self):
        handler = _make_handler("/health", b"")
        handler.do_GET()
        assert handler._response_code == 200
        assert b'"status": "ok"' in handler.wfile.getvalue()

    def test_unknown_get_returns_404(self):
        handler = _make_handler("/unknown", b"")
        handler.do_GET()
        assert handler._response_code == 404
