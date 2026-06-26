"""Tests for the shared HTTP client with connection pooling."""

from __future__ import annotations

import threading
from unittest.mock import patch, MagicMock

import httpx
import pytest

from bobi import http as pooled


@pytest.fixture(autouse=True)
def _reset_client():
    """Reset the shared client between tests."""
    pooled.close()
    yield
    pooled.close()


@pytest.fixture(autouse=True)
def _bubble_present():
    """Publishing requires a bubble credential (auth-v1) — _post_topic skips the
    doomed unsigned POST when none is loaded. These transport tests use fake
    project paths with no bubble.json, so stand one in to exercise the real
    signed-publish path. Harmless to non-publish tests."""
    with patch("bobi.config.load_bubble_state",
               return_value={"bubble_id": "bub_test", "bubble_key": "bkey_test"}):
        yield


class TestClient:
    def test_lazy_creation(self):
        """Client is not created until first access."""
        assert pooled._client is None
        c = pooled.client()
        assert c is not None
        assert isinstance(c, httpx.Client)

    def test_singleton(self):
        """Same client instance is returned on repeated calls."""
        c1 = pooled.client()
        c2 = pooled.client()
        assert c1 is c2

    def test_thread_safe_creation(self):
        """Concurrent calls to client() produce only one instance."""
        clients = []
        barrier = threading.Barrier(4)

        def _get():
            barrier.wait()
            clients.append(pooled.client())

        threads = [threading.Thread(target=_get) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(id(c) for c in clients)) == 1

    def test_bounded_concurrency(self):
        """Client has connection limits configured."""
        c = pooled.client()
        # httpx.Client stores pool config in _transport._pool
        transport = c._transport
        pool = transport._pool
        assert pool._max_connections == 20
        assert pool._max_keepalive_connections == 10


class TestClose:
    def test_close_resets_client(self):
        pooled.client()
        assert pooled._client is not None
        pooled.close()
        assert pooled._client is None

    def test_close_before_creation_is_safe(self):
        pooled.close()  # should not raise

    def test_close_twice_is_safe(self):
        pooled.client()
        pooled.close()
        pooled.close()

    def test_new_client_after_close(self):
        c1 = pooled.client()
        pooled.close()
        c2 = pooled.client()
        assert c1 is not c2


class TestConnectionReuse:
    """Verify that the pooled client reuses connections instead of
    creating new ones for each request."""

    def test_post_event_reuses_connection(self, httpx_mock_fixture):
        """Multiple post_event calls to the same host reuse the TCP
        connection via the shared client's pool."""
        from bobi.events.publish import post_event
        from pathlib import Path

        call_count = 0

        def _handler(request):
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(_handler)
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client):
            with patch('bobi.events.publish._event_server_url',
                       return_value='http://localhost:8080'):
                ok1 = post_event("monitor/test", {"a": 1},
                                 project_path=Path("/tmp/fake"))
                ok2 = post_event("monitor/test", {"a": 2},
                                 project_path=Path("/tmp/fake"))

        assert ok1
        assert ok2
        assert call_count == 2  # both calls went through the same mock client


@pytest.fixture
def httpx_mock_fixture():
    """Fixture that just enables httpx mock transport tests."""
    yield


class TestPostEventMigration:
    """Verify post_event works correctly after urllib→httpx migration."""

    def test_success_returns_true(self):
        from bobi.events.publish import post_event

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ok": True})
        )
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client), \
             patch('bobi.events.publish._event_server_url',
                   return_value='http://localhost:8080'):
            result = post_event("monitor/test", {"key": "val"},
                                project_path="/tmp/fake")

        assert result is True

    def test_failure_returns_false(self):
        from bobi.events.publish import post_event

        transport = httpx.MockTransport(
            lambda request: httpx.Response(500, text="Internal Server Error")
        )
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client), \
             patch('bobi.events.publish._event_server_url',
                   return_value='http://localhost:8080'):
            result = post_event("monitor/test", {"key": "val"},
                                project_path="/tmp/fake")

        # 500 response with non-JSON body should fail gracefully
        assert result is False

    def test_network_error_returns_false(self):
        from bobi.events.publish import post_event

        def _raise(request):
            raise httpx.ConnectError("Connection refused")

        transport = httpx.MockTransport(_raise)
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client), \
             patch('bobi.events.publish._event_server_url',
                   return_value='http://localhost:8080'):
            result = post_event("monitor/test", {"key": "val"},
                                project_path="/tmp/fake")

        assert result is False

    def test_bare_event_type_defaults_source_to_monitor(self):
        from bobi.events.publish import post_event

        captured = []

        def _capture(request):
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(_capture)
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client), \
             patch('bobi.events.publish._event_server_url',
                   return_value='http://localhost:8080'):
            post_event("test_event", {"k": "v"}, project_path="/tmp/fake")

        assert len(captured) == 1
        assert "/events/test_event" in str(captured[0].url)
        body = captured[0].content.decode()
        assert '"source":"monitor"' in body or '"source": "monitor"' in body


