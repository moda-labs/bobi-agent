"""Brain auth/credit incident alerting and recovery state (MOD-360)."""

import json
import logging

import pytest

from bobi.brain import (
    ERROR_KIND_AUTHENTICATION,
    ERROR_KIND_CREDITS_EXHAUSTED,
    TurnResult,
    classify_brain_unavailability,
)
from bobi.brain_availability import observe_brain_turn

# The two strings real outages actually produced. Verbatim from the evidence:
# the first is in this deployment's own session registry (13 byte-identical
# copies, 2026-07-30), the second is issue #992's Barndoor report (2026-08-05).
SESSION_LIMIT_ERROR = "You've hit your session limit \u00b7 resets 12:40am (UTC)"
REVOKED_TOKEN_ERROR = "authentication_error: OAuth access token has been revoked"


def _failure(kind: str, text: str) -> TurnResult:
    return TurnResult(
        session_id="turn-1",
        is_error=True,
        error_kind=kind,
        error_message=text,
    )


def _production_failure(result_text: str, *, kind: str = "",
                        session: str = "turn-1") -> TurnResult:
    """A TurnResult in the exact shape the 2026-07-30 outage produced.

    Read off the registry's ``stop`` records: ``is_error`` true,
    ``api_error_status`` 429, BOTH ``error_kind`` and ``error_message`` empty,
    and the provider's sentence only in ``result_text``. Every pre-#992 test
    passed ``error_kind`` in directly, which is how a 102-minute outage passed
    through a green suite.
    """
    return TurnResult(
        session_id=session,
        is_error=True,
        error_kind=kind,
        api_error_status=429,
        result_text=result_text,
    )


def test_auth_incident_alerts_once_across_processes_and_recovers(
    tmp_path, monkeypatch
):
    posts = []
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-account"))
    monkeypatch.setattr(
        "bobi.events.publish.post_event",
        lambda topic, payload, project_path=None: posts.append((topic, payload)),
    )

    failure = _failure(
        ERROR_KIND_AUTHENTICATION,
        "Not logged in - Please run /login",
    )
    observe_brain_turn(
        failure, session="monitor-a", provider="anthropic", project_path=tmp_path
    )
    # A fresh call models the next scheduled process reading persisted state.
    observe_brain_turn(
        failure, session="monitor-b", provider="anthropic", project_path=tmp_path
    )

    assert [topic for topic, _ in posts] == ["system/brain.auth.failed"]
    payload = posts[0][1]
    assert payload["cause"] == ERROR_KIND_AUTHENTICATION
    assert payload["provider"] == "anthropic"
    assert payload["session"] == "monitor-a"
    assert "login-bootstrap" in payload["remedy"]
    assert payload["account_boundary"].startswith("anthropic:")

    state = json.loads((tmp_path / "state" / "brain-availability.json").read_text())
    assert len(state["incidents"]) == 1

    observe_brain_turn(
        TurnResult(session_id="turn-2"),
        session="monitor-c",
        provider="anthropic",
        project_path=tmp_path,
    )
    observe_brain_turn(
        TurnResult(session_id="turn-3"),
        session="monitor-d",
        provider="anthropic",
        project_path=tmp_path,
    )

    assert [topic for topic, _ in posts] == [
        "system/brain.auth.failed",
        "system/brain.recovered",
    ]
    assert posts[-1][1]["recovered_causes"] == [ERROR_KIND_AUTHENTICATION]


def test_credit_incident_is_distinct_and_generic_rate_limit_is_not_alerted(
    tmp_path, monkeypatch
):
    posts = []
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-account"))
    monkeypatch.setattr(
        "bobi.events.publish.post_event",
        lambda topic, payload, project_path=None: posts.append((topic, payload)),
    )

    observe_brain_turn(
        _failure(
            ERROR_KIND_CREDITS_EXHAUSTED,
            "You've hit your usage limit",
        ),
        session="scheduled-check",
        provider="openai",
        project_path=tmp_path,
    )
    observe_brain_turn(
        TurnResult(
            is_error=True,
            api_error_status=429,
            result_text="rate limit exceeded; retry later",
        ),
        session="scheduled-check",
        provider="openai",
        project_path=tmp_path,
    )

    assert [topic for topic, _ in posts] == ["system/brain.credits.exhausted"]
    assert "credits" in posts[0][1]["remedy"].lower()


