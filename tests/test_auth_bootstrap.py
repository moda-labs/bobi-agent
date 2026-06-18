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


# --- orchestration (everything faked) ---------------------------------------

@pytest.fixture
def slack_config(tmp_path, monkeypatch):
    """A project with a Slack bot_token so run_bootstrap gets past config checks."""
    from modastack import paths

    monkeypatch.setattr(paths, "_ROOT", None, raising=False)
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
    monkeypatch.setenv(ab.LOGIN_CHANNEL_ENV, "C-LOGIN")
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
    assert all(p[1] == "C-LOGIN" for p in posts)


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
