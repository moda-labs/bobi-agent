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


def test_startup_info_warns_when_inbound_events_use_local_ingress(bobi_install):
    from bobi.service import build_startup_info

    info = build_startup_info(
        bobi_install.repo_path,
        pid=os.getpid(),
        log_file=bobi_install.state_dir / "manager.log",
    )

    assert info.event_server_url == "localhost:8080"
    assert "slack" in info.ingress_warning
    assert "external webhooks cannot reach" in info.ingress_warning
    assert "event_server_url" in info.ingress_hint


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
