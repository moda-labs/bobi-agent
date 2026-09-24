"""Local OS service generation and CLI lifecycle delegation."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from bobi.cli import main
from tests.conftest import TEST_AGENT_NAME


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Unit files land under HOME; never the developer's real one. Tests that
    need a specific HOME set it again."""
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))


def _ok(command, stdout=""):
    return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


def test_systemd_unit_runs_the_supervisor(bobi_install, monkeypatch):
    from bobi import service_manager

    monkeypatch.setattr(service_manager.shutil, "which", lambda _: "/opt/bin/bobi")

    unit = service_manager.render_systemd_unit(
        TEST_AGENT_NAME, bobi_install.repo_path,
    )

    assert (
        f"ExecStart=/opt/bin/bobi agent {TEST_AGENT_NAME} "
        "supervise -- --foreground"
    ) in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=30" in unit
    assert "StartLimitIntervalSec=300" in unit
    assert "StartLimitBurst=4" in unit
    assert "network-online.target" not in unit
    assert f"BOBI_HOME={bobi_install.agents_dir.parent}" in unit
    assert str(bobi_install.state_dir / "manager.log") in unit


def test_launchd_plist_runs_the_supervisor(bobi_install, monkeypatch):
    from bobi import service_manager

    monkeypatch.setattr(service_manager.shutil, "which", lambda _: "/opt/bin/bobi")

    payload = plistlib.loads(service_manager.render_launchd_plist(
        TEST_AGENT_NAME, bobi_install.repo_path,
    ).encode())

    assert payload["Label"] == service_manager.LAUNCHD_LABEL
    assert payload["ProgramArguments"] == [
        "/opt/bin/bobi", "agent", TEST_AGENT_NAME,
        "supervise", "--", "--foreground",
    ]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert payload["ThrottleInterval"] == 30
    assert payload["EnvironmentVariables"]["BOBI_HOME"] == str(
        bobi_install.agents_dir.parent
    )


def test_systemd_install_is_idempotent(tmp_path, monkeypatch):
    from bobi import service_manager

    home = tmp_path / "home"
    root = home / ".bobi" / "agents" / "test-agent" / "run"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BOBI_HOME", str(home / ".bobi"))
    monkeypatch.setattr(service_manager.os, "geteuid", lambda: 501)
    monkeypatch.setattr(service_manager.shutil, "which", lambda _: "/bin/bobi")
    calls = []

    def run(command, timeout=30):
        calls.append(command)
        return _ok(command)

    monkeypatch.setattr(service_manager, "_run", run)

    target = service_manager.install("test-agent", root, platform="linux")
    service_manager.install("test-agent", root, platform="linux")

    assert target == home / ".config" / "systemd" / "user" / "bobi.service"
    assert target.is_file()
    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "bobi"],
        ["systemctl", "--user", "is-enabled", "bobi"],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "bobi"],
        ["systemctl", "--user", "restart", "bobi"],
    ]


def test_launchd_install_reloads_an_existing_agent(tmp_path, monkeypatch):
    from bobi import service_manager

    home = tmp_path / "home"
    root = home / ".bobi" / "agents" / "test-agent" / "run"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BOBI_HOME", str(home / ".bobi"))
    monkeypatch.setattr(service_manager.os, "geteuid", lambda: 501)
    monkeypatch.setattr(service_manager.os, "getuid", lambda: 501)
    monkeypatch.setattr(service_manager.shutil, "which", lambda _: "/bin/bobi")
    monkeypatch.setattr(service_manager, "_uid_target", lambda: "gui/501")
    calls = []
    existing = home / "Library" / "LaunchAgents" / (
        service_manager.LAUNCHD_LABEL + ".plist"
    )
    existing.parent.mkdir(parents=True)
    existing.write_text("old")

    def run(command, timeout=30):
        calls.append(command)
        return _ok(command)

    monkeypatch.setattr(service_manager, "_run", run)

    target = service_manager.install("test-agent", root, platform="darwin")

    assert target == home / "Library" / "LaunchAgents" / (
        service_manager.LAUNCHD_LABEL + ".plist"
    )
    assert calls == [
        ["launchctl", "print", "gui/501/com.moda-labs.bobi"],
        ["launchctl", "bootout", "gui/501/com.moda-labs.bobi"],
        ["launchctl", "bootstrap", "gui/501", str(target)],
    ]


def test_install_refuses_a_root_owned_service(tmp_path, monkeypatch):
    from bobi import service_manager

    monkeypatch.setattr(service_manager.os, "geteuid", lambda: 0)

    try:
        service_manager.install("test-agent", tmp_path, platform="linux")
    except RuntimeError as exc:
        assert "root-owned" in str(exc)
    else:
        raise AssertionError("root service installation was accepted")


