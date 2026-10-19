"""Opt-in live proof that a policy-routed model reaches the Claude CLI."""

import os

import pytest

from .conftest import _drain, requires_claude


@requires_claude
@pytest.mark.skipif(os.environ.get("BOBI_JEV_LIVE_CLAUDE") != "1",
                    reason="requires explicit live Claude routing opt-in")
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_routed_model_runs_in_real_claude_cli(tmp_path, monkeypatch):
    import json
    from bobi.brain import get_brain
    from bobi.metrics.routing import resolve_route
    from bobi.metrics.runtime import MetricsRuntime
    from tests.metrics.helpers import configured, context, fixed_policy
    from dataclasses import replace
    from bobi import paths
    from bobi.brain import GATEWAY_BASE_URL_ENV

    monkeypatch.setattr(paths, "_root", tmp_path)
    monkeypatch.setenv("BOBI_ROOT", str(tmp_path))
    monkeypatch.setenv("BOBI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BOBI_BRAIN", "claude")
    monkeypatch.delenv(GATEWAY_BASE_URL_ENV, raising=False)
    monkeypatch.delenv("BOBI_BRAIN_MODEL", raising=False)
    raw = configured(control_model="sonnet", candidate_models=["sonnet", "haiku"], brain="claude", mode="enforce")
    monkeypatch.setenv("BOBI_METRICS_EXPERIMENT_JSON", json.dumps(raw))
    # The policy answer is fixed; the real Claude CLI is what this test proves.
    fixed_policy(monkeypatch, "haiku")
    runtime = MetricsRuntime(tmp_path, mode="enabled")
    client = None
    try:
        outcome = await resolve_route(replace(context(), brain="claude"), runtime=runtime)
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
