"""Unit tests for the Discord channel plumbing (#2).

Covers the Python side: subscription detection, the signed app registration
client, and the setup connector card - the same surface test_whatsapp.py
covers for WhatsApp. The event-server side (Gateway state machine,
normalizer, adapter, registration handler) is covered in event-server/test/;
the end-to-end loop in tests/integration/test_discord_gateway.py.
"""

import json
from unittest.mock import patch

from bobi import http as pooled
from bobi.config import Config, ServiceConfig
from bobi.events.adapters import detect
from bobi.events.server import register_discord_apps

APP_ID = "111222333444555666"


def _cfg(bot_token: str = "dc-tok", application_id: str = APP_ID) -> Config:
    creds = {}
    if bot_token:
        creds["bot_token"] = bot_token
    if application_id:
        creds["application_id"] = application_id
    return Config(services=[ServiceConfig(name="discord", credentials=creds)])


class _Resp:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


class _ApiResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


class TestDetectDiscord:
    def test_detects_topic_after_upstream_validation(self):
        with patch.object(pooled, "get",
                          side_effect=lambda *a, **k: _ApiResp({"id": APP_ID})):
            assert detect("discord", None, _cfg()) == [f"discord:{APP_ID}"]

    def test_requires_both_credentials(self):
        calls = []
        with patch.object(pooled, "get",
                          side_effect=lambda *a, **k: calls.append(1)):
            assert detect("discord", None, _cfg(bot_token="")) == []
            assert detect("discord", None, _cfg(application_id="")) == []
        assert calls == [], "must not hit the Discord API without creds"

    def test_rejected_credential_does_not_subscribe(self):
        """A bad token must NOT yield the topic: the #488 grant check rejects
        the whole deployment registration atomically, so subscribing with an
        unregistrable credential would take down every other subscription."""
        with patch.object(pooled, "get", side_effect=lambda *a, **k: _ApiResp(
                {"message": "401: Unauthorized", "code": 0})):
            assert detect("discord", None, _cfg()) == []

    def test_discord_subscription_is_app_wide_not_channel_scoped(self):
        cfg = Config(services=[ServiceConfig(
            name="discord",
            credentials={"bot_token": "dc-tok", "application_id": APP_ID},
            channels=["123456789012345678"],
        )])

        with patch.object(pooled, "get",
                          side_effect=lambda *a, **k: _ApiResp({"id": APP_ID})):
            assert detect("discord", None, cfg) == [f"discord:{APP_ID}"]


class TestRegisterDiscordApps:
    def test_signed_registration_posts_and_returns_app_id(self):
        captured = {}

        def _request(method, url, *, content=None, headers=None, timeout=None):
            captured.update(method=method, content=content)
            captured.update(url=url, headers=headers)
            return _Resp(200)

        with patch.object(pooled, "request", side_effect=_request):
            result = register_discord_apps(
                "http://localhost:8080", _cfg(), "bub_x", "bkey_x")

        assert result == [APP_ID]
        assert captured["method"] == "POST"
        assert captured["url"].endswith("/discord/apps")
        body = json.loads(captured["content"])
        assert body["application_id"] == APP_ID
        assert body["bot_token"] == "dc-tok"
        # Signed-only: the bubble signature must be present.
        assert captured["headers"]["x-moda-bubble"] == "bub_x"
        assert captured["headers"]["x-moda-algo"] == "hmac-sha256"
        assert captured["headers"]["x-moda-signature"]

    def test_noop_without_bubble_credentials_or_config(self):
        calls = []
        with patch.object(pooled, "request",
                          side_effect=lambda *a, **k: calls.append(1)):
            # No bubble key: nothing to register (signed-only endpoint).
            assert register_discord_apps(
                "http://localhost:8080", _cfg()) == []
            # No discord credentials configured at all.
            assert register_discord_apps(
                "http://localhost:8080", Config(), "bub_x", "bkey_x") == []
        assert calls == [], "must not POST without creds + bubble"

    def test_rejected_registration_returns_empty(self):
        with patch.object(pooled, "request", side_effect=lambda *a, **k: _Resp(403)):
            assert register_discord_apps(
                "http://localhost:8080", _cfg(), "bub_x", "bkey_x") == []


class TestConnectorCard:
    def test_discord_card_in_catalog(self):
        from bobi.setup.services import CATALOG

        card = CATALOG["discord"]
        assert card.kind == "native"
        secret_vars = {s.var for m in card.methods for s in m.secrets}
        assert {"DISCORD_BOT_TOKEN", "DISCORD_APPLICATION_ID"} <= secret_vars
        assert card.credential_var == "DISCORD_BOT_TOKEN"
