"""Unit tests for the subscription-login bootstrap (containerized-23 / #343).

The pty spawn, Slack post, and event-bus wait are injected as fakes so the
orchestration is exercised without a real claude binary, Slack, or Worker. The
URL scraper and code extractor are tested directly. The live round-trip is an
integration concern (deployed env, alongside C10/C12).
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from bobi import auth_bootstrap as ab


@pytest.fixture(autouse=True)
def default_claude_brain(monkeypatch, tmp_path):
    from bobi.brain import BRAIN_ENV

    monkeypatch.setenv(BRAIN_ENV, "claude")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    # The ask state lives under codex's config dir, which with CODEX_HOME unset
    # is ~/.codex. Keep HOME inside tmp_path so no test can write into a real
    # codex home; the config fixtures below override it with their own.
    monkeypatch.setenv("HOME", str(tmp_path / "default-home"))


def _ready_event_for(channel, ask_ts="1700000000.000001", *,
                     ts="1700000000.000002", text="ready"):
    """A human "ready" reply shaped the way the real adapter delivers one."""
    anchor = ab._reply_anchor(channel, ask_ts)
    is_channel = ab._is_channel_destination(channel)
    fields = {"user_id": "U0HUMAN", "ts": ts}
    if is_channel:
        fields["thread_ts"] = anchor
    conversation = ""
    if channel.legacy_slack_channel:
        fields["channel"] = channel.legacy_slack_channel
    else:
        conversation = channel.destination
        if is_channel and ":thread:" not in conversation:
            conversation = f"{conversation}:thread:{anchor}"
    event = {
        "source": channel.source,
        "type": f"{channel.source}.thread_reply",
        "text": text,
        "fields": fields,
    }
    if conversation:
        event["conversation"] = conversation
    return event


@pytest.fixture(autouse=True)
def ready_reply_listener(monkeypatch, request):
    """Ask-first is unconditional (Z4), so every `run_bootstrap` test needs a
    live listener and a human reply before the login CLI is spawned.

    Tests that are *about* the ask - ordering, correlation, re-attach - pass
    their own `connect_listener=`. Tests that exercise the real subscription
    and registration path carry `@pytest.mark.real_listener`.
    """
    if request.node.get_closest_marker("real_listener"):
        return

    def connect(project_path, channel, timeout):
        return _FakeListener([_ready_event_for(channel)], channel=channel)

    monkeypatch.setattr(ab, "_connect_chat_listener", connect)


# --- credentials / needs_bootstrap ------------------------------------------

def test_credentials_path_follows_home(tmp_path):
    assert ab.credentials_path(tmp_path) == tmp_path / ".claude" / ".credentials.json"


def test_credentials_exist_requires_structurally_valid_claude_oauth(tmp_path):
    assert not ab.credentials_exist(tmp_path)
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text("{}")
    assert not ab.credentials_exist(tmp_path)
    creds.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "access",
            "refreshToken": "refresh",
        },
    }))
    assert ab.credentials_exist(tmp_path)


def test_credentials_exist_honors_claude_config_dir(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config_dir = tmp_path / "claude-volume"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    creds = config_dir / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "access", "refreshToken": "refresh"},
    }))

    assert ab.credentials_path(home) == creds
    assert ab.credentials_exist(home) is True
    monkeypatch.setenv("BOBI_AUTH", "subscription")
    assert ab.needs_bootstrap(home) is False


def test_needs_bootstrap_only_in_subscription_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("BOBI_AUTH", "api_key")
    assert ab.needs_bootstrap(tmp_path) is False
    monkeypatch.setenv("BOBI_AUTH", "subscription")
    assert ab.needs_bootstrap(tmp_path) is True
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text("{}")
    assert ab.needs_bootstrap(tmp_path) is True
    (tmp_path / ".claude" / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"refreshToken": "refresh"},
    }))
    assert ab.needs_bootstrap(tmp_path) is False


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (None, "credential file is missing"),
        ("{not-json", "credential JSON is malformed"),
        ("[]", "credential JSON root is not an object"),
        ("{}", "claudeAiOauth is missing or malformed"),
        (json.dumps({"claudeAiOauth": {}}),
         "refresh token is missing or blank"),
        (json.dumps({"claudeAiOauth": {"refreshToken": "   "}}),
         "refresh token is missing or blank"),
        (json.dumps({
            "claudeAiOauth": {
                "refreshToken": "refresh",
                "refreshTokenExpiresAt": "tomorrow",
            },
        }), "refresh token expiry is malformed"),
        ('{"claudeAiOauth":{"refreshToken":"refresh",'
         '"refreshTokenExpiresAt":NaN}}',
         "refresh token expiry is malformed"),
        (json.dumps({
            "claudeAiOauth": {
                "refreshToken": "refresh",
                "refreshTokenExpiresAt": 1_699_999_999_999,
            },
        }), "refresh token is expired"),
    ],
)
def test_claude_subscription_credentials_reject_unusable_shapes(
    tmp_path, payload, reason,
):
    path = tmp_path / ".claude" / ".credentials.json"
    if payload is not None:
        path.parent.mkdir(parents=True)
        path.write_text(payload)

    status = ab.subscription_credentials_status(
        path, "claude", now_ms=1_700_000_000_000,
    )

    assert status.valid is False
    assert status.reason == reason


@pytest.mark.parametrize(
    ("oauth", "reason"),
    [
        (
            {"accessToken": "", "refreshToken": "refresh"},
            "refresh token is present",
        ),
        (
            {
                "accessToken": "access",
                "refreshToken": "refresh",
                "refreshTokenExpiresAt": 1_700_000_000_001,
            },
            "refresh token is present and unexpired",
        ),
    ],
)
def test_claude_subscription_credentials_accept_recoverable_shapes(
    tmp_path, oauth, reason,
):
    path = tmp_path / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"claudeAiOauth": oauth}))

    status = ab.subscription_credentials_status(
        path, "claude", now_ms=1_700_000_000_000,
    )

    assert status.valid is True
    assert status.reason == reason


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (None, "credential file is missing"),
        ("{not-json", "credential JSON is malformed"),
        ("[]", "credential JSON root is not an object"),
        ("{}", "tokens is missing or malformed"),
        (json.dumps({"tokens": {}}), "refresh token is missing or blank"),
        (json.dumps({"tokens": {"refresh_token": "  "}}),
         "refresh token is missing or blank"),
    ],
)
def test_codex_subscription_credentials_reject_unusable_shapes(
    tmp_path, payload, reason,
):
    path = tmp_path / ".codex" / "auth.json"
    if payload is not None:
        path.parent.mkdir(parents=True)
        path.write_text(payload)

    status = ab.subscription_credentials_status(path, "codex")

    assert status.valid is False
    assert status.reason == reason


def test_codex_subscription_credentials_accept_real_oauth_shape(tmp_path):
    path = tmp_path / ".codex" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "OPENAI_API_KEY": None,
        "auth_mode": "apikey",
        "tokens": {
            "id_token": "id",
            "access_token": "",
            "refresh_token": "refresh",
            "account_id": "account",
        },
        "last_refresh": "2026-08-06T00:00:00Z",
    }))

    status = ab.subscription_credentials_status(path, "codex")

    assert status.valid is True
    assert status.reason == "refresh token is present"


def test_credential_status_cli_reports_invalid_reason(tmp_path, capsys):
    path = tmp_path / ".claude" / ".credentials.json"

    exit_code = ab._credential_status_cli([
        "credential-status", "claude", str(path),
    ])

    assert exit_code == 1
    assert capsys.readouterr().out.strip() == (
        "credentials invalid: credential file is missing"
    )


def test_credential_status_cli_reports_valid_reason(tmp_path, capsys):
    path = tmp_path / ".codex" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "tokens": {"refresh_token": "refresh"},
    }))

    exit_code = ab._credential_status_cli([
        "credential-status", "codex", str(path),
    ])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == (
        "credentials valid: refresh token is present"
    )


# --- URL scraping -----------------------------------------------------------

def test_read_until_url_scrapes_oauth_url():
    """Drive _read_until_url against a real pty fed the actual claude output."""
    import pty

    sample = (
        "Opening browser to sign in\r\n"
        "If the browser didn't open, visit: "
        "https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=xyz\r\n"
        "Paste code here if prompted > "
    )
    master, slave = pty.openpty()
    os.write(slave, sample.encode())
    try:
        url = ab._read_until_url(master, timeout=5)
    finally:
        os.close(slave)
        os.close(master)
    assert url == (
        "https://claude.com/cai/oauth/authorize?code=true&client_id=abc&state=xyz"
    )


def test_read_until_url_times_out():
    import pty

    master, slave = pty.openpty()
    os.write(slave, b"no url in this output\r\n")
    try:
        with pytest.raises(TimeoutError):
            ab._read_until_url(master, timeout=1)
    finally:
        os.close(slave)
        os.close(master)


# --- code extraction --------------------------------------------------------

def test_extract_code_from_slack_event():
    ev = {"source": "slack", "fields": {"channel": "C123", "text": "  abc#def  "}}
    assert ab._extract_code(ev, "C123") == "abc#def"


def test_extract_code_takes_last_token():
    ev = {"source": "slack", "fields": {"channel": "C123", "text": "code: abc#def"}}
    assert ab._extract_code(ev, "C123") == "abc#def"


def test_extract_code_ignores_other_channels():
    ev = {"source": "slack", "fields": {"channel": "C999", "text": "abc"}}
    assert ab._extract_code(ev, "C123") is None


def test_extract_code_ignores_non_slack():
    ev = {"source": "github", "fields": {"text": "abc"}}
    assert ab._extract_code(ev, "C123") is None


def test_extract_code_ignores_empty_text():
    ev = {"source": "slack", "fields": {"channel": "C123", "text": "   "}}
    assert ab._extract_code(ev, "C123") is None


def test_extract_code_from_discord_conversation():
    channel = ab.LoginChannel(
        destination="discord:111222333444555666:dm:999888777666555444",
        source="discord",
        topic="discord:111222333444555666",
    )
    ev = {
        "source": "discord",
        "type": "discord.message_create",
        "conversation": "discord:111222333444555666:dm:999888777666555444",
        "text": "code: abc#def",
        "fields": {"channel": "999888777666555444"},
    }
    assert ab._extract_code(ev, channel) == "abc#def"


def test_extract_code_from_discord_reply_event_shape():
    channel = ab.LoginChannel(
        destination="discord:111222333444555666:channel:999888777666555444",
        source="discord",
        topic="discord:111222333444555666",
    )
    ev = {
        "source": "discord",
        "type": "discord.reply",
        "conversation": "discord:111222333444555666:channel:999888777666555444",
        "text": "code: abc#def",
        "fields": {
            "channel_id": "999888777666555444",
            "message_id": "1300000000000000001",
        },
    }
    assert ab._extract_code(ev, channel) == "abc#def"


def test_extract_code_rejects_other_discord_conversation():
    channel = ab.LoginChannel(
        destination="discord:111222333444555666:dm:999888777666555444",
        source="discord",
        topic="discord:111222333444555666",
    )
    ev = {
        "source": "discord",
        "conversation": "discord:111222333444555666:dm:000000000000000000",
        "text": "abc#def",
    }
    assert ab._extract_code(ev, channel) is None


def test_extract_code_from_whatsapp_conversation():
    channel = ab.LoginChannel(
        destination="whatsapp:111222333444555666:dm:15551234567",
        source="whatsapp",
        topic="whatsapp:111222333444555666",
    )
    ev = {
        "source": "whatsapp",
        "type": "whatsapp.message",
        "conversation": "whatsapp:111222333444555666:dm:15551234567",
        "text": "code: abc#def",
        "fields": {
            "phone_number_id": "111222333444555666",
            "user_id": "15551234567",
        },
    }
    assert ab._extract_code(ev, channel) == "abc#def"


def test_extract_code_accepts_slack_thread_for_base_conversation():
    channel = ab.LoginChannel(
        destination="slack:T123:dm:D456",
        source="slack",
        topic="slack:T123:app:A123",
    )
    ev = {
        "source": "slack",
        "conversation": "slack:T123:dm:D456:thread:1779500000.000100",
        "text": "abc#def",
    }
    assert ab._extract_code(ev, channel) == "abc#def"


def test_extract_code_rejects_wrong_slack_thread_for_thread_conversation():
    channel = ab.LoginChannel(
        destination="slack:T123:dm:D456:thread:1779500000.000100",
        source="slack",
        topic="slack:T123:app:A123",
    )
    ev = {
        "source": "slack",
        "conversation": "slack:T123:dm:D456:thread:1779500000.000200",
        "text": "abc#def",
    }
    assert ab._extract_code(ev, channel) is None


def test_paste_back_instruction_for_discord_server_channel_mentions_reply():
    channel = ab.LoginChannel(
        destination="discord:111222333444555666:channel:999888777666555444",
        source="discord",
        topic="discord:111222333444555666",
    )
    instruction = ab._paste_back_instruction(channel)
    assert "reply to this message" in instruction
    assert "@mention the bot" in instruction


@pytest.mark.parametrize(
    "channel",
    [
        ab.LoginChannel(
            destination="slack:T123:channel:C456",
            source="slack",
            topic="slack:T123:app:A123",
        ),
        ab.LoginChannel(
            destination="C456",
            source="slack",
            topic="slack:T123:app:A123",
            legacy_slack_channel="C456",
        ),
    ],
)
def test_paste_back_instruction_for_slack_channel_mentions_reply(channel):
    instruction = ab._paste_back_instruction(channel)
    assert "reply to this message" in instruction
    assert "@mention the bot" in instruction
    assert "in this channel" not in instruction


def test_paste_back_instruction_for_slack_dm_stays_in_channel():
    channel = ab.LoginChannel(
        destination="slack:T123:dm:D456",
        source="slack",
        topic="slack:T123:app:A123",
    )
    instruction = ab._paste_back_instruction(channel)
    assert "in this channel" in instruction
    assert "@mention the bot" not in instruction


def test_extract_code_from_real_adapter_dm_shape():
    """Reproduces the prod bug: the Slack adapter (event-server/core/src/
    adapters/chat-sdk-slack.ts) emits `text` at the TOP LEVEL and in `payload`, with `fields`
    holding only channel/channel_type/user_id/ts — never `text`. The login
    bootstrap must read the code out of that real shape."""
    ev = {
        "source": "slack",
        "type": "slack.dm",
        "text": "abc#def",
        "fields": {
            "channel": "D0B51JP1N4C",
            "channel_type": "im",
            "user_id": "U0952RZTHBR",
            "ts": "1779500000.000100",
        },
        "payload": {
            "channel": "D0B51JP1N4C",
            "channel_type": "im",
            "text": "abc#def",
        },
    }
    assert ab._extract_code(ev, "D0B51JP1N4C") == "abc#def"


def test_extract_code_real_shape_rejects_other_channel():
    ev = {
        "source": "slack",
        "type": "slack.dm",
        "text": "abc#def",
        "fields": {"channel": "D999", "channel_type": "im"},
        "payload": {"channel": "D999", "text": "abc#def"},
    }
    assert ab._extract_code(ev, "D0B51JP1N4C") is None


# --- orchestration (everything faked) ---------------------------------------

@pytest.fixture
def slack_config(tmp_path, monkeypatch):
    """A project with a Slack bot_token so run_bootstrap gets past config checks."""
    from bobi import paths

    monkeypatch.setattr(paths, "_root", None, raising=False)
    project = tmp_path / "proj"
    paths.package_dir(project).mkdir(parents=True)
    paths.agent_yaml_path(project).write_text(
        "agent: test\n"
        "event_server_url: wss://example\n"
        "services:\n"
        "  - name: slack\n"
        "    credentials:\n"
        "      bot_token: xoxb-test\n"
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv(ab.LOGIN_CHANNEL_ENV, "C0LOGIN42")
    return project


@pytest.fixture
def empty_thread_history(monkeypatch):
    """The ask's thread holds nothing new, so the run blocks on the live
    listener. Lets a re-attach be exercised without faking a gap reply."""
    import bobi.events.gateway as gateway_mod

    monkeypatch.setattr(gateway_mod, "channels_history",
                        lambda project, conv, limit=100: [])


@pytest.fixture
def slack_bot_identity(monkeypatch):
    """The bot's own Slack identity, which the history reads baseline against."""
    import bobi.slack as slack_mod

    monkeypatch.setattr(slack_mod, "resolve_auth_info",
                        lambda token: ("T1", "B1", "U0BOT"))
    monkeypatch.setattr(slack_mod, "resolve_app_id", lambda token, bot_id: "A1")
    return "U0BOT"


