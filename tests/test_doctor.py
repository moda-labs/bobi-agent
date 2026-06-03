"""Tests for modastack doctor health checks."""

from unittest.mock import patch, MagicMock

from modastack.doctor import (
    check_manager_running,
    check_event_server,
    check_dashboard,
    check_repos,
    check_workflows,
    run_doctor,
)
from modastack.config import GlobalConfig


class TestCheckManagerRunning:

    @patch("modastack.doctor.is_alive", create=True)
    def test_passes_when_alive(self, _):
        with patch("modastack.manager.session.is_alive", return_value=True):
            r = check_manager_running()
        assert r.ok
        assert "Running" in r.detail

    @patch("modastack.manager.session.is_alive", return_value=False)
    def test_fails_when_not_alive(self, _):
        r = check_manager_running()
        assert not r.ok
        assert "Not running" in r.detail
        assert "modastack start" in r.hint

    @patch("modastack.manager.session.is_alive", side_effect=RuntimeError("boom"))
    def test_fails_on_exception(self, _):
        r = check_manager_running()
        assert not r.ok
        assert "boom" in r.detail


class TestCheckEventServer:

    @patch("modastack.doctor.GlobalConfig.load")
    def test_fails_when_no_url(self, mock_load):
        mock_load.return_value = GlobalConfig(event_server_url="")
        r = check_event_server()
        assert not r.ok
        assert "No event_server URL" in r.detail

    @patch("modastack.doctor.socket.create_connection")
    @patch("modastack.doctor.GlobalConfig.load")
    def test_passes_when_reachable(self, mock_load, mock_conn):
        mock_load.return_value = GlobalConfig(
            event_server_url="https://events.example.com"
        )
        mock_sock = MagicMock()
        mock_conn.return_value = mock_sock
        r = check_event_server()
        assert r.ok
        assert "Reachable" in r.detail
        mock_sock.close.assert_called_once()

    @patch("modastack.doctor.socket.create_connection", side_effect=OSError("refused"))
    @patch("modastack.doctor.GlobalConfig.load")
    def test_fails_when_unreachable(self, mock_load, _):
        mock_load.return_value = GlobalConfig(
            event_server_url="https://events.example.com"
        )
        r = check_event_server()
        assert not r.ok
        assert "refused" in r.detail


class TestCheckDashboard:

    @patch("urllib.request.urlopen")
    def test_passes_when_responding(self, mock_open):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"{}"
        mock_open.return_value = mock_resp
        r = check_dashboard()
        assert r.ok
        assert "8095" in r.detail

    @patch("urllib.request.urlopen", side_effect=OSError("refused"))
    def test_fails_when_not_responding(self, _):
        r = check_dashboard()
        assert not r.ok
        assert "Not responding" in r.detail


class TestCheckRepos:

    @patch("modastack.doctor.GlobalConfig.load")
    def test_passes_with_no_repos(self, mock_load):
        mock_load.return_value = GlobalConfig(repos=[])
        r = check_repos()
        assert r.ok
        assert "No repos" in r.detail

    @patch("modastack.doctor.GlobalConfig.load")
    def test_passes_when_all_exist(self, mock_load, tmp_path):
        repo1 = tmp_path / "repo1"
        repo2 = tmp_path / "repo2"
        repo1.mkdir()
        repo2.mkdir()
        mock_load.return_value = GlobalConfig(repos=[repo1, repo2])
        r = check_repos()
        assert r.ok
        assert "2 repos" in r.detail

    @patch("modastack.doctor.GlobalConfig.load")
    def test_fails_when_repo_missing(self, mock_load, tmp_path):
        exists = tmp_path / "exists"
        exists.mkdir()
        missing = tmp_path / "gone"
        mock_load.return_value = GlobalConfig(repos=[exists, missing])
        r = check_repos()
        assert not r.ok
        assert "gone" in r.detail


class TestCheckWorkflows:

    def test_passes_with_valid_yaml(self, tmp_path):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "test.yaml").write_text(
            "name: test\ntrigger: manual\nsteps:\n  - name: step1\n    prompt: do it\n"
        )
        empty_dir = tmp_path / "user_workflows"

        with patch("modastack.workflow.triggers.WORKFLOWS_DIR", wf_dir), \
             patch("modastack.workflow.triggers.USER_WORKFLOWS_DIR", empty_dir):
            r = check_workflows()
        assert r.ok
        assert "1 workflow" in r.detail

    def test_fails_with_invalid_yaml(self, tmp_path):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "bad.yaml").write_text("name: bad\nsteps: [[[invalid")
        empty_dir = tmp_path / "user_workflows"

        with patch("modastack.workflow.triggers.WORKFLOWS_DIR", wf_dir), \
             patch("modastack.workflow.triggers.USER_WORKFLOWS_DIR", empty_dir):
            r = check_workflows()
        assert not r.ok
        assert "bad.yaml" in r.detail

    def test_passes_with_no_files(self, tmp_path):
        empty1 = tmp_path / "w1"
        empty2 = tmp_path / "w2"

        with patch("modastack.workflow.triggers.WORKFLOWS_DIR", empty1), \
             patch("modastack.workflow.triggers.USER_WORKFLOWS_DIR", empty2):
            r = check_workflows()
        assert r.ok
        assert "No workflow" in r.detail


class TestRunDoctor:

    @patch("modastack.doctor.check_workflows")
    @patch("modastack.doctor.check_repos")
    @patch("modastack.doctor.check_dashboard")
    @patch("modastack.doctor.check_event_server")
    @patch("modastack.doctor.check_manager_running")
    def test_returns_all_checks(self, m1, m2, m3, m4, m5):
        from modastack.browser import CheckResult
        for m in (m1, m2, m3, m4, m5):
            m.return_value = CheckResult(name="test", ok=True, detail="ok")
        results = run_doctor()
        assert len(results) == 5
        assert all(r.ok for r in results)
