"""Background lifecycle for the unified web app (`bobi app start|stop|...`).

The app runs detached by default, exactly like the agents themselves: `start`
spawns `bobi app run` as a detached child, `run` binds loopback and serves in
the foreground of that child. Machine-level state lives under
`$BOBI_HOME/webapp/`:

    app.pid    # daemon process id
    app.port   # bound port (written by the running server)
    app.token  # persisted API token (0600) — survives restarts, so the
               # dashboard URL is bookmarkable
    app.log    # daemon stdout/stderr

The persisted-token contract mirrors the container UI (`ui.token`): a
per-launch token dies with foreground mode, so a daemon needs a durable one.
The loopback Host guard remains the primary boundary; the token is
defense-in-depth, same trust model as the other local UIs.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from bobi import paths
from bobi.webui_common.security import WEBUI_TOKEN_HEADER

DEFAULT_PORT = 8642
START_TIMEOUT = 20.0


def _state_dir() -> Path:
    d = paths.webapp_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pid_path() -> Path:
    return _state_dir() / "app.pid"


def _port_path() -> Path:
    return _state_dir() / "app.port"


def _token_path() -> Path:
    return _state_dir() / "app.token"


def _log_path() -> Path:
    return _state_dir() / "app.log"


def configured_port() -> int:
    raw = os.environ.get("BOBI_APP_PORT", "")
    try:
        return int(raw) if raw else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT


def ensure_token() -> str:
    """The persisted app token, minted on first use (0600)."""
    path = _token_path()
    if path.exists():
        token = path.read_text().strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    path.write_text(token)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def _read_int(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _ping(port: int, token: str, timeout: float = 1.0) -> bool:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/ping",
        headers={WEBUI_TOKEN_HEADER: token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return bool(json.loads(resp.read() or b"{}").get("ok"))
    except Exception:
        return False


def app_url(port: int, token: str) -> str:
    return f"http://127.0.0.1:{port}/?n={token}"


@dataclass(frozen=True)
class AppStatus:
    running: bool
    pid: int = 0
    port: int = 0
    url: str = ""


def status() -> AppStatus:
    """Liveness = pid alive AND the server answers /api/ping."""
    pid = _read_int(_pid_path())
    port = _read_int(_port_path())
    if not (_pid_alive(pid) and port):
        return AppStatus(running=False, pid=0, port=0)
    token = ensure_token()
    if not _ping(port, token):
        return AppStatus(running=False, pid=pid, port=port)
    return AppStatus(running=True, pid=pid, port=port,
                     url=app_url(port, token))


def start(*, open_browser: bool = True) -> AppStatus:
    """Ensure the app daemon is running; returns its status.

    Idempotent: an already-running app is reused, not respawned."""
    st = status()
    if st.running:
        if open_browser:
            _open_browser(st.url)
        return st

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    with open(_log_path(), "a") as lf:
        proc = subprocess.Popen(
            [sys.executable, "-m", "bobi.cli", "app", "run"],
            stdout=lf,
            stderr=lf,
            env=env,
            start_new_session=True,
        )
    _pid_path().write_text(str(proc.pid))

    token = ensure_token()
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"bobi app failed to start (exit {proc.returncode}) — "
                f"see {_log_path()}"
            )
        port = _read_int(_port_path())
        if port and _ping(port, token):
            url = app_url(port, token)
            if open_browser:
                _open_browser(url)
            return AppStatus(running=True, pid=proc.pid, port=port, url=url)
        time.sleep(0.2)
    raise RuntimeError(
        f"bobi app did not become ready within {START_TIMEOUT:.0f}s — "
        f"see {_log_path()}"
    )


def stop() -> AppStatus:
    """Stop the daemon; returns the pre-stop status."""
    import signal

    pid = _read_int(_pid_path())
    if not _pid_alive(pid):
        _pid_path().unlink(missing_ok=True)
        _port_path().unlink(missing_ok=True)
        return AppStatus(running=False)
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not _pid_alive(pid):
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
    _pid_path().unlink(missing_ok=True)
    _port_path().unlink(missing_ok=True)
    return AppStatus(running=False, pid=pid)


def run_foreground() -> int:
    """The daemon child (`bobi app run`): bind loopback, serve until stopped.

    Also usable directly in a terminal for development."""
    import socket

    import uvicorn

    from bobi.webapp.server import build_app

    port = configured_port()
    token = ensure_token()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as e:
        print(f"bobi app: cannot bind 127.0.0.1:{port} ({e}). "
              f"Set BOBI_APP_PORT to use another port.", file=sys.stderr)
        return 1
    bound = sock.getsockname()[1]
    _port_path().write_text(str(bound))
    _pid_path().write_text(str(os.getpid()))

    app = build_app(token=token)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        _port_path().unlink(missing_ok=True)
        _pid_path().unlink(missing_ok=True)
    return 0


def _open_browser(url: str) -> None:
    import threading
    import webbrowser

    threading.Timer(0.3, lambda: webbrowser.open(url)).start()