class TestPostEventSigning:
    """Verify post_event attaches bubble signing headers (#313).

    The release smoke broke because the CI workflow used unsigned curl
    instead of post_event. This test ensures post_event actually sends
    x-moda-* headers when a bubble credential exists, so the event
    server accepts the publish (v0.21.0+ rejects unsigned POSTs with 403).
    """

    def test_signing_headers_present_when_bubble_exists(self):
        from bobi.events.publish import post_event

        captured = []

        def _capture(request):
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(_capture)
        mock_client = httpx.Client(transport=transport)
        fake_bubble = {"bubble_id": "bub_smoke", "bubble_key": "bkey_test"}

        with patch.object(pooled, '_client', mock_client), \
             patch('bobi.events.publish._event_server_url',
                   return_value='http://localhost:8080'), \
             patch('bobi.config.load_bubble_state',
                   return_value=fake_bubble):
            result = post_event("release-pipeline/smoke.ping",
                                {"version": "0.21.0"},
                                project_path="/tmp/fake")

        assert result is True
        assert len(captured) == 1
        req = captured[0]
        # Must have all five signing headers
        assert req.headers["x-moda-bubble"] == "bub_smoke"
        assert req.headers["x-moda-algo"] == "hmac-sha256"
        assert req.headers["x-moda-timestamp"].isdigit()
        assert len(req.headers["x-moda-nonce"]) >= 8
        assert len(req.headers["x-moda-signature"]) == 64  # sha256 hex

    def test_no_publish_without_bubble(self):
        """Without bubble credentials the publish is skipped (no round-trip)."""
        from bobi.events.publish import post_event

        captured = []

        def _capture(request):
            captured.append(request)
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(_capture)
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client), \
             patch('bobi.events.publish._event_server_url',
                   return_value='http://localhost:8080'), \
             patch('bobi.config.load_bubble_state',
                   return_value={}):
            result = post_event("monitor/test", {"k": "v"}, project_path="/tmp/fake")

        # No bubble → early return False, no HTTP request made
        assert result is False
        assert len(captured) == 0

    def test_403_from_stale_bubble_returns_false(self):
        """A signed publish that gets 403 (stale bubble) returns False."""
        from bobi.events.publish import post_event

        transport = httpx.MockTransport(
            lambda request: httpx.Response(403, json={"error": "missing signature"})
        )
        mock_client = httpx.Client(transport=transport)
        fake_bubble = {"bubble_id": "bub_stale", "bubble_key": "bkey_old"}

        with patch.object(pooled, '_client', mock_client), \
             patch('bobi.events.publish._event_server_url',
                   return_value='http://localhost:8080'), \
             patch('bobi.config.load_bubble_state',
                   return_value=fake_bubble):
            result = post_event("monitor/test", {"k": "v"},
                                project_path="/tmp/fake")

        assert result is False


class TestNoUrllibRemains:
    """Ensure urllib.request is no longer imported in converted modules."""

    def test_publish_no_urllib(self):
        import bobi.events.publish as mod
        import inspect
        source = inspect.getsource(mod)
        assert "urllib.request" not in source

    def test_slack_no_urllib(self):
        import bobi.slack as mod
        import inspect
        source = inspect.getsource(mod)
        assert "urllib.request" not in source
