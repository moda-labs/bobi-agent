"""Unit tests for modastack.manager_health — the manager health endpoint."""

import json
import urllib.request

import pytest

from modastack import manager_health


@pytest.fixture(autouse=True)
def _reset_server():
    """Ensure the health server is stopped and module state is clean."""
    manager_health.stop()
    manager_health._server = None
    manager_health._thread = None
    manager_health._port_file = None
    yield
    manager_health.stop()


class TestHealthServer:

    def test_start_returns_port_and_writes_port_file(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        port = manager_health.start(state_dir, "test-project")
        assert isinstance(port, int)
        assert port > 0

        port_file = state_dir / "manager-health.port"
        assert port_file.exists()
        assert int(port_file.read_text().strip()) == port

    def test_health_endpoint_returns_ok(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        port = manager_health.start(state_dir, "test-project",
                                    session_status_fn=lambda: [])

        url = f"http://127.0.0.1:{port}/health"
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read())

        assert data["status"] == "ok"
        assert data["project"] == "test-project"
        assert data["pid"] > 0
        assert isinstance(data["sessions"], list)

    def test_health_endpoint_includes_session_status(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        fake_sessions = [
            {"name": "moda-mgr-repo", "role": "manager", "status": "running"},
            {"name": "eng-42", "role": "engineer", "status": "idle"},
        ]
        port = manager_health.start(state_dir, "my-project",
                                    session_status_fn=lambda: fake_sessions)

        url = f"http://127.0.0.1:{port}/health"
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read())

        assert len(data["sessions"]) == 2
        assert data["sessions"][0]["name"] == "moda-mgr-repo"
        assert data["sessions"][1]["role"] == "engineer"

    def test_non_health_path_returns_404(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        port = manager_health.start(state_dir, "test-project",
                                    session_status_fn=lambda: [])

        url = f"http://127.0.0.1:{port}/nonexistent"
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(url, timeout=2)
        assert exc_info.value.code == 404

    def test_stop_cleans_up_port_file(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        manager_health.start(state_dir, "test-project",
                             session_status_fn=lambda: [])
        port_file = state_dir / "manager-health.port"
        assert port_file.exists()

        manager_health.stop()
        assert not port_file.exists()

    def test_start_is_idempotent(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        port1 = manager_health.start(state_dir, "test-project",
                                     session_status_fn=lambda: [])
        port2 = manager_health.start(state_dir, "test-project",
                                     session_status_fn=lambda: [])
        assert port1 == port2

    def test_health_probe_function(self, tmp_path):
        """Test the health() client function against a running server."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        port = manager_health.start(state_dir, "test-project",
                                    session_status_fn=lambda: [])

        data = manager_health.health(f"http://127.0.0.1:{port}")
        assert data is not None
        assert data["status"] == "ok"

    def test_health_probe_returns_none_on_bad_port(self):
        """health() returns None when nothing is listening."""
        data = manager_health.health("http://127.0.0.1:1", timeout=0.5)
        assert data is None
