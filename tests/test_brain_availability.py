"""Brain auth/credit incident alerting and recovery state (MOD-360)."""

import json

from bobi.brain import (
    ERROR_KIND_AUTHENTICATION,
    ERROR_KIND_CREDITS_EXHAUSTED,
    TurnResult,
)
from bobi.brain_availability import observe_brain_turn


def _failure(kind: str, text: str) -> TurnResult:
    return TurnResult(
        session_id="turn-1",
        is_error=True,
        error_kind=kind,
        error_message=text,
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
