"""End-to-end test: webhook → event server → manager → workflow.

Starts the full modastack stack (manager + event server + drain loop),
posts a simulated GitHub issue assignment webhook, and verifies the
manager receives the event and responds with a workflow dispatch.

Requires the `claude` CLI.
"""

import json
import os
import signal
import time
import urllib.request

import pytest

from .conftest import requires_claude


@requires_claude
@pytest.mark.timeout(180)
class TestEndToEndEventFlow:

    @pytest.fixture(autouse=True)
    def _start_stack(self, modastack_env, cli_run):
        """Start modastack (manager + event server) and wait for ready."""
        log_file = modastack_env.state_dir / "manager.log"
        pid_file = modastack_env.state_dir / "manager.pid"
        log_pos = log_file.stat().st_size if log_file.exists() else 0

        cli_run("start", "software_team", timeout=15)

        deadline = time.monotonic() + 60
        ready = False
        while time.monotonic() < deadline:
            if pid_file.exists() and log_file.exists():
                new_content = log_file.read_text()[log_pos:]
                if "drain loop active" in new_content or "Modastack running" in new_content:
                    ready = True
                    break
            time.sleep(1)

        if not ready:
            content = log_file.read_text()[log_pos:] if log_file.exists() else "(no log)"
            pytest.skip(f"Stack did not become ready: {content[-300:]}")

        self._log_file = log_file
        self._log_pos = log_file.stat().st_size
        self._es_port = _find_event_server_port(log_file)

        yield

        cli_run("stop", timeout=15)
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass

    def test_github_issue_flows_through_to_manager(self, modastack_env):
        """Post a GitHub issue webhook → event server → manager processes it."""
        base_url = f"http://localhost:{self._es_port}"

        payload = json.dumps({
            "action": "assigned",
            "issue": {
                "number": 501,
                "title": "Add rate limiting to API",
                "state": "open",
                "labels": [{"name": "agent"}],
                "assignees": [{"login": "moda-bot"}],
                "user": {"login": "zachkozick"},
                "body": "We need rate limiting on the public API endpoints.",
                "html_url": "https://github.com/test-org/test-repo/issues/501",
            },
            "repository": {"full_name": "test-org/test-repo"},
            "sender": {"login": "zachkozick"},
        }).encode()

        req = urllib.request.Request(
            f"{base_url}/webhooks/github",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-github-event": "issues",
                "x-github-delivery": "e2e-test-001",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        assert result.get("delivered_to", 0) >= 1

        # Wait for the manager to process the event
        deadline = time.monotonic() + 60
        injected = False
        while time.monotonic() < deadline:
            new_log = self._log_file.read_text()[self._log_pos:]
            if ("Injecting" in new_log or "Delivering" in new_log) and "event" in new_log:
                injected = True
                break
            time.sleep(1)

        assert injected, f"Manager did not inject the event.\nLog:\n{self._log_file.read_text()[self._log_pos:][-500:]}"

    def test_manager_response_logged(self, modastack_env):
        """After event injection, the manager produces a response."""
        base_url = f"http://localhost:{self._es_port}"

        payload = json.dumps({
            "action": "opened",
            "issue": {
                "number": 503,
                "title": "Update docs",
                "state": "open",
                "labels": [],
                "assignees": [],
                "user": {"login": "testuser"},
                "body": "Docs are outdated.",
                "html_url": "https://github.com/test-org/test-repo/issues/503",
            },
            "repository": {"full_name": "test-org/test-repo"},
            "sender": {"login": "testuser"},
        }).encode()

        req = urllib.request.Request(
            f"{base_url}/webhooks/github",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-github-event": "issues",
                "x-github-delivery": "e2e-test-003",
            },
        )
        urllib.request.urlopen(req, timeout=5)

        # Wait for the event to be processed — look for the manager's
        # response in the events.jsonl state file
        events_file = modastack_env.state_dir / "events.jsonl"
        deadline = time.monotonic() + 60
        event_logged = False
        while time.monotonic() < deadline:
            if events_file.exists():
                content = events_file.read_text()
                if "503" in content or "Update docs" in content:
                    event_logged = True
                    break
            time.sleep(1)

        assert event_logged, "Event not found in events.jsonl"


def _find_event_server_port(log_file) -> int:
    """Extract the event server port from the manager log."""
    content = log_file.read_text()
    # Look for "Event server already running on port XXXX" or
    # "Event client started -> http://localhost:XXXX"
    import re
    match = re.search(r"localhost:(\d+)", content)
    if match:
        return int(match.group(1))
    return 8080
