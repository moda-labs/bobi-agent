"""Tests for Venn.ai REST API client — startup service validation."""

import httpx
from unittest.mock import patch

from modastack import http as pooled
from modastack.venn import check_services, list_servers, format_service_report, VennServer


def _venn_handler(servers):
    """Return an httpx MockTransport handler that responds with the given server list."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "success": True,
            "result": {
                "servers": [
                    {"server_id": s["id"], "server_name": s["name"], "connected": s.get("connected", True)}
                    for s in servers
                ]
            }
        })
    return handler


def _mock_client(servers):
    """Build a mock httpx.Client with a transport returning the given servers."""
    transport = httpx.MockTransport(_venn_handler(servers))
    return httpx.Client(transport=transport)


class TestListServers:
    def test_returns_connected_servers(self):
        client = _mock_client([
            {"id": "work-gmail", "name": "gmail", "connected": True},
            {"id": "salesforce", "name": "salesforce", "connected": False},
        ])

        with patch.object(pooled, '_client', client):
            servers = list_servers("test-key")

        assert len(servers) == 2
        assert servers[0].server_id == "work-gmail"
        assert servers[0].connected is True
        assert servers[1].connected is False

    def test_api_failure_returns_empty(self):
        transport = httpx.MockTransport(lambda request: (_ for _ in ()).throw(Exception("connection refused")))
        client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', client):
            servers = list_servers("bad-key")
        assert servers == []


class TestCheckServices:
    def test_all_services_connected(self):
        client = _mock_client([
            {"id": "work-gmail", "name": "gmail"},
            {"id": "sf", "name": "salesforce"},
        ])

        with patch.object(pooled, '_client', client):
            result = check_services("key", ["email", "salesforce"])
        assert result.connected == ["email", "salesforce"]
        assert result.missing == []

    def test_missing_service(self):
        client = _mock_client([
            {"id": "work-gmail", "name": "gmail"},
        ])

        with patch.object(pooled, '_client', client):
            result = check_services("key", ["email", "salesforce"])
        assert result.connected == ["email"]
        assert result.missing == ["salesforce"]

    def test_alias_expansion(self):
        """'email' matches 'gmail' or 'outlook' via alias table."""
        client = _mock_client([
            {"id": "personal-gmail", "name": "gmail"},
            {"id": "cal", "name": "googlecalendar"},
        ])

        with patch.object(pooled, '_client', client):
            result = check_services("key", ["email", "calendar"])
        assert result.connected == ["email", "calendar"]

    def test_disconnected_server_counts_as_missing(self):
        client = _mock_client([
            {"id": "sf", "name": "salesforce", "connected": False},
        ])

        with patch.object(pooled, '_client', client):
            result = check_services("key", ["salesforce"])
        assert result.missing == ["salesforce"]


class TestFormatReport:
    def test_report_format(self):
        from modastack.venn import ServiceCheck
        check = ServiceCheck(connected=["email"], missing=["salesforce"])
        report = format_service_report(check, native_services=["github"])
        assert "github" in report
        assert "email" in report
        assert "salesforce" in report
        assert "not connected" in report
