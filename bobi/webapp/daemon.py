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

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from bobi import http, paths
from bobi.sdk import pid_alive, read_int_file
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
    """The persisted app token, minted on first use (0600).

    Unlike a per-launch UI secret this one is *read back* when it already
    exists — that is the whole point of persisting it, so the dashboard URL
    survives a restart. Only the mint half is shared with the other UIs.
    """
    path = _token_path()
    if path.exists():
        token = path.read_text().strip()
        if token:
            return token
    from bobi.webui_common.launcher import write_secret

    return write_secret(path)


def _ping(port: int, token: str, timeout: float = 1.0) -> bool:
    """Whether the app answers /api/ping on *port* with *token*.

    Anything short of a JSON ``{"ok": true}`` body is a no: a connection
    refused during startup polling, a rejected token, a half-built server
    that 500s. Callers use this as a liveness proof, so failure closed is
    the only safe reading.
    """
    try:
        resp = http.get(
            f"http://127.0.0.1:{port}/api/ping",
            headers={WEBUI_TOKEN_HEADER: token},
            timeout=timeout,
        )
        return bool(resp.json().get("ok"))
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
    # Set by stop() when the recorded pid is alive but did not identify itself
    # as this app, so nothing was signalled (D037).
    unverified: bool = False
    # Set by stop() when the pid is alive and ours but belongs to another uid,
    # so the signal was refused. Distinct from `unverified`: there the state
    # was stale and got cleared, here the app is genuinely still running.
    not_permitted: bool = False


def _is_this_app(pid: int) -> bool:
    """Whether the live *pid* is this bobi app and not a reused pid.

    Identity is "it answers /api/ping on the recorded port with the local
    token" — the same proof :func:`status` requires before reporting running.
    """
    port = read_int_file(_port_path())
    if not port:
        return False
    return _ping(port, ensure_token())


def status() -> AppStatus:
    """Liveness = pid alive AND the server answers /api/ping."""
    pid = read_int_file(_pid_path())
    port = read_int_file(_port_path())
    if not (pid_alive(pid) and port):
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
        port = read_int_file(_port_path())
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


def stop(*, force: bool = False) -> AppStatus:
    """Stop the daemon; returns the pre-stop status.

    A live pid is not proof the pidfile still describes THIS app. run_foreground
    only removes the pidfile on a graceful exit, so a crash leaves it pointing
    at a dead pid the OS is free to reuse — and signalling that blindly kills an
    unrelated process (D037). Identity is confirmed the same way :func:`status`
    has always confirmed it: the app answers /api/ping on the recorded port with
    the local token. ``force`` skips the check for an app wedged past answering.
    """
    import signal

    pid = read_int_file(_pid_path())
    if not pid_alive(pid):
        _pid_path().unlink(missing_ok=True)
        _port_path().unlink(missing_ok=True)
        return AppStatus(running=False)
    if not force and not _is_this_app(pid):
        # Alive, but not ours to kill. Clear the stale state so the next
        # `bobi app start` is not blocked by it, and signal nothing.
        _pid_path().unlink(missing_ok=True)
        _port_path().unlink(missing_ok=True)
        return AppStatus(running=False, pid=pid, unverified=True)
    # `pid_alive` counts EPERM as alive — a pid we may not signal is
    # emphatically not free for the OS to hand out — so the kill below is the
    # first place a uid mismatch can surface (an app once started via sudo, or
    # a BOBI_HOME shared into a container). Report it instead of tracebacking,
    # and leave the pid/port files alone: they still describe a live app, so
    # clearing them would let the next `start` spawn a second daemon.
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not pid_alive(pid):
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
    except PermissionError:
        return AppStatus(running=False, pid=pid, not_permitted=True)
    _pid_path().unlink(missing_ok=True)
    _port_path().unlink(missing_ok=True)
    return AppStatus(running=False, pid=pid)


def run_foreground() -> int:
    """The daemon child (`bobi app run`): bind loopback, serve until stopped.

    This binds its own socket rather than going through `serve_local`: the
    port is fixed (not ephemeral) and the pid/port files have to be written
    between bind and serve, so `start` can find the daemon. Everything either
    side of that is the shared launch contract.

    Also usable directly in a terminal for development."""
    from bobi.webapp.server import build_app
    from bobi.webui_common.launcher import run_server, serve_socket

    port = configured_port()
    token = ensure_token()

    try:
        sock = serve_socket("127.0.0.1", port)
    except OSError as e:
        print(f"bobi app: cannot bind 127.0.0.1:{port} ({e}). "
              f"Set BOBI_APP_PORT to use another port.", file=sys.stderr)
        return 1
    bound = sock.getsockname()[1]
    _port_path().write_text(str(bound))
    _pid_path().write_text(str(os.getpid()))

    try:
        run_server(build_app(token=token), sock)
    finally:
        sock.close()
        _port_path().unlink(missing_ok=True)
        _pid_path().unlink(missing_ok=True)
    return 0


def _open_browser(url: str) -> None:
    from bobi.webui_common.launcher import open_browser_soon

    open_browser_soon(url, delay=0.3)