def test_launchd_restart_bootstraps_an_installed_but_stopped_agent(
    tmp_path, monkeypatch,
):
    from bobi import service_manager

    home = tmp_path / "home"
    target = home / "Library" / "LaunchAgents" / (
        service_manager.LAUNCHD_LABEL + ".plist"
    )
    target.parent.mkdir(parents=True)
    target.write_text("unit")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(service_manager.os, "getuid", lambda: 501)
    monkeypatch.setattr(service_manager, "_uid_target", lambda: "gui/501")
    calls = []

    def run(command, timeout=30):
        calls.append(command)
        if command[1] == "print":
            return subprocess.CompletedProcess(command, 113, stdout="", stderr="")
        return _ok(command)

    monkeypatch.setattr(service_manager, "_run", run)

    assert service_manager.service_action("launchd", "restart") is True
    assert calls == [
        ["launchctl", "print", "gui/501/com.moda-labs.bobi"],
        ["launchctl", "bootstrap", "gui/501", str(target)],
    ]


def test_unloaded_launchd_unit_is_configured_but_not_active(tmp_path, monkeypatch):
    from bobi import service_manager

    home = tmp_path / "home"
    target = home / "Library" / "LaunchAgents" / (
        service_manager.LAUNCHD_LABEL + ".plist"
    )
    target.parent.mkdir(parents=True)
    target.write_text("unit")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(service_manager.sys, "platform", "darwin")
    monkeypatch.setattr(service_manager.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        service_manager, "_run",
        lambda command, timeout=30: subprocess.CompletedProcess(
            command, 113, stdout="", stderr="not loaded",
        ),
    )

    assert service_manager.active_manager() is None
    assert service_manager.configured_manager() == "launchd"


def test_systemd_uninstall_stops_removes_and_reloads(tmp_path, monkeypatch):
    from bobi import service_manager

    home = tmp_path / "home"
    target = home / ".config" / "systemd" / "user" / "bobi.service"
    target.parent.mkdir(parents=True)
    target.write_text("unit")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(service_manager.os, "geteuid", lambda: 501)
    calls = []

    def run(command, timeout=30):
        calls.append(command)
        return _ok(command)

    monkeypatch.setattr(service_manager, "_run", run)

    assert service_manager.uninstall(platform="linux") == target
    assert not target.exists()
    assert calls == [
        ["systemctl", "--user", "disable", "--now", "bobi"],
        ["systemctl", "--user", "daemon-reload"],
    ]


def test_stop_delegates_to_launchd(bobi_install, monkeypatch):
    actions = []
    monkeypatch.setattr("bobi.cli._active_service_manager", lambda agent=None: "launchd")
    monkeypatch.setattr(
        "bobi.cli._service_action",
        lambda manager, action: actions.append((manager, action)) or True,
    )
    monkeypatch.setattr(
        "bobi.service.stop_team",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("direct stop should not run")
        ),
    )

    result = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "stop"])

    assert result.exit_code == 0, result.output
    assert actions == [("launchd", "stop")]
    assert "Stopping via launchd" in result.output


def test_launchd_uninstall_stops_and_removes(tmp_path, monkeypatch):
    from bobi import service_manager

    home = tmp_path / "home"
    target = home / "Library" / "LaunchAgents" / (
        service_manager.LAUNCHD_LABEL + ".plist"
    )
    target.parent.mkdir(parents=True)
    target.write_text("unit")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(service_manager.os, "geteuid", lambda: 501)
    monkeypatch.setattr(service_manager.os, "getuid", lambda: 501)
    monkeypatch.setattr(service_manager, "_uid_target", lambda: "gui/501")
    calls = []

    def run(command, timeout=30):
        calls.append(command)
        return _ok(command)

    monkeypatch.setattr(service_manager, "_run", run)

    assert service_manager.uninstall(platform="darwin") == target
    assert not target.exists()
    assert calls == [
        ["launchctl", "print", "gui/501/com.moda-labs.bobi"],
        ["launchctl", "bootout", "gui/501/com.moda-labs.bobi"],
    ]


def test_install_stops_running_manager(tmp_path, bobi_install, monkeypatch):
    from bobi import service_manager

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(service_manager.os, "geteuid", lambda: 501)
    monkeypatch.setattr(service_manager.os, "getuid", lambda: 501)
    stopped_roots = []

    def stop_team(root, force=False):
        stopped_roots.append(root)
        return SimpleNamespace(
            still_running=False, permission_denied=False, pid=0,
        )

    monkeypatch.setattr("bobi.service.stop_team", stop_team)
    monkeypatch.setattr(service_manager, "_run", lambda cmd, **kw: _ok(cmd))

    service_manager.install(TEST_AGENT_NAME, bobi_install.repo_path, platform="darwin")

    assert stopped_roots == [bobi_install.repo_path]


