"""Unit tests for the WhatsApp channel plumbing (#656, epic #190 Phase 3).

Covers the Python side: subscription detection, the signed number
registration client, and the setup connector card. The event-server side
(webhook pipeline, adapter, window enforcement) is covered in
event-server/test/; the end-to-end loop in tests/integration/.
"""

import json
from unittest.mock import patch

from bobi import http as pooled
from bobi.config import Config, ServiceConfig
from bobi.events.adapters import detect
from bobi.events.server import register_whatsapp_numbers

PNID = "747556541"


def _cfg(access_token: str = "EAAG-tok", phone_number_id: str = PNID) -> Config:
    creds = {}
    if access_token:
        creds["access_token"] = access_token
    if phone_number_id:
        creds["phone_number_id"] = phone_number_id
    return Config(services=[ServiceConfig(name="whatsapp", credentials=creds)])


class _Resp:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


class _GraphResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


class TestDetectWhatsApp:
    def test_detects_topic_after_upstream_validation(self):
        with patch.object(pooled, "get",
                          side_effect=lambda *a, **k: _GraphResp({"id": PNID})):
            assert detect("whatsapp", None, _cfg()) == [f"whatsapp:{PNID}"]

    def test_requires_both_credentials(self):
        calls = []
        with patch.object(pooled, "get",
                          side_effect=lambda *a, **k: calls.append(1)):
            assert detect("whatsapp", None, _cfg(access_token="")) == []
            assert detect("whatsapp", None, _cfg(phone_number_id="")) == []
        assert calls == [], "must not hit the Graph API without creds"

    def test_rejected_credential_does_not_subscribe(self):
        """A bad token must NOT yield the topic: the #488 grant check rejects
        the whole deployment registration atomically, so subscribing with an
        unregistrable credential would take down every other subscription."""
        with patch.object(pooled, "get", side_effect=lambda *a, **k: _GraphResp(
                {"error": {"message": "bad token"}})):
            assert detect("whatsapp", None, _cfg()) == []


class TestRegisterWhatsAppNumbers:
    def test_signed_registration_posts_and_returns_pnid(self):
        captured = {}

        def _request(method, url, *, content=None, headers=None, timeout=None):
            captured.update(method=method, content=content)
            captured.update(url=url, headers=headers)
            return _Resp(200)

        with patch.object(pooled, "request", side_effect=_request):
            result = register_whatsapp_numbers(
                "http://localhost:8080", _cfg(), "bub_x", "bkey_x")

        assert result == [PNID]
        assert captured["method"] == "POST"
        assert captured["url"].endswith("/whatsapp/numbers")
        body = json.loads(captured["content"])
        assert body["phone_number_id"] == PNID
        assert body["access_token"] == "EAAG-tok"
        # Signed-only: the bubble signature must be present.
        assert captured["headers"]["x-moda-bubble"] == "bub_x"
        assert captured["headers"]["x-moda-algo"] == "hmac-sha256"
        assert captured["headers"]["x-moda-signature"]

    def test_noop_without_bubble_credentials_or_config(self):
        calls = []
        with patch.object(pooled, "request",
                          side_effect=lambda *a, **k: calls.append(1)):
            # No bubble key: nothing to register (signed-only endpoint).
            assert register_whatsapp_numbers(
                "http://localhost:8080", _cfg()) == []
            # No whatsapp credentials configured at all.
            assert register_whatsapp_numbers(
                "http://localhost:8080", Config(), "bub_x", "bkey_x") == []
        assert calls == [], "must not POST without creds + bubble"

    def test_rejected_registration_returns_empty(self):
        with patch.object(pooled, "request", side_effect=lambda *a, **k: _Resp(403)):
            assert register_whatsapp_numbers(
                "http://localhost:8080", _cfg(), "bub_x", "bkey_x") == []


class TestConnectorCard:
    def test_whatsapp_card_in_catalog(self):
        from bobi.setup.services import CATALOG

        card = CATALOG["whatsapp"]
        assert card.kind == "native"
        secret_vars = {s.var for m in card.methods for s in m.secrets}
        assert {"WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
                "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN"} <= secret_vars
        assert card.credential_var == "WHATSAPP_ACCESS_TOKEN"


class TestGatewayTypedErrorDetail:
    def test_gateway_error_surfaces_typed_detail(self):
        """The typed outside_message_window error carries human-readable
        guidance in `detail`; the client must surface it, not just the code
        (the server includes it specifically so the agent can report the
        situation)."""
        import pytest

        from bobi.events import gateway

        class _ErrResp:
            status_code = 400

            def json(self):
                return {
                    "error": "outside_message_window",
                    "detail": "no inbound message from this user in the last 24h",
                }

        with patch("bobi.events.publish.bubble_context",
                   return_value=("http://localhost:8080", "bub_x", "bkey_x")), \
             patch("bobi.events.signing.signed_request",
                   return_value=_ErrResp()):
            with pytest.raises(gateway.GatewayError) as ei:
                gateway._request(None, "POST", "/channels/send", {"text": "hi"})
        assert "outside_message_window" in str(ei.value)
        assert "no inbound message from this user" in str(ei.value)
