"""Shared launchers for Bobi's local web UIs."""

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

AppFactory = Callable[[str], FastAPI]
Announcer = Callable[[str], str]


def _new_secret() -> str:
    return secrets.token_urlsafe(24)


def _serve_socket(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    return sock


def serve_local(
    app_factory: AppFactory,
    *,
    open_browser: bool = True,
    label: str = "web UI",
    announce: Announcer | None = None,
) -> int:
    """Run a local web UI on `127.0.0.1:0` in the foreground."""
    secret = _new_secret()
    sock = _serve_socket("127.0.0.1", 0)
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/?n={secret}"

    app = app_factory(secret)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    # Let the app end its own process cleanly (e.g. setup's "Close & end
    # setup" button posts /api/shutdown, which flips should_exit).
    app.state.uvicorn_server = server

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    if announce is not None:
        print(announce(url))
    else:
        print(f"\n  {label} is running at {url}\n  (Ctrl-C to stop)\n")

    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
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
        token = _new_secret()
        tok_file = state_dir / "ui.token"
        tok_file.write_text(token)
        try:
            os.chmod(tok_file, 0o600)
        except OSError:
            pass

    sock = _serve_socket(bind_host, bind_port)
    bound_port = sock.getsockname()[1]
    (state_dir / "ui.port").write_text(str(bound_port))

    app = app_factory(token)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))

    threading.Thread(
        target=lambda: server.run(sockets=[sock]),
        daemon=True,
        name="agent-ui",
    ).start()
    return bound_port