def test_configured_agent_and_safety(tmp_path, monkeypatch):
    from bobi import service_manager

    home = tmp_path / "home"
    target = home / "Library" / "LaunchAgents" / (
        service_manager.LAUNCHD_LABEL + ".plist"
    )
    target.parent.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(service_manager.sys, "platform", "darwin")
    monkeypatch.setattr(service_manager.shutil, "which", lambda _: "/bin/bobi")

    target.write_text(service_manager.render_launchd_plist("agent-a", tmp_path))

    assert service_manager.configured_agent(platform="darwin") == "agent-a"
    assert service_manager.configured_manager(agent_name="agent-a", platform="darwin") == "launchd"
    assert service_manager.configured_manager(agent_name="agent-b", platform="darwin") is None

    try:
        service_manager.uninstall(agent_name="agent-b", platform="darwin")
    except RuntimeError as exc:
        assert "configured for agent 'agent-a'" in str(exc)
    else:
        raise AssertionError("Expected uninstall for mismatched agent to fail")


def test_stop_force_stops_service_and_kills_process(bobi_install, monkeypatch):
    actions = []
    monkeypatch.setattr("bobi.cli._active_service_manager", lambda agent=None: "launchd")
    monkeypatch.setattr(
        "bobi.cli._service_action",
        lambda manager, action: actions.append((manager, action)) or True,
    )
    seen = {}

    def stop_team(project_path, force=False):
        seen["force"] = force
        return SimpleNamespace(
            invalid_pid=False, stale=False, permission_denied=False,
            stopped=False, killed=False, still_running=False, pid=0,
            event_server_running=False, event_server_port=8080,
        )

    monkeypatch.setattr("bobi.service.stop_team", stop_team)

    result = CliRunner().invoke(
        main, ["agent", TEST_AGENT_NAME, "stop", "--force"],
    )

    assert result.exit_code == 0, result.output
    assert actions == [("launchd", "stop")]
    assert seen == {"force": True}
    assert "Stopping via launchd" in result.output


def test_start_delegates_to_service_manager(bobi_install, monkeypatch):
    actions = []
    monkeypatch.setattr("bobi.cli._configured_service_manager", lambda agent=None: "launchd")
    monkeypatch.setattr("bobi.cli._active_service_manager", lambda agent=None: None)
    monkeypatch.setattr(
        "bobi.cli._service_action",
        lambda manager, action: actions.append((manager, action)) or True,
    )
    monkeypatch.setattr("bobi.service_manager.wait_for_manager_pid", lambda root, **kw: 9876)
    monkeypatch.setattr(
        "bobi.service.spawn_team",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should delegate")),
    )

    result = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "start"])

    assert result.exit_code == 0, result.output
    assert actions == [("launchd", "restart")]
    assert "Bobi started (pid 9876)" in result.output


def test_systemd_cli_delegation(bobi_install, monkeypatch):
    actions = []
    monkeypatch.setattr("bobi.cli._active_service_manager", lambda agent=None: "systemd")
    monkeypatch.setattr("bobi.cli._configured_service_manager", lambda agent=None: "systemd")
    monkeypatch.setattr(
        "bobi.cli._service_action",
        lambda manager, action: actions.append((manager, action)) or True,
    )
    monkeypatch.setattr("bobi.service_manager.wait_for_manager_pid", lambda root, **kw: 5555)

    stop_res = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "stop"])
    assert stop_res.exit_code == 0
    assert "Stopping via systemd" in stop_res.output

    restart_res = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "restart"])
    assert restart_res.exit_code == 0
    assert "Restarting via systemd" in restart_res.output
    assert "pid 5555" in restart_res.output


