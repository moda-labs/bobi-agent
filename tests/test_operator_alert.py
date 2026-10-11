"""The shared operator-alert path (#992).

``post_operator_alert`` is the one non-LLM route a system alert takes to a
human, used by the supervisor's crash-loop and budget-exhaustion alerts and by
brain-availability incidents. Its env gate is the only thing standing between
an unconfigured deployment and a stray Slack post, and before these tests the
whole function body could be replaced by ``return True`` with 5514 tests still
passing.
"""

import logging

import pytest

from bobi.slack import post_operator_alert


@pytest.fixture
def sent(monkeypatch):
    """Record delivered posts, overriding conftest's transport recorder."""
    calls = []
    monkeypatch.setattr(
        "bobi.slack.post_slack_message",
        lambda token, channel, text, thread_ts="", **kw: calls.append(
            {"token": token, "channel": channel, "text": text, **kw}
        ) or {"ok": True},
    )
    return calls


def test_delivers_to_the_configured_channel_with_the_callers_timeout(
    sent, monkeypatch
):
    monkeypatch.setenv("WATCHDOG_ALERT_CHANNEL", "C0OPS")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-configured")

    assert post_operator_alert("[bobi] brain unavailable", timeout=3.0) is True
    assert sent == [{
        "token": "xoxb-configured",
        "channel": "C0OPS",
        "text": "[bobi] brain unavailable",
        "timeout": 3.0,
    }]


def test_bobi_prefixed_token_wins_over_the_bare_one(sent, monkeypatch):
    monkeypatch.setenv("WATCHDOG_ALERT_CHANNEL", "C0OPS")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-ambient")
    monkeypatch.setenv("BOBI_SLACK_BOT_TOKEN", "xoxb-bobis-own")

    post_operator_alert("alert")
    assert sent[0]["token"] == "xoxb-bobis-own"


@pytest.mark.parametrize("channel,token", [
    (None, "xoxb-configured"),   # token only: no channel to post to
    ("C0OPS", None),             # channel only: no credential to post with
    (None, None),
])
def test_gate_needs_both_and_degrades_to_log_only(
    sent, monkeypatch, caplog, channel, token
):
    """Both halves are required. A one-sided gate would post with a ``None``
    channel - which is a real request to Slack, from a deployment that never
    configured alerting."""
    for var in ("WATCHDOG_ALERT_CHANNEL", "SLACK_BOT_TOKEN",
                "BOBI_SLACK_BOT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    if channel:
        monkeypatch.setenv("WATCHDOG_ALERT_CHANNEL", channel)
    if token:
        monkeypatch.setenv("SLACK_BOT_TOKEN", token)

    with caplog.at_level(logging.WARNING):
        assert post_operator_alert("alert", what="brain availability") is False

    assert sent == []
    assert "WATCHDOG_ALERT_CHANNEL" in caplog.text
    # The caller's label is the operator's grep handle for a dropped alert.
    assert "brain availability" in caplog.text


def test_transport_failures_raise_so_callers_decide_how_loud(monkeypatch):
    monkeypatch.setenv("WATCHDOG_ALERT_CHANNEL", "C0OPS")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-configured")

    def boom(*a, **k):
        raise RuntimeError("slack returned ratelimited")

    monkeypatch.setattr("bobi.slack.post_slack_message", boom)
    with pytest.raises(RuntimeError):
        post_operator_alert("alert")
