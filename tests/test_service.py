"""Tests for the plain service core used by CLI and web adapters."""

import os
from types import SimpleNamespace


def test_launch_team_spawns_detached_manager_and_returns_entry(bobi_install, monkeypatch):
    from bobi import paths
    from bobi.config import save_bubble_state, save_deployment_state
    from bobi.sdk import SessionEntry, get_registry
    from bobi.service import launch_team

    manager_name = "bobi-test-agent-director"
    spawned = {}

    def fake_popen(cmd, stdout=None, stderr=None, cwd=None, env=None,
                   start_new_session=False):
        pid = os.getpid()
        spawned["cmd"] = cmd
        spawned["cwd"] = cwd
        spawned["env"] = env
        spawned["start_new_session"] = start_new_session
        get_registry().register(SessionEntry(
            name=manager_name,
            role="director",
            cwd=str(bobi_install.repo_path),
            pid=pid,
            status="running",
        ))
        save_bubble_state(bobi_install.repo_path, "bubble-id", "bubble-key")
        save_deployment_state(
            bobi_install.repo_path, manager_name, "deployment-id", "api-key"
        )
        return SimpleNamespace(pid=pid)

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "bobi.validate.validate_config",
        lambda project: SimpleNamespace(ok=True, checks=[]),
    )

    entry = launch_team(bobi_install.repo_path, wait_timeout=1)

    assert entry.name == manager_name
    assert entry.pid == os.getpid()
    assert spawned["cmd"][:5] == [
        os.sys.executable, "-m", "bobi.cli", "agent", paths.agent_name_for_root(bobi_install.repo_path),
    ]
    assert spawned["cmd"][-2:] == ["start", "--foreground"]
    assert spawned["cwd"] == str(bobi_install.repo_path)
    assert spawned["start_new_session"] is True
    assert spawned["env"]["PYTHONUNBUFFERED"] == "1"


def test_launch_team_waits_for_manager_transport(bobi_install, monkeypatch):
    from bobi.sdk import SessionEntry, get_registry
    from bobi.service import TransportReadyTimeout, launch_team

    manager_name = "bobi-test-agent-director"

    def fake_popen(cmd, stdout=None, stderr=None, cwd=None, env=None,
                   start_new_session=False):
        get_registry().register(SessionEntry(
            name=manager_name,
            role="director",
            cwd=str(bobi_install.repo_path),
            pid=os.getpid(),
            status="running",
        ))
        return SimpleNamespace(
            pid=os.getpid(),
            poll=lambda: None,
            terminate=lambda: None,
            wait=lambda timeout=None: None,
        )

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "bobi.validate.validate_config",
        lambda project: SimpleNamespace(ok=True, checks=[]),
    )

    try:
        launch_team(bobi_install.repo_path, wait_timeout=0.01)
    except TransportReadyTimeout as exc:
        assert exc.manager_name == manager_name
    else:
        raise AssertionError("launch_team returned before transport registration")


def test_spawn_team_returns_without_waiting_for_registration(bobi_install, monkeypatch):
    from bobi.service import spawn_team

    spawned = {}

    def fake_popen(cmd, stdout=None, stderr=None, cwd=None, env=None,
                   start_new_session=False):
        spawned["cmd"] = cmd
        return SimpleNamespace(pid=os.getpid(), poll=lambda: None)

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "bobi.validate.validate_config",
        lambda project: SimpleNamespace(ok=True, checks=[]),
    )

    result = spawn_team(bobi_install.repo_path)

    assert result.startup.pid == os.getpid()
    assert spawned["cmd"][-2:] == ["start", "--foreground"]


def test_run_team_foreground_loads_runtime_dotenv(bobi_install, monkeypatch):
    from bobi.service import run_team_foreground

    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    (bobi_install.repo_path / ".env").write_text(
        "ANTHROPIC_AUTH_TOKEN=from-runtime-dotenv\n")
    monkeypatch.setattr(
        "bobi.validate.validate_config",
        lambda project: SimpleNamespace(ok=True, checks=[]),
    )
    monkeypatch.setattr(
        "bobi.service.run_manager_from_config",
        lambda *args, **kwargs: None,
    )

    run_team_foreground(bobi_install.repo_path, fresh=True)

    assert os.environ["ANTHROPIC_AUTH_TOKEN"] == "from-runtime-dotenv"


def test_startup_info_warns_when_inbound_events_use_local_ingress(bobi_install):
    from bobi.service import build_startup_info

    info = build_startup_info(
        bobi_install.repo_path,
        pid=os.getpid(),
        log_file=bobi_install.state_dir / "manager.log",
    )

    assert info.event_server_url == "localhost:8080"
    assert "slack" in info.ingress_warning
    assert "public HTTPS ingress" in info.ingress_warning
    assert "event_server_url" in info.ingress_hint