def test_local_runtime_delegation(tmp_path, monkeypatch):
    from bobi.webapp.runtime import LocalRuntime, TeamAlreadyRunning

    rt = LocalRuntime()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BOBI_HOME", str(home / ".bobi"))
    agent_dir = home / ".bobi" / "agents" / "my-agent" / "run"
    agent_dir.mkdir(parents=True)
    (agent_dir / "package").mkdir(parents=True)
    (agent_dir / "package" / "agent.yaml").write_text("agent: my-agent\nentry_role: coordinator\n")

    actions = []
    monkeypatch.setattr(
        "bobi.service_manager.configured_manager", lambda name, **kwargs: "launchd",
    )
    monkeypatch.setattr(
        "bobi.service_manager.active_manager", lambda name, **kwargs: "launchd",
    )
    monkeypatch.setattr(
        "bobi.service_manager.service_action",
        lambda mgr, action: actions.append((mgr, action)) or True,
    )
    monkeypatch.setattr("bobi.service_manager.wait_for_manager_pid", lambda root, **kw: 1234)

    try:
        rt.start_team("my-agent")
    except TeamAlreadyRunning:
        pass
    else:
        raise AssertionError("Expected TeamAlreadyRunning")

    res = rt.stop_team("my-agent")
    assert res["ok"] is True
    assert actions == [("launchd", "stop")]

    res = rt.restart_team("my-agent")
    assert res["ok"] is True
    assert res["pid"] == 1234
    assert actions == [("launchd", "stop"), ("launchd", "restart")]


def test_restart_delegates_to_launchd_and_reports_pid(bobi_install, monkeypatch):
    actions = []
    monkeypatch.setattr("bobi.cli._configured_service_manager", lambda agent=None: "launchd")
    monkeypatch.setattr(
        "bobi.cli._service_action",
        lambda manager, action: actions.append((manager, action)) or True,
    )
    monkeypatch.setattr("bobi.cli._service_pid", lambda manager: "4321")
    monkeypatch.setattr(
        "bobi.service.spawn_team",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("direct start should not run")
        ),
    )

    result = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "restart"])

    assert result.exit_code == 0, result.output
    assert actions == [("launchd", "restart")]
    assert "Bobi restarted (pid 4321)" in result.output


def test_restart_does_not_claim_success_when_launchd_fails(
    bobi_install, monkeypatch,
):
    monkeypatch.setattr("bobi.cli._configured_service_manager", lambda agent=None: "launchd")
    monkeypatch.setattr(
        "bobi.service_manager.service_action",
        lambda manager, action: (_ for _ in ()).throw(RuntimeError("launchd failed")),
    )

    result = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "restart"])

    assert result.exit_code != 0
    assert "launchd failed" in result.output
    assert "Bobi restarted" not in result.output


def test_install_and_uninstall_service_commands(bobi_install, monkeypatch):
    target = Path("/tmp/bobi.service")
    installed = []
    removed = []
    monkeypatch.setattr(
        "bobi.service_manager.install",
        lambda name, root: installed.append((name, root)) or target,
    )
    monkeypatch.setattr(
        "bobi.service_manager.uninstall",
        lambda agent_name=None: removed.append(agent_name or True) or target,
    )

    install_result = CliRunner().invoke(
        main, ["agent", TEST_AGENT_NAME, "install-service"],
    )
    uninstall_result = CliRunner().invoke(
        main, ["agent", TEST_AGENT_NAME, "uninstall-service"],
    )

    assert install_result.exit_code == 0, install_result.output
    assert installed == [(TEST_AGENT_NAME, bobi_install.repo_path)]
    assert "Installed and started" in install_result.output
    assert uninstall_result.exit_code == 0, uninstall_result.output
    assert removed == [TEST_AGENT_NAME]
    assert "Removed service" in uninstall_result.output


def test_start_service_fresh_and_subscribe_guard(bobi_install, monkeypatch):
    cleared = []
    actions = []
    monkeypatch.setattr("bobi.cli._configured_service_manager", lambda agent=None: "launchd")
    monkeypatch.setattr("bobi.cli._active_service_manager", lambda agent=None: None)
    monkeypatch.setattr(
        "bobi.cli._service_action",
        lambda manager, action: actions.append((manager, action)) or True,
    )
    monkeypatch.setattr("bobi.service_manager.wait_for_manager_pid", lambda root, **kw: 9876)
    monkeypatch.setattr(
        "bobi.service.clear_manager_session",
        lambda root: cleared.append(root),
    )

    # start --fresh
    res = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "start", "--fresh"])
    assert res.exit_code == 0, res.output
    assert cleared == [bobi_install.repo_path]
    assert "Cleared manager session" in res.output

    # start --subscribe rejected
    sub_res = CliRunner().invoke(
        main, ["agent", TEST_AGENT_NAME, "start", "--subscribe", "linear:MOD"],
    )
    assert sub_res.exit_code != 0
    assert "Custom --subscribe is not supported through the OS service" in sub_res.output


