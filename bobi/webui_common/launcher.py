"""Shared launchers for Bobi's local web UIs.

`serve_local` and `serve_container` are the two whole-launch entry points.
Below them sit the primitives they are built from — `serve_socket`,
`run_server`, `write_secret`, `open_browser_soon` — which exist as their own
names because a launcher that cannot use either entry point should still
compose the shared contract rather than re-solve it. The webapp daemon is
that caller: it needs a fixed port and pid/port files written between bind
and serve, so it binds its own socket but shares everything else.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import secrets
import socket
import threading
import webbrowser

from fastapi import FastAPI
import uvicorn

from bobi.fsutil import atomic_write_text

AppFactory = Callable[[str], FastAPI]


def _new_secret() -> str:
    return secrets.token_urlsafe(24)


def serve_socket(host: str, port: int) -> socket.socket:
    """A bound, reusable listening socket for *host* — IPv6 when *host* is one.

    Raises `OSError` if the port is taken; the caller owns closing it.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    return sock


def run_server(app: FastAPI, sock: socket.socket) -> None:
    """Serve *app* on the already-bound *sock* until it stops.

    The server is attached to `app.state.uvicorn_server` so an app can end
    its own process cleanly (setup's "Close & end setup" button posts
    /api/shutdown, which flips `should_exit`). Ctrl-C is a normal way to
    stop a foreground UI, not an error. Closing *sock* is the caller's job:
    it may have other teardown to sequence around it.
    """
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    app.state.uvicorn_server = server
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass


def write_secret(path: Path) -> str:
    """Mint a launch secret, persist it at *path*, and lock it to 0600.

    The chmod is best-effort: on a filesystem that has no POSIX modes the
    token is still usable, and the loopback Host guard — not the token — is
    the primary boundary for every local UI.
    """
    secret = _new_secret()
    path.write_text(secret)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret


def open_browser_soon(url: str, *, delay: float = 0.5) -> None:
    """Open *url* after *delay*, giving the server time to start listening."""
    threading.Timer(delay, lambda: webbrowser.open(url)).start()


def serve_local(
    app_factory: AppFactory,
    *,
    open_browser: bool = True,
    label: str = "web UI",
) -> int:
    """Run a local web UI on `127.0.0.1:0` in the foreground."""
    secret = _new_secret()
    sock = serve_socket("127.0.0.1", 0)
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/?n={secret}"

    app = app_factory(secret)
    if open_browser:
        open_browser_soon(url)
    print(f"\n  {label} is running at {url}\n  (Ctrl-C to stop)\n")

    try:
        run_server(app, sock)
    finally:
        sock.close()
    return 0


def serve_container(
    app_factory: AppFactory,
    *,
    state_dir: Path,
    host: str | None = None,
    port: int | None = None,
) -> int:
    """Run a web UI in a daemon thread and write the container tunnel contract."""
    bind_host = host if host is not None else os.environ.get("BOBI_UI_HOST", "::")
    bind_port = port if port is not None else int(os.environ.get("BOBI_UI_PORT", "8080"))

    token = os.environ.get("BOBI_UI_TOKEN", "")
    if not token:
        token = write_secret(state_dir / "ui.token")

    sock = serve_socket(bind_host, bind_port)
    bound_port = sock.getsockname()[1]
    atomic_write_text(state_dir / "ui.port", str(bound_port))

    app = app_factory(token)
    threading.Thread(
        target=lambda: run_server(app, sock),
        daemon=True,
        name="agent-ui",
    ).start()
    return bound_port