@pytest.fixture
def slack_gateway_config(slack_config, monkeypatch):
    """Slack through the channel gateway: the destination is a conversation ref,
    not a raw channel id, so posts go through `channels_send`."""
    import bobi.slack as slack_mod

    monkeypatch.setattr(slack_mod, "resolve_auth_info",
                        lambda token: ("T1", "B1", "U0BOT"))
    monkeypatch.setattr(slack_mod, "resolve_app_id", lambda token, bot_id: "A1")
    monkeypatch.setenv(ab.LOGIN_CHANNEL_ENV, "slack:T1:channel:C0LOGIN42")
    return slack_config


@pytest.fixture
def discord_config(tmp_path, monkeypatch):
    from bobi import paths

    monkeypatch.setattr(paths, "_root", None, raising=False)
    project = tmp_path / "proj"
    paths.package_dir(project).mkdir(parents=True)
    paths.agent_yaml_path(project).write_text(
        "agent: test\n"
        "event_server_url: http://localhost:8080\n"
        "services:\n"
        "  - name: discord\n"
        "    credentials:\n"
        "      bot_token: dc-test\n"
        "      application_id: '111222333444555666'\n"
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv(
        ab.LOGIN_CHANNEL_ENV,
        "discord:111222333444555666:dm:999888777666555444",
    )
    return project


@pytest.fixture
def remote_discord_config(discord_config):
    from bobi import paths

    agent_yaml = paths.agent_yaml_path(discord_config)
    agent_yaml.write_text(
        agent_yaml.read_text().replace(
            "event_server_url: http://localhost:8080",
            "event_server_url: https://event.example",
        )
    )
    return discord_config


@pytest.fixture
def whatsapp_config(tmp_path, monkeypatch):
    from bobi import paths

    monkeypatch.setattr(paths, "_root", None, raising=False)
    project = tmp_path / "proj"
    paths.package_dir(project).mkdir(parents=True)
    paths.agent_yaml_path(project).write_text(
        "agent: test\n"
        "event_server_url: https://event.example\n"
        "services:\n"
        "  - name: whatsapp\n"
        "    credentials:\n"
        "      access_token: wa-test\n"
        "      phone_number_id: '111222333444555666'\n"
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv(
        ab.LOGIN_CHANNEL_ENV,
        "whatsapp:111222333444555666:dm:15551234567",
    )
    return project


@pytest.mark.parametrize(
    ("brain_block", "expected_gateway_url"),
    [
        ("", ""),
        (
            "brain:\n  kind: claude\n  base_url: https://gateway.example\n",
            "https://gateway.example",
        ),
        (
            "brain:\n  kind: gateway\n  base_url: https://gateway.example\n",
            "https://gateway.example",
        ),
    ],
    ids=["native-claude", "gateway-base-url-subscription", "gateway-alias-subscription"],
)
def test_run_bootstrap_happy_path(
    slack_config, monkeypatch, brain_block, expected_gateway_url,
):
    from bobi.brain.gateway import gateway_base_url
    from bobi import paths

    if brain_block:
        agent_yaml = paths.agent_yaml_path(slack_config)
        agent_yaml.write_text(agent_yaml.read_text() + brain_block)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    posts = []
    written = []

    config_dir = slack_config.parent / "claude-volume"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    creds = config_dir / ".credentials.json"

    class FakeProc:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def fake_spawn(home):
        return FakeProc(), -1  # master_fd unused (we fake _read_until_url path)

    # _read_until_url reads from a real fd; bypass it by faking the orchestrator's
    # collaborators that touch the fd.
    monkeypatch.setattr(ab, "_read_until_url", lambda fd, timeout: "https://x/oauth/authorize?c=1")
    monkeypatch.setattr(ab, "_write_line", lambda fd, text: written.append(text))

    def fake_post(token, channel, text, thread_ts=""):
        posts.append((token, channel, text, thread_ts))
        return {"ok": True, "ts": "1700000000.000001"}

    def fake_wait(project_path, channel, timeout, listener=None):
        # Simulate the human pasting the code; claude then writes creds.
        creds.parent.mkdir(parents=True)
        creds.write_text(json.dumps({
            "claudeAiOauth": {"refreshToken": "refresh"},
        }))
        return "the-code"

    ok = ab.run_bootstrap(
        slack_config,
        spawn_login=fake_spawn,
        post_message=fake_post,
        wait_for_code=fake_wait,
    )
    assert ok is True
    assert written == ["the-code"]
    # First post = URL prompt; final post = success.
    prompt = next(p[2] for p in posts if "oauth/authorize" in p[2])
    assert "reply to this message" in prompt
    assert "@mention the bot" in prompt
    assert "in this channel" not in prompt
    assert any("complete" in p[2] for p in posts)
    assert all(p[1] == "C0LOGIN42" for p in posts)
    assert gateway_base_url() == expected_gateway_url


def test_run_bootstrap_posts_to_discord_conversation(discord_config, monkeypatch):
    import bobi.events.gateway as gateway_mod
    import bobi.events.server as server_mod

    sent = []
    written = []
    creds = os.path.join(os.environ["HOME"], ".claude", ".credentials.json")

    class FakeProc:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(ab, "_read_until_url", lambda fd, timeout: "https://x/oauth/authorize?c=1")
    monkeypatch.setattr(ab, "_write_line", lambda fd, text: written.append(text))
    monkeypatch.setattr(
        server_mod,
        "ensure_bubble",
        lambda es_url, project_path: {"bubble_id": "bub", "bubble_key": "key"},
    )
    monkeypatch.setattr(server_mod, "register_discord_apps", lambda *a, **k: ["111222333444555666"])
    monkeypatch.setattr(
        server_mod,
        "health",
        lambda es_url: {
            "status": "ok",
            "mode": "local",
            "discord_gateway": [{
                "application_id": "111222333444555666",
                "state": "connected",
            }],
        },
    )
    monkeypatch.setattr(gateway_mod, "channels_send",
                        lambda project, conv, text, mode="post": (
                            sent.append((conv, text, mode))
                            or {"ok": True, "ts": "1700000000.000001"}))

    def fake_spawn(home):
        return FakeProc(), -1

    def fake_wait(project_path, channel, timeout, listener=None):
        assert channel == ab.LoginChannel(
            destination="discord:111222333444555666:dm:999888777666555444",
            source="discord",
            topic="discord:111222333444555666",
        )
        os.makedirs(os.path.dirname(creds), exist_ok=True)
        with open(creds, "w") as f:
            f.write(json.dumps({
                "claudeAiOauth": {"refreshToken": "refresh"},
            }))
        return "the-code"

    ok = ab.run_bootstrap(
        discord_config,
        spawn_login=fake_spawn,
        post_message=lambda *a, **k: (_ for _ in ()).throw(AssertionError("Slack post unused")),
        wait_for_code=fake_wait,
    )

    assert ok is True
    assert written == ["the-code"]
    assert all(
        conv == "discord:111222333444555666:dm:999888777666555444"
        for conv, _text, _mode in sent
    )
    # Ask-first: the ask lands before anything credential-granting does.
    assert "I will begin the login flow" in sent[0][1]
    assert "oauth/authorize" not in sent[0][1]
    assert any("oauth/authorize" in text for _conv, text, _mode in sent)
    assert "complete" in sent[-1][1]


def test_run_bootstrap_rejects_discord_paste_back_on_remote_event_server(
    remote_discord_config,
    monkeypatch,
):
    monkeypatch.setattr(
        ab,
        "_ensure_discord_inbound_ready",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(
            "Discord subscription-login paste-back requires an event server "
            "with the local Discord Gateway driver."
        )),
    )
    with pytest.raises(RuntimeError, match="local Discord Gateway driver"):
        ab.run_bootstrap(remote_discord_config, spawn_login=lambda h: None)


def test_ensure_discord_inbound_ready_rejects_event_server_without_gateway(
    discord_config,
    monkeypatch,
):
    import bobi.events.server as server_mod

    monkeypatch.setattr(
        server_mod,
        "ensure_bubble",
        lambda es_url, project_path: {"bubble_id": "bub", "bubble_key": "key"},
    )
    monkeypatch.setattr(server_mod, "register_discord_apps", lambda *a, **k: ["111222333444555666"])
    monkeypatch.setattr(
        server_mod,
        "health",
        lambda es_url: {"status": "ok", "mode": "worker"},
    )

    with pytest.raises(RuntimeError, match="connected Gateway"):
        ab._ensure_discord_inbound_ready(
            discord_config,
            ab.Config.load(discord_config),
            ab.LoginChannel(
                destination="discord:111222333444555666:dm:999888777666555444",
                source="discord",
                topic="discord:111222333444555666",
            ),
            timeout=0.01,
        )


def test_ensure_discord_inbound_ready_allows_internal_local_hostname(
    discord_config,
    monkeypatch,
):
    from bobi import paths
    import bobi.events.server as server_mod

    agent_yaml = paths.agent_yaml_path(discord_config)
    agent_yaml.write_text(
        agent_yaml.read_text().replace(
            "event_server_url: http://localhost:8080",
            "event_server_url: http://event-server:8080",
        )
    )
    monkeypatch.setattr(
        server_mod,
        "ensure_bubble",
        lambda es_url, project_path: {"bubble_id": "bub", "bubble_key": "key"},
    )
    monkeypatch.setattr(server_mod, "register_discord_apps", lambda *a, **k: ["111222333444555666"])
    monkeypatch.setattr(
        server_mod,
        "health",
        lambda es_url: {
            "status": "ok",
            "mode": "local",
            "discord_gateway": [{
                "application_id": "111222333444555666",
                "state": "connected",
            }],
        },
    )

    ab._ensure_discord_inbound_ready(
        discord_config,
        ab.Config.load(discord_config),
        ab.LoginChannel(
            destination="discord:111222333444555666:dm:999888777666555444",
            source="discord",
            topic="discord:111222333444555666",
        ),
        timeout=0.01,
    )


def test_run_bootstrap_skips_when_creds_present(slack_config, monkeypatch):
    config_dir = slack_config.parent / "claude-volume"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    creds = config_dir / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(json.dumps({
        "claudeAiOauth": {"refreshToken": "refresh"},
    }))

    called = {"spawn": False}

    def fake_spawn(home):
        called["spawn"] = True
        raise AssertionError("should not spawn when creds exist")

    assert ab.run_bootstrap(slack_config, spawn_login=fake_spawn) is True
    assert called["spawn"] is False


def test_run_bootstrap_refuses_with_api_key_set(slack_config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        ab.run_bootstrap(slack_config, spawn_login=lambda h: None)


@pytest.mark.parametrize("kind", ["claude", "gateway"])
def test_run_bootstrap_refuses_gateway_brain_with_auth_token(
    slack_config, monkeypatch, kind,
):
    """An explicit gateway token must not be replaced by subscription auth."""
    from bobi import paths

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-token")
    paths.agent_yaml_path(slack_config).write_text(
        paths.agent_yaml_path(slack_config).read_text()
        + f"brain:\n  kind: {kind}\n  base_url: http://localhost:4000\n"
    )
    with pytest.raises(RuntimeError, match="ANTHROPIC_AUTH_TOKEN"):
        ab.run_bootstrap(slack_config, spawn_login=lambda h: None)


@pytest.mark.parametrize("kind", ["codex", "gateway-openai"])
def test_run_bootstrap_refuses_gateway_openai_brain(
    slack_config, monkeypatch, kind,
):
    """An OpenAI-compatible gateway team has no subscription login either."""
    from bobi import paths

    paths.agent_yaml_path(slack_config).write_text(
        paths.agent_yaml_path(slack_config).read_text()
        + f"brain:\n  kind: {kind}\n  base_url: http://localhost:9000/v1\n"
    )
    with pytest.raises(RuntimeError, match="gateway"):
        ab.run_bootstrap(slack_config, spawn_login=lambda h: None)


def test_run_bootstrap_requires_channel(slack_config, monkeypatch):
    monkeypatch.delenv(ab.LOGIN_CHANNEL_ENV, raising=False)
    with pytest.raises(RuntimeError, match="BOBI_LOGIN_CHANNEL"):
        ab.run_bootstrap(slack_config, spawn_login=lambda h: None)


@pytest.mark.real_listener
def test_wait_for_code_subscribes_to_app_qualified_slack_topic(slack_config, monkeypatch):
    import bobi.events.client as client_mod
    import bobi.events.server as server_mod
    import bobi.slack as slack_mod

    registered = {}

    monkeypatch.setattr(
        server_mod,
        "ensure_bubble",
        lambda es_url, project_path: {"bubble_id": "bub", "bubble_key": "key"},
    )
    monkeypatch.setattr(server_mod, "register_slack_workspaces", lambda *a, **k: ["T123"])
    monkeypatch.setattr(slack_mod, "resolve_auth_info", lambda token: ("T123", "B123", "U123"))
    monkeypatch.setattr(slack_mod, "resolve_app_id", lambda token, bot_id: "A123")

    def fake_register(es_url, name, topics, bubble_id="", bubble_key=""):
        registered["topics"] = topics
        return "dep", "api-key"

    monkeypatch.setattr(server_mod, "register", fake_register)

    class FakeClient:
        def __init__(self, es_url, deployment_id, api_key, queue):
            self.queue = queue

        def start(self):
            self.queue.put({
                "source": "slack",
                "text": "the-code",
                "fields": {"channel": "D0LOGIN"},
            })

        def wait_connected(self, timeout):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(client_mod, "EventServerClient", FakeClient)

    assert ab._wait_for_code(slack_config, "D0LOGIN", timeout=1) == "the-code"
    assert registered["topics"] == ["slack:T123:app:A123"]


def test_register_login_channel_remints_after_stale_bubble_rejection(
    slack_config,
    monkeypatch,
):
    """An empty channel registration only permits a re-mint after a signed
    JOIN proves that the persisted bubble is stale (#868 / MOD-307)."""
    import bobi.events.server as server_mod

    ensure_calls = []
    workspace_bubbles = []

    def fake_ensure(es_url, project_path, force_remint_of=""):
        ensure_calls.append(force_remint_of)
        if force_remint_of:
            assert force_remint_of == "bub-old"
            return {"bubble_id": "bub-new", "bubble_key": "key-new"}
        return {"bubble_id": "bub-old", "bubble_key": "key-old"}

    def fake_register_workspaces(es_url, cfg, bubble_id="", bubble_key=""):
        workspace_bubbles.append((bubble_id, bubble_key))
        return [] if bubble_id == "bub-old" else ["T123"]

    def fake_register(es_url, name, topics, bubble_id="", bubble_key=""):
        assert topics == []
        assert (bubble_id, bubble_key) == ("bub-old", "key-old")
        raise server_mod.BubbleRejected("stale bubble")

    monkeypatch.setattr(server_mod, "ensure_bubble", fake_ensure)
    monkeypatch.setattr(server_mod, "register_slack_workspaces", fake_register_workspaces)
    monkeypatch.setattr(server_mod, "register", fake_register)

    bubble = ab._register_login_channel(
        slack_config,
        ab.Config.load(slack_config),
        ab.LoginChannel(
            destination="slack:T123:dm:D0LOGIN",
            source="slack",
            topic="slack:T123:app:A123",
        ),
    )

    assert bubble == {"bubble_id": "bub-new", "bubble_key": "key-new"}
    assert ensure_calls == ["", "bub-old"]
    assert workspace_bubbles == [
        ("bub-old", "key-old"),
        ("bub-new", "key-new"),
    ]


@pytest.mark.real_listener
def test_wait_for_code_recovers_if_bubble_stales_after_channel_registration(
    slack_config,
    monkeypatch,
):
    """If the server restarts between channel registration and listener JOIN,
    re-mint, recreate the channel grant, and retry the listener once."""
    import bobi.events.client as client_mod
    import bobi.events.server as server_mod

    current_bubble = {"id": "bub-old", "key": "key-old"}
    workspace_bubbles = []
    listener_bubbles = []

    def fake_ensure(es_url, project_path, force_remint_of=""):
        if force_remint_of:
            assert force_remint_of == "bub-old"
            current_bubble.update(id="bub-new", key="key-new")
        return {
            "bubble_id": current_bubble["id"],
            "bubble_key": current_bubble["key"],
        }

    def fake_register_workspaces(es_url, cfg, bubble_id="", bubble_key=""):
        workspace_bubbles.append((bubble_id, bubble_key))
        return ["T123"]

    def fake_register(es_url, name, topics, bubble_id="", bubble_key=""):
        listener_bubbles.append((bubble_id, bubble_key, list(topics)))
        if bubble_id == "bub-old":
            raise server_mod.BubbleRejected("stale bubble")
        return "dep", "api-key"

    class FakeClient:
        def __init__(self, es_url, deployment_id, api_key, queue):
            self.queue = queue

        def start(self):
            self.queue.put({
                "source": "slack",
                "text": "the-code",
                "fields": {"channel": "D0LOGIN"},
            })

        def wait_connected(self, timeout):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(server_mod, "ensure_bubble", fake_ensure)
    monkeypatch.setattr(server_mod, "register_slack_workspaces", fake_register_workspaces)
    monkeypatch.setattr(server_mod, "register", fake_register)
    monkeypatch.setattr(client_mod, "EventServerClient", FakeClient)
    monkeypatch.setattr(ab, "_slack_topic", lambda cfg: "slack:T123:app:A123")

    assert ab._wait_for_code(slack_config, "D0LOGIN", timeout=1) == "the-code"
    assert workspace_bubbles == [
        ("bub-old", "key-old"),
        ("bub-new", "key-new"),
    ]
    assert listener_bubbles == [
        ("bub-old", "key-old", ["slack:T123:app:A123"]),
        ("bub-new", "key-new", ["slack:T123:app:A123"]),
    ]


@pytest.mark.real_listener
def test_wait_for_code_subscribes_to_discord_app_topic(discord_config, monkeypatch):
    import bobi.events.client as client_mod
    import bobi.events.server as server_mod

    registered = {}

    monkeypatch.setattr(
        server_mod,
        "ensure_bubble",
        lambda es_url, project_path: {"bubble_id": "bub", "bubble_key": "key"},
    )
    monkeypatch.setattr(server_mod, "register_discord_apps", lambda *a, **k: ["111222333444555666"])

    def fake_register(es_url, name, topics, bubble_id="", bubble_key=""):
        registered["topics"] = topics
        return "dep", "api-key"

    monkeypatch.setattr(server_mod, "register", fake_register)

    class FakeClient:
        def __init__(self, es_url, deployment_id, api_key, queue):
            self.queue = queue

        def start(self):
            self.queue.put({
                "source": "discord",
                "conversation": "discord:111222333444555666:dm:999888777666555444",
                "text": "the-code",
            })

        def wait_connected(self, timeout):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(client_mod, "EventServerClient", FakeClient)

    assert ab._wait_for_code(
        discord_config,
        ab.LoginChannel(
            destination="discord:111222333444555666:dm:999888777666555444",
            source="discord",
            topic="discord:111222333444555666",
        ),
        timeout=1,
    ) == "the-code"
    assert registered["topics"] == ["discord:111222333444555666"]


@pytest.mark.real_listener
def test_wait_for_code_subscribes_to_whatsapp_number_topic(whatsapp_config, monkeypatch):
    import bobi.events.client as client_mod
    import bobi.events.server as server_mod

    registered = {}

    monkeypatch.setattr(
        server_mod,
        "ensure_bubble",
        lambda es_url, project_path: {"bubble_id": "bub", "bubble_key": "key"},
    )
    monkeypatch.setattr(server_mod, "register_whatsapp_numbers", lambda *a, **k: ["111222333444555666"])

    def fake_register(es_url, name, topics, bubble_id="", bubble_key=""):
        registered["topics"] = topics
        return "dep", "api-key"

    monkeypatch.setattr(server_mod, "register", fake_register)

    class FakeClient:
        def __init__(self, es_url, deployment_id, api_key, queue):
            self.queue = queue

        def start(self):
            self.queue.put({
                "source": "whatsapp",
                "conversation": "whatsapp:111222333444555666:dm:15551234567",
                "text": "the-code",
            })

        def wait_connected(self, timeout):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(client_mod, "EventServerClient", FakeClient)

    assert ab._wait_for_code(
        whatsapp_config,
        ab.LoginChannel(
            destination="whatsapp:111222333444555666:dm:15551234567",
            source="whatsapp",
            topic="whatsapp:111222333444555666",
        ),
        timeout=1,
    ) == "the-code"
    assert registered["topics"] == ["whatsapp:111222333444555666"]


@pytest.mark.real_listener
def test_wait_for_code_refuses_missing_slack_app_identity(slack_config, monkeypatch):
    import bobi.slack as slack_mod

    monkeypatch.setattr(slack_mod, "resolve_auth_info", lambda token: ("T123", "B123", "U123"))
    monkeypatch.setattr(slack_mod, "resolve_app_id", lambda token, bot_id: "")

    with pytest.raises(slack_mod.SlackAppIdentityError, match="users:read"):
        ab._wait_for_code(slack_config, "D0LOGIN", timeout=1)


# --- Codex brain: device-auth (poll) flow (#485) ----------------------------

def test_credentials_path_for_codex(tmp_path, monkeypatch):
    from bobi.brain import BRAIN_ENV

    monkeypatch.setenv(BRAIN_ENV, "codex")
    assert ab.credentials_path(tmp_path) == tmp_path / ".codex" / "auth.json"


def test_credentials_path_for_codex_honors_codex_home(tmp_path, monkeypatch):
    from bobi.brain import BRAIN_ENV

    monkeypatch.setenv(BRAIN_ENV, "codex")
    codex_home = tmp_path / "codex-volume"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert ab.credentials_path(tmp_path) == codex_home / "auth.json"


def test_scrape_login_codex_gets_url_and_code():
    """Drive _scrape_login against a pty fed the real `codex login --device-auth`
    output — it must lift both the device URL and the one-time code.

    The output is ANSI-colored (codex wraps the URL/code in color codes); the
    code regex's \\b anchor breaks when ESC[94m sits directly before the code, so
    the scraper must strip ANSI first. Regression for the live ci-codex-test boot
    ("did not see the codex login URL/code within 120s")."""
    import pty

    sample = (
        "Welcome to Codex [\x1b[90mv0.142.0\x1b[0m]\r\n"
        "1. Open this link in your browser and sign in to your account\r\n"
        "   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\r\n"
        "2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\r\n"
        "   \x1b[94m5RAR-HF15T\x1b[0m\r\n"
    )
    master, slave = pty.openpty()
    os.write(slave, sample.encode())
    try:
        url, code = ab._scrape_login(master, 5, ab._SPECS["codex"])
    finally:
        os.close(slave)
        os.close(master)
    assert url == "https://auth.openai.com/codex/device"
    assert code == "5RAR-HF15T"


def test_run_bootstrap_codex_device_poll(slack_config, monkeypatch):
    """Codex flow: scrape URL + code, post both, wait for the CLI to poll-auth,
    then verify auth.json landed — no code is pasted back."""
    from bobi.brain import BRAIN_ENV

    monkeypatch.setenv(BRAIN_ENV, "codex")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    posts = []
    creds = os.path.join(os.environ["HOME"], ".codex", "auth.json")

    class FakeProc:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            # The CLI polls, the human authorizes, codex writes auth.json.
            os.makedirs(os.path.dirname(creds), exist_ok=True)
            with open(creds, "w") as f:
                f.write(json.dumps({
                    "tokens": {"refresh_token": "refresh"},
                }))
            return 0

    def fake_spawn(home):
        return FakeProc(), -1

    def fake_scrape(fd, timeout, spec):
        return "https://auth.openai.com/codex/device", "5RAR-HF15T"

    def fake_post(token, channel, text, thread_ts=""):
        posts.append((token, channel, text, thread_ts))
        return {"ok": True, "ts": "1700000000.000001"}

    ok = ab.run_bootstrap(
        slack_config,
        spawn_login=fake_spawn,
        post_message=fake_post,
        scrape_login=fake_scrape,
    )
    assert ok is True
    # The prompt post carries BOTH the device URL and the one-time code.
    assert any("codex/device" in p[2] and "5RAR-HF15T" in p[2] for p in posts)
    assert any("complete" in p[2] for p in posts)
    assert all(p[1] == "C0LOGIN42" for p in posts)


def test_run_bootstrap_codex_refuses_with_openai_key(slack_config, monkeypatch):
    """In codex subscription mode OPENAI_API_KEY would shadow the OAuth creds."""
    from bobi.brain import BRAIN_ENV

    monkeypatch.setenv(BRAIN_ENV, "codex")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-x")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        ab.run_bootstrap(slack_config, spawn_login=lambda h: None)


# --- the login destination is configuration, not an argument ----------------
#
# `login-bootstrap` is registered on the `agent` group (bobi/cli.py), which is
# the surface any worker's shell reaches, and worker sessions run with
# `permission_mode="bypassPermissions"` (bobi/brain/claude.py). These drive the
# REAL CLI on that real group - not run_bootstrap directly - so the worker's
# own invocation is what is under test. Only the three seams that leave the
# process are faked: the login pty, the chat post, and the event-bus wait.

OPERATOR_CHANNEL = "C0LOGIN42"      # what $BOBI_LOGIN_CHANNEL is configured to
ATTACKER_CHANNEL = "D0ATTACKER99"   # a Slack DM the caller picked
# Synthetic stand-in. A real sign-in URL is a live credential-granting link and
# never belongs in a fixture, a log, or a PR.
FAKE_LOGIN_URL = "https://login.invalid/oauth/authorize?synthetic=1"


@pytest.fixture
def cli_login_install(bobi_install, tmp_path, monkeypatch):
    """An installed team with no credentials, whose login channel is configured."""
    import yaml

    agent_yaml = bobi_install.repo_path / "package" / "agent.yaml"
    cfg = yaml.safe_load(agent_yaml.read_text())
    cfg["event_server_url"] = "wss://example"
    cfg["services"] = [{
        "name": "slack",
        "events": True,
        "credentials": {"bot_token": "xoxb-test"},
    }]
    agent_yaml.write_text(yaml.dump(cfg))

    monkeypatch.setenv("HOME", str(tmp_path / "login-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-volume"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv(ab.LOGIN_CHANNEL_ENV, OPERATOR_CHANNEL)
    return bobi_install


def _fake_login_seams(monkeypatch, tmp_path) -> list[tuple[str, str]]:
    """Fake the pty, the chat post, and the bus wait. Returns the posts made."""
    posts: list[tuple[str, str]] = []
    creds = tmp_path / "claude-volume" / ".credentials.json"

    class FakeProc:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def fake_wait(project_path, channel, timeout, listener=None):
        # Stand in for the human pasting the code back; claude writes creds.
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text(json.dumps({
            "claudeAiOauth": {"refreshToken": "refresh"},
        }))
        return "synthetic-code"

    monkeypatch.setattr(ab, "_spawn_login", lambda home: (FakeProc(), -1))
    monkeypatch.setattr(ab, "_read_until_url", lambda fd, timeout: FAKE_LOGIN_URL)
    monkeypatch.setattr(ab, "_write_line", lambda fd, text: None)
    monkeypatch.setattr(
        ab, "post_slack_message",
        lambda token, channel, text, thread_ts="": (
            posts.append((channel, text))
            or {"ok": True, "ts": "1700000000.000001"}))
    monkeypatch.setattr(ab, "_wait_for_code", fake_wait)
    return posts


def test_login_bootstrap_refuses_a_caller_chosen_destination(
    cli_login_install, tmp_path, monkeypatch,
):
    """A worker must not be able to redirect the brain's OAuth login flow.

    Regression for the `--channel` hole: the flag outranked
    $BOBI_LOGIN_CHANNEL, so `bobi agent <name> login-bootstrap --channel
    D<attacker>` posted the live sign-in URL (and, on codex, the one-time
    device code) wherever the caller said - then read the pasted code back
    from that same caller-chosen destination and wrote it into the login
    CLI's stdin.
    """
    from click.testing import CliRunner

    from bobi.cli import main
    from tests.conftest import TEST_AGENT_NAME

    posts = _fake_login_seams(monkeypatch, tmp_path)

    result = CliRunner().invoke(main, [
        "agent", TEST_AGENT_NAME, "login-bootstrap",
        "--channel", ATTACKER_CHANNEL,
    ])

    # Pin the *reason* for the refusal, not just its exit code - a broken
    # fixture would otherwise satisfy both assertions below vacuously.
    assert result.exit_code == 2, (
        "a caller-chosen login destination must be refused as a usage error:\n"
        + result.output
    )
    assert "No such option" in result.output and "--channel" in result.output, (
        result.output
    )
    assert posts == [], (
        f"the login flow ran for a refused invocation and posted: {posts}"
    )


def test_login_bootstrap_posts_only_to_the_configured_channel(
    cli_login_install, tmp_path, monkeypatch,
):
    """First boot still works: docker-entrypoint.sh's exact bare invocation.

    This is the flow the command exists for, and the reason the fix removes the
    override rather than the destination - $BOBI_LOGIN_CHANNEL still drives it.
    """
    from click.testing import CliRunner

    from bobi.cli import main
    from tests.conftest import TEST_AGENT_NAME

    posts = _fake_login_seams(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        main, ["agent", TEST_AGENT_NAME, "login-bootstrap"])

    assert result.exit_code == 0, result.output
    assert posts, "the login URL must still reach the configured channel"
    assert {channel for channel, _text in posts} == {OPERATOR_CHANNEL}
    assert any("oauth/authorize" in text for _channel, text in posts)


# ---------------------------------------------------------------------------
# _resolve_login_channel — the conversation-ref grammar
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ref,expected_topic", [
    # The 6-part threaded form is part of the shared grammar; the private
    # parser this now delegates to (Q012) accepted it too, so a ref that used
    # to resolve must still resolve.
    ("discord:guild1:dm:123:thread:456", "discord:guild1"),
    ("whatsapp:acct1:dm:15551234567", "whatsapp:acct1"),
])
def test_resolve_login_channel_accepts_shared_grammar_refs(ref, expected_topic):
    from bobi.config import Config

    ch = ab._resolve_login_channel(Config(), ref)
    assert ch.destination == ref
    assert ch.topic == expected_topic
    assert ch.legacy_slack_channel == ""


@pytest.mark.parametrize("ref", [
    "discord:guild1:channel",             # too few segments
    "discord::channel:123",               # empty segment
    "discord:guild1:voice:123",           # chat_type outside CHAT_TYPES
    "discord:guild1:channel:123:reply:4",  # 6 parts but not 'thread'
])
def test_resolve_login_channel_rejects_non_conversation_refs(ref):
    """A ref the shared grammar rejects must fall through to the legacy Slack
    path — which, with no bot_token configured, is a clear RuntimeError rather
    than a silent mis-parse."""
    from bobi.config import Config

    with pytest.raises(RuntimeError, match="not a conversation ref"):
        ab._resolve_login_channel(Config(), ref)


def test_auth_bootstrap_keeps_no_private_copy_of_the_grammar():
    """Q012: the grammar has one Python implementation. A re-inlined private
    parser here is the drift this guards against."""
    assert not hasattr(ab, "_parse_conversation")


# --- #958 ask-first: stage 1 (ordering, human-only, one correlation rule) ---
#
# These drive the real `_ready_reply` predicate through a faked transport: the
# injected listener hands `run_bootstrap` a real SimpleQueue the test fills, so
# the correlation rule itself is under test rather than a stub of it.

def _slack_event(text="ready", *, conversation="", thread_ts=None, channel="C0LOGIN42",
                 ts="1700000000.000100", bot_id=None, source="slack",
                 event_type="slack.thread_reply"):
    fields = {"channel": channel, "user_id": "U0HUMAN", "ts": ts}
    if thread_ts is not None:
        fields["thread_ts"] = thread_ts
    if bot_id is not None:
        fields["bot_id"] = bot_id
    event = {"source": source, "type": event_type, "text": text, "fields": fields}
    if conversation:
        event["conversation"] = conversation
    return event


class _ConsumedIds(set):
    """Records the moment the implementation accepts a reply, for item 1."""

    def __init__(self, log):
        super().__init__()
        self._log = log

    def add(self, item):
        self._log.append("ready reply consumed")
        super().add(item)


class _FakeListener:
    """Stands in for the live event-bus listener; its queue is driven by the test."""

    def __init__(self, events=(), log=None, channel=None):
        from queue import SimpleQueue

        self.queue = SimpleQueue()
        for ev in events:
            self.queue.put(ev)
        self.stopped = False
        self._log = log if log is not None else []
        self.channel = channel
        self.consumed_ids = _ConsumedIds(self._log)
        self._log.append("listener connected")

    @property
    def client(self):
        return self

    def stop(self):
        self.stopped = True


@pytest.fixture
def ask_state_home(tmp_path, monkeypatch):
    """Isolate the ask state file (and codex's credential dir) under tmp_path."""
    home = tmp_path / "codex-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _run_ask_first(project, *, events, monkeypatch, log=None, creds_writer=None,
                   post_ts="1700000000.000001", **kwargs):
    """Drive run_bootstrap's ask-first path with a faked transport.

    Returns (ok, log, posts) where `log` is the ordered trace the ordering
    assertion in verification item 1 needs.
    """
    log = log if log is not None else []
    posts = []

    class FakeProc:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def fake_spawn(home):
        log.append("spawn_login")
        if creds_writer is not None:
            creds_writer()
        return FakeProc(), -1

    def fake_post(token, channel, text, thread_ts=""):
        log.append("ask posted" if "ready" in text else "post")
        posts.append((channel, text, thread_ts))
        return {"ok": True, "ts": post_ts}

    def fake_connect(project_path, channel, timeout):
        return _FakeListener(events, log=log, channel=channel)

    monkeypatch.setattr(ab, "_read_until_url",
                        lambda fd, timeout: "https://x/oauth/authorize?c=1")
    monkeypatch.setattr(ab, "_write_line", lambda fd, text: None)
    monkeypatch.setattr(
        ab, "_scrape_login",
        lambda fd, timeout, spec: ("https://auth.openai.com/codex/device", "ABCD-12345"),
    )
    ok = ab.run_bootstrap(
        project,
        spawn_login=fake_spawn,
        post_message=fake_post,
        connect_listener=fake_connect,
        **kwargs,
    )
    return ok, log, posts


# 1. Ordering, both flows.

def test_ask_first_orders_connect_post_reply_spawn_paste_back(
    slack_config, ask_state_home, monkeypatch,
):
    """Item 1, paste_back: the listener is live before the ask is posted."""
    creds = os.path.join(os.environ["HOME"], ".claude", ".credentials.json")

    def write_creds():
        os.makedirs(os.path.dirname(creds), exist_ok=True)
        with open(creds, "w") as f:
            f.write(json.dumps({"claudeAiOauth": {"refreshToken": "refresh"}}))

    log = []

    def fake_wait(project_path, channel, timeout, listener=None):
        log.append("code pasted")
        return "the-code"

    ok, log, posts = _run_ask_first(
        slack_config, events=[_slack_event(thread_ts="1700000000.000001")],
        monkeypatch=monkeypatch, log=log, creds_writer=write_creds,
        wait_for_code=fake_wait,
    )
    assert ok is True
    assert log[:4] == [
        "listener connected", "ask posted", "ready reply consumed", "spawn_login",
    ]


def test_ask_first_orders_connect_post_reply_spawn_device_poll(
    slack_config, ask_state_home, monkeypatch,
):
    """Item 1, device_poll: same guarantee on the codex branch."""
    codex_creds = ask_state_home / "auth.json"

    def write_creds():
        codex_creds.write_text(json.dumps({"tokens": {"refresh_token": "r"}}))

    ok, log, posts = _run_ask_first(
        slack_config, events=[_slack_event(thread_ts="1700000000.000001")],
        monkeypatch=monkeypatch, creds_writer=write_creds, target="codex",
    )
    assert ok is True
    assert log[:4] == [
        "listener connected", "ask posted", "ready reply consumed", "spawn_login",
    ]


# 2. A bot reply is not ready.

def test_a_bot_reply_alone_never_satisfies_the_wait(
    slack_config, ask_state_home, monkeypatch,
):
    """Item 2, first leg. A third-party bot posting in the ask's thread must
    not start a login. Asserting the timeout is what makes this leg real:
    counting consumptions cannot tell a bot reply from a human one."""
    with pytest.raises(TimeoutError, match="no human replied"):
        ab.run_bootstrap(
            slack_config, target="codex", ask_timeout=0.1,
            spawn_login=lambda h: (_ for _ in ()).throw(
                AssertionError("a bot reply must not spawn the login CLI")),
            post_message=lambda t, c, x, thread_ts="": {
                "ok": True, "ts": "1700000000.000001"},
            connect_listener=lambda p, c, t: _FakeListener(
                [_slack_event("ready", thread_ts="1700000000.000001",
                              ts="1700000000.000009", bot_id="B0THIRDPARTY")],
                channel=c),
        )


def test_a_human_reply_after_a_bot_one_is_the_one_consumed(
    slack_config, ask_state_home, monkeypatch,
):
    """Item 2, second leg. The bot event is skipped and the HUMAN message is
    the one recorded as consumed - pinned by id, because a consumption count
    alone passes whichever message satisfied the wait."""
    codex_creds = ask_state_home / "auth.json"
    listener = _FakeListener([
        _slack_event("ready", thread_ts="1700000000.000001",
                     ts="1700000000.000009", bot_id="B0THIRDPARTY"),
        _slack_event("ready", thread_ts="1700000000.000001",
                     ts="1700000000.000010"),
    ], channel=None)

    def connect(project_path, channel, timeout):
        listener.channel = channel
        return listener

    monkeypatch.setattr(
        ab, "_scrape_login",
        lambda fd, t, spec: ("https://auth.openai.com/codex/device", "ABCD-12345"))

    class FakeProc:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            codex_creds.write_text(json.dumps({"tokens": {"refresh_token": "r"}}))
            return 0

    ok = ab.run_bootstrap(
        slack_config, target="codex",
        spawn_login=lambda h: (FakeProc(), -1),
        post_message=lambda t, c, x, thread_ts="": {
            "ok": True, "ts": "1700000000.000001"},
        connect_listener=connect,
    )
    assert ok is True
    assert set(listener.consumed_ids) == {"1700000000.000010"}, (
        "the human reply is the consumed one, not the bot's")


# 3. Correlation, channel destination: thread_ts only, never the event type.

@pytest.mark.parametrize(
    ("label", "event", "ready"),
    [
        ("thread_reply in the ask's thread",
         _slack_event(thread_ts="1700000000.000001"), True),
        ("thread_reply in another thread",
         _slack_event(thread_ts="1700000000.999999"), False),
        ("mention, top-level in the login channel",
         _slack_event(event_type="slack.mention", thread_ts=None), False),
        ("mention, inside the ask's thread",
         _slack_event(event_type="slack.mention", thread_ts="1700000000.000001"), True),
    ],
)
def test_channel_correlation_keys_on_thread_ts_only(label, event, ready):
    """Item 3. The fourth leg is the one an event-type filter breaks (Z5)."""
    channel = ab.LoginChannel(
        destination="C0LOGIN42", source="slack", topic="slack:T1:app:A1",
        legacy_slack_channel="C0LOGIN42",
    )
    got = ab._ready_reply(event, channel, "1700000000.000001")
    assert (got is not None) is ready, label


# 4. Correlation, DM destination: no anchor needed.

@pytest.mark.parametrize(
    ("source", "destination"),
    [
        ("slack", "slack:T1:dm:D0HUMAN"),
        ("discord", "discord:111222333444555666:dm:999888777666555444"),
        ("whatsapp", "whatsapp:15550000000:dm:15551111111"),
    ],
)
def test_dm_correlation_accepts_a_reply_with_no_thread_anchor(source, destination):
    """Item 4: the bug an anchor-always filter ships. A DM has no anchor."""
    channel = ab.LoginChannel(
        destination=destination, source=source, topic=f"{source}:x",
    )
    event = _slack_event(conversation=destination, thread_ts=None,
                         channel="", source=source)
    assert ab._ready_reply(event, channel, "1700000000.000001") is not None


# 16c. A pre-threaded destination still correlates.

def test_pre_threaded_destination_correlates_on_the_destination_thread():
    """Item 16c: the ask-`ts` predicate this replaces would hang forever."""
    root = "1699999999.000000"
    destination = f"slack:T1:channel:C0LOGIN42:thread:{root}"
    channel = ab.LoginChannel(
        destination=destination, source="slack", topic="slack:T1:app:A1",
    )
    anchor = ab._reply_anchor(channel, ask_ts="1700000000.000001")
    assert anchor == root, "the destination's own thread id is the anchor"
    event = _slack_event(conversation=destination, thread_ts=root)
    assert ab._ready_reply(event, channel, anchor) is not None


# --- #958 ask-first: targets, guards, and the Discord refusals -------------

# 5. A Discord guild-channel destination is refused.

def test_discord_guild_channel_is_refused_as_a_login_destination():
    """Item 5. Under the one correlation rule a channel destination needs a
    thread anchor, and the Discord adapter emits none, so the run would hang to
    timeout. The error names the event-server change required."""
    from bobi.config import Config

    with pytest.raises(RuntimeError) as exc:
        ab._resolve_login_channel(Config(), "discord:guild1:channel:123")
    assert "referenced-message id" in str(exc.value)
    assert "event-server change" in str(exc.value)


def test_discord_dm_still_resolves():
    """Item 5, the other half: DMs take the DM branch and need no adapter change."""
    from bobi.config import Config

    ch = ab._resolve_login_channel(Config(), "discord:guild1:dm:123")
    assert ch.destination == "discord:guild1:dm:123"


# 6. The Discord inbound requirement now covers device_poll.

def test_discord_inbound_requirement_covers_device_poll(
    remote_discord_config, ask_state_home, monkeypatch,
):
    """Item 6: a codex-target Discord login against an event server with no
    local Gateway driver raises, where today only paste_back did."""
    monkeypatch.setattr(
        ab, "_ensure_discord_inbound_ready",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError(
            "Discord subscription login requires an event server with "
            "the local Discord Gateway driver."
        )),
    )
    with pytest.raises(RuntimeError, match="local Discord Gateway driver"):
        ab.run_bootstrap(
            remote_discord_config, target="codex", spawn_login=lambda h: None,
        )