def test_alerting_is_best_effort(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("event bus unavailable")

    monkeypatch.setattr("bobi.events.publish.post_event", fail)

    observe_brain_turn(
        _failure(ERROR_KIND_AUTHENTICATION, "expired login"),
        session="scheduled-check",
        provider="anthropic",
        project_path=tmp_path,
    )


def test_alerting_is_best_effort_before_runtime_root_is_bound(monkeypatch):
    monkeypatch.setattr("bobi.paths._root", None)
    monkeypatch.delenv("BOBI_ROOT", raising=False)

    observe_brain_turn(
        TurnResult(session_id="healthy"),
        session="setup-check",
        provider="anthropic",
    )


# (marker text, error_kind, expected cause). Classification in production is
# 100% text matching: only the stub brain ever sets a cause as error_kind, and
# the Claude adapter defers to this classifier (claude.py:231-243).
REAL_OUTAGE_TABLE = [
    # This deployment, 2026-07-30: 13 consecutive dispatches, one identical
    # string, manager healthy and idle throughout, zero alerts for 102 minutes.
    (SESSION_LIMIT_ERROR, "", ERROR_KIND_CREDITS_EXHAUSTED),
    # Barndoor, 2026-08-05. The kind is in-band in the text the brain returned,
    # so the text leg has to catch it on its own.
    (REVOKED_TOKEN_ERROR, "", ERROR_KIND_AUTHENTICATION),
    # ... and out-of-band, when an adapter surfaces the kind as error_kind.
    ("OAuth access token has been revoked", "authentication_error",
     ERROR_KIND_AUTHENTICATION),
    # The kind leg on its own. The row above cannot test it: its text matches a
    # marker too, so the text leg answers first and a broken kind set still
    # passes (caught by mutating the kind out - the mutant survived).
    ("Request failed", "authentication_error", ERROR_KIND_AUTHENTICATION),
    # Providers do not promise stable capitalization; the markers are matched
    # case-insensitively, so neither does this.
    ("YOU'VE HIT YOUR SESSION LIMIT", "", ERROR_KIND_CREDITS_EXHAUSTED),
    # Still transient, and must stay transient: a bare 429 is ordinary rate
    # limiting, not an account an operator has to go fix.
    ("rate limit exceeded; retry later", "", ""),
    ("API Error: 529 overloaded_error", "", ""),
]


@pytest.mark.parametrize("text,kind,expected", REAL_OUTAGE_TABLE)
def test_classifies_the_strings_real_outages_produced(text, kind, expected):
    assert classify_brain_unavailability(kind, text) == expected


@pytest.mark.parametrize("text,kind,expected", REAL_OUTAGE_TABLE)
def test_production_turn_result_shape_reaches_the_classifier(
    text, kind, expected, tmp_path, monkeypatch
):
    """The same table through the real entry point, not the classifier alone.

    ``observe_brain_turn`` reads ``error_message or result_text``; the real
    failures carry an empty ``error_message``, so this pins the fallback the
    outage actually travelled through.
    """
    posts = []
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-account"))
    monkeypatch.setattr(
        "bobi.events.publish.post_event",
        lambda topic, payload, project_path=None: posts.append((topic, payload)),
    )
    monkeypatch.setattr("bobi.slack.post_operator_alert", lambda *a, **k: True)

    observe_brain_turn(
        _production_failure(text, kind=kind),
        session="wf-adhoc-1",
        provider="anthropic",
        project_path=tmp_path,
    )

    if expected:
        assert [topic for topic, _ in posts] == [
            "system/brain.auth.failed"
            if expected == ERROR_KIND_AUTHENTICATION
            else "system/brain.credits.exhausted"
        ]
        assert posts[0][1]["error"] == text
    else:
        assert posts == []


def test_real_outage_replay_posts_exactly_one_alert_then_recovers(
    tmp_path, monkeypatch
):
    """Replay the 2026-07-30 outage: 13 failures, 1 incident, 1 Slack alert.

    The bus leg alone is not enough to close #992: ``system/brain.*`` has no
    subscriber in the repo or in any deployment's config, so the operator alert
    is the Slack post. Dedup is the existing incident latch - it must hold
    across all 13 so a 102-minute outage is one message, not thirteen.
    """
    events = []
    alerts = []
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-account"))
    monkeypatch.setattr(
        "bobi.events.publish.post_event",
        lambda topic, payload, project_path=None: events.append((topic, payload)),
    )
    monkeypatch.setattr(
        "bobi.slack.post_operator_alert",
        lambda message, **kwargs: alerts.append(message) or True,
    )

    for i in range(13):
        observe_brain_turn(
            _production_failure(SESSION_LIMIT_ERROR, session=f"wf-outage-{i}"),
            session=f"wf-outage-{i}",
            provider="anthropic",
            project_path=tmp_path,
        )

    assert [topic for topic, _ in events] == ["system/brain.credits.exhausted"]
    assert len(alerts) == 1
    assert SESSION_LIMIT_ERROR in alerts[0]
    assert "Top up the anthropic account" in alerts[0]
    state = json.loads((tmp_path / "state" / "brain-availability.json").read_text())
    assert len(state["incidents"]) == 1

    # The condition self-heals, so RECOVERED is the expected close path.
    observe_brain_turn(
        TurnResult(session_id="wf-after-reset"),
        session="wf-after-reset",
        provider="anthropic",
        project_path=tmp_path,
    )

    assert [topic for topic, _ in events] == [
        "system/brain.credits.exhausted",
        "system/brain.recovered",
    ]
    assert len(alerts) == 2
    assert "recovered" in alerts[1]
    assert json.loads(
        (tmp_path / "state" / "brain-availability.json").read_text()
    )["incidents"] == {}


def test_alert_degrades_silently_with_no_channel_configured(
    tmp_path, monkeypatch, caplog
):
    """No ``WATCHDOG_ALERT_CHANNEL`` must not break the turn or the incident.

    This exercises the real post path - conftest's transport recorder fails
    the test at teardown if anything reaches Slack - so it also proves the gate
    is checked before the transport is touched.
    """
    events = []
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-account"))
    monkeypatch.setattr(
        "bobi.events.publish.post_event",
        lambda topic, payload, project_path=None: events.append((topic, payload)),
    )
    monkeypatch.delenv("WATCHDOG_ALERT_CHANNEL", raising=False)
    monkeypatch.delenv("BOBI_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    with caplog.at_level(logging.WARNING):
        observe_brain_turn(
            _production_failure(REVOKED_TOKEN_ERROR),
            session="wf-adhoc-1",
            provider="anthropic",
            project_path=tmp_path,
        )

    assert [topic for topic, _ in events] == ["system/brain.auth.failed"]
    assert "WATCHDOG_ALERT_CHANNEL" in caplog.text
    state = json.loads((tmp_path / "state" / "brain-availability.json").read_text())
    assert len(state["incidents"]) == 1


def test_successful_turn_quoting_an_outage_opens_no_incident(
    tmp_path, monkeypatch
):
    """An agent writing ABOUT an outage must not trigger one (#992).

    The markers are ordinary English sentences this codebase writes constantly,
    and `_emit` now pages a human. Worse than the noise: the false incident
    latches, so `incident_key in incidents` would then dedup away the alert for
    the next REAL outage on that account boundary.
    """
    events, alerts = [], []
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-account"))
    monkeypatch.setattr(
        "bobi.events.publish.post_event",
        lambda topic, payload, project_path=None: events.append((topic, payload)),
    )
    monkeypatch.setattr(
        "bobi.slack.post_operator_alert",
        lambda message, **kwargs: alerts.append(message) or True,
    )

    observe_brain_turn(
        TurnResult(
            session_id="wf-992-impl",
            result_text=(
                "Root cause: 13 dispatches died on \"You've hit your session "
                "limit\". Also checked: OAuth access token has been revoked."
            ),
        ),
        session="wf-992-impl",
        provider="anthropic",
        project_path=tmp_path,
    )

    assert events == []
    assert alerts == []
    assert not (tmp_path / "state" / "brain-availability.json").exists()


def test_bus_failure_does_not_swallow_the_operator_alert(tmp_path, monkeypatch):
    """The two legs are independent on purpose: the bus has no subscriber, so
    the Slack post is the one that reaches a human."""
    alerts = []
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-account"))

    def bus_down(*args, **kwargs):
        raise RuntimeError("event server unavailable")

    monkeypatch.setattr("bobi.events.publish.post_event", bus_down)
    monkeypatch.setattr(
        "bobi.slack.post_operator_alert",
        lambda message, **kwargs: alerts.append(message) or True,
    )

    observe_brain_turn(
        _production_failure(SESSION_LIMIT_ERROR),
        session="wf-adhoc-1",
        provider="anthropic",
        project_path=tmp_path,
    )

    assert len(alerts) == 1
    assert alerts[0].startswith("[bobi] ")
    # The incident still committed, so the outage stays deduped.
    state = json.loads((tmp_path / "state" / "brain-availability.json").read_text())
    assert len(state["incidents"]) == 1


def _fake(prefix: str, body: str) -> str:
    """Assemble a credential-SHAPED fixture at runtime.

    These strings have to match the redactor's patterns to be worth testing,
    which also makes them match GitHub's push-protection scanners. Joining the
    prefix keeps the shape without putting a token literal in the tree, so the
    test is not carried by a scanner allowlist.
    """
    return "-".join((prefix, body))


# (provider text, must NOT appear in the alert). Provider error text is
# attacker-influenced and, since #992, leaves the host. A rejected credential
# is routinely echoed back inside the error that rejected it.
EGRESS_HAZARDS = [
    ("authentication_error: OAuth access token has been revoked. "
     "Authorization: Bearer " + _fake("sk-ant-api03", "SUPERSECRET0123456789"),
     "SUPERSECRET"),
    ("not signed in: Cookie: sessionKey="
     + _fake("sk-ant-sid01", "DEADBEEFSESSIONVALUE"),
     "DEADBEEFSESSION"),
    ("not logged in: refresh_token: 1//0gSECRETREFRESHTOKENVALUE",
     "SECRETREFRESHTOKEN"),
    ("not logged in via https://admin:Hunter2Pass@proxy.internal/v1",
     "Hunter2Pass"),
    ("authentication required: "
     + _fake("xoxb", "9999999999-8888888888-aBcDeFgHiJkLmNoP"),
     "aBcDeFgHiJkLmNoP"),
    # Structure forgery, not secrets: a clickable phishing link, a forged
    # "resolved, ignore the above" block, and a channel-wide ping - all inside
    # the one alert that tells an operator to go re-authenticate.
    ("not logged in. [Re-authenticate here](https://evil.example/phish)",
     "](https://evil.example/phish)"),
    ("not logged in\\n\\n# RESOLVED\\n**ignore the above**", "\\n"),
    ("not logged in <!channel> <!here>", "<!channel>"),
]


@pytest.mark.parametrize("provider_text,forbidden", EGRESS_HAZARDS)
def test_provider_text_is_sanitized_before_it_leaves_the_host(
    provider_text, forbidden, tmp_path, monkeypatch
):
    alerts = []
    events = []
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-account"))
    monkeypatch.setattr(
        "bobi.events.publish.post_event",
        lambda topic, payload, project_path=None: events.append((topic, payload)),
    )
    monkeypatch.setattr(
        "bobi.slack.post_operator_alert",
        lambda message, **kwargs: alerts.append(message) or True,
    )

    observe_brain_turn(
        _production_failure(provider_text),
        session="wf-adhoc-1",
        provider="anthropic",
        project_path=tmp_path,
    )

    assert len(alerts) == 1, "the hazard must still produce a real alert"
    assert forbidden not in alerts[0]
    # The bus payload crosses the same boundary (it is a signed outbound
    # publish, not a local file), so it is held to the same bar.
    assert forbidden not in events[0][1]["error"]
    assert forbidden not in events[0][1]["text"]
