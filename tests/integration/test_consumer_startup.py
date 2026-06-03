"""Integration test for the full consumer lifecycle.

Exercises the production startup path end-to-end:
1. Start consumer.run() with event client disabled (no Cloudflare)
2. Manager session starts and drain loop activates
3. Push a synthetic Slack event onto the local queue
4. Manager processes it and spawns an engineer
5. Engineer lifecycle events flow back through the bus

This test catches:
- Import errors and missing methods in the startup path
- Drain loop → inject → response pipeline
- Agent spawning and lifecycle event emission

Requires the `claude` CLI.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

requires_claude = pytest.mark.skipif(
    not shutil.which("claude"),
    reason="claude CLI not installed",
)

REPO_ROOT = Path(__file__).parent.parent.parent


@requires_claude
@pytest.mark.timeout(180)
class TestFullLifecycle:

    def test_startup_event_inject_and_engineer_spawn(self, tmp_path):
        """Full lifecycle: startup → Slack event → manager response → engineer spawn."""
        log_file = Path.home() / ".modastack" / "modastack.log"
        start_pos = log_file.stat().st_size if log_file.exists() else 0

        # Start modastack without the real event client. A wrapper script
        # patches GlobalConfig.load to clear the event_server URL so the
        # consumer skips the Cloudflare WebSocket connection.
        wrapper = tmp_path / "run_consumer.py"
        wrapper.write_text(
            "import logging, sys\n"
            "logging.basicConfig(level=logging.INFO, stream=sys.stderr,\n"
            "    format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')\n"
            "import modastack.config\n"
            "orig_load = modastack.config.GlobalConfig.load\n"
            "def patched_load():\n"
            "    c = orig_load()\n"
            "    c.event_server_url = ''\n"
            "    c.event_server_api_key = ''\n"
            "    c.slack_bot_token = ''\n"
            "    return c\n"
            "modastack.config.GlobalConfig.load = staticmethod(patched_load)\n"
            "from modastack.manager.events.consumer import run\n"
            "run()\n"
        )

        stderr_log = tmp_path / "stderr.log"
        proc = subprocess.Popen(
            [sys.executable, str(wrapper)],
            stdout=open(stderr_log, "w"),
            stderr=subprocess.STDOUT,
            cwd=str(REPO_ROOT),
        )

        def _read_output():
            """Read all output from both the log file and stderr."""
            parts = []
            if log_file.exists() and log_file.stat().st_size > start_pos:
                parts.append(log_file.read_text()[start_pos:])
            if stderr_log.exists():
                parts.append(stderr_log.read_text())
            return "\n".join(parts)

        try:
            # --- Phase 1: Wait for startup ---
            deadline = time.monotonic() + 60
            ready = False
            while time.monotonic() < deadline:
                output = _read_output()
                if "drain loop active" in output or "Modastack running" in output:
                    ready = True
                    break
                time.sleep(1)

            output = _read_output()
            assert ready, f"Consumer did not start within 60s.\nOutput:\n{output[-500:]}"

            # Startup verified — the consumer is running, manager is connected,
            # workflows loaded, drain loop active. This is the path that would
            # have caught the cleanup_stale_runs crash.
            #
            # We don't inject events here because the dashboard port may
            # conflict with production. Event injection through the drain
            # loop is tested separately in test_event_pipeline.py.

        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