def _stopped(**overrides):
    fields = dict(still_running=False, permission_denied=False, pid=0)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_install_sigkills_then_refuses_a_surviving_manager(tmp_path, monkeypatch):
    from bobi import service_manager

    monkeypatch.setattr(service_manager.os, "geteuid", lambda: 501)
    monkeypatch.setattr(service_manager.os, "getuid", lambda: 501)
    calls = []

    def stop_team(root, force=False):
        calls.append(force)
        return _stopped(still_running=True, pid=99)

    monkeypatch.setattr("bobi.service.stop_team", stop_team)
    ran = []
    monkeypatch.setattr(
        service_manager, "_run", lambda command, timeout=30: ran.append(command) or _ok(command),
    )

    try:
        service_manager.install("test-agent", tmp_path, platform="darwin")
    except RuntimeError as exc:
        assert "still running" in str(exc)
    else:
        raise AssertionError("install continued beside a live manager")

    assert calls == [False, True]
    assert ran == []


def test_install_continues_after_sigkill(tmp_path, monkeypatch):
    from bobi import service_manager

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(service_manager.os, "geteuid", lambda: 501)
    monkeypatch.setattr(service_manager.os, "getuid", lambda: 501)
    monkeypatch.setattr(service_manager.shutil, "which", lambda _: "/bin/bobi")
    calls = []

    def stop_team(root, force=False):
        calls.append(force)
        return _stopped(still_running=not force, pid=99)

    monkeypatch.setattr("bobi.service.stop_team", stop_team)
    monkeypatch.setattr(service_manager, "_run", lambda command, timeout=30: _ok(command))

    target = service_manager.install("test-agent", tmp_path / "run", platform="linux")

    assert calls == [False, True]
    assert target.is_file()


def test_stop_cancels_an_enabled_but_inactive_systemd_unit(bobi_install, monkeypatch):
    actions = []
    monkeypatch.setattr("bobi.cli._active_service_manager", lambda agent=None: None)
    monkeypatch.setattr(
        "bobi.service_manager.stop_manager", lambda agent_name=None, platform=None: "systemd",
    )
    monkeypatch.setattr(
        "bobi.cli._service_action",
        lambda manager, action: actions.append((manager, action)) or True,
    )
    seen = {}

    def stop_team(project_path, force=False):
        seen["force"] = force
        return SimpleNamespace(
            invalid_pid=False, stale=False, permission_denied=False,
            stopped=False, killed=False, still_running=False, pid=0,
            event_server_running=False, event_server_port=8080,
        )

    monkeypatch.setattr("bobi.service.stop_team", stop_team)

    result = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "stop"])

    assert result.exit_code == 0, result.output
    assert actions == [("systemd", "stop")]
    assert seen == {"force": False}
    assert "Stopping via systemd" in result.output


def test_linux_stop_manager_includes_an_inactive_enabled_unit(tmp_path, monkeypatch):
    from bobi import service_manager

    home = tmp_path / "home"
    target = home / ".config" / "systemd" / "user" / "bobi.service"
    target.parent.mkdir(parents=True)
    target.write_text(
        "Description=Bobi Agent eng\n"
        "ExecStart=/bin/bobi agent eng supervise -- --foreground\n"
    )
    monkeypatch.setenv("HOME", str(home))

    def run(command, timeout=30):
        if command[2] == "is-enabled":
            return _ok(command)
        if command[2] == "is-active":
            return subprocess.CompletedProcess(command, 3, stdout="activating", stderr="")
        return _ok(command)

    monkeypatch.setattr(service_manager, "_run", run)

    assert service_manager.active_manager(agent_name="eng", platform="linux") is None
    assert service_manager.stop_manager(agent_name="eng", platform="linux") == "systemd"
    assert service_manager.stop_manager(agent_name="other", platform="linux") is None


def test_local_runtime_stops_an_inactive_systemd_unit(tmp_path, monkeypatch):
    from bobi.webapp.runtime import LocalRuntime

    rt = LocalRuntime()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BOBI_HOME", str(home / ".bobi"))
    agent_dir = home / ".bobi" / "agents" / "my-agent" / "run"
    agent_dir.mkdir(parents=True)
    (agent_dir / "package").mkdir(parents=True)
    (agent_dir / "package" / "agent.yaml").write_text(
        "agent: my-agent\nentry_role: coordinator\n"
    )
    actions = []
    monkeypatch.setattr(
        "bobi.service_manager.stop_manager", lambda name, platform=None: "systemd",
    )
    monkeypatch.setattr(
        "bobi.service_manager.service_action",
        lambda mgr, action: actions.append((mgr, action)) or True,
    )
    monkeypatch.setattr(
        "bobi.service.stop_team",
        lambda root, force=False: SimpleNamespace(
            stopped=False, killed=False, stale=False, pid=0,
            permission_denied=False, still_running=False,
        ),
    )

    res = rt.stop_team("my-agent")

    assert res["ok"] is True
    assert actions == [("systemd", "stop")]