# 7. The URL and device code are posted into the ask's thread, both paths.

def test_device_code_is_posted_into_the_ask_thread_legacy_slack(
    slack_config, ask_state_home, monkeypatch,
):
    """Item 7, legacy path: post_slack_message carries the ask's thread_ts."""
    codex_creds = ask_state_home / "auth.json"
    ok, log, posts = _run_ask_first(
        slack_config, events=[_slack_event(thread_ts="1700000000.000001")],
        monkeypatch=monkeypatch, target="codex",
        creds_writer=lambda: codex_creds.write_text(
            json.dumps({"tokens": {"refresh_token": "r"}})),
    )
    assert ok is True
    ask = posts[0]
    assert ask[2] == "", "the ask itself opens the thread"
    code_post = next(p for p in posts if "ABCD-12345" in p[1])
    assert code_post[2] == "1700000000.000001"
    assert all(p[0] == "C0LOGIN42" for p in posts)


def test_device_code_is_posted_into_the_ask_thread_gateway(
    slack_gateway_config, ask_state_home, monkeypatch,
):
    """Item 7, gateway path: the destination ref is `<dest>:thread:<ask ts>`."""
    import bobi.events.gateway as gateway_mod
    import bobi.events.server as server_mod

    sent = []
    codex_creds = ask_state_home / "auth.json"
    monkeypatch.setattr(
        server_mod, "ensure_bubble",
        lambda es_url, project_path: {"bubble_id": "bub", "bubble_key": "key"})
    monkeypatch.setattr(server_mod, "register_slack_workspaces", lambda *a, **k: ["T1"])
    monkeypatch.setattr(
        gateway_mod, "channels_send",
        lambda project, conv, text, mode="post": (
            sent.append((conv, text)) or {"ok": True, "ts": "1700000000.000001"}))

    ok, log, posts = _run_ask_first(
        slack_gateway_config,
        events=[_slack_event(conversation="slack:T1:channel:C0LOGIN42:thread:1700000000.000001",
                             thread_ts="1700000000.000001", channel="")],
        monkeypatch=monkeypatch, target="codex",
        creds_writer=lambda: codex_creds.write_text(
            json.dumps({"tokens": {"refresh_token": "r"}})),
    )
    assert ok is True
    assert sent[0][0] == "slack:T1:channel:C0LOGIN42", "the ask opens the thread"
    code_post = next(c for c, t in sent if "ABCD-12345" in t)
    assert code_post == "slack:T1:channel:C0LOGIN42:thread:1700000000.000001"


