"""Tests for the setup harness status read — which agent runs bobi and
whether it's authenticated. Pure function; everything external (the `claude`
CLI on PATH, the env key, the on-disk subscription creds) is monkeypatched."""

import pytest

from bobi.setup import harness


@pytest.fixture(autouse=True)
def _clear_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _patch(monkeypatch, *, cli=True, key=False, subscription=False):
    monkeypatch.setattr(harness.shutil, "which",
                        lambda name: "/usr/bin/claude" if cli else None)
    monkeypatch.setattr(harness, "_oauth_credentials_present",
                        lambda: subscription)
    if key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


def test_api_key_mode_authenticated(monkeypatch):
    _patch(monkeypatch, cli=True, key=True)
    hs = harness.harness_status("claude-opus-4-8")
    assert hs.agent == "Claude Code"
    assert hs.model == "claude-opus-4-8"
    assert hs.cli_present is True
    assert hs.auth_mode == "api_key"
    assert hs.authenticated is True


def test_subscription_mode_authenticated(monkeypatch):
    _patch(monkeypatch, cli=True, subscription=True)
    hs = harness.harness_status()
    assert hs.auth_mode == "subscription"
    assert hs.authenticated is True
    # No --model configured → the CLI's default, shown as such.
    assert hs.model == "default"


def test_api_key_outranks_subscription(monkeypatch):
    # ANTHROPIC_API_KEY silently outranks subscription creds (it bills the API),
    # so it's the reported mode whenever it's set — mirror that precedence.
    _patch(monkeypatch, cli=True, key=True, subscription=True)
    assert harness.harness_status().auth_mode == "api_key"


def test_not_logged_in(monkeypatch):
    _patch(monkeypatch, cli=True)
    hs = harness.harness_status()
    assert hs.cli_present is True
    assert hs.auth_mode is None
    assert hs.authenticated is False
    assert hs.login_command == "claude auth login"


def test_cli_missing_is_never_authenticated(monkeypatch):
    # Even with a key, a missing CLI means the harness can't run.
    _patch(monkeypatch, cli=False, key=True)
    hs = harness.harness_status()
    assert hs.cli_present is False
    assert hs.authenticated is False


def test_to_dict_shape(monkeypatch):
    _patch(monkeypatch, cli=True, key=True)
    d = harness.harness_status("m").to_dict()
    assert set(d) == {"agent", "model", "cli_present", "authenticated",
                      "auth_mode", "login_command"}


def test_macos_keychain_counts_as_authenticated(monkeypatch):
    # The Mac dev default: Claude Code stores OAuth in the login keychain, not
    # ~/.claude/.credentials.json. The keychain hit must read as logged in, or
    # working machines get a false "not logged in".
    from bobi import auth_bootstrap
    monkeypatch.setattr(auth_bootstrap, "credentials_exist", lambda home=None: False)
    monkeypatch.setattr(harness, "_macos_keychain_has_claude", lambda: True)
    monkeypatch.setattr(harness.shutil, "which", lambda n: "/usr/bin/claude")
    hs = harness.harness_status()
    assert hs.authenticated is True
    assert hs.auth_mode == "subscription"


def test_keychain_check_is_darwin_only(monkeypatch):
    # On non-macOS the keychain probe must short-circuit to False without
    # shelling out to `security` (which doesn't exist there).
    monkeypatch.setattr(harness.platform, "system", lambda: "Linux")
    called = []
    monkeypatch.setattr(harness.subprocess, "run",
                        lambda *a, **k: called.append(a) or None)
    assert harness._macos_keychain_has_claude() is False
    assert called == []
