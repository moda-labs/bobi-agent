"""Unit tests for the local event server (FastAPI)."""

import hashlib
import hmac
import json
import time

import pytest
from starlette.testclient import TestClient

from modastack.manager.events.event_server import app, _deployments, _api_key_index, _subscription_index, _lock


@pytest.fixture(autouse=True)
def _reset_state():
    """Clear all in-memory state between tests."""
    _deployments.clear()
    _api_key_index.clear()
    _subscription_index.clear()
    app.state.webhook_secret = ""
    app.state.slack_signing_secret = ""
    yield
    _deployments.clear()
    _api_key_index.clear()
    _subscription_index.clear()


@pytest.fixture
def client():
    return TestClient(app)


def _register(client, name="test-deploy", subscriptions=None):
    """Helper: register a deployment and return (deployment_id, api_key)."""
    subs = subscriptions or ["org/repo"]
    resp = client.post("/deployments", json={"name": name, "subscriptions": subs})
    assert resp.status_code == 201
    data = resp.json()
    return data["deployment_id"], data["api_key"]


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["mode"] == "local"
        assert data["deployments"] == 0

    def test_health_counts_deployments(self, client):
        _register(client)
        _register(client, name="second", subscriptions=["other/repo"])
        resp = client.get("/health")
        assert resp.json()["deployments"] == 2


# ---------------------------------------------------------------------------
# GitHub webhook
# ---------------------------------------------------------------------------


