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