def test_startup_info_warns_for_explicit_start_subscription(bobi_install):
    from bobi import paths
    from bobi.service import build_startup_info

    paths.agent_yaml_path(bobi_install.repo_path).write_text(
        "agent: test-agent\n"
        "entry_point: director\n"
    )

    info = build_startup_info(
        bobi_install.repo_path,
        pid=os.getpid(),
        log_file=bobi_install.state_dir / "manager.log",
        extra_subscriptions=["slack"],
    )

    assert "slack" in info.ingress_warning


def test_startup_info_ignores_outbound_chat_transports(bobi_install):
    from bobi import paths
    from bobi.service import build_startup_info

    paths.agent_yaml_path(bobi_install.repo_path).write_text(
        "agent: test-agent\n"
        "entry_point: director\n"
        "services:\n"
        "  - name: slack\n"
        "    events: true\n"
        "    credentials:\n"
        "      app_token: xapp-configured\n"
        "  - name: discord\n"
        "    events: true\n"
    )

    info = build_startup_info(
        bobi_install.repo_path,
        pid=os.getpid(),
        log_file=bobi_install.state_dir / "manager.log",
        extra_subscriptions=["slack:T_TEAM", "discord:A_APP"],
    )

    assert info.ingress_warning == ""


def test_startup_info_mixed_transports_warns_only_for_webhooks(bobi_install):
    from bobi import paths
    from bobi.service import build_startup_info

    paths.agent_yaml_path(bobi_install.repo_path).write_text(
        "agent: test-agent\n"
        "entry_point: director\n"
        "services:\n"
        "  - name: slack\n"
        "    events: true\n"
        "    credentials:\n"
        "      app_token: xapp-configured\n"
        "  - name: discord\n"
        "    events: true\n"
        "  - name: github\n"
        "    events: true\n"
    )

    info = build_startup_info(
        bobi_install.repo_path,
        pid=os.getpid(),
        log_file=bobi_install.state_dir / "manager.log",
        extra_subscriptions=["discord:A_APP", "linear/issues"],
    )

    assert "github" in info.ingress_warning
    assert "linear/issues" in info.ingress_warning
    assert "slack" not in info.ingress_warning
    assert "discord" not in info.ingress_warning


def test_team_status_returns_manager_and_active_agents(bobi_install):
    from bobi.sdk import SessionEntry, get_registry
    from bobi.service import team_status

    pid_file = bobi_install.state_dir / "manager.pid"
    pid_file.write_text(str(os.getpid()))
    get_registry().register(SessionEntry(
        name="bobi-test-agent-director",
        role="director",
        cwd=str(bobi_install.repo_path),
        pid=os.getpid(),
        status="running",
    ))
    get_registry().register(SessionEntry(
        name="wf-test-agent-task",
        role="engineer",
        cwd=str(bobi_install.repo_path),
        pid=os.getpid(),
        status="idle",
        rotation_count=2,
    ))

    status = team_status(bobi_install.repo_path)

    assert status.manager_running is True
    assert status.manager_pid == os.getpid()
    assert sorted(agent.name for agent in status.active_agents) == [
        "bobi-test-agent-director",
        "wf-test-agent-task",
    ]