# 8. The outcome post lands in the thread too.

@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_outcome_post_lands_in_the_ask_thread(
    slack_config, ask_state_home, monkeypatch, outcome,
):
    """Item 8, success and failure. Timeout is covered separately below."""
    codex_creds = ask_state_home / "auth.json"
    writer = (
        (lambda: codex_creds.write_text(json.dumps({"tokens": {"refresh_token": "r"}})))
        if outcome == "success" else None
    )
    ok, log, posts = _run_ask_first(
        slack_config, events=[_slack_event(thread_ts="1700000000.000001")],
        monkeypatch=monkeypatch, target="codex", creds_writer=writer,
    )
    assert ok is (outcome == "success")
    final = posts[-1]
    assert final[2] == "1700000000.000001", "the outcome is threaded"
    assert ("complete" in final[1]) is (outcome == "success")


def test_timeout_outcome_post_lands_in_the_ask_thread(
    slack_config, ask_state_home, monkeypatch,
):
    """Item 8, timeout: the thread must not end on "waiting for you"."""
    ok, log, posts = pytest.raises(TimeoutError), None, None
    captured = []

    def fake_post(token, channel, text, thread_ts=""):
        captured.append((channel, text, thread_ts))
        return {"ok": True, "ts": "1700000000.000001"}

    with pytest.raises(TimeoutError, match="no human replied"):
        ab.run_bootstrap(
            slack_config, target="codex", ask_timeout=0.1,
            spawn_login=lambda h: (_ for _ in ()).throw(
                AssertionError("the login CLI must not be spawned")),
            post_message=fake_post,
            connect_listener=lambda p, c, t: _FakeListener([], channel=c),
        )
    assert captured[-1][2] == "1700000000.000001"
    assert "nobody replied" in captured[-1][1]


