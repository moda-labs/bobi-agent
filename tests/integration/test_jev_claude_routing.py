"""Opt-in live proof that a static routed model reaches the Claude CLI."""

import os

import pytest

from .conftest import _drain, requires_claude


@requires_claude
@pytest.mark.skipif(os.environ.get("BOBI_JEV_LIVE_CLAUDE") != "1",
                    reason="requires explicit live Claude routing opt-in")
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_static_routed_model_runs_in_real_claude_cli(tmp_path, monkeypatch):
    import json
    from bobi.brain import get_brain
    from bobi.metrics.routing import resolve_route
    from bobi.metrics.runtime import MetricsRuntime
    from tests.metrics.helpers import configured, context
    from dataclasses import replace
    from bobi import paths
    from bobi.brain import GATEWAY_BASE_URL_ENV

    monkeypatch.setattr(paths, "_root", tmp_path)
    monkeypatch.setenv("BOBI_ROOT", str(tmp_path))
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BOBI_BRAIN", "claude")
    monkeypatch.delenv(GATEWAY_BASE_URL_ENV, raising=False)
    monkeypatch.delenv("BOBI_BRAIN_MODEL", raising=False)
    raw = configured()
    raw["control_model"] = "sonnet"
    raw["variants"][0]["model"] = "sonnet"
    raw["policy"].update(brain="claude", mode="enforce", candidate_models=["sonnet", "haiku"],
                         options={"model": "haiku"})
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    monkeypatch.setenv("BOBI_METRICS_ASSIGNMENT_SECRET", "test-secret")
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    client = None
    try:
        outcome = await resolve_route(replace(context(raw, "treatment"), brain="claude"), runtime=runtime)
        assert outcome.model == "haiku" and outcome.decision
        client = get_brain("claude").make_session(cwd=str(tmp_path),
            system_prompt="Reply concisely. Do not use tools.",
            options={"model": outcome.model, "max_turns": 1})
        await client.connect()
        await client.query("Reply with OK.")
        _, result = await _drain(client)
        assert result is not None and not result.is_error
        assert result.usage and all("haiku" in usage.model for usage in result.usage)
    finally:
        if client is not None:
            await client.disconnect()
        assert runtime.close(timeout=2)