def test_linux_active_manager_requires_is_active(tmp_path, monkeypatch):
    from bobi import service_manager

    home = tmp_path / "home"
    target = home / ".config" / "systemd" / "user" / "bobi.service"
    target.parent.mkdir(parents=True)
    target.write_text("unit")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(service_manager.sys, "platform", "linux")

    # is-enabled is True, but is-active is False (stopped service)
    def run(command, timeout=30):
        if command[2] == "is-enabled":
            return _ok(command)
        if command[2] == "is-active":
            return subprocess.CompletedProcess(command, 3, stdout="inactive", stderr="")
        return _ok(command)

    monkeypatch.setattr(service_manager, "_run", run)

    assert service_manager.configured_manager(platform="linux") == "systemd"
    assert service_manager.active_manager(platform="linux") is None

    # now service becomes active
    def run_active(command, timeout=30):
        return _ok(command)

    monkeypatch.setattr(service_manager, "_run", run_active)
    assert service_manager.active_manager(platform="linux") == "systemd"


def test_install_validates_platform_before_stopping(tmp_path, monkeypatch):
    from bobi import service_manager

    monkeypatch.setattr(service_manager.os, "geteuid", lambda: 501)
    stopped = []
    monkeypatch.setattr("bobi.service.stop_team", lambda root: stopped.append(root))

    try:
        service_manager.install("test-agent", tmp_path, platform="win32")
    except RuntimeError as exc:
        assert "unsupported platform 'win32'" in str(exc)
    else:
        raise AssertionError("Expected unsupported platform to error")

    assert stopped == []


def test_systemd_unit_escapes_percent_specifiers(tmp_path, monkeypatch):
    from bobi import service_manager

    monkeypatch.setattr(service_manager.shutil, "which", lambda _: "/opt/bin/bobi")
    percent_root = tmp_path / "agent%path"
    unit = service_manager.render_systemd_unit("test-agent", percent_root)
    assert "agent%%path" in unit


def test_handwritten_systemd_unit_delegates_to_service(tmp_path, monkeypatch):
    from bobi import service_manager

    home = tmp_path / "home"
    target = home / ".config" / "systemd" / "user" / "bobi.service"
    target.parent.mkdir(parents=True)
    target.write_text(
        "[Unit]\n"
        "Description=My Hand Crafted Bobi\n"
        "[Service]\n"
        "ExecStart=/usr/local/bin/bobi agent eng start --foreground\n"
        "Restart=always\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(service_manager.sys, "platform", "linux")
    monkeypatch.setattr(service_manager, "_run", lambda cmd, **kw: _ok(cmd))

    # Matches via ExecStart agent <name> start
    assert service_manager.configured_agent(platform="linux") == "eng"
    assert service_manager.configured_manager("eng", platform="linux") == "systemd"
    assert service_manager.stop_manager("eng", platform="linux") == "systemd"

    # Even a unit without any agent name (legacy unit) falls back to managing the agent
    target.write_text("[Unit]\nDescription=Generic Bobi\n[Service]\nExecStart=/bin/true\n")
    assert service_manager.configured_agent(platform="linux") is None
    assert service_manager.configured_manager("eng", platform="linux") == "systemd"
    assert service_manager.stop_manager("eng", platform="linux") == "systemd"


def test_uid_target_falls_back_to_user_domain(monkeypatch):
    from bobi import service_manager

    monkeypatch.setattr(service_manager.os, "getuid", lambda: 501)
    monkeypatch.setattr(service_manager, "_gui_available", lambda uid: False)
    assert service_manager._uid_target() == "user/501"

    monkeypatch.setattr(service_manager, "_gui_available", lambda uid: True)
    assert service_manager._uid_target() == "gui/501"


def test_launchd_service_active_distinguishes_running_state(tmp_path, monkeypatch):
    from bobi import service_manager

    home = tmp_path / "home"
    target = home / "Library" / "LaunchAgents" / (
        service_manager.LAUNCHD_LABEL + ".plist"
    )
    target.parent.mkdir(parents=True)
    target.write_text("unit")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(service_manager.sys, "platform", "darwin")
    monkeypatch.setattr(service_manager.os, "getuid", lambda: 501)
    monkeypatch.setattr(service_manager, "_gui_available", lambda uid: True)

    # When loaded but waiting/idle (not running)
    monkeypatch.setattr(
        service_manager, "_run",
        lambda cmd, timeout=30: subprocess.CompletedProcess(
            cmd, 0, stdout="gui/501/com.moda-labs.bobi = {\n\tstate = waiting\n}\n", stderr="",
        ),
    )
    assert service_manager.has_launchd_service() is True
    assert service_manager.is_launchd_service_active() is False
    assert service_manager.active_manager() is None
    # Loaded service still delegates to launchd for stop
    assert service_manager.stop_manager() == "launchd"

    # When running
    monkeypatch.setattr(
        service_manager, "_run",
        lambda cmd, timeout=30: subprocess.CompletedProcess(
            cmd, 0, stdout="gui/501/com.moda-labs.bobi = {\n\tstate = running\n\tpid = 4321\n}\n", stderr="",
        ),
    )
    assert service_manager.is_launchd_service_active() is True
    assert service_manager.active_manager() == "launchd"


def test_wait_for_manager_pid_excludes_stale_pid(tmp_path, monkeypatch):
    from bobi import service_manager

    pid_path = tmp_path / "state" / "manager.pid"
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text("1111\n")

    monkeypatch.setattr("bobi.paths.manager_pid_path", lambda root: pid_path)
    monkeypatch.setattr("bobi.service._pid_alive", lambda pid: True)

    # If exclude_pid is 1111, it will not return 1111
    assert service_manager.wait_for_manager_pid(tmp_path, timeout=0.2, exclude_pid=1111) is None

    # Once pid changes to 2222, it returns 2222
    pid_path.write_text("2222\n")
    assert service_manager.wait_for_manager_pid(tmp_path, timeout=0.2, exclude_pid=1111) == 2222


def test_start_via_service_sweeps_direct_manager(bobi_install, monkeypatch):
    from bobi import paths

    monkeypatch.setattr("bobi.cli._configured_service_manager", lambda agent=None: "launchd")
    monkeypatch.setattr("bobi.cli._active_service_manager", lambda agent=None: None)
    monkeypatch.setattr("bobi.cli._service_action", lambda manager, action: True)

    stopped_roots = []

    def fake_stop(root, force=False):
        stopped_roots.append((root, force))
        return SimpleNamespace(still_running=False, permission_denied=False, pid=999)

    monkeypatch.setattr("bobi.service.stop_team", fake_stop)
    excluded = []

    def fake_wait(root, timeout=3.0, exclude_pid=None):
        excluded.append(exclude_pid)
        return 1234

    monkeypatch.setattr("bobi.service_manager.wait_for_manager_pid", fake_wait)
    pid_path = paths.manager_pid_path(bobi_install.repo_path)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("999\n")

    result = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "start"])

    assert result.exit_code == 0, result.output
    assert len(stopped_roots) >= 1
    assert stopped_roots[0][0] == bobi_install.repo_path
    assert excluded == [999]
    assert "Bobi started (pid 1234)" in result.output