# 9, 10, 11. The <tool> target.

def test_target_retargets_command_and_credential_path(
    slack_config, ask_state_home, monkeypatch,
):
    """Item 9: a claude brain with target="codex" runs codex's login and
    resolves codex's credential path."""
    seen = {}
    codex_creds = ask_state_home / "auth.json"

    def fake_spawn(home):
        spec = ab._active_spec()
        seen["cmd"] = " ".join(spec.login_cmd)
        seen["creds"] = ab.credentials_path(home)
        codex_creds.write_text(json.dumps({"tokens": {"refresh_token": "r"}}))

        class FakeProc:
            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        return FakeProc(), -1

    monkeypatch.setattr(
        ab, "_scrape_login",
        lambda fd, timeout, spec: ("https://auth.openai.com/codex/device", "ABCD-12345"))
    ok = ab.run_bootstrap(
        slack_config, target="codex", spawn_login=fake_spawn,
        post_message=lambda t, c, x, thread_ts="": {"ok": True, "ts": "1700000000.000001"},
        connect_listener=lambda p, c, t: _FakeListener(
            [_ready_event_for(c)], channel=c),
    )
    assert ok is True
    assert seen["cmd"] == "codex login --device-auth"
    assert seen["creds"] == codex_creds


def test_spawn_login_is_still_called_with_exactly_one_argument(
    slack_config, ask_state_home, monkeypatch,
):
    """Item 10: the pin that the call site stayed `(home)`-only.

    Every fake that reaches it has a one-argument signature, so threading a
    spec through it would raise TypeError. The env override is what keeps the
    call site byte-identical.
    """
    codex_creds = ask_state_home / "auth.json"
    calls = []

    def one_arg_only(home):
        calls.append(home)
        codex_creds.write_text(json.dumps({"tokens": {"refresh_token": "r"}}))

        class FakeProc:
            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        return FakeProc(), -1

    monkeypatch.setattr(
        ab, "_scrape_login",
        lambda fd, timeout, spec: ("https://auth.openai.com/codex/device", "ABCD-12345"))
    assert ab.run_bootstrap(
        slack_config, target="codex", spawn_login=one_arg_only,
        post_message=lambda t, c, x, thread_ts="": {"ok": True, "ts": "1700000000.000001"},
        connect_listener=lambda p, c, t: _FakeListener(
            [_ready_event_for(c)], channel=c),
    ) is True
    assert len(calls) == 1


