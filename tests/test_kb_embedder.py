"""Unit tests for the embedding sidecar client — mocked subprocess and HTTP."""

import json
import os
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import httpx
import pytest

from bobi import http as pooled
from bobi.kb import embedder


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Redirect sidecar state files to a temp directory."""
    sd = tmp_path / ".bobi" / "state"
    sd.mkdir(parents=True)
    monkeypatch.setattr(embedder, "_state_dir", lambda root=None: sd)
    monkeypatch.setattr("bobi.kb.embedder._state_dir", lambda root=None: sd)
    return sd


@pytest.fixture(autouse=True)
def reset_verified_port(monkeypatch):
    """embed() caches the last good sidecar port in a module global."""
    monkeypatch.setattr(embedder, "_verified_port", None)


@pytest.fixture
def mock_project_root(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "bobi.kb.embedder._state_dir",
        lambda root=None: tmp_path / ".bobi" / "state",
    )
    (tmp_path / ".bobi" / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------------------
# _check_health
# ---------------------------------------------------------------------------

class TestCheckHealth:
    def test_returns_true_on_ok(self, state_dir):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ok"})
        )
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client):
            assert embedder._check_health(8000) is True

    def test_returns_false_on_error(self, state_dir):
        def raise_connect_error(request):
            raise httpx.ConnectError("conn refused")

        transport = httpx.MockTransport(raise_connect_error)
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client):
            assert embedder._check_health(8000) is False

    def test_returns_false_on_bad_json(self, state_dir):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"not json")
        )
        mock_client = httpx.Client(transport=transport)

        with patch.object(pooled, '_client', mock_client):
            assert embedder._check_health(8000) is False


# ---------------------------------------------------------------------------
# is_running
# ---------------------------------------------------------------------------

class TestIsRunning:
    def test_no_pid_file(self, state_dir):
        assert embedder.is_running() is False

    def test_stale_pid(self, state_dir):
        (state_dir / "embedding-sidecar.pid").write_text("999999")
        (state_dir / "embedding-sidecar.port").write_text("8000")
        with patch("bobi.sdk.pid_alive", return_value=False):
            assert embedder.is_running() is False

    def test_alive_and_healthy(self, state_dir):
        (state_dir / "embedding-sidecar.pid").write_text("12345")
        (state_dir / "embedding-sidecar.port").write_text("8000")
        with patch("bobi.sdk.pid_alive", return_value=True), \
             patch.object(embedder, "_check_health", return_value=True):
            assert embedder.is_running() is True

    def test_alive_but_unhealthy(self, state_dir):
        (state_dir / "embedding-sidecar.pid").write_text("12345")
        (state_dir / "embedding-sidecar.port").write_text("8000")
        with patch("bobi.sdk.pid_alive", return_value=True), \
             patch.object(embedder, "_check_health", return_value=False):
            assert embedder.is_running() is False


# ---------------------------------------------------------------------------
# ensure_running
# ---------------------------------------------------------------------------

class TestEnsureRunning:
    def test_already_running(self, state_dir):
        (state_dir / "embedding-sidecar.port").write_text("9000")
        with patch.object(embedder, "_check_health", return_value=True):
            port = embedder.ensure_running()
            assert port == 9000

    def test_cold_start(self, state_dir, monkeypatch):
        monkeypatch.setattr(
            "bobi.sdk.get_project_root",
            lambda: state_dir.parent.parent,
        )
        call_count = 0

        def mock_check_health(port):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                return True
            return False

        def mock_popen(*args, **kwargs):
            (state_dir / "embedding-sidecar.port").write_text("7777")
            return MagicMock()

        with patch.object(embedder, "_check_health", side_effect=mock_check_health), \
             patch("subprocess.Popen", side_effect=mock_popen), \
             patch("time.sleep"):
            port = embedder.ensure_running()
            assert port == 7777

    def test_timeout_raises(self, state_dir, monkeypatch):
        monkeypatch.setattr(
            "bobi.sdk.get_project_root",
            lambda: state_dir.parent.parent,
        )
        with patch.object(embedder, "_check_health", return_value=False), \
             patch("subprocess.Popen", return_value=MagicMock()), \
             patch("time.sleep"):
            with pytest.raises(RuntimeError, match="failed to start"):
                embedder.ensure_running()


# ---------------------------------------------------------------------------
# embed
# ---------------------------------------------------------------------------

class TestEmbed:
    def test_returns_embeddings(self, state_dir):
        fake_embeddings = [[0.1, 0.2], [0.3, 0.4]]

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={"embeddings": fake_embeddings})
        )
        mock_client = httpx.Client(transport=transport)

        with patch.object(embedder, "ensure_running", return_value=8000), \
             patch.object(pooled, '_client', mock_client):
            result = embedder.embed(["hello", "world"])
            assert result == fake_embeddings

    def test_restarts_sidecar_when_cached_port_is_dead(self, state_dir):
        """The sidecar died since the last embed. The pooled httpx client
        raises ConnectError (NOT an OSError), so a recovery clause that only
        catches OSError never fires: the dead port stays cached and every
        later embed — cold-memory sync, KB indexing — fails forever."""
        ports: list[int] = []

        def handler(request):
            port = request.url.port
            ports.append(port)
            if port == 8000:
                raise httpx.ConnectError("[Errno 111] Connection refused")
            return httpx.Response(200, json={"embeddings": [[0.5, 0.6]]})

        mock_client = httpx.Client(transport=httpx.MockTransport(handler))

        embedder._verified_port = 8000
        with patch.object(embedder, "ensure_running", return_value=8001) as ensure, \
             patch.object(pooled, '_client', mock_client):
            result = embedder.embed(["hello"])

        assert result == [[0.5, 0.6]]
        assert ports == [8000, 8001]
        ensure.assert_called_once_with()
        # The dead port must not stay cached, or the retry never happens again.
        assert embedder._verified_port == 8001


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

class TestStop:
    def test_stops_running_process(self, state_dir):
        (state_dir / "embedding-sidecar.pid").write_text("12345")
        (state_dir / "embedding-sidecar.port").write_text("8000")

        with patch("os.kill") as mock_kill:
            embedder.stop()
            mock_kill.assert_called_once_with(12345, signal.SIGTERM)

        assert not (state_dir / "embedding-sidecar.pid").exists()
        assert not (state_dir / "embedding-sidecar.port").exists()

    def test_stop_no_pid_file(self, state_dir):
        embedder.stop()

    def test_stop_stale_pid(self, state_dir):
        (state_dir / "embedding-sidecar.pid").write_text("999999")
        (state_dir / "embedding-sidecar.port").write_text("8000")

        with patch("os.kill", side_effect=ProcessLookupError):
            embedder.stop()

        assert not (state_dir / "embedding-sidecar.pid").exists()
