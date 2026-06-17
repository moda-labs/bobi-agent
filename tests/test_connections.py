"""Tests for multi-model connections registry and cost attribution."""

import json
import os
from pathlib import Path
from textwrap import dedent

import pytest

from modastack.config import Config, ConnectionEntry


def _write_agent_yaml(tmp_path, body):
    d = tmp_path / ".modastack"
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.yaml").write_text(dedent(body))


# ---------------------------------------------------------------------------
# ConnectionEntry parsing
# ---------------------------------------------------------------------------


class TestConnectionsParsing:
    def test_connections_parsed(self, tmp_path):
        _write_agent_yaml(tmp_path, """
            entry_point: manager
            connections:
              - name: openai-images
                kind: image
                provider: openai
                api_key: sk-test-123
                model: gpt-image-1
              - name: gemini-chat
                kind: chat
                provider: google
                api_key: AIza-test
                model: gemini-2.5-pro
        """)
        cfg = Config.load(tmp_path)
        assert len(cfg.connections) == 2
        assert cfg.connections[0].name == "openai-images"
        assert cfg.connections[0].kind == "image"
        assert cfg.connections[0].provider == "openai"
        assert cfg.connections[0].api_key == "sk-test-123"
        assert cfg.connections[0].model == "gpt-image-1"
        assert cfg.connections[1].name == "gemini-chat"
        assert cfg.connections[1].kind == "chat"

    def test_connections_env_var_interpolation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
        _write_agent_yaml(tmp_path, """
            entry_point: manager
            connections:
              - name: openai-images
                kind: image
                provider: openai
                api_key: ${OPENAI_API_KEY}
        """)
        cfg = Config.load(tmp_path)
        assert cfg.connections[0].api_key == "sk-from-env"

    def test_connections_empty(self, tmp_path):
        _write_agent_yaml(tmp_path, """
            entry_point: manager
            connections: []
        """)
        cfg = Config.load(tmp_path)
        assert cfg.connections == []

    def test_connections_missing(self, tmp_path):
        _write_agent_yaml(tmp_path, """
            entry_point: manager
        """)
        cfg = Config.load(tmp_path)
        assert cfg.connections == []

    def test_connections_skips_invalid(self, tmp_path):
        _write_agent_yaml(tmp_path, """
            entry_point: manager
            connections:
              - name: valid
                kind: image
                provider: openai
                api_key: sk-test
              - name: no-kind
                provider: openai
              - kind: image
              - name: also-valid
                kind: chat
                provider: google
                api_key: AIza-test
        """)
        cfg = Config.load(tmp_path)
        assert len(cfg.connections) == 2
        assert cfg.connections[0].name == "valid"
        assert cfg.connections[1].name == "also-valid"

    def test_connection_lookup(self, tmp_path):
        _write_agent_yaml(tmp_path, """
            entry_point: manager
            connections:
              - name: openai-images
                kind: image
                provider: openai
                api_key: sk-test
        """)
        cfg = Config.load(tmp_path)
        conn = cfg.connection("openai-images")
        assert conn is not None
        assert conn.provider == "openai"
        assert cfg.connection("nonexistent") is None

    def test_connections_by_kind(self, tmp_path):
        _write_agent_yaml(tmp_path, """
            entry_point: manager
            connections:
              - name: img1
                kind: image
                provider: openai
                api_key: sk-1
              - name: chat1
                kind: chat
                provider: google
                api_key: AIza-1
              - name: img2
                kind: image
                provider: google
                api_key: AIza-2
        """)
        cfg = Config.load(tmp_path)
        images = cfg.connections_by_kind("image")
        assert len(images) == 2
        assert {c.name for c in images} == {"img1", "img2"}
        chats = cfg.connections_by_kind("chat")
        assert len(chats) == 1

    def test_connection_extra_fields(self, tmp_path):
        _write_agent_yaml(tmp_path, """
            entry_point: manager
            connections:
              - name: custom
                kind: gateway
                provider: openrouter
                api_key: or-test
                base_url: https://openrouter.ai/api/v1
        """)
        cfg = Config.load(tmp_path)
        conn = cfg.connections[0]
        assert conn.extra.get("base_url") == "https://openrouter.ai/api/v1"