def test_foreground_start_rejects_active_service(bobi_install, monkeypatch):
    monkeypatch.setattr("bobi.cli._active_service_manager", lambda agent=None: "launchd")
    monkeypatch.setattr("bobi.service_manager.wait_for_manager_pid", lambda root, **kw: 5555)

    # When BOBI_SUPERVISED is unset, foreground start should reject with ClickException
    monkeypatch.delenv("BOBI_SUPERVISED", raising=False)
    result = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "start", "--foreground"])

    assert result.exit_code != 0
    assert "managed by launchd service (pid 5555)" in result.output

    # When BOBI_SUPERVISED is set (supervisor child), foreground start proceeds past service guard
    ran_foreground = []
    monkeypatch.setenv("BOBI_SUPERVISED", "1")
    monkeypatch.setattr(
        "bobi.service.run_team_foreground",
        lambda root, **kw: ran_foreground.append(root),
    )
    result_supervised = CliRunner().invoke(main, ["agent", TEST_AGENT_NAME, "start", "--foreground"])
    assert result_supervised.exit_code == 0, result_supervised.output
    assert ran_foreground == [bobi_install.repo_path]


def test_sweep_direct_manager_ignores_current_pid(tmp_path, monkeypatch):
    import os
    from bobi import paths, service

    pid_path = paths.manager_pid_path(tmp_path)
    pid_path.parent.mkdir(parents=True)
    my_pid = os.getpid()
    pid_path.write_text(f"{my_pid}\n")

    stopped = []
    monkeypatch.setattr("bobi.service.stop_team", lambda root, **kw: stopped.append(root))

    res = service.sweep_direct_manager(tmp_path)
    assert res.pid == 0
    assert stopped == []
    assert not pid_path.exists()


def test_darwin_stop_manager_scoping_ignores_mismatched_agent(monkeypatch):
    from bobi import service_manager

    monkeypatch.setattr(service_manager.sys, "platform", "darwin")
    monkeypatch.setattr(service_manager, "has_launchd_service", lambda: True)
    monkeypatch.setattr(service_manager, "is_launchd_service_active", lambda: True)
    monkeypatch.setattr(service_manager, "configured_agent", lambda platform="darwin": "agent-a")

    assert service_manager.stop_manager("agent-a", platform="darwin") == "launchd"
    assert service_manager.stop_manager("agent-b", platform="darwin") is None