class TestGitHubWebhook:
    def _issue_payload(self):
        return {
            "action": "opened",
            "issue": {
                "number": 42,
                "title": "Test issue",
                "body": "Description",
                "labels": [],
                "assignees": [],
                "state": "open",
                "html_url": "https://github.com/org/repo/issues/42",
            },
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "testuser"},
        }

    def test_github_webhook_routes_to_deployment(self, client):
        dep_id, api_key = _register(client, subscriptions=["org/repo"])
        resp = client.post(
            "/webhooks/github",
            json=self._issue_payload(),
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 200
        assert resp.json()["delivered_to"] == 1

        # Check event in buffer
        dep = _deployments[dep_id]
        assert len(dep.event_buffer) == 1
        event = dep.event_buffer[0]
        assert event["source"] == "github"
        assert event["type"] == "github.issues"
        assert event["repo"] == "org/repo"
        assert event["seq"] == 1

    def test_github_webhook_no_subscribers(self, client):
        _register(client, subscriptions=["other/repo"])
        resp = client.post(
            "/webhooks/github",
            json=self._issue_payload(),
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 200
        assert resp.json()["delivered_to"] == 0

    def test_github_no_repo_returns_400(self, client):
        resp = client.post(
            "/webhooks/github",
            json={"action": "opened"},
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 400

    def test_github_invalid_json(self, client):
        resp = client.post(
            "/webhooks/github",
            content=b"not valid json{{{",
            headers={"Content-Type": "application/json", "X-GitHub-Event": "push"},
        )
        assert resp.status_code == 400


class TestGitHubSignatureVerification:
    SECRET = "test_webhook_secret_123"

    def _issue_payload_bytes(self):
        return json.dumps({
            "action": "opened",
            "issue": {"number": 1, "title": "t", "body": "", "labels": [],
                      "assignees": [], "state": "open", "html_url": ""},
            "repository": {"full_name": "org/repo"},
            "sender": {"login": "u"},
        }).encode()

    def test_valid_signature(self, client):
        app.state.webhook_secret = self.SECRET
        body = self._issue_payload_bytes()
        sig = hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "issues",
                "X-Hub-Signature-256": f"sha256={sig}",
            },
        )
        assert resp.status_code == 200

    def test_invalid_signature(self, client):
        app.state.webhook_secret = self.SECRET
        body = self._issue_payload_bytes()
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "issues",
                "X-Hub-Signature-256": "sha256=invalid_signature_here",
            },
        )
        assert resp.status_code == 401

    def test_missing_signature(self, client):
        app.state.webhook_secret = self.SECRET
        body = self._issue_payload_bytes()
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={"Content-Type": "application/json", "X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 401

    def test_no_secret_skips_verification(self, client):
        app.state.webhook_secret = ""
        body = self._issue_payload_bytes()
        resp = client.post(
            "/webhooks/github",
            content=body,
            headers={"Content-Type": "application/json", "X-GitHub-Event": "issues"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Linear webhook
# ---------------------------------------------------------------------------


class TestLinearWebhook:
    def test_linear_webhook(self, client):
        dep_id, _ = _register(client, subscriptions=["linear:PROJ"])
        payload = {
            "action": "update",
            "type": "Issue",
            "data": {
                "id": "abc-123",
                "identifier": "PROJ-42",
                "title": "Linear task",
                "state": {"name": "In Progress"},
                "team": {"key": "PROJ"},
                "assignee": {"name": "Alice"},
            },
        }
        resp = client.post("/webhooks/linear", json=payload)
        assert resp.status_code == 200
        assert resp.json()["delivered_to"] == 1

        dep = _deployments[dep_id]
        assert len(dep.event_buffer) == 1
        event = dep.event_buffer[0]
        assert event["source"] == "linear"
        assert event["type"] == "linear.Issue.update"
        assert event["team_key"] == "PROJ"


# ---------------------------------------------------------------------------
# Slack webhook
# ---------------------------------------------------------------------------


class TestSlackWebhook:
    def test_url_verification(self, client):
        resp = client.post(
            "/webhooks/slack",
            json={"type": "url_verification", "challenge": "test123"},
        )
        assert resp.status_code == 200
        assert resp.json()["challenge"] == "test123"

    def test_slack_event_routed(self, client):
        dep_id, _ = _register(client, subscriptions=["slack:T12345"])
        payload = {
            "type": "event_callback",
            "team_id": "T12345",
            "event_id": "Ev01",
            "event": {
                "type": "app_mention",
                "user": "U12345",
                "channel": "C12345",
                "channel_type": "channel",
                "text": "<@U99> hello bot",
                "ts": "1234567890.123456",
                "thread_ts": "",
            },
        }
        resp = client.post("/webhooks/slack", json=payload)
        assert resp.status_code == 200
        assert resp.json()["delivered_to"] == 1

        dep = _deployments[dep_id]
        event = dep.event_buffer[0]
        assert event["type"] == "slack.mention"

    def test_slack_retry_rejected(self, client):
        _register(client, subscriptions=["slack:T12345"])
        payload = {
            "type": "event_callback",
            "team_id": "T12345",
            "event": {
                "type": "app_mention",
                "user": "U12345",
                "channel": "C12345",
                "channel_type": "channel",
                "text": "hello",
                "ts": "123",
            },
        }
        resp = client.post(
            "/webhooks/slack",
            json=payload,
            headers={"X-Slack-Retry-Num": "1"},
        )
        assert resp.status_code == 200
        # No event should be buffered
        for dep in _deployments.values():
            assert len(dep.event_buffer) == 0

    def test_slack_bot_message_skipped(self, client):
        _register(client, subscriptions=["slack:T12345"])
        payload = {
            "type": "event_callback",
            "team_id": "T12345",
            "event": {
                "type": "app_mention",
                "user": "U12345",
                "bot_id": "B123",
                "channel": "C12345",
                "channel_type": "channel",
                "text": "bot says hi",
                "ts": "123",
            },
        }
        resp = client.post("/webhooks/slack", json=payload)
        assert resp.status_code == 200
        for dep in _deployments.values():
            assert len(dep.event_buffer) == 0

    def test_slack_dm_event(self, client):
        dep_id, _ = _register(client, subscriptions=["slack:T12345"])
        payload = {
            "type": "event_callback",
            "team_id": "T12345",
            "event": {
                "type": "message",
                "user": "U12345",
                "channel": "D12345",
                "channel_type": "im",
                "text": "hello",
                "ts": "123",
            },
        }
        resp = client.post("/webhooks/slack", json=payload)
        assert resp.status_code == 200
        dep = _deployments[dep_id]
        assert len(dep.event_buffer) == 1
        assert dep.event_buffer[0]["type"] == "slack.dm"

    def test_slack_thread_reply(self, client):
        dep_id, _ = _register(client, subscriptions=["slack:T12345"])
        payload = {
            "type": "event_callback",
            "team_id": "T12345",
            "event": {
                "type": "message",
                "user": "U12345",
                "channel": "C12345",
                "channel_type": "channel",
                "text": "reply",
                "ts": "123.456",
                "thread_ts": "123.000",
            },
        }
        resp = client.post("/webhooks/slack", json=payload)
        assert resp.status_code == 200
        dep = _deployments[dep_id]
        assert len(dep.event_buffer) == 1
        assert dep.event_buffer[0]["type"] == "slack.thread_reply"


# ---------------------------------------------------------------------------
# Deployment management
# ---------------------------------------------------------------------------


class TestDeployments:
    def test_register_deployment(self, client):
        resp = client.post(
            "/deployments",
            json={"name": "my-deploy", "subscriptions": ["org/repo"]},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "deployment_id" in data
        assert data["api_key"].startswith("moda_")

    def test_register_missing_fields(self, client):
        resp = client.post("/deployments", json={"name": "test"})
        assert resp.status_code == 400

    def test_update_subscriptions(self, client):
        dep_id, api_key = _register(client, subscriptions=["org/repo"])
        resp = client.put(
            f"/deployments/{dep_id}/subscriptions",
            json={"add": ["slack:T999"]},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "slack:T999" in data["subscriptions"]
        assert data["added"] == 1

    def test_update_subscriptions_bad_auth(self, client):
        dep_id, _ = _register(client)
        resp = client.put(
            f"/deployments/{dep_id}/subscriptions",
            json={"add": ["slack:T999"]},
            headers={"Authorization": "Bearer bad_key"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Subscription routing
# ---------------------------------------------------------------------------


class TestSubscriptionRouting:
    def test_event_reaches_subscribed_deployment(self, client):
        dep_id, api_key = _register(client, subscriptions=["org/repo"])
        # Send a GitHub webhook for org/repo
        resp = client.post(
            "/webhooks/github",
            json={
                "action": "opened",
                "issue": {"number": 1, "title": "t", "body": "", "labels": [],
                          "assignees": [], "state": "open", "html_url": ""},
                "repository": {"full_name": "org/repo"},
                "sender": {"login": "u"},
            },
            headers={"X-GitHub-Event": "issues"},
        )
        assert resp.json()["delivered_to"] == 1
        dep = _deployments[dep_id]
        assert len(dep.event_buffer) == 1

    def test_event_does_not_reach_unsubscribed(self, client):
        dep1_id, _ = _register(client, name="d1", subscriptions=["org/repo"])
        dep2_id, _ = _register(client, name="d2", subscriptions=["other/repo"])

        client.post(
            "/webhooks/github",
            json={
                "action": "opened",
                "issue": {"number": 1, "title": "t", "body": "", "labels": [],
                          "assignees": [], "state": "open", "html_url": ""},
                "repository": {"full_name": "org/repo"},
                "sender": {"login": "u"},
            },
            headers={"X-GitHub-Event": "issues"},
        )
        assert len(_deployments[dep1_id].event_buffer) == 1
        assert len(_deployments[dep2_id].event_buffer) == 0

    def test_multiple_subscribers_get_event(self, client):
        dep1_id, _ = _register(client, name="d1", subscriptions=["org/repo"])
        dep2_id, _ = _register(client, name="d2", subscriptions=["org/repo"])

        client.post(
            "/webhooks/github",
            json={
                "action": "opened",
                "issue": {"number": 1, "title": "t", "body": "", "labels": [],
                          "assignees": [], "state": "open", "html_url": ""},
                "repository": {"full_name": "org/repo"},
                "sender": {"login": "u"},
            },
            headers={"X-GitHub-Event": "issues"},
        )
        assert len(_deployments[dep1_id].event_buffer) == 1
        assert len(_deployments[dep2_id].event_buffer) == 1


# ---------------------------------------------------------------------------
# WebSocket subscription
# ---------------------------------------------------------------------------


class TestWebSocket:
    def test_websocket_connect_and_receive_event(self, client):
        dep_id, api_key = _register(client, subscriptions=["org/repo"])

        with client.websocket_connect(
            f"/deployments/{dep_id}/subscribe?token={api_key}"
        ) as ws:
            # Should receive connected message
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "connected"
            assert msg["deployment_id"] == dep_id
            assert "next_seq" in msg

            # Send a webhook while connected
            client.post(
                "/webhooks/github",
                json={
                    "action": "opened",
                    "issue": {"number": 1, "title": "t", "body": "", "labels": [],
                              "assignees": [], "state": "open", "html_url": ""},
                    "repository": {"full_name": "org/repo"},
                    "sender": {"login": "u"},
                },
                headers={"X-GitHub-Event": "issues"},
            )

            # Should receive the event over WebSocket
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "event"
            assert msg["data"]["source"] == "github"
            assert msg["data"]["type"] == "github.issues"
            assert msg["data"]["seq"] == 1

    def test_websocket_replay(self, client):
        dep_id, api_key = _register(client, subscriptions=["org/repo"])

        # Send events before connecting WebSocket
        for i in range(3):
            client.post(
                "/webhooks/github",
                json={
                    "action": "opened",
                    "issue": {"number": i, "title": f"issue {i}", "body": "",
                              "labels": [], "assignees": [], "state": "open",
                              "html_url": ""},
                    "repository": {"full_name": "org/repo"},
                    "sender": {"login": "u"},
                },
                headers={"X-GitHub-Event": "issues"},
            )

        # Connect with last_seen=1 — should replay seq 2 and 3
        with client.websocket_connect(
            f"/deployments/{dep_id}/subscribe?token={api_key}&last_seen=1"
        ) as ws:
            # Replay messages first
            msg1 = json.loads(ws.receive_text())
            assert msg1["type"] == "replay"
            assert msg1["data"]["seq"] == 2

            msg2 = json.loads(ws.receive_text())
            assert msg2["type"] == "replay"
            assert msg2["data"]["seq"] == 3

            # Then connected message
            msg3 = json.loads(ws.receive_text())
            assert msg3["type"] == "connected"

    def test_websocket_ping_pong(self, client):
        dep_id, api_key = _register(client, subscriptions=["org/repo"])

        with client.websocket_connect(
            f"/deployments/{dep_id}/subscribe?token={api_key}"
        ) as ws:
            # Consume connected message
            ws.receive_text()

            # Send ping
            ws.send_text(json.dumps({"type": "ping"}))
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "pong"

    def test_websocket_invalid_token(self, client):
        dep_id, _ = _register(client, subscriptions=["org/repo"])

        with pytest.raises(Exception):
            # Should be rejected
            with client.websocket_connect(
                f"/deployments/{dep_id}/subscribe?token=bad_key"
            ) as ws:
                ws.receive_text()

    def test_websocket_no_token(self, client):
        dep_id, _ = _register(client, subscriptions=["org/repo"])

        with pytest.raises(Exception):
            with client.websocket_connect(
                f"/deployments/{dep_id}/subscribe"
            ) as ws:
                ws.receive_text()

    def test_websocket_ack_message(self, client):
        """Ack messages should be accepted without error."""
        dep_id, api_key = _register(client, subscriptions=["org/repo"])

        with client.websocket_connect(
            f"/deployments/{dep_id}/subscribe?token={api_key}"
        ) as ws:
            ws.receive_text()  # connected
            ws.send_text(json.dumps({"type": "ack", "seq": 1}))
            # No crash — just verify the connection stays alive
            ws.send_text(json.dumps({"type": "ping"}))
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "pong"


# ---------------------------------------------------------------------------
# Auth endpoints (local stubs)
# ---------------------------------------------------------------------------


class TestAuthConfig:
    def test_returns_local_mode(self, client):
        resp = client.get("/auth/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["client_id"] == "local"
        assert data["mode"] == "local"


class TestAuthCallback:
    def test_returns_session_token(self, client):
        resp = client.post("/auth/github/callback", json={
            "code": "test-code",
            "redirect_uri": "http://localhost:12345/callback",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["token"].startswith("moda_sess_")
        assert data["github_username"] == "local-dev"
        assert data["github_user_id"] == 0

    def test_rejects_invalid_json(self, client):
        resp = client.post("/auth/github/callback", content="not json",
                          headers={"content-type": "application/json"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# DELETE /deployments/{id}
# ---------------------------------------------------------------------------


class TestDeleteDeployment:
    def test_delete_removes_deployment(self, client):
        dep_id, api_key = _register(client, subscriptions=["org/repo"])

        resp = client.delete(
            f"/deployments/{dep_id}",
            headers={"authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert dep_id not in _deployments

    def test_delete_cleans_subscription_index(self, client):
        dep_id, api_key = _register(client, subscriptions=["org/test-repo"])
        assert "org/test-repo" in _subscription_index

        client.delete(
            f"/deployments/{dep_id}",
            headers={"authorization": f"Bearer {api_key}"},
        )
        assert "org/test-repo" not in _subscription_index

    def test_delete_without_auth_returns_401(self, client):
        dep_id, _ = _register(client)
        resp = client.delete(f"/deployments/{dep_id}")
        assert resp.status_code == 401

    def test_delete_wrong_key_returns_403(self, client):
        dep_id, _ = _register(client)
        resp = client.delete(
            f"/deployments/{dep_id}",
            headers={"authorization": "Bearer wrong_key"},
        )
        assert resp.status_code in (403, 404)

    def test_delete_nonexistent_returns_404(self, client):
        dep_id, api_key = _register(client)
        resp = client.delete(
            "/deployments/nonexistent-id",
            headers={"authorization": f"Bearer {api_key}"},
        )
        assert resp.status_code in (403, 404)