@pytest.mark.parametrize(("target", "expect"), [
    # `stub` is a known *brain* kind, so a generic typo case would pass while
    # this one does not: the allow-list must be the login specs, not the
    # brain registry.
    ("stub", "reject"),
    ("gateway-openai", "codex"),
    ("gateway", "claude"),
    ("codex", "codex"),
    ("claude", "claude"),
    ("nonesuch", "reject"),
])
def test_target_validation_by_named_case(slack_config, ask_state_home,
                                         monkeypatch, target, expect):
    """Item 11. `_active_spec` falls back to Claude for anything unrecognized,
    so an unvalidated target would silently run the *Claude* flow."""
    seen = {}

    def fake_spawn(home):
        seen["kind"] = ab._active_spec().kind
        raise RuntimeError("stop here; the spec is what is under test")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if expect == "reject":
        with pytest.raises(RuntimeError, match="is not one of: claude, codex"):
            ab.run_bootstrap(slack_config, target=target, spawn_login=fake_spawn)
        return
    with pytest.raises(RuntimeError, match="stop here"):
        ab.run_bootstrap(
            slack_config, target=target, spawn_login=fake_spawn,
            post_message=lambda t, c, x, thread_ts="": {"ok": True, "ts": "1700000000.000001"},
            connect_listener=lambda p, c, t: _FakeListener(
                [_ready_event_for(c)], channel=c),
        )
    assert seen["kind"] == expect


# --- #958 ask-first: guard scoping, re-attach, budgets, rebind -------------

def _stop_at_spawn(monkeypatch, slack_config, **kwargs):
    """Run far enough to prove which spec was selected, then stop."""
    seen = {}

    def fake_spawn(home):
        seen["kind"] = ab._active_spec().kind
        raise RuntimeError("reached the login spawn")

    with pytest.raises(RuntimeError, match="reached the login spawn"):
        ab.run_bootstrap(
            slack_config, spawn_login=fake_spawn,
            post_message=lambda t, c, x, thread_ts="": {"ok": True, "ts": "1700000000.000001"},
            connect_listener=lambda p, c, t: _FakeListener(
                [_ready_event_for(c)], channel=c),
            **kwargs,
        )
    return seen["kind"]


# 12. Guard scoping by resolved provider.

@pytest.mark.parametrize(("env", "target", "outcome"), [
    # A Claude gateway with a gateway token: refused for Claude's credential
    # path, by argument presence and by explicit target alike.
    (("ANTHROPIC_AUTH_TOKEN", "gw-token"), None, "refused"),
    (("ANTHROPIC_AUTH_TOKEN", "gw-token"), "claude", "refused"),
    # ...and permitted for codex, which is D6's stated limitation.
    (("ANTHROPIC_AUTH_TOKEN", "gw-token"), "codex", "codex"),
    # The shadow-env guard reads `spec.shadow_env`, so it must be written
    # against the right variable for each spec.
    (("ANTHROPIC_API_KEY", "sk-ant-x"), "claude", "refused"),
    # OPENAI_API_KEY is not Claude's shadow var and must not be read as one.
    (("OPENAI_API_KEY", "sk-openai-x"), "claude", "claude"),
    (("OPENAI_API_KEY", "sk-openai-x"), "codex", "codex"),
])
def test_guards_are_scoped_by_resolved_provider(
    slack_config, ask_state_home, monkeypatch, env, target, outcome,
):
    """Item 12. Both refusals are arguments about Claude's *credential path*,
    not about whether an argument was supplied."""
    from bobi import paths

    name, value = env
    if name == "ANTHROPIC_AUTH_TOKEN":
        agent_yaml = paths.agent_yaml_path(slack_config)
        agent_yaml.write_text(
            agent_yaml.read_text()
            + "brain:\n  kind: claude\n  base_url: https://gateway.example\n")
    monkeypatch.setenv(name, value)
    if outcome == "refused":
        with pytest.raises(RuntimeError) as exc:
            ab.run_bootstrap(slack_config, target=target,
                             spawn_login=lambda h: None)
        assert name in str(exc.value) or "gateway credentials" in str(exc.value)
        return
    assert _stop_at_spawn(monkeypatch, slack_config, target=target) == outcome


def test_codex_brain_refusal_with_openai_key_is_still_pinned(
    slack_config, monkeypatch,
):
    """Item 12's last row: driving BRAIN_ENV directly leaves `resolved_kind`
    None, so the codex-brain refusal is unchanged by the target scoping."""
    from bobi.brain import BRAIN_ENV

    monkeypatch.setenv(BRAIN_ENV, "codex")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-x")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        ab.run_bootstrap(slack_config, spawn_login=lambda h: None)


# 13. D6's limitation is asserted as documented behaviour, not as a guard.

def test_gateway_brained_team_may_mint_a_direct_codex_credential(
    slack_config, ask_state_home, monkeypatch,
):
    """Item 13. On a gateway-brained config `login-bootstrap codex` is
    PERMITTED and resolves the codex spec.

    This mints a provider credential *outside* the gateway that team
    authenticates through. That is D6: stated as a limitation, not guarded.
    The right control the day a gateway-brained team exists is a per-team
    policy knob, not the gateway guard, which is about the brain's credential
    path and says so in its own message. Deleting this test deletes the only
    record that the behaviour is deliberate.
    """
    from bobi import paths

    agent_yaml = paths.agent_yaml_path(slack_config)
    agent_yaml.write_text(
        agent_yaml.read_text()
        + "brain:\n  kind: claude\n  base_url: https://gateway.example\n")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert _stop_at_spawn(monkeypatch, slack_config, target="codex") == "codex"


# 14. cli.py's pre-check is target-aware.

def test_cli_precheck_is_target_aware(cli_login_install, tmp_path, monkeypatch):
    """Item 14: with Claude credentials present and codex requested, the
    command proceeds instead of printing "already present"."""
    from click.testing import CliRunner

    from bobi.cli import main
    from tests.conftest import TEST_AGENT_NAME

    config_dir = tmp_path / "claude-volume"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"refreshToken": "refresh"}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(
        ab, "run_bootstrap",
        lambda project_path, **kw: (_ for _ in ()).throw(
            RuntimeError(f"proceeded with target={kw.get('target')!r}")))

    bare = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "login-bootstrap"])
    assert "already present" in bare.output

    codex = CliRunner().invoke(
        main, ["agent", TEST_AGENT_NAME, "login-bootstrap", "codex"])
    assert "proceeded with target='codex'" in codex.output


# 15, 16, 16a, 16b. Re-attach, the marker, and the watermark.

def _ask_state(home, kind, destination, ts):
    (home / f".login-ask-{kind}").write_text(
        json.dumps({"destination": destination, "ts": ts}))


def test_reattach_posts_nothing_and_consumes_a_gap_reply(
    slack_config, ask_state_home, slack_bot_identity, monkeypatch,
):
    """Item 15: a stored ask id plus a thread whose newest message is a human
    one proceeds with no ask posted."""
    import bobi.events.gateway as gateway_mod

    _ask_state(ask_state_home, "codex", "C0LOGIN42", "1700000000.000001")
    codex_creds = ask_state_home / "auth.json"
    monkeypatch.setattr(
        gateway_mod, "channels_history",
        lambda project, conv, limit=100: [
            {"user": "U0BOT", "text": "🔐 *bobi subscription login* - `codex`\n"
                                      "Reply when you are ready, and then "
                                      "I will begin the login flow", "ts": "1.0"},
            {"user": "U0HUMAN", "text": "ready", "ts": "2.0"},
        ])
    ok, log, posts = _run_ask_first(
        slack_config, events=[], monkeypatch=monkeypatch, target="codex",
        creds_writer=lambda: codex_creds.write_text(
            json.dumps({"tokens": {"refresh_token": "r"}})),
    )
    assert ok is True
    assert not any("ready" in text for _c, text, _t in posts), "no ask re-posted"
    assert "spawn_login" in log


@pytest.mark.parametrize(("history", "label"), [
    ([{"user": "U0BOT", "text": "ask", "ts": "1.0"}], "newest is the bot's own"),
    ([{"user": "U0BOT", "text": "ask", "ts": "1.0"},
      {"user": "", "text": "ready", "ts": "2.0"}], "empty user is not human"),
])
def test_reattach_blocks_when_the_catch_up_read_finds_nothing(
    slack_config, ask_state_home, slack_bot_identity, monkeypatch, history, label,
):
    """Item 15's other legs. Without the bot-baseline rule a consumed reply
    would be re-read on every restart; without the empty-user rule the check
    would not fail closed."""
    import bobi.events.gateway as gateway_mod

    _ask_state(ask_state_home, "codex", "C0LOGIN42", "1700000000.000001")
    monkeypatch.setattr(gateway_mod, "channels_history",
                        lambda project, conv, limit=100: history)
    with pytest.raises(TimeoutError, match="no human replied"):
        ab.run_bootstrap(
            slack_config, target="codex", ask_timeout=0.1,
            spawn_login=lambda h: (_ for _ in ()).throw(
                AssertionError("must not spawn")),
            post_message=lambda t, c, x, thread_ts="": {"ok": True, "ts": "x"},
            connect_listener=lambda p, c, t: _FakeListener([], channel=c),
        ), label


def test_catch_up_read_is_skipped_entirely_on_a_non_slack_destination(
    whatsapp_config, ask_state_home, monkeypatch,
):
    """Item 15's last leg. Asserts the SKIP, not a faked history response: a
    faked response would pass against a transport whose endpoint rejects the
    call outright (WhatsApp's adapter has no fetchConversation)."""
    import bobi.events.gateway as gateway_mod

    destination = "whatsapp:111222333444555666:dm:15551234567"
    _ask_state(ask_state_home, "codex", destination, "1700000000.000001")
    monkeypatch.setattr(
        gateway_mod, "channels_history",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("history must not be called on WhatsApp")))
    with pytest.raises(TimeoutError, match="no human replied"):
        ab.run_bootstrap(
            whatsapp_config, target="codex", ask_timeout=0.1,
            spawn_login=lambda h: (_ for _ in ()).throw(
                AssertionError("must not spawn")),
            post_message=lambda t, c, x, thread_ts="": {"ok": True, "ts": "x"},
            connect_listener=lambda p, c, t: _FakeListener([], channel=c),
        )


def test_marker_with_no_id_recovers_it_from_channel_history(
    slack_config, ask_state_home, slack_bot_identity, monkeypatch,
):
    """Item 16: a marker written before the post, plus a channel history
    carrying this bot's ask, recovers that id and posts nothing."""
    import bobi.events.gateway as gateway_mod

    _ask_state(ask_state_home, "codex", "C0LOGIN42", "")
    codex_creds = ask_state_home / "auth.json"
    reads = []

    def fake_history(project, conv, limit=100):
        reads.append(conv)
        if ":thread:" in conv:
            return [{"user": "U0BOT", "text": "ask", "ts": "1.0"},
                    {"user": "U0HUMAN", "text": "ready", "ts": "2.0"}]
        return [{"user": "U0BOT",
                 "text": "🔐 *bobi subscription login* - `codex` on eng-team\n"
                         "Reply to this message in a thread when you are "
                         "ready, and then I will begin the login flow",
                 "ts": "1700000000.000042"}]

    monkeypatch.setattr(gateway_mod, "channels_history", fake_history)
    ok, log, posts = _run_ask_first(
        slack_config, events=[], monkeypatch=monkeypatch, target="codex",
        creds_writer=lambda: codex_creds.write_text(
            json.dumps({"tokens": {"refresh_token": "r"}})),
    )
    assert ok is True
    assert not any("you are ready" in text for _c, text, _t in posts)
    # The unanchored read came first, then the recovered thread.
    assert ":thread:" not in reads[0]
    assert reads[1].endswith(":thread:1700000000.000042")


def test_marker_with_no_id_on_a_non_slack_destination_posts_nothing(
    whatsapp_config, ask_state_home, monkeypatch,
):
    """Item 16: recovery is Slack-only, so elsewhere it posts nothing and blocks."""
    destination = "whatsapp:111222333444555666:dm:15551234567"
    _ask_state(ask_state_home, "codex", destination, "")
    posts = []
    with pytest.raises(TimeoutError, match="no human replied"):
        ab.run_bootstrap(
            whatsapp_config, target="codex", ask_timeout=0.1,
            spawn_login=lambda h: (_ for _ in ()).throw(
                AssertionError("must not spawn")),
            post_message=lambda t, c, x, thread_ts="": posts.append(x),
            connect_listener=lambda p, c, t: _FakeListener([], channel=c),
        )
    assert not any("you are ready" in text for text in posts)


