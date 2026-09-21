"""User-level OS service integration for local Bobi agents."""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from bobi import paths
from bobi.fsutil import atomic_write_text

SERVICE_NAME = "bobi"
LAUNCHD_LABEL = "com.moda-labs.bobi"
SYSTEMD_UNIT = f"{SERVICE_NAME}.service"
LAUNCHD_PLIST = f"{LAUNCHD_LABEL}.plist"


def _uid_target() -> str:
    return f"gui/{os.getuid()}"


def _command() -> list[str]:
    executable = shutil.which("bobi")
    if executable:
        return [executable]
    return [sys.executable, "-m", "bobi.cli"]


def _path_env() -> str:
    entries = [str(Path.home() / ".local" / "bin")]
    entries.extend(p for p in os.environ.get("PATH", "").split(os.pathsep) if p)
    return os.pathsep.join(dict.fromkeys(entries))


def _arguments(name: str) -> list[str]:
    return [*_command(), "agent", name, "supervise", "--", "--foreground"]


def systemd_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / LAUNCHD_PLIST


def _launchd_target() -> str:
    return f"{_uid_target()}/{LAUNCHD_LABEL}"


def render_systemd_unit(name: str, root: Path) -> str:
    args = shlex.join(_arguments(name))
    log_path = shlex.quote(str(paths.manager_log_path(root)))
    return (
        "[Unit]\n"
        f"Description=Bobi Agent {name}\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={args}\n"
        f"WorkingDirectory={shlex.quote(str(root))}\n"
        f"Environment=BOBI_HOME={shlex.quote(str(paths.home_dir()))}\n"
        f"Environment=PATH={shlex.quote(_path_env())}\n"
        f"StandardOutput=append:{log_path}\n"
        f"StandardError=append:{log_path}\n"
        "Restart=on-failure\n"
        "RestartSec=30\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_launchd_plist(name: str, root: Path) -> str:
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": _arguments(name),
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "BOBI_HOME": str(paths.home_dir()),
            "PATH": _path_env(),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(paths.manager_log_path(root)),
        "StandardErrorPath": str(paths.manager_log_path(root)),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML).decode("utf-8")


def _run(command: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def _report_failure(command: list[str], result: subprocess.CompletedProcess) -> bool:
    if result.returncode == 0:
        return True
    detail = result.stderr.strip() or result.stdout.strip() or "command failed"
    command_text = shlex.join(command)
    raise RuntimeError(f"{command_text}: {detail}")


def has_systemd_service() -> bool:
    if not systemd_path().exists():
        return False
    try:
        result = _run(["systemctl", "--user", "is-enabled", SERVICE_NAME], timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def has_launchd_service() -> bool:
    if not launchd_path().exists():
        return False
    try:
        result = _run(["launchctl", "print", _launchd_target()], timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def active_manager() -> str | None:
    if sys.platform.startswith("linux") and has_systemd_service():
        return "systemd"
    if sys.platform == "darwin" and has_launchd_service():
        return "launchd"
    return None


def configured_manager() -> str | None:
    """Return the manager with a generated unit, even when it is stopped."""
    if sys.platform.startswith("linux") and systemd_path().exists():
        return "systemd"
    if sys.platform == "darwin" and launchd_path().exists():
        return "launchd"
    return None


def _systemctl(action: str) -> bool:
    command = ["systemctl", "--user", action, SERVICE_NAME]
    try:
        result = _run(command)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"systemctl {action} unavailable: {exc}") from exc
    return _report_failure(command, result)


def _launchctl(action: str) -> bool:
    if action == "stop":
        command = ["launchctl", "bootout", _launchd_target()]
    elif action == "restart":
        command = ["launchctl", "kickstart", "-k", _launchd_target()]
    else:
        raise ValueError(f"unsupported launchctl action: {action}")
    try:
        result = _run(command)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"launchctl {action} unavailable: {exc}") from exc
    return _report_failure(command, result)


def service_action(manager: str, action: str) -> bool:
    if manager == "systemd":
        return _systemctl(action)
    if manager == "launchd":
        loaded = has_launchd_service()
        if action == "stop" and not loaded:
            return True
        if action == "restart" and not loaded:
            command = ["launchctl", "bootstrap", _uid_target(), str(launchd_path())]
            try:
                result = _run(command)
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"launchctl bootstrap unavailable: {exc}") from exc
            return _report_failure(command, result)
        return _launchctl(action)
    raise ValueError(f"unsupported service manager: {manager}")


def service_pid(manager: str) -> str:
    if manager == "systemd":
        command = ["systemctl", "--user", "show", SERVICE_NAME,
                   "--property=MainPID", "--value"]
    elif manager == "launchd":
        command = ["launchctl", "print", _launchd_target()]
    else:
        raise ValueError(f"unsupported service manager: {manager}")
    try:
        result = _run(command, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    if manager == "systemd":
        return result.stdout.strip() or "unknown"
    match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", result.stdout, re.MULTILINE)
    return match.group(1) if match else "unknown"


def install(name: str, root: Path, platform: str | None = None) -> Path:
    if getattr(os, "geteuid", lambda: -1)() == 0:
        raise RuntimeError("refusing to install a root-owned service; run as the Bobi user")
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        target = launchd_path()
        was_loaded = has_launchd_service()
        atomic_write_text(target, render_launchd_plist(name, root))
        if was_loaded:
            _launchctl("stop")
        command = ["launchctl", "bootstrap", _uid_target(), str(target)]
        try:
            result = _run(command)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"launchctl bootstrap unavailable: {exc}") from exc
        _report_failure(command, result)
        return target
    if platform.startswith("linux"):
        target = systemd_path()
        was_enabled = has_systemd_service()
        atomic_write_text(target, render_systemd_unit(name, root))
        try:
            reload_result = _run(["systemctl", "--user", "daemon-reload"])
            _report_failure(["systemctl", "--user", "daemon-reload"], reload_result)
            command = ["systemctl", "--user", "enable", "--now", SERVICE_NAME]
            result = _run(command)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"systemctl install unavailable: {exc}") from exc
        _report_failure(command, result)
        if was_enabled:
            _systemctl("restart")
        return target
    raise RuntimeError(f"unsupported platform {platform!r}; use macOS or Linux")


def uninstall(platform: str | None = None) -> Path:
    if getattr(os, "geteuid", lambda: -1)() == 0:
        raise RuntimeError("refusing to remove a root-owned service; run as the Bobi user")
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        target = launchd_path()
        if has_launchd_service():
            _launchctl("stop")
        target.unlink(missing_ok=True)
        return target
    if platform.startswith("linux"):
        target = systemd_path()
        if target.exists():
            command = ["systemctl", "--user", "disable", "--now", SERVICE_NAME]
            try:
                result = _run(command)
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"systemctl uninstall unavailable: {exc}") from exc
            _report_failure(command, result)
            target.unlink()
            reload_command = ["systemctl", "--user", "daemon-reload"]
            try:
                result = _run(reload_command)
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError(f"systemctl daemon-reload unavailable: {exc}") from exc
            _report_failure(reload_command, result)
        return target
    raise RuntimeError(f"unsupported platform {platform!r}; use macOS or Linux")
