"""Tests for slack_manifest — manifest rendering, JSON conversion, deep link.

The manifest is the one source of truth for the scopes + events the bobi
Slack adapter (event-server/src/adapters/chat-sdk-slack.ts) consumes. These tests pin
that the rendered manifest stays wired to the event server's /webhooks/slack
path and carries every event the adapter normalizes.
"""

import json
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from click.testing import CliRunner

from bobi.cli import main
from bobi.deploy import DEFAULT_EVENT_SERVER
from bobi.slack_manifest import (
    WEBHOOK_PATH,
    create_app_url,
    manifest_to_dict,
    manifest_to_json,
    render_manifest,
    webhook_url,
)

EVENT_SERVER = "https://my-worker.workers.dev"

# Every event the Slack adapter turns into a bobi event must be subscribed.
EXPECTED_BOT_EVENTS = {
    "app_mention",       # -> slack.mention
    "message.im",        # -> slack.dm
    "message.mpim",      # -> slack.dm
    "message.channels",  # -> slack.thread_reply (public)
    "message.groups",    # -> slack.thread_reply (private)
}

# Scopes those events require, plus the scopes the outbound CLI tools need.
EXPECTED_BOT_SCOPES = {
    "app_mentions:read", "channels:history", "channels:read", "chat:write",
    "files:read", "files:write", "groups:history", "groups:read", "im:history",
    "im:read", "im:write", "mpim:history", "users:read",
}


def test_render_substitutes_name_and_request_url():
    out = render_manifest("Eng Bot", EVENT_SERVER)
    data = yaml.safe_load(out)
    assert data["display_information"]["name"] == "Eng Bot"
    assert data["features"]["bot_user"]["display_name"] == "Eng Bot"
    assert (
        data["settings"]["event_subscriptions"]["request_url"]
        == f"{EVENT_SERVER}/webhooks/slack"
    )


def test_render_strips_trailing_slash_on_event_server():
    data = manifest_to_dict(render_manifest("Bot", EVENT_SERVER + "/"))
    assert (
        data["settings"]["event_subscriptions"]["request_url"]
        == f"{EVENT_SERVER}/webhooks/slack"
    )


def test_request_url_uses_webhook_path_constant():
    assert webhook_url(EVENT_SERVER) == EVENT_SERVER + WEBHOOK_PATH
    assert webhook_url(EVENT_SERVER + "/") == EVENT_SERVER + WEBHOOK_PATH


def test_manifest_carries_all_adapter_events():
    data = manifest_to_dict(render_manifest("Bot", EVENT_SERVER))
    events = set(data["settings"]["event_subscriptions"]["bot_events"])
    assert events == EXPECTED_BOT_EVENTS


def test_manifest_carries_required_scopes():
    data = manifest_to_dict(render_manifest("Bot", EVENT_SERVER))
    scopes = set(data["oauth_config"]["scopes"]["bot"])
    assert scopes == EXPECTED_BOT_SCOPES


def test_manifest_uses_http_events_not_socket_mode():
    data = manifest_to_dict(render_manifest("Bot", EVENT_SERVER))
    assert data["settings"]["socket_mode_enabled"] is False


def test_manifest_to_json_roundtrips_to_same_dict():
    rendered = render_manifest("Bot", EVENT_SERVER)
    assert json.loads(manifest_to_json(rendered)) == manifest_to_dict(rendered)


def test_create_app_url_embeds_valid_manifest_json():
    rendered = render_manifest("Eng Bot", EVENT_SERVER)
    url = create_app_url(rendered)
    parsed = urlparse(url)
    assert parsed.netloc == "api.slack.com"
    qs = parse_qs(parsed.query)
    assert qs["new_app"] == ["1"]
    manifest = json.loads(qs["manifest_json"][0])
    assert manifest["display_information"]["name"] == "Eng Bot"
    assert (
        manifest["settings"]["event_subscriptions"]["request_url"]
        == f"{EVENT_SERVER}/webhooks/slack"
    )


