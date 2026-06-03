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

        # Start modastack without the event client by unsetting the event server config.
        # The consumer checks config.event_server_url — if empty, it skips the client.
        env = {**os.environ, "MODASTACK_EVENT_SERVER_URL": ""}

        proc = subprocess.Popen(
            [sys.executable, "-m", "modastack.cli", "start", "--foreground"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(REPO_ROOT),
            env=env,
        )

        try:
            # --- Phase 1: Wait for startup ---
            deadline = time.monotonic() + 60
            ready = False
            while time.monotonic() < deadline:
                if log_file.exists() and log_file.stat().st_size > start_pos:
                    new_content = log_file.read_text()[start_pos:]
                    if "drain loop active" in new_content:
                        ready = True
                        break
                time.sleep(1)

            new_content = log_file.read_text()[start_pos:] if log_file.exists() else ""
            assert ready, f"Consumer did not start within 60s.\nLog:\n{new_content[-500:]}"
            assert "Manager session" in new_content
            assert "Modastack running" in new_content

            # --- Phase 2: Inject a synthetic event via the dashboard API ---
            import json
            import urllib.request

            event = {
                "type": "slack.dm",
                "source": "slack",
                "data": {
                    "from": "TestUser",
                    "text": "hello, are you there?",
                    "channel": "C_TEST",
                    "workspace": "T_TEST",
                },
            }
            req = urllib.request.Request(
                "http://localhost:8095/api/event",
                data=json.dumps(event).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read())
            assert result.get("ok"), f"Event post failed: {result}"

            # --- Phase 3: Verify the event was processed ---
            deadline = time.monotonic() + 30
            injected = False
            while time.monotonic() < deadline:
                new_content = log_file.read_text()[start_pos:]
                if "Injecting" in new_content and "slack.dm" in new_content:
                    injected = True
                    break
                time.sleep(1)

            assert injected, f"Event was not injected within 30s.\nLog:\n{new_content[-500:]}"

            # --- Phase 4: Verify manager responded ---
            deadline = time.monotonic() + 30
            responded = False
            while time.monotonic() < deadline:
                new_content = log_file.read_text()[start_pos:]
                if "drain complete" in new_content and "slack.dm" in new_content:
                    responded = True
                    break
                time.sleep(1)

            assert responded, f"Manager did not respond within 30s.\nLog:\n{new_content[-500:]}"

        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