class TestDetachedRestart:
    """#859: a restart must outlive the process that asked for it.

    The caller is routinely a descendant of the manager being stopped (the
    manager hosts its own agent session in-process), so a stop+start pair run
    in the caller loses its start phase and leaves the team down. These pin
    the seam that makes the pair independent of the caller.
    """

    def _fake_popen(self, spawned, exit_code=0):
        def fake_popen(cmd, stdout=None, stderr=None, cwd=None, env=None,
                       start_new_session=False):
            spawned["cmd"] = cmd
            spawned["cwd"] = cwd
            spawned["env"] = env
            spawned["start_new_session"] = start_new_session
            # Read the streams here: `spawn_restart` closes its handle once
            # the child owns the fd, so they are dead by the time we assert.
            spawned["stdout_name"] = getattr(stdout, "name", None)
            spawned["one_stream"] = stdout is stderr
            return SimpleNamespace(pid=os.getpid(), poll=lambda: exit_code)
        return fake_popen

    def test_spawn_restart_detaches_the_worker(self, bobi_install, monkeypatch):
        from bobi import paths
        from bobi.service import spawn_restart

        spawned = {}
        monkeypatch.setattr("subprocess.Popen", self._fake_popen(spawned))

        handle = spawn_restart(bobi_install.repo_path)

        assert spawned["start_new_session"] is True, (
            "a worker in the caller's session dies with the caller")
        assert spawned["cmd"] == [
            os.sys.executable, "-m", "bobi.cli", "agent",
            paths.agent_name_for_root(bobi_install.repo_path),
            "restart", "--detached-worker",
        ]
        assert spawned["cwd"] == str(bobi_install.repo_path)
        assert spawned["env"]["BOBI_ROOT"] == str(bobi_install.repo_path)
        assert handle.log_file == bobi_install.state_dir / "restart.log"

    def test_spawn_restart_writes_to_a_file_not_a_pipe(self, bobi_install,
                                                       monkeypatch):
        """A pipe the caller holds can stall the worker, or EPIPE it dead."""
        from bobi.service import spawn_restart

        spawned = {}
        monkeypatch.setattr("subprocess.Popen", self._fake_popen(spawned))

        spawn_restart(bobi_install.repo_path)

        assert spawned["stdout_name"] == str(
            bobi_install.state_dir / "restart.log")
        assert spawned["one_stream"] is True

    def test_spawn_restart_passes_fresh_through(self, bobi_install, monkeypatch):
        from bobi.service import spawn_restart

        spawned = {}
        monkeypatch.setattr("subprocess.Popen", self._fake_popen(spawned))

        spawn_restart(bobi_install.repo_path, fresh=True)

        assert spawned["cmd"][-1] == "--fresh"

    def test_restart_team_streams_the_worker_log_and_reports_the_pid(
        self, bobi_install, monkeypatch
    ):
        from bobi.service import restart_team

        spawned = {}
        (bobi_install.state_dir / "manager.pid").unlink(missing_ok=True)

        def popen_that_logs(*args, **kwargs):
            proc = self._fake_popen(spawned)(*args, **kwargs)
            (bobi_install.state_dir / "restart.log").write_text(
                "Stopping bobi (pid 41)...\nStopped.\n")
            (bobi_install.state_dir / "manager.pid").write_text(str(os.getpid()))
            return proc

        monkeypatch.setattr("subprocess.Popen", popen_that_logs)
        lines = []

        result = restart_team(bobi_install.repo_path, on_output=lines.append)

        assert result.pid == os.getpid()
        assert lines == ["Stopping bobi (pid 41)...", "Stopped."]

    def test_restart_team_raises_when_the_worker_fails(self, bobi_install,
                                                       monkeypatch):
        from bobi.service import RestartFailed, restart_team

        spawned = {}

        def popen_that_fails(*args, **kwargs):
            proc = self._fake_popen(spawned, exit_code=1)(*args, **kwargs)
            (bobi_install.state_dir / "restart.log").write_text(
                "Preflight:\nmissing SLACK_BOT_TOKEN\n")
            return proc

        monkeypatch.setattr("subprocess.Popen", popen_that_fails)

        try:
            restart_team(bobi_install.repo_path)
        except RestartFailed as exc:
            assert "worker exit 1" in str(exc)
            assert "missing SLACK_BOT_TOKEN" in exc.report()
        else:
            raise AssertionError("a failed worker was reported as a restart")

    def test_restart_team_raises_when_no_manager_comes_back(self, bobi_install,
                                                            monkeypatch):
        """Worker exit 0 is not proof: the team has to actually be up."""
        from bobi import service
        from bobi.service import RestartFailed, restart_team

        spawned = {}
        monkeypatch.setattr("subprocess.Popen", self._fake_popen(spawned))
        monkeypatch.setattr(service, "MANAGER_PID_TIMEOUT", 0.01)
        (bobi_install.state_dir / "manager.pid").unlink(missing_ok=True)

        try:
            restart_team(bobi_install.repo_path)
        except RestartFailed as exc:
            assert "no manager is running" in str(exc)
        else:
            raise AssertionError("a dead team was reported as a live restart")

    def test_restart_team_raises_when_the_manager_never_changed(
        self, bobi_install, monkeypatch
    ):
        """`stop` exits 0 on a manager that refused to die, and the `start`
        behind it exits 0 on AlreadyRunning - two green phases that restarted
        nothing. A surviving pid is the only honest test."""
        from bobi import service
        from bobi.service import RestartFailed, restart_team

        spawned = {}
        monkeypatch.setattr("subprocess.Popen", self._fake_popen(spawned))
        monkeypatch.setattr(service, "MANAGER_PID_TIMEOUT", 0.01)
        (bobi_install.state_dir / "manager.pid").write_text(str(os.getpid()))

        try:
            restart_team(bobi_install.repo_path)
        except RestartFailed as exc:
            assert f"left manager pid {os.getpid()} running" in str(exc)
        else:
            raise AssertionError("an untouched manager was reported restarted")