def test_a_failed_post_leaves_the_ask_state_byte_identical(
    slack_config, ask_state_home, slack_bot_identity, monkeypatch,
):
    """Item 16, the D1 pin. An implementation that cleared the state on a
    failed post would post a second ask on the next boot."""
    import bobi.events.gateway as gateway_mod

    state = ask_state_home / ".login-ask-codex"
    _ask_state(ask_state_home, "codex", "C0LOGIN42", "1700000000.000001")
    before = state.read_bytes()
    monkeypatch.setattr(
        gateway_mod, "channels_history",
        lambda project, conv, limit=100: [
            {"user": "U0BOT", "text": "ask", "ts": "1.0"},
            {"user": "U0HUMAN", "text": "ready", "ts": "2.0"}])

    def exploding_post(token, channel, text, thread_ts=""):
        raise RuntimeError("channel outage")

    with pytest.raises(RuntimeError, match="channel outage"):
        ab.run_bootstrap(
            slack_config, target="codex",
            spawn_login=lambda h: (_FakeLoginProc(), -1),
            post_message=exploding_post,
            connect_listener=lambda p, c, t: _FakeListener([], channel=c),
            scrape_login=lambda fd, t, spec: ("https://auth.openai.com/codex/device", "A"),
        )
    assert state.read_bytes() == before


def test_ask_state_is_cleared_on_success_and_only_on_success(
    slack_config, ask_state_home, slack_bot_identity, empty_thread_history,
    monkeypatch,
):
    """Item 16: the id is cleared on success, and a failed login keeps it."""
    state = ask_state_home / ".login-ask-codex"
    codex_creds = ask_state_home / "auth.json"

    ok, _log, _posts = _run_ask_first(
        slack_config, events=[_slack_event(thread_ts="1700000000.000001")],
        monkeypatch=monkeypatch, target="codex")
    assert ok is False, "no credential was written"
    assert state.exists(), "a failed login keeps the ask"

    ok, _log, _posts = _run_ask_first(
        slack_config, events=[_slack_event(thread_ts="1700000000.000001")],
        monkeypatch=monkeypatch, target="codex",
        creds_writer=lambda: codex_creds.write_text(
            json.dumps({"tokens": {"refresh_token": "r"}})))
    assert ok is True
    assert not state.exists()


def test_a_destination_change_invalidates_the_stored_ask(
    slack_config, ask_state_home, monkeypatch,
):
    """Item 16a: a stored destination that no longer matches is treated as
    absent, and a fresh ask is posted."""
    _ask_state(ask_state_home, "codex", "C0SOMEWHERE-ELSE", "1700000000.000001")
    codex_creds = ask_state_home / "auth.json"
    ok, log, posts = _run_ask_first(
        slack_config, events=[_slack_event(thread_ts="1700000000.000001")],
        monkeypatch=monkeypatch, target="codex",
        creds_writer=lambda: codex_creds.write_text(
            json.dumps({"tokens": {"refresh_token": "r"}})))
    assert ok is True
    assert any("you are ready" in text for _c, text, _t in posts)
    assert json.loads((ask_state_home / ".login-ask-codex").read_text()
                      ) if (ask_state_home / ".login-ask-codex").exists() else True


def test_the_consumed_reply_is_not_re_read_as_the_pasted_code(
    slack_config, ask_state_home, slack_bot_identity, monkeypatch,
):
    """Item 16b. The same human reply is visible in both the catch-up history
    and the live queue. Phase 1 is satisfied once, and phase 3a must reject it
    by message id rather than writing "are" - the last word of "ready when you
    are" - into the pty as the OAuth code.
    """
    import bobi.events.gateway as gateway_mod

    _ask_state(ask_state_home, "claude", "C0LOGIN42", "1700000000.000001")
    monkeypatch.setattr(
        gateway_mod, "channels_history",
        lambda project, conv, limit=100: [
            {"user": "U0BOT", "text": "ask", "ts": "1.0"},
            {"user": "U0HUMAN", "text": "ready when you are",
             "ts": "1700000000.000002"}])
    # The live copy of the very same reply stays in the queue, because phase 1
    # was satisfied by the history read without draining it.
    live = _slack_event("ready when you are", thread_ts="1700000000.000001",
                        ts="1700000000.000002")
    listener = _FakeListener([live], channel=None)
    written = []
    monkeypatch.setattr(ab, "_read_until_url",
                        lambda fd, timeout: "https://x/oauth/authorize?c=1")
    monkeypatch.setattr(ab, "_write_line", lambda fd, text: written.append(text))

    def connect(project_path, channel, timeout):
        listener.channel = channel
        return listener

    with pytest.raises(TimeoutError, match="auth code not received"):
        ab.run_bootstrap(
            slack_config, timeout=0.1,
            spawn_login=lambda h: (_FakeLoginProc(), -1),
            post_message=lambda t, c, x, thread_ts="": {"ok": True, "ts": "1700000000.000001"},
            connect_listener=connect,
        )
    assert written == [], "the ready reply must never reach the pty"


class _FakeLoginProc:
    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


# 17. Phase budgets do not share.

def test_phase_budgets_do_not_share(
    slack_config, ask_state_home, slack_bot_identity, empty_thread_history,
    monkeypatch,
):
    """Item 17: phase 1 consuming its whole budget does not reduce phase 3b's,
    and a reply one tick late does not rescue the run."""
    waits = []

    class SlowProc:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            waits.append(timeout)
            return 0

    # Phase 1 burns its full budget before the reply arrives.
    late = _FakeListener([], channel=None)

    def connect(project_path, channel, timeout):
        late.channel = channel
        return late

    monkeypatch.setattr(
        ab, "_scrape_login",
        lambda fd, t, spec: ("https://auth.openai.com/codex/device", "ABCD-12345"))
    captured = []
    with pytest.raises(TimeoutError, match="no human replied"):
        ab.run_bootstrap(
            slack_config, target="codex", ask_timeout=0.1, device_timeout=900,
            spawn_login=lambda h: (SlowProc(), -1),
            post_message=lambda t, c, x, thread_ts="": (
                captured.append(x) or {"ok": True, "ts": "1700000000.000001"}),
            connect_listener=connect,
        )
    assert waits == [], "the login CLI is never spawned without a reply"
    assert any("nobody replied" in text for text in captured)

    # A successful run gets the full, separate device budget.
    codex_creds = ask_state_home / "auth.json"

    def write(timeout=None):
        waits.append(timeout)
        codex_creds.write_text(json.dumps({"tokens": {"refresh_token": "r"}}))
        return 0

    proc = SlowProc()
    proc.wait = write
    ab.run_bootstrap(
        slack_config, target="codex", ask_timeout=1800, device_timeout=900,
        spawn_login=lambda h: (proc, -1),
        post_message=lambda t, c, x, thread_ts="": {"ok": True, "ts": "1700000000.000001"},
        connect_listener=lambda p, c, t: _FakeListener([_ready_event_for(c)], channel=c),
    )
    assert waits == [900], "phase 3b gets its own budget, not what phase 1 left"


# 18. Reaping.

def test_a_login_that_ignores_terminate_is_killed_by_process_group(monkeypatch):
    """Item 18: signal the process group, wait with a bound, escalate, reap."""
    import signal

    signalled = []

    class StubbornProc:
        pid = 4242

        def __init__(self):
            self._waits = 0

        def poll(self):
            return None if self._waits < 2 else 0

        def wait(self, timeout=None):
            self._waits += 1
            if self._waits < 2:
                raise subprocess.TimeoutExpired("login", timeout)
            return 0

        def terminate(self):
            signalled.append("terminate")

        def kill(self):
            signalled.append("kill")

    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)))
    ab._reap_login(StubbornProc(), -1)
    assert signalled == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


# 19. Rebind.

def test_rebind_quarantines_collision_free_and_refuses_a_symlink(
    slack_config, ask_state_home, monkeypatch,
):
    """Item 19. A structurally valid credential short-circuits a bare run;
    --rebind posts the ask and quarantines on the reply."""
    codex_creds = ask_state_home / "auth.json"
    codex_creds.write_text(json.dumps({"tokens": {"refresh_token": "old"}}))

    # Bare: short-circuits, nothing posted.
    posts = []
    assert ab.run_bootstrap(
        slack_config, target="codex",
        spawn_login=lambda h: (_ for _ in ()).throw(AssertionError("no spawn")),
        post_message=lambda t, c, x, thread_ts="": posts.append(x),
        connect_listener=lambda p, c, t: (_ for _ in ()).throw(
            AssertionError("no listener")),
    ) is True
    assert posts == []

    # --rebind: the ask is posted and the old credential moves aside.
    ok, log, _posts = _run_ask_first(
        slack_config, events=[_slack_event(thread_ts="1700000000.000001")],
        monkeypatch=monkeypatch, target="codex", rebind=True,
        creds_writer=lambda: codex_creds.write_text(
            json.dumps({"tokens": {"refresh_token": "fresh"}})))
    assert ok is True
    quarantined = sorted(ask_state_home.glob("auth.json.quarantined-*"))
    assert len(quarantined) == 1
    assert json.loads(quarantined[0].read_text())["tokens"]["refresh_token"] == "old"

    # A second rebind in the same second does not overwrite the first.
    ok, log, _posts = _run_ask_first(
        slack_config, events=[_slack_event(thread_ts="1700000000.000001")],
        monkeypatch=monkeypatch, target="codex", rebind=True,
        creds_writer=lambda: codex_creds.write_text(
            json.dumps({"tokens": {"refresh_token": "fresher"}})))
    assert ok is True
    assert len(sorted(ask_state_home.glob("auth.json.quarantined-*"))) == 2


def test_rebind_refuses_a_symlinked_credential(ask_state_home, tmp_path):
    """Item 19's last leg. Renaming a symlink would move the link and leave
    the real credential live, so the quarantine would silently not happen."""
    real = tmp_path / "real-auth.json"
    real.write_text(json.dumps({"tokens": {"refresh_token": "real"}}))
    (ask_state_home / "auth.json").symlink_to(real)
    import os as _os

    _os.environ["CODEX_HOME"] = str(ask_state_home)
    from bobi.brain import BRAIN_ENV
    _os.environ[BRAIN_ENV] = "codex"
    try:
        with pytest.raises(RuntimeError, match="not a regular file"):
            ab._quarantine_credential(ask_state_home)
    finally:
        _os.environ[BRAIN_ENV] = "claude"
    assert real.read_text(), "the real credential is untouched"


# 20. Two concurrent runs do not de-index each other.

@pytest.mark.real_listener
def test_two_concurrent_listeners_get_unique_deployment_names(
    slack_config, monkeypatch,
):
    """Item 20, the regression pin for the fixed-name supersede.

    Registering an existing name in the same bubble removes the prior
    deployment and its subscriptions, so two fixed-name runs in flight
    de-index each other: a reply to the FIRST ask would reach only the second
    listener, whose predicate rejects it.
    """
    import bobi.events.client as client_mod
    import bobi.events.server as server_mod
    import bobi.slack as slack_mod

    names = []
    monkeypatch.setattr(
        server_mod, "ensure_bubble",
        lambda es_url, project_path: {"bubble_id": "bub", "bubble_key": "key"})
    monkeypatch.setattr(server_mod, "register_slack_workspaces", lambda *a, **k: ["T1"])
    monkeypatch.setattr(slack_mod, "resolve_auth_info", lambda token: ("T1", "B1", "U0BOT"))
    monkeypatch.setattr(slack_mod, "resolve_app_id", lambda token, bot_id: "A1")

    def fake_register(es_url, name, topics, bubble_id="", bubble_key=""):
        names.append(name)
        return f"dep-{name}", "api-key"

    monkeypatch.setattr(server_mod, "register", fake_register)

    class FakeClient:
        def __init__(self, es_url, deployment_id, api_key, queue):
            self.queue = queue

        def start(self):
            return None

        def wait_connected(self, timeout):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(client_mod, "EventServerClient", FakeClient)

    first = ab._connect_chat_listener(slack_config, "C0LOGIN42", 1)
    other_pid = os.getpid() + 1
    monkeypatch.setattr(os, "getpid", lambda: other_pid)
    second = ab._connect_chat_listener(slack_config, "C0LOGIN42", 1)
    first.stop()
    second.stop()
    assert len(set(names)) == 2, f"both runs registered as {names}"
    assert all(n.startswith("login-bootstrap-") for n in names)


