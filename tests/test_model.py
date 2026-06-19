"""Tests for the ModelSpec resolver."""

import os

import pytest

from modastack.config import ConnectionEntry
from modastack.model import (
    ModelSpec,
    PROVIDER_DEFAULT_MODELS,
    PROVIDER_BASE_URLS,
    PROVIDER_ENV_VARS,
    default_connection_name,
    env_var_for_provider,
    resolve,
)


class TestResolve:
    def test_explicit_model_wins(self):
        conn = ConnectionEntry(
            name="gw", kind="gateway", provider="openai",
            api_key="sk-test", model="gpt-4o-mini",
        )
        spec = resolve(conn, requested_model="o3")
        assert spec.model == "o3"

    def test_connection_model_over_default(self):
        conn = ConnectionEntry(
            name="gw", kind="gateway", provider="openai",
            api_key="sk-test", model="gpt-4o-mini",
        )
        spec = resolve(conn)
        assert spec.model == "gpt-4o-mini"

    def test_provider_default_when_no_model(self):
        conn = ConnectionEntry(
            name="gw", kind="gateway", provider="openai",
            api_key="sk-test",
        )
        spec = resolve(conn)
        assert spec.model == PROVIDER_DEFAULT_MODELS["openai"]

    def test_empty_model_for_unknown_provider(self):
        conn = ConnectionEntry(
            name="gw", kind="gateway", provider="custom-llm",
            api_key="key",
        )
        spec = resolve(conn)
        assert spec.model == ""

    def test_api_key_from_connection(self):
        conn = ConnectionEntry(
            name="gw", kind="gateway", provider="openai",
            api_key="sk-direct",
        )
        spec = resolve(conn)
        assert spec.api_key == "sk-direct"

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        conn = ConnectionEntry(
            name="gw", kind="gateway", provider="openai",
        )
        spec = resolve(conn)
        assert spec.api_key == "sk-from-env"

    def test_api_key_connection_over_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        conn = ConnectionEntry(
            name="gw", kind="gateway", provider="openai",
            api_key="sk-direct",
        )
        spec = resolve(conn)
        assert spec.api_key == "sk-direct"

    def test_base_url_from_extra(self):
        conn = ConnectionEntry(
            name="gw", kind="gateway", provider="openai",
            api_key="sk-test",
            extra={"base_url": "https://custom.example.com/v1"},
        )
        spec = resolve(conn)
        assert spec.base_url == "https://custom.example.com/v1"

    def test_base_url_default(self):
        conn = ConnectionEntry(
            name="gw", kind="gateway", provider="openrouter",
            api_key="or-test",
        )
        spec = resolve(conn)
        assert spec.base_url == PROVIDER_BASE_URLS["openrouter"]

    def test_extra_fields_passed_through(self):
        conn = ConnectionEntry(
            name="gw", kind="gateway", provider="openrouter",
            api_key="or-test",
            extra={"http_referer": "https://myapp.com", "base_url": "https://x.com"},
        )
        spec = resolve(conn)
        assert spec.extra == {"http_referer": "https://myapp.com"}
        assert "base_url" not in spec.extra

    def test_provider_normalized_to_lowercase(self):
        conn = ConnectionEntry(
            name="gw", kind="gateway", provider="OpenAI",
            api_key="sk-test",
        )
        spec = resolve(conn)
        assert spec.provider == "openai"

    def test_frozen_spec(self):
        conn = ConnectionEntry(
            name="gw", kind="gateway", provider="openai",
            api_key="sk-test",
        )
        spec = resolve(conn)
        with pytest.raises(AttributeError):
            spec.model = "different"  # type: ignore[misc]


class TestConnectionNaming:
    def test_default_name(self):
        assert default_connection_name("openai", "gateway") == "openai-gateway"
        assert default_connection_name("Google", "Image") == "google-image"

    def test_env_var_for_known_provider(self):
        assert env_var_for_provider("openai") == "OPENAI_API_KEY"
        assert env_var_for_provider("google") == "GOOGLE_API_KEY"

    def test_env_var_for_unknown_provider(self):
        assert env_var_for_provider("custom-llm") == ""

    def test_env_var_case_insensitive(self):
        assert env_var_for_provider("OpenAI") == "OPENAI_API_KEY"
