"""Tests for event client — normalization, formatting, queue ingestion."""

import json
from unittest.mock import patch, MagicMock

from modastack.manager.events.event_client import (
    _normalize_event,
    _normalize_slack,
    format_event_for_manager,
    event_queue,
)


class TestNormalizeGitHub:

    def test_issues_opened(self):
        data = {
            "source": "github", "type": "github.issues",
            "payload": {
                "action": "opened",
                "repository": {"full_name": "moda-labs/test"},
                "issue": {
                    "number": 42, "title": "Bug", "body": "details",
                    "labels": [{"name": "bug"}], "assignees": [{"login": "zach"}],
                    "state": "open", "html_url": "https://github.com/...",
                },
            },
        }
        result = _normalize_event(data)
        assert result["type"] == "task.opened"
        assert result["source"] == "github"
        assert result["data"]["issue_id"] == "42"
        assert result["data"]["title"] == "Bug"
        assert result["data"]["labels"] == ["bug"]

    def test_pull_request_merged(self):
        data = {
            "source": "github", "type": "github.pull_request",
            "payload": {
                "action": "closed",
                "repository": {"full_name": "moda-labs/test"},
                "pull_request": {
                    "number": 10, "title": "Fix", "state": "closed",
                    "merged": True, "head": {"ref": "fix-branch"},
                    "html_url": "https://github.com/...",
                },
            },
        }
        result = _normalize_event(data)
        assert result["type"] == "pr.closed"
        assert result["data"]["merged"] is True

    def test_push_event(self):
        data = {
            "source": "github", "type": "github.push",
            "payload": {"ref": "refs/heads/main", "sender": {"login": "zach"},
                        "repository": {"full_name": "moda-labs/test"}},
        }
        result = _normalize_event(data)
        assert result["type"] == "git.push"

    def test_unknown_github_event(self):
        data = {
            "source": "github", "type": "github.star",
            "payload": {"action": "created", "repository": {"full_name": "moda-labs/test"}},
        }
        result = _normalize_event(data)
        assert result["type"] == "github.star"


class TestNormalizeLinear:

    def test_issue_update(self):
        data = {
            "source": "linear", "type": "linear.Issue.update",
            "payload": {
                "data": {
                    "identifier": "ENG-42", "title": "Add caching",
                    "state": {"name": "In Progress"},
                    "team": {"key": "ENG"},
                },
            },
        }
        result = _normalize_event(data)
        assert result["type"] == "linear.Issue.update"
        assert result["data"]["issue_id"] == "ENG-42"
        assert result["data"]["state"] == "In Progress"


class TestNormalizeSlack:

    @patch("modastack.manager.events.event_client.GlobalConfig")
    @patch("modastack.manager.events.event_client._resolve_slack_user")
    def test_dm(self, mock_resolve, mock_config):
        mock_resolve.return_value = "Zach"
        mock_config.load.return_value = MagicMock(slack_bot_token="xoxb-test")
        mock_config.load.return_value.slack_token_for.return_value = "xoxb-test"

        data = {
            "source": "slack", "type": "slack.dm", "workspace": "T123",
            "payload": {
                "user_id": "U123", "channel": "D456",
                "channel_type": "im", "text": "hello",
                "ts": "123.456", "thread_ts": "",
            },
        }
        result = _normalize_event(data)
        assert result["type"] == "slack.dm"
        assert result["data"]["from"] == "Zach"
        assert result["data"]["text"] == "hello"
        assert result["data"]["workspace"] == "T123"
        assert result["data"]["channel"] == "D456"

    @patch("modastack.manager.events.event_client.GlobalConfig")
    @patch("modastack.manager.events.event_client._resolve_slack_user")
    def test_mention_strips_bot_prefix(self, mock_resolve, mock_config):
        mock_resolve.return_value = "Zach"
        mock_config.load.return_value = MagicMock(slack_bot_token="xoxb-test")
        mock_config.load.return_value.slack_token_for.return_value = "xoxb-test"

        data = {
            "source": "slack", "type": "slack.mention", "workspace": "T123",
            "payload": {
                "user_id": "U123", "channel": "C789",
                "channel_type": "channel", "text": "<@UBOTID> check deploy",
                "ts": "123.456", "thread_ts": "",
            },
        }
        result = _normalize_event(data)
        assert result["data"]["text"] == "check deploy"

    def test_unknown_source_returns_none(self):
        data = {"source": "jira", "type": "jira.issue", "payload": {}}
        assert _normalize_event(data) is None


class TestFormatEventForManager:

    def test_slack_event(self):
        event = {
            "type": "slack.dm", "source": "slack",
            "data": {"from": "Zach", "text": "hello", "channel": "D456",
                     "workspace": "T123", "thread_ts": ""},
        }
        text = format_event_for_manager(event)
        assert "Event: slack/slack.dm" in text
        assert "from: Zach" in text
        assert "text: hello" in text
        assert "channel: D456" in text
        assert "workspace: T123" in text

    def test_github_event(self):
        event = {
            "type": "task.opened", "source": "github",
            "data": {"issue_id": "42", "title": "Bug", "repo": "moda-labs/test"},
        }
        text = format_event_for_manager(event)
        assert "Event: github/task.opened" in text
        assert "issue_id: 42" in text

    def test_empty_fields_omitted(self):
        event = {
            "type": "slack.dm", "source": "slack",
            "data": {"from": "Zach", "text": "hi", "channel": "D456",
                     "workspace": "T123", "thread_ts": ""},
        }
        text = format_event_for_manager(event)
        assert "thread_ts" not in text


class TestEventQueue:

    def test_queue_starts_empty(self):
        while not event_queue.empty():
            event_queue.get_nowait()
        assert event_queue.empty()
