"""A launched child agent says why routing is off when its config was withheld.

A Claude provider launch blanks the routing config in the tool subprocess and
names it in a marker, so a ``bobi`` CLI child restores it from the runtime
``.env``. When that ``.env`` has no config, routing stays off; the child agent
must record ``routing_config_unrestored`` whether it runs in the CLI process
(``--wait``) or as a detached process, not fall back silently.
"""

import json
import time

import pytest

from bobi.metrics.router import EXPERIMENT_CONFIG_ENV
from bobi.metrics.routing import FALLBACK_LOG_NAME
from bobi.metrics.runtime import METRICS_MODE_ENV

LAUNCH_TIMEOUT_S = 60
CHILD_TIMEOUT_S = 60


def _fallback_reasons(state_dir, run_key):
    log = state_dir / FALLBACK_LOG_NAME
    if not log.exists():
        return []
    records = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return [record["fallback_reason"] for record in records if run_key in record.get("session", "")]


@pytest.mark.timeout(300)
@pytest.mark.parametrize("wait", [False, True], ids=["detached", "wait"])
def test_launched_child_records_a_routing_config_it_could_not_restore(
    stub_bobi_env, stub_cli_run, stub_clean_session, monkeypatch, wait,
):
    from bobi.config import _PROVIDER_CLEARED_ENV, parse_env_file
    from bobi.workflow.orchestrator import make_session_name

    assert EXPERIMENT_CONFIG_ENV not in parse_env_file(stub_bobi_env.project_path / ".env")
    run_key = f"routing-withheld-{'wait' if wait else 'detached'}"
    stub_clean_session(make_session_name("adhoc", stub_bobi_env.agent_name, run_key))
    # What a Claude provider launch hands the tool subprocess that runs `bobi`.
    monkeypatch.setenv(EXPERIMENT_CONFIG_ENV, "")
    monkeypatch.setenv(_PROVIDER_CLEARED_ENV, EXPERIMENT_CONFIG_ENV)
    monkeypatch.setenv(METRICS_MODE_ENV, "enabled")
    result = stub_cli_run(
        "subagents", "launch", "-w", "adhoc", "--role", "engineer", "--id", run_key,
        "--task", "Say 'hello' and exit.", *(["--wait"] if wait else []),
        timeout=LAUNCH_TIMEOUT_S,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"

    deadline = time.monotonic() + CHILD_TIMEOUT_S
    reasons = _fallback_reasons(stub_bobi_env.state_dir, run_key)
    while not reasons and time.monotonic() < deadline:
        time.sleep(0.5)
        reasons = _fallback_reasons(stub_bobi_env.state_dir, run_key)
    assert "routing_config_unrestored" in reasons, (reasons, result.stdout, result.stderr)


async def _codex_tool_env():
    """The environment the Codex brain hands its CLI, which its shell tools inherit."""
    import sys

    from bobi.brain.codex import _spawn_codex

    dump = "import json, os; print(json.dumps(dict(os.environ)))"
    return [event async for event in _spawn_codex([sys.executable, "-c", dump], cwd=".")][0]


@pytest.mark.timeout(300)
@pytest.mark.parametrize("parent", ["holds-config", "inherited-marker"])
def test_a_codex_parents_detached_child_records_a_routing_config_it_could_not_restore(
    stub_bobi_env, stub_cli_run, stub_clean_session, monkeypatch, parent,
):
    import asyncio
    import os

    from bobi.config import _PROVIDER_CLEARED_ENV, parse_env_file
    from bobi.workflow.orchestrator import make_session_name

    assert EXPERIMENT_CONFIG_ENV not in parse_env_file(stub_bobi_env.project_path / ".env")
    run_key = f"routing-withheld-codex-{parent}"
    stub_clean_session(make_session_name("adhoc", stub_bobi_env.agent_name, run_key))
    if parent == "holds-config":
        # A Codex parent whose routing config came from its process environment.
        monkeypatch.setenv(EXPERIMENT_CONFIG_ENV, '{"control_model": "parent-only"}')
        monkeypatch.delenv(_PROVIDER_CLEARED_ENV, raising=False)
    else:
        # A Codex parent that itself could not restore the config it was launched without.
        monkeypatch.delenv(EXPERIMENT_CONFIG_ENV, raising=False)
        monkeypatch.setenv(_PROVIDER_CLEARED_ENV, EXPERIMENT_CONFIG_ENV)
    monkeypatch.setenv(METRICS_MODE_ENV, "enabled")
    tool_env = asyncio.run(_codex_tool_env())
    assert not tool_env.get(EXPERIMENT_CONFIG_ENV)
    for name in set(os.environ) - set(tool_env):
        monkeypatch.delenv(name)
    for name, value in tool_env.items():
        monkeypatch.setenv(name, value)
    result = stub_cli_run(
        "subagents", "launch", "-w", "adhoc", "--role", "engineer", "--id", run_key,
        "--task", "Say 'hello' and exit.", timeout=LAUNCH_TIMEOUT_S,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"

    deadline = time.monotonic() + CHILD_TIMEOUT_S
    reasons = _fallback_reasons(stub_bobi_env.state_dir, run_key)
    while not reasons and time.monotonic() < deadline:
        time.sleep(0.5)
        reasons = _fallback_reasons(stub_bobi_env.state_dir, run_key)
    assert "routing_config_unrestored" in reasons, (reasons, result.stdout, result.stderr)