def test_gateway_posts_with_no_inbound_ref_are_still_thread_anchored(
    slack_gateway_config, ask_state_home, monkeypatch,
):
    """Item 7/8 on the gateway path, where no inbound conversation ref exists.

    The URL and outcome posts normally reply to the inbound event's own ref,
    which is already thread-anchored - so they pass even if `_threaded_ref` is
    broken. The timeout outcome has no inbound ref: it must be anchored by
    appending `:thread:<ask ts>` to the destination, which is the only thing
    that threads a post when nobody replied.
    """
    import bobi.events.gateway as gateway_mod
    import bobi.events.server as server_mod

    sent = []
    monkeypatch.setattr(
        server_mod, "ensure_bubble",
        lambda es_url, project_path: {"bubble_id": "bub", "bubble_key": "key"})
    monkeypatch.setattr(server_mod, "register_slack_workspaces", lambda *a, **k: ["T1"])
    monkeypatch.setattr(
        gateway_mod, "channels_send",
        lambda project, conv, text, mode="post": (
            sent.append((conv, text)) or {"ok": True, "ts": "1700000000.000001"}))

    with pytest.raises(TimeoutError, match="no human replied"):
        ab.run_bootstrap(
            slack_gateway_config, target="codex", ask_timeout=0.1,
            spawn_login=lambda h: (_ for _ in ()).throw(
                AssertionError("must not spawn")),
            connect_listener=lambda p, c, t: _FakeListener([], channel=c),
        )
    assert sent[0][0] == "slack:T1:channel:C0LOGIN42", "the ask opens the thread"
    assert sent[-1][0] == "slack:T1:channel:C0LOGIN42:thread:1700000000.000001"
    assert "nobody replied" in sent[-1][1]


def test_the_catch_up_read_targets_the_thread_on_a_gateway_destination(
    slack_gateway_config, ask_state_home, slack_bot_identity, monkeypatch,
):
    """The gateway branch of the history ref. A legacy Slack destination
    assembles its ref from the team id, so a legacy-only assertion leaves the
    gateway branch - the one production refs take - unpinned."""
    import bobi.events.gateway as gateway_mod

    destination = "slack:T1:channel:C0LOGIN42"
    (ask_state_home / ".login-ask-codex").write_text(
        json.dumps({"destination": destination, "ts": "1700000000.000001"}))
    reads = []
    monkeypatch.setattr(
        gateway_mod, "channels_history",
        lambda project, conv, limit=100: reads.append(conv) or [])

    with pytest.raises(TimeoutError, match="no human replied"):
        ab.run_bootstrap(
            slack_gateway_config, target="codex", ask_timeout=0.1,
            spawn_login=lambda h: (_ for _ in ()).throw(
                AssertionError("must not spawn")),
            post_message=lambda t, c, x, thread_ts="": {"ok": True, "ts": "x"},
            connect_listener=lambda p, c, t: _FakeListener([], channel=c),
        )
    assert reads == ["slack:T1:channel:C0LOGIN42:thread:1700000000.000001"]


# --- #958 review findings: the pasted code is correlated too ----------------

@pytest.mark.parametrize(("label", "event", "accepted"), [
    ("human reply in the ask's thread",
     _slack_event("ABC-123", thread_ts="1700000000.000001"), True),
    # The three the uncorrelated predicate accepted, each of which would have
    # been written straight into the login CLI's stdin.
    ("a BOT message in the ask's thread",
     _slack_event("ABC-123", thread_ts="1700000000.000001",
                  bot_id="B0THIRDPARTY"), False),
    ("a human reply in ANOTHER thread",
     _slack_event("ABC-123", thread_ts="1799999999.000001"), False),
    ("a top-level message in the login channel",
     _slack_event("ABC-123", thread_ts=None), False),
])
def test_the_pasted_code_is_correlated_like_the_ready_reply(label, event, accepted):
    """The code this returns is written into the OAuth pty, so it carries the
    same human-only and thread-anchor rule as the ready reply.

    Without it, any bot or unrelated message in a shared login channel supplies
    the authorization code.
    """
    channel = ab.LoginChannel(
        destination="C0LOGIN42", source="slack", topic="slack:T1:app:A1",
        legacy_slack_channel="C0LOGIN42",
    )
    got = ab._extract_code(event, channel, (), "1700000000.000001")
    assert (got == "ABC-123") is accepted, label


def test_the_pasted_code_wait_uses_the_asks_anchor(
    slack_config, ask_state_home, monkeypatch,
):
    """End to end: the anchor established by the ask reaches the code phase, so
    a code posted outside the ask's thread is ignored and the run times out."""
    listener = _FakeListener([], channel=None)

    def connect(project_path, channel, timeout):
        listener.channel = channel
        return listener

    written = []
    monkeypatch.setattr(ab, "_read_until_url",
                        lambda fd, timeout: "https://x/oauth/authorize?c=1")
    monkeypatch.setattr(ab, "_write_line", lambda fd, text: written.append(text))
    # Ready in the ask's thread, then a code from somewhere else entirely.
    listener.queue.put(_slack_event("ready", thread_ts="1700000000.000001",
                                    ts="1700000000.000002"))
    listener.queue.put(_slack_event("WRONG-CODE", thread_ts="1799999999.000001",
                                    ts="1700000000.000003"))

    with pytest.raises(TimeoutError, match="auth code not received"):
        ab.run_bootstrap(
            slack_config, timeout=0.3,
            spawn_login=lambda h: (_FakeLoginProc(), -1),
            post_message=lambda t, c, x, thread_ts="": {
                "ok": True, "ts": "1700000000.000001"},
            connect_listener=connect,
        )
    assert written == [], "a code from another thread must never reach the pty"
    assert listener.anchor == "1700000000.000001"


# --- #958 review findings: the initial-post failure must not wedge ----------

def test_a_failed_initial_ask_post_does_not_wedge_the_next_boot(
    slack_config, ask_state_home, slack_bot_identity, monkeypatch,
):
    """A marker with no id, plus a history read that finds no ask, means the
    post genuinely failed: post a fresh ask rather than re-attaching to an
    anchor that can never be satisfied.

    Re-attaching with an empty anchor wedges a shared-channel login forever -
    every later boot blocks to timeout and no reply can ever match.
    """
    import bobi.events.gateway as gateway_mod

    state = ask_state_home / ".login-ask-codex"
    # Boot 1: the post fails after the marker is written.
    def exploding_post(token, channel, text, thread_ts=""):
        raise RuntimeError("channel outage")

    with pytest.raises(RuntimeError, match="channel outage"):
        ab.run_bootstrap(
            slack_config, target="codex", ask_timeout=0.1,
            spawn_login=lambda h: (_ for _ in ()).throw(
                AssertionError("must not spawn")),
            post_message=exploding_post,
            connect_listener=lambda p, c, t: _FakeListener([], channel=c),
        )
    assert json.loads(state.read_text()) == {
        "destination": "C0LOGIN42", "ts": ""}, "the marker records the attempt"

    # Boot 2: history holds no ask, so a fresh one is posted.
    monkeypatch.setattr(gateway_mod, "channels_history",
                        lambda project, conv, limit=100: [])
    codex_creds = ask_state_home / "auth.json"
    ok, log, posts = _run_ask_first(
        slack_config, events=[_slack_event(thread_ts="1700000000.000001")],
        monkeypatch=monkeypatch, target="codex",
        creds_writer=lambda: codex_creds.write_text(
            json.dumps({"tokens": {"refresh_token": "r"}})))
    assert ok is True
    assert any("you are ready" in text for _c, text, _t in posts), (
        "a fresh ask must be posted, not a re-attach to nothing")


def test_ask_id_recovery_does_not_adopt_another_kinds_ask(
    slack_config, ask_state_home, slack_bot_identity, monkeypatch,
):
    """One channel can carry two kinds' asks (a claude brain plus the codex
    tool), and the state is per-kind. A wording-only match would adopt the
    other kind's newer ask and post this login's device URL into its thread.
    """
    import bobi.events.gateway as gateway_mod

    _ask_state(ask_state_home, "codex", "C0LOGIN42", "")
    monkeypatch.setattr(
        gateway_mod, "channels_history",
        lambda project, conv, limit=100: [{
            "user": "U0BOT",
            "text": "🔐 *bobi subscription login* - `claude` on eng-team\n"
                    "Reply to this message in a thread when you are ready, "
                    "and then I will begin the login flow",
            "ts": "1700000000.000777",
        }])
    codex_creds = ask_state_home / "auth.json"
    ok, log, posts = _run_ask_first(
        slack_config, events=[_slack_event(thread_ts="1700000000.000001")],
        monkeypatch=monkeypatch, target="codex",
        creds_writer=lambda: codex_creds.write_text(
            json.dumps({"tokens": {"refresh_token": "r"}})))
    assert ok is True
    # A fresh codex ask, and nothing posted into claude's thread.
    assert any("you are ready" in text for _c, text, _t in posts)
    assert all(t != "1700000000.000777" for _c, _text, t in posts)


# --- #958 review findings: rebind, failure posts, Discord ids, CLI errors ---

def test_rebind_posts_a_fresh_ask_over_stale_state(
    slack_config, ask_state_home, slack_bot_identity, monkeypatch,
):
    """A crash between the credential landing and the state clear leaves stale
    state. `--rebind` must still post a fresh ask: re-attaching lands in a
    thread that ends in this bot's own success post, so the catch-up read finds
    no human after it and the one recovery path for a revoked credential wedges.
    """
    import bobi.events.gateway as gateway_mod

    codex_creds = ask_state_home / "auth.json"
    codex_creds.write_text(json.dumps({"tokens": {"refresh_token": "revoked"}}))
    _ask_state(ask_state_home, "codex", "C0LOGIN42", "1700000000.000001")
    monkeypatch.setattr(
        gateway_mod, "channels_history",
        lambda project, conv, limit=100: (_ for _ in ()).throw(
            AssertionError("a rebind must not re-attach, so it must not read "
                           "the old thread")))

    ok, log, posts = _run_ask_first(
        slack_config, events=[_slack_event(thread_ts="1700000000.000001")],
        monkeypatch=monkeypatch, target="codex", rebind=True,
        creds_writer=lambda: codex_creds.write_text(
            json.dumps({"tokens": {"refresh_token": "fresh"}})))
    assert ok is True
    assert any("you are ready" in text for _c, text, _t in posts)


def test_a_failure_after_the_ask_still_closes_the_thread(
    slack_config, ask_state_home, monkeypatch,
):
    """A scrape or code-wait failure must not leave the thread on "Waiting for
    you to authorize" with nothing after it."""
    posts = []

    def boom(fd, timeout, spec):
        raise TimeoutError("did not see the codex login URL/code within 120s")

    monkeypatch.setattr(ab, "_scrape_login", boom)
    with pytest.raises(TimeoutError, match="did not see the codex login"):
        ab.run_bootstrap(
            slack_config, target="codex",
            spawn_login=lambda h: (_FakeLoginProc(), -1),
            post_message=lambda t, c, x, thread_ts="": (
                posts.append((x, thread_ts))
                or {"ok": True, "ts": "1700000000.000001"}),
            connect_listener=lambda p, c, t: _FakeListener(
                [_ready_event_for(c)], channel=c),
        )
    assert "failed" in posts[-1][0]
    assert posts[-1][1] == "1700000000.000001", "the failure lands in the thread"
    assert "fly ssh console" in posts[-1][0]


def test_a_discord_message_id_is_recorded_as_consumed():
    """Discord's adapter names the id `message_id`, not `ts`. Reading only `ts`
    silently loses the consumed-reply watermark on that transport."""
    event = {
        "source": "discord", "type": "discord.dm", "text": "ready",
        "conversation": "discord:111:dm:222",
        "fields": {"user_id": "U", "message_id": "9988776655"},
    }
    assert ab._event_message_id(event) == "9988776655"
    channel = ab.LoginChannel(destination="discord:111:dm:222", source="discord",
                              topic="discord:111")
    assert ab._ready_reply(event, channel, "") is not None
    assert ab._ready_reply(event, channel, "", {"9988776655"}) is None


def test_cli_reports_an_unknown_tool_cleanly(cli_login_install, monkeypatch):
    """An unknown TOOL is a user error, so it must read as one rather than as a
    traceback. The pre-check resolves the target, so it can raise."""
    from click.testing import CliRunner

    from bobi.cli import main
    from tests.conftest import TEST_AGENT_NAME

    result = CliRunner().invoke(
        main, ["agent", TEST_AGENT_NAME, "login-bootstrap", "nonesuch"])
    assert result.exit_code == 1
    assert "Login bootstrap failed:" in result.output
    assert "is not one of: claude, codex" in result.output


@pytest.mark.parametrize(("destination", "is_channel"), [
    ("C0LOGIN42", True),     # public channel
    ("G0LEGACY99", True),    # legacy private group
    ("D0B51JP1N4C", False),  # 1:1 DM
])
def test_a_legacy_destination_is_classified_by_its_id_shape(destination, is_channel):
    """A legacy `$BOBI_LOGIN_CHANNEL` is a raw Slack id, and the path it came by
    says nothing about which kind it is.

    Keying on "is it the legacy path" demands a thread anchor from a legacy DM,
    and a plain Slack DM reply never carries one - so the login could never
    start on a documented, supported configuration. The legacy path is also the
    only one that runs in production today.
    """
    channel = ab.LoginChannel(
        destination=destination, source="slack", topic="slack:T1:app:A1",
        legacy_slack_channel=destination,
    )
    assert ab._is_channel_destination(channel) is is_channel
    # A plain DM reply, with no anchor at all, is ready only on the DM branch.
    reply = _slack_event("ready", thread_ts=None, channel=destination)
    assert (ab._ready_reply(reply, channel, "1700000000.000001") is not None) is (
        not is_channel)
