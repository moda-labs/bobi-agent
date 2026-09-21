"""Local OS service generation and CLI lifecycle delegation."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from bobi.cli import main
from tests.conftest import TEST_AGENT_NAME


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
    assert payload["KeepAlive"] is True
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
    monkeypatch.setattr("bobi.cli._active_service_manager", lambda: "launchd")
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


def test_stop_force_bypasses_launchd(bobi_install, monkeypatch):
    actions = []
    monkeypatch.setattr("bobi.cli._active_service_manager", lambda: "launchd")
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
    assert actions == []
    assert seen == {"force": True}


def test_restart_delegates_to_launchd_and_reports_pid(bobi_install, monkeypatch):
    actions = []
    monkeypatch.setattr("bobi.cli._active_service_manager", lambda: "launchd")
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
    monkeypatch.setattr("bobi.cli._active_service_manager", lambda: "launchd")
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
        lambda: removed.append(True) or target,
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
    assert removed == [True]
    assert "Removed service" in uninstall_result.output
