"""Unit tests for the subscription-login bootstrap (containerized-23 / #343).

The pty spawn, Slack post, and event-bus wait are injected as fakes so the
orchestration is exercised without a real claude binary, Slack, or Worker. The
URL scraper and code extractor are tested directly. The live round-trip is an
integration concern (deployed env, alongside C10/C12).
"""
from __future__ import annotations

import os

import pytest

from modastack import auth_bootstrap as ab


# --- credentials / needs_bootstrap ------------------------------------------

def test_credentials_path_follows_home(tmp_path):
    assert ab.credentials_path(tmp_path) == tmp_path / ".claude" / ".credentials.json"


def test_credentials_exist(tmp_path):
    assert not ab.credentials_exist(tmp_path)
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text("{}")
    assert ab.credentials_exist(tmp_path)


def test_needs_bootstrap_only_in_subscription_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("MODASTACK_AUTH", "api_key")
    assert ab.needs_bootstrap(tmp_path) is False
    monkeypatch.setenv("MODASTACK_AUTH", "subscription")
    assert ab.needs_bootstrap(tmp_path) is True
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text("{}")
    assert ab.needs_bootstrap(tmp_path) is False


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


def test_extract_code_from_real_adapter_dm_shape():
    """Reproduces the prod bug: the Slack adapter (event-server/src/adapters/
    slack.ts) emits `text` at the TOP LEVEL and in `payload`, with `fields`
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
    from modastack import paths

    monkeypatch.setattr(paths, "_root", None, raising=False)
    project = tmp_path / "proj"
    (project / ".modastack").mkdir(parents=True)
    (project / ".modastack" / "agent.yaml").write_text(
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


def test_run_bootstrap_happy_path(slack_config, monkeypatch):
    posts = []
    written = []
    home = slack_config  # unused; creds keyed off $HOME

    home_dir = os.environ["HOME"]
    creds = os.path.join(home_dir, ".claude", ".credentials.json")

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

    def fake_post(token, channel, text):
        posts.append((token, channel, text))

    def fake_wait(project_path, channel, timeout):
        # Simulate the human pasting the code; claude then writes creds.
        os.makedirs(os.path.dirname(creds), exist_ok=True)
        with open(creds, "w") as f:
            f.write("{}")
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
    assert any("oauth/authorize" in p[2] for p in posts)
    assert any("complete" in p[2] for p in posts)
    assert all(p[1] == "C0LOGIN42" for p in posts)


def test_run_bootstrap_skips_when_creds_present(slack_config, monkeypatch):
    creds = os.path.join(os.environ["HOME"], ".claude", ".credentials.json")
    os.makedirs(os.path.dirname(creds), exist_ok=True)
    with open(creds, "w") as f:
        f.write("{}")

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


def test_run_bootstrap_requires_channel(slack_config, monkeypatch):
    monkeypatch.delenv(ab.LOGIN_CHANNEL_ENV, raising=False)
    with pytest.raises(RuntimeError, match="MODASTACK_LOGIN_CHANNEL"):
        ab.run_bootstrap(slack_config, spawn_login=lambda h: None)


# --- Codex brain: device-auth (poll) flow (#485) ----------------------------

def test_credentials_path_for_codex(tmp_path, monkeypatch):
    from modastack.brain import BRAIN_ENV

    monkeypatch.setenv(BRAIN_ENV, "codex")
    assert ab.credentials_path(tmp_path) == tmp_path / ".codex" / "auth.json"


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
    from modastack.brain import BRAIN_ENV

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
                f.write("{}")
            return 0

    def fake_spawn(home):
        return FakeProc(), -1

    def fake_scrape(fd, timeout, spec):
        return "https://auth.openai.com/codex/device", "5RAR-HF15T"

    def fake_post(token, channel, text):
        posts.append((token, channel, text))

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
    from modastack.brain import BRAIN_ENV

    monkeypatch.setenv(BRAIN_ENV, "codex")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-x")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        ab.run_bootstrap(slack_config, spawn_login=lambda h: None)
