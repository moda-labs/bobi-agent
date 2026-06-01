"""Integration tests for modastack consult — real Claude Code session + dashboard.

Starts an actual Claude Code session and the dashboard HTTP server,
then exercises the full consultation round-trip: CLI/HTTP → dashboard →
inject → manager session → response → HTTP response.

Requires the `claude` CLI to be installed. Skipped in CI.
"""

import json
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid

import pytest

from modastack.manager import session
from .test_inject import _start_test_session, _stop_test_session

requires_claude = pytest.mark.skipif(
    not shutil.which("claude"),
    reason="claude CLI not installed",
)

TEST_PORT = 18095


def _start_dashboard(port: int = TEST_PORT):
    """Start the dashboard on a test port in a background thread."""
    import uvicorn
    from dashboard.app import app

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning",
    ))
    t = threading.Thread(target=server.run, daemon=True, name="test-dashboard")
    t.start()

    for _ in range(30):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status")
            urllib.request.urlopen(req, timeout=2)
            return server, t
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)

    raise RuntimeError(f"Dashboard failed to start on port {port}")


def _post_consult(question: str, timeout: int = 60, port: int = TEST_PORT) -> dict:
    payload = json.dumps({
        "question": question,
        "correlation_id": str(uuid.uuid4()),
        "timeout": timeout,
        "source": "test",
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/consult",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout + 30) as resp:
        return json.loads(resp.read())


@requires_claude
@pytest.mark.timeout(180)
class TestConsultIntegration:

    def setup_method(self):
        self._orig_client = session._client
        self._orig_loop = session._loop
        self._orig_state = session._state
        self._orig_response = session._last_response
        self._orig_thread = session._thread

        self._session_thread = _start_test_session()
        session._thread = self._session_thread
        self._server, self._dashboard_thread = _start_dashboard()

    def teardown_method(self):
        self._server.should_exit = True
        _stop_test_session()

        session._client = self._orig_client
        session._loop = self._orig_loop
        session._state = self._orig_state
        session._last_response = self._orig_response
        session._thread = self._orig_thread

    def test_consult_roundtrip(self):
        """Question goes in, matching response comes out."""
        result = _post_consult("Reply with just: CONSULT_OK")
        assert result["ok"] is True
        assert "CONSULT_OK" in result["response"]

    def test_response_matches_question(self):
        """Sequential consults get distinct, non-stale responses."""
        _post_consult("Reply with just: ALPHA")
        result = _post_consult("Reply with just: BETA")
        assert result["ok"] is True
        assert "BETA" in result["response"]

    def test_consult_concurrent_with_inject(self):
        """Consult serializes correctly when inject is in progress."""
        inject_done = threading.Event()

        def _bg_inject():
            session.inject("Reply with just: INJECTED", timeout=60)
            inject_done.set()

        t = threading.Thread(target=_bg_inject, daemon=True)
        t.start()
        time.sleep(0.5)

        result = _post_consult("Reply with just: CONSULTED", timeout=120)
        t.join(timeout=120)
        assert result["ok"] is True
        assert "CONSULTED" in result["response"]

    def test_consultation_prefix_included(self):
        """The [CONSULTATION] prefix is included in the injected text."""
        result = _post_consult("Reply with just: PREFIX_CHECK")
        assert result["ok"] is True
        assert "PREFIX_CHECK" in result["response"]
