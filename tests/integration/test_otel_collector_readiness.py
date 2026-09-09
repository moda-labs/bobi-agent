"""The collector readiness gate, proven against docker's port-publish race (#1083).

`test_otel_collector.py` needs a real collector, so it only runs in the
`docker`-marked lane. The gate it depends on is what actually reddened main,
and that failure mode needs no collector to reproduce: a port that accepts TCP
and resets every request until a backend appears behind it. That is exactly
what `docker run -p` presents while the container boots, so these tests pin the
regression deterministically and run on every push.
"""
from __future__ import annotations

import socket
import struct
import threading

import httpx
import pytest

from .test_otel_collector import _await_ready


class _StubProc:
    """Stands in for the collector subprocess: alive unless given a returncode."""

    def __init__(self, returncode: int | None = None):
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


class _PublishedPort:
    """docker-proxy's observable behaviour, with no docker daemon involved.

    Binds the published port up front, so a bare TCP connect succeeds
    immediately. Until `become_ready()` it resets every connection, which is
    what the proxy does when its dial to the still-booting container is
    refused. After that it answers the way the collector's OTLP receiver does
    on an unmapped path, with a 404.
    """

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.port: int = self._sock.getsockname()[1]
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def become_ready(self) -> None:
        self._ready.set()

    def _serve(self) -> None:
        while not self._stopping.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                # Only a stop() teardown retires the loop. Returning on any
                # accept error would leave the port accepting from the backlog
                # with nothing answering, and the resulting timeout would blame
                # `_await_ready` for a defect in this stub.
                if self._stopping.is_set():
                    return
                continue
            with conn:
                if self._ready.is_set():
                    conn.settimeout(5.0)
                    try:
                        conn.recv(65536)
                        conn.sendall(
                            b"HTTP/1.1 404 Not Found\r\n"
                            b"Content-Length: 0\r\n"
                            b"Connection: close\r\n\r\n"
                        )
                    except OSError:
                        pass
                else:
                    # A zero linger turns close() into an RST, which is what the
                    # client sees as `[Errno 104] Connection reset by peer`.
                    conn.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                    )

    def stop(self) -> None:
        self._stopping.set()
        self._thread.join(timeout=5)
        self._sock.close()


@pytest.fixture
def published_port():
    port = _PublishedPort()
    try:
        yield port
    finally:
        port.stop()


def test_a_bare_tcp_connect_is_not_readiness(published_port):
    """The regression in miniature: the old gate's primitive passes here.

    A connect succeeding while the very next request is reset is the whole bug.
    The gate returned on the connect, the fixture yielded, and the first test to
    POST ate the reset.
    """
    with socket.socket() as probe:
        probe.settimeout(2.0)
        assert probe.connect_ex(("127.0.0.1", published_port.port)) == 0, (
            "a published-but-not-ready port must still accept a bare TCP "
            "connect, otherwise this test is not reproducing the race"
        )

    with pytest.raises(httpx.TransportError):
        httpx.post(
            f"http://127.0.0.1:{published_port.port}/v1/metrics",
            content=b"",
            headers={"Content-Type": "application/x-protobuf"},
            timeout=5.0,
        )


def test_await_ready_blocks_until_the_collector_answers(published_port):
    finished = threading.Event()
    raised: list[BaseException] = []

    def probe() -> None:
        try:
            # Under the join budget below, so a failed assertion cannot leave
            # this thread polling a port the kernel has since handed to another
            # test, turning one real failure into a second unrelated one.
            _await_ready(published_port.port, _StubProc(), timeout=10.0)
        except BaseException as exc:  # noqa: BLE001 - reported to the assertion below
            raised.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    try:
        assert not finished.wait(1.5), (
            "the readiness gate returned while the port only accepted and reset, "
            "which is the race that reddened main"
        )

        published_port.become_ready()
        assert finished.wait(15.0), "the gate never returned once the collector answered"
        assert not raised, f"the gate raised after the collector answered: {raised!r}"
    finally:
        thread.join(timeout=15)


def test_await_ready_gives_up_when_nothing_ever_answers(published_port):
    with pytest.raises(AssertionError, match="did not answer HTTP"):
        _await_ready(published_port.port, _StubProc(), timeout=2.0)


def test_await_ready_reports_a_collector_that_exited(published_port):
    with pytest.raises(AssertionError, match="exited early with 1"):
        _await_ready(published_port.port, _StubProc(returncode=1), timeout=5.0)