def test_cli_works_without_an_install_and_falls_back_to_cloud(tmp_path):
    """The command must run before any install exists — no root, no
    error; it falls back to the bobi cloud event server."""
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["create-slack-bot", "--app-name", "Bot"])
    assert result.exit_code == 0, result.output
    assert f"{DEFAULT_EVENT_SERVER}/webhooks/slack" in result.output
    assert "api.slack.com/apps?new_app=1" in result.output


def test_cli_writes_json_file(tmp_path):
    runner = CliRunner()
    out = tmp_path / "manifest.json"
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, [
            "create-slack-bot", "--format", "json",
            "--event-server", EVENT_SERVER, "-o", str(out), "--no-url",
        ])
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text())
    assert (
        data["settings"]["event_subscriptions"]["request_url"]
        == f"{EVENT_SERVER}/webhooks/slack"
    )


def test_cli_open_launches_browser(monkeypatch, tmp_path):
    """--open opens the one-click create link in the browser."""
    launched = []
    monkeypatch.setattr("click.launch", lambda url: launched.append(url))
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["create-slack-bot", "--open"])
    assert result.exit_code == 0, result.output
    assert len(launched) == 1
    assert "api.slack.com/apps?new_app=1" in launched[0]
    assert "Opened the create page in your browser." in result.output


def test_cli_no_open_does_not_launch(monkeypatch, tmp_path):
    """--no-open prints the link but never touches the browser."""
    launched = []
    monkeypatch.setattr("click.launch", lambda url: launched.append(url))
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["create-slack-bot", "--no-open"])
    assert result.exit_code == 0, result.output
    assert launched == []
    assert "api.slack.com/apps?new_app=1" in result.output


def test_cli_default_stays_quiet_when_not_a_tty(monkeypatch, tmp_path):
    """Default (no flag) must not launch a browser under the test runner /
    pipes / CI — only when stdout is an interactive terminal."""
    launched = []
    monkeypatch.setattr("click.launch", lambda url: launched.append(url))
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["create-slack-bot", "--app-name", "Bot"])
    assert result.exit_code == 0, result.output
    assert launched == []


def test_cli_interactive_prompts_for_name_and_event_server(monkeypatch, tmp_path):
    """At a real terminal, the command asks for the app name and the event
    server URL (with the localhost-tunnel guidance) BEFORE rendering the
    manifest; typed answers land in it."""
    monkeypatch.setattr("bobi.cli._interactive_terminal", lambda: True)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            main, ["create-slack-bot", "--no-open"],
            input="My Bot\nhttps://tunnel.example.com\n",
        )
    assert result.exit_code == 0, result.output
    assert "Slack app display name" in result.output
    assert "localhost:8080" in result.output  # the tunnel guidance
    assert "https://tunnel.example.com/webhooks/slack" in result.output
    assert "name: My Bot" in result.output


def test_cli_interactive_enter_accepts_cloud_default(monkeypatch, tmp_path):
    """Pressing Enter at both prompts uses the default name and the bobi
    cloud event server."""
    monkeypatch.setattr("bobi.cli._interactive_terminal", lambda: True)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            main, ["create-slack-bot", "--no-open"], input="\n\n")
    assert result.exit_code == 0, result.output
    assert f"{DEFAULT_EVENT_SERVER}/webhooks/slack" in result.output
    assert "name: bobi agent" in result.output


def test_cli_flags_answer_the_interactive_questions(monkeypatch, tmp_path):
    """--app-name/--event-server pre-answer the prompts: no input is
    consumed even at a real terminal (no input given, so a prompt would
    abort)."""
    monkeypatch.setattr("bobi.cli._interactive_terminal", lambda: True)
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, [
            "create-slack-bot", "--no-open",
            "--app-name", "Bot", "--event-server", EVENT_SERVER,
        ])
    assert result.exit_code == 0, result.output
    assert "Slack app display name" not in result.output
    assert f"{EVENT_SERVER}/webhooks/slack" in result.output