def test_foreground_start_allowed_after_linux_stop(bobi_install, monkeypatch):
    """A stopped-but-enabled unit must not block `start --foreground`: that is
    where `stop` leaves a Linux install, and where --subscribe is sent."""
    from bobi import service_manager

    home = Path(bobi_install.agents_dir.parent)
    unit = home / ".config" / "systemd" / "user" / service_manager.SYSTEMD_UNIT
    unit.parent.mkdir(parents=True)
    unit.write_text(service_manager.render_systemd_unit(
        TEST_AGENT_NAME, bobi_install.repo_path,
    ))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(service_manager.sys, "platform", "linux")
    monkeypatch.setattr("bobi.cli.sys.platform", "linux")

    def fake_run(cmd, timeout=30):
        # Enabled, but inactive after `systemctl --user stop`.
        rc = 3 if "is-active" in cmd else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

    monkeypatch.setattr(service_manager, "_run", fake_run)
    monkeypatch.delenv("BOBI_SUPERVISED", raising=False)
    ran = []
    monkeypatch.setattr(
        "bobi.service.run_team_foreground", lambda root, **kw: ran.append(root),
    )

    result = CliRunner().invoke(
        main, ["agent", TEST_AGENT_NAME, "start", "--foreground"],
    )

    assert result.exit_code == 0, result.output
    assert ran == [bobi_install.repo_path]
    assert "systemd service is installed" in result.output


def test_stop_team_removes_invalid_pid_file(tmp_path):
    from bobi import paths, service

    pid_path = paths.manager_pid_path(tmp_path)
    pid_path.parent.mkdir(parents=True)
    pid_path.write_text("garbage\n")

    result = service.stop_team(tmp_path)

    assert result.invalid_pid is True
    assert not pid_path.exists()


def _spawn_fake_manager(root):
    import sys as _sys
    from bobi import paths

    proc = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(60)"])
    pid_path = paths.manager_pid_path(root)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{proc.pid}\n")
    return proc


def test_supervisor_sweeps_only_under_os_service(tmp_path, monkeypatch):
    """Without the unit/plist marker (a container) the supervisor must not
    signal whatever pid manager.pid names."""
    from bobi.service_manager import OS_SERVICE_ENV
    from bobi.supervisor.__main__ import _sweep_if_os_service

    proc = _spawn_fake_manager(tmp_path)
    try:
        monkeypatch.delenv(OS_SERVICE_ENV, raising=False)
        assert _sweep_if_os_service(tmp_path) is False
        assert proc.poll() is None

        monkeypatch.setenv(OS_SERVICE_ENV, "1")
        assert _sweep_if_os_service(tmp_path) is True
        assert proc.wait(timeout=10) is not None
        import os as _os
        assert OS_SERVICE_ENV not in _os.environ
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_generated_units_carry_os_service_marker(bobi_install, monkeypatch):
    from bobi import service_manager

    monkeypatch.setattr(service_manager.shutil, "which", lambda _: "/opt/bin/bobi")
    unit = service_manager.render_systemd_unit(TEST_AGENT_NAME, bobi_install.repo_path)
    plist = plistlib.loads(service_manager.render_launchd_plist(
        TEST_AGENT_NAME, bobi_install.repo_path,
    ).encode())

    assert f"Environment={service_manager.OS_SERVICE_ENV}=1" in unit
    assert plist["EnvironmentVariables"][service_manager.OS_SERVICE_ENV] == "1"


def test_unnamed_unit_serves_nobody_with_several_agents(tmp_path, monkeypatch):
    from bobi import service_manager

    home = tmp_path / "home"
    target = home / ".config" / "systemd" / "user" / "bobi.service"
    target.parent.mkdir(parents=True)
    target.write_text("[Unit]\nDescription=Generic Bobi\n[Service]\nExecStart=/bin/true\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(service_manager, "_run", lambda cmd, **kw: _ok(cmd))
    monkeypatch.setattr(service_manager.paths, "list_agents", lambda: ["alpha", "beta"])

    for name in ("alpha", "beta"):
        assert service_manager.configured_manager(name, platform="linux") is None
        assert service_manager.active_manager(name, platform="linux") is None

    monkeypatch.setattr(service_manager.paths, "list_agents", lambda: ["alpha"])
    assert service_manager.configured_manager("alpha", platform="linux") == "systemd"
    assert service_manager.active_manager("alpha", platform="linux") == "systemd"
