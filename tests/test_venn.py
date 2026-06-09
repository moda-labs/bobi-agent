"""Tests for Venn.ai REST API client — startup service validation."""

import json
from unittest.mock import patch, MagicMock

from modastack.venn import check_services, list_servers, format_service_report, VennServer


def _mock_venn_response(servers):
    """Build a mock urlopen response with the given server list."""
    body = json.dumps({
        "success": True,
        "result": {
            "servers": [
                {"server_id": s["id"], "server_name": s["name"], "connected": s.get("connected", True)}
                for s in servers
            ]
        }
    }).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestListServers:
    @patch("urllib.request.urlopen")
    def test_returns_connected_servers(self, mock_urlopen):
        mock_urlopen.return_value = _mock_venn_response([
            {"id": "work-gmail", "name": "gmail", "connected": True},
            {"id": "salesforce", "name": "salesforce", "connected": False},
        ])

        servers = list_servers("test-key")

        assert len(servers) == 2
        assert servers[0].server_id == "work-gmail"
        assert servers[0].connected is True
        assert servers[1].connected is False

    @patch("urllib.request.urlopen")
    def test_api_failure_returns_empty(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("connection refused")
        servers = list_servers("bad-key")
        assert servers == []


class TestCheckServices:
    @patch("urllib.request.urlopen")
    def test_all_services_connected(self, mock_urlopen):
        mock_urlopen.return_value = _mock_venn_response([
            {"id": "work-gmail", "name": "gmail"},
            {"id": "sf", "name": "salesforce"},
        ])

        result = check_services("key", ["email", "salesforce"])
        assert result.connected == ["email", "salesforce"]
        assert result.missing == []

    @patch("urllib.request.urlopen")
    def test_missing_service(self, mock_urlopen):
        mock_urlopen.return_value = _mock_venn_response([
            {"id": "work-gmail", "name": "gmail"},
        ])

        result = check_services("key", ["email", "salesforce"])
        assert result.connected == ["email"]
        assert result.missing == ["salesforce"]

    @patch("urllib.request.urlopen")
    def test_alias_expansion(self, mock_urlopen):
        """'email' matches 'gmail' or 'outlook' via alias table."""
        mock_urlopen.return_value = _mock_venn_response([
            {"id": "personal-gmail", "name": "gmail"},
            {"id": "cal", "name": "googlecalendar"},
        ])

        result = check_services("key", ["email", "calendar"])
        assert result.connected == ["email", "calendar"]

    @patch("urllib.request.urlopen")
    def test_disconnected_server_counts_as_missing(self, mock_urlopen):
        mock_urlopen.return_value = _mock_venn_response([
            {"id": "sf", "name": "salesforce", "connected": False},
        ])

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
