"""The process table, and stopping a process tree by walking it.

A signal to one process does not stop what it started, and neither does a
signal to its process group: a tool command under the claude CLI sits in its own
session and group (#1122). The parent chain is the one link that reaches all of
it, so a tree is found by walking that chain, and each member is signalled by
pid.

A pid alone does not identify a process. Pids wrap, and a restart reassigns all
of them while old records still name them. So every process carries a start
token, compared only for equality: ``proc:<boot_id>:<starttime>`` from ``/proc``,
``ps:<lstart>`` from ``ps`` where there is no ``/proc``. Tokens from different
sources never match.

Standard library only, so ``bobi.sdk`` can import this without a cycle.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Collection, Iterable

log = logging.getLogger(__name__)

PROC_ROOT = Path("/proc")

# What verify() answers.
VERIFIED = "verified"
GONE = "gone"
UNKNOWN = "unknown"

# How long a tree gets to act on SIGTERM, then on SIGKILL. The claude CLI takes
# about 2.5s to exit on SIGTERM.
TERM_GRACE = 10.0
KILL_GRACE = 5.0
_POLL_SECONDS = 0.05

# A pid file is stale when its process started this long after the file's last
# write, since every writer writes after its process starts. The margin is wide
# on purpose: a clock step moves start times computed from btime, a false stale
# could stop the event bus, and a false valid spares one process and is reported.
_STALE_PID_FILE_SECONDS = 3600.0

# Why a member is still running when terminate() returns.
DID_NOT_EXIT = "did not exit after SIGKILL"
NOT_PERMITTED = "signal not permitted"
TABLE_UNAVAILABLE = "process table unavailable"


@dataclass(frozen=True)
class Proc:
    """One row of the process table."""

    pid: int
    parent: int
    state: str
    token: str
    started_at: float
    name: str


@dataclass(frozen=True)
class Member:
    """A process in a kill set, with the identity it had when it was found."""

    pid: int
    token: str
    name: str


@dataclass(frozen=True)
class Survivor:
    """A member still running when terminate() gave up on it."""

    pid: int
    name: str
    reason: str


Table = dict[int, Proc]


# --- reading the table -------------------------------------------------------

def _boot_id(proc_root: Path) -> str:
    try:
        return (proc_root / "sys" / "kernel" / "random" / "boot_id").read_text().strip()
    except OSError:
        return ""


def _boot_time(proc_root: Path = PROC_ROOT) -> float:
    try:
        for line in (proc_root / "stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def _read_stat(proc_root: Path, pid: int, boot_id: str, boot_time: float) -> Proc | None:
    """One ``/proc/<pid>/stat`` row, or None when the process has gone."""
    try:
        # errors="replace": a process can name itself anything, and one that
        # fails to decode must not read as a process that has exited.
        stat = (proc_root / str(pid) / "stat").read_text(errors="replace")
        close = stat.rfind(")")
        fields = stat[close + 2:].split()
        state, parent = fields[0], int(fields[1])
    except (OSError, ValueError, IndexError):
        return None
    # starttime is field 22 of the file, and the slice starts at field 3.
    starttime = fields[19] if len(fields) > 19 else ""
    token = f"proc:{boot_id}:{starttime}" if boot_id and starttime else ""
    started_at = 0.0
    if boot_time and starttime.isdigit():
        started_at = boot_time + int(starttime) / os.sysconf("SC_CLK_TCK")
    return Proc(pid, parent, state[0], token, started_at,
                stat[stat.find("(") + 1:close])


def _read_proc(proc_root: Path) -> Table:
    try:
        pids = [int(e.name) for e in proc_root.iterdir() if e.name.isdigit()]
    except OSError:
        return {}
    boot_id = _boot_id(proc_root)
    boot_time = _boot_time(proc_root)
    rows = (_read_stat(proc_root, pid, boot_id, boot_time) for pid in pids)
    return {row.pid: row for row in rows if row}


def _parse_ps_line(line: str) -> Proc | None:
    parts = line.split(None, 8)
    if len(parts) < 8:
        return None
    lstart = " ".join(parts[3:8])
    try:
        started_at = datetime.strptime(
            lstart, "%a %b %d %H:%M:%S %Y",
        ).replace(tzinfo=timezone.utc).timestamp()
        pid, parent = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    name = Path(parts[8].strip()).name if len(parts) > 8 else ""
    return Proc(pid, parent, parts[2][0], f"ps:{lstart}", started_at, name)


def _read_ps(*selector: str) -> Table | None:
    """The table from ``ps``, or None when ``ps`` cannot answer."""
    try:
        output = subprocess.check_output(
            ["ps", *selector, "-o", "pid=", "-o", "ppid=", "-o", "stat=",
             "-o", "lstart=", "-o", "comm="],
            encoding="utf-8", errors="replace", timeout=5,
            stderr=subprocess.DEVNULL,
            # lstart is the token, so its text must not vary with the caller.
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    rows = (_parse_ps_line(line) for line in output.splitlines())
    return {row.pid: row for row in rows if row} or None


def read_table(proc_root: Path = PROC_ROOT) -> Table | None:
    """Every process on the host, or None when no source could be read."""
    return (_read_proc(proc_root) if proc_root.is_dir() else None) or _read_ps("-A")


def start_token(pid: int) -> str:
    """The start token of *pid*, or "" when it cannot be read."""
    if pid <= 0:
        return ""
    if PROC_ROOT.is_dir():
        proc = _read_stat(PROC_ROOT, pid, _boot_id(PROC_ROOT), 0.0)
    else:
        proc = (_read_ps("-p", str(pid)) or {}).get(pid)
    return proc.token if proc else ""


# --- questions about a process -----------------------------------------------

def verify(table: Table | None, pid: int, token: str) -> str:
    """Whether *pid* is still the process *token* recorded.

    ``GONE`` covers a missing process, a zombie and a recycled pid. With no
    token to compare, or no table to compare it with, the answer is ``UNKNOWN``.
    """
    if table is None:
        return UNKNOWN
    proc = table.get(pid)
    if proc is None or proc.state == "Z":
        return GONE
    if not token or not proc.token:
        return UNKNOWN
    return VERIFIED if proc.token == token else GONE


def is_self_or_ancestor(table: Table | None, pid: int, *,
                        start: int | None = None) -> bool:
    """Whether *pid* is *start* (default: this process) or up its parent chain."""
    if start is None:
        if pid in (os.getpid(), os.getppid()):
            return True
        start = os.getpid()
    seen: set[int] = set()
    current = start
    while current > 0 and current not in seen:
        if current == pid:
            return True
        seen.add(current)
        proc = (table or {}).get(current)
        current = proc.parent if proc else 0
    return False


def protected_pids(files: Iterable[Path], table: Table | None) -> dict[int, str]:
    """Pids named by a valid pid file, each mapped to the file's name.

    A file is ignored as stale only if it predates this boot, or its process
    started well after its last write. With no table neither can be shown, so
    the file counts: wrongly sparing a process beats wrongly stopping a daemon.
    """
    boot_time = _boot_time()
    if not boot_time and table and 1 in table:
        boot_time = table[1].started_at
    protected: dict[int, str] = {}
    for path in files:
        try:
            pid = int(path.read_text().strip())
            written = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if pid <= 0:
            continue
        proc = table.get(pid) if table is not None else None
        if table is not None and (proc is None or proc.state == "Z"):
            continue
        if boot_time and written < boot_time:
            continue
        if proc and proc.started_at > written + _STALE_PID_FILE_SECONDS:
            continue
        protected[pid] = path.name
    return protected


# --- stopping a tree ---------------------------------------------------------

def _walk(table: Table, roots: Iterable[int],
          protected: Collection[int]) -> list[Member]:
    """*roots* and their live descendants, breadth-first.

    The walk stops at a protected pid: neither it nor anything under it is a
    member. A zombie is not a member either, since it has already stopped.
    """
    children: dict[int, list[int]] = {}
    for proc in table.values():
        children.setdefault(proc.parent, []).append(proc.pid)

    members: list[Member] = []
    queue = deque(roots)
    seen = set(queue)
    while queue:
        proc = table.get(queue.popleft())
        if proc is None or proc.state == "Z":
            continue
        members.append(Member(proc.pid, proc.token, proc.name))
        for child in children.get(proc.pid, ()):
            if child not in seen and child not in protected:
                seen.add(child)
                queue.append(child)
    return members


def kill_set(table: Table, root: int, protected: Collection[int]) -> list[Member]:
    """*root*, then every live descendant, breadth-first."""
    return _walk(table, [root], protected)


def _has_exited(member: Member, table: Table) -> bool:
    proc = table.get(member.pid)
    return proc is None or proc.state == "Z" or proc.token != member.token


def _send(pid: int, sig: signal.Signals) -> None:
    """Deliver one signal. The one seam a test patches to force a survivor."""
    os.kill(pid, sig)


def signal_unverified(pid: int) -> None:
    """One SIGTERM to a process whose identity could not be checked, and no more."""
    try:
        _send(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def describe(pid: int, name: str) -> str:
    """How a process reads in a log line: ``pid 12345 (pytest)``."""
    return f"pid {pid} ({name})" if name else f"pid {pid}"


def count(number: int) -> str:
    """``1 process``, ``4 processes``."""
    return f"{number} process" if number == 1 else f"{number} processes"


def terminate(
    members: Iterable[Member],
    *,
    term_grace: float = TERM_GRACE,
    kill_grace: float = KILL_GRACE,
    protected: Collection[int] = (),
    before_signal: Callable[[list[Member]], None] | None = None,
    label: str = "process tree",
) -> list[Survivor]:
    """Stop *members*: SIGTERM in order, wait, SIGKILL what is left, wait again.

    The table is re-read on every poll, and a new descendant of a member still
    alive joins the set, unless it is *protected*. *before_signal* runs with each
    batch of members before any of them is signalled, so a caller can record
    what it is about to stop while the records can still say so.

    Returns the members still running at the end. A failed table read proves
    nothing either way, so a member that could not be confirmed gone is a
    survivor, and SIGKILL is not sent to a pid whose identity cannot be checked.

    An interrupt at any point, *before_signal* included, sends SIGKILL to every
    live member at once and then propagates.
    """
    live: dict[int, Member] = {}
    denied: set[int] = set()
    table_readable = True

    def send(member: Member, sig: signal.Signals) -> None:
        try:
            _send(member.pid, sig)
        except ProcessLookupError:
            pass  # already gone; the next read confirms it
        except PermissionError:
            denied.add(member.pid)

    def admit(batch: list[Member], sig: signal.Signals) -> None:
        live.update((member.pid, member) for member in batch)
        if before_signal:
            before_signal(batch)
        for member in batch:
            send(member, sig)

    def settle(sig: signal.Signals, grace: float) -> None:
        nonlocal table_readable
        deadline = time.monotonic() + grace
        while True:
            table = read_table()
            table_readable = table is not None
            if table is not None:
                for member in list(live.values()):
                    if _has_exited(member, table):
                        del live[member.pid]
                if sig == signal.SIGKILL:
                    # On every read, not just the first: each one has just
                    # checked who is still ours, and one failed read must not
                    # cost a process its SIGKILL.
                    for member in live.values():
                        send(member, sig)
                new = [m for m in _walk(table, list(live), protected)
                       if m.pid not in live]
                if new:
                    admit(new, sig)
            if not live or time.monotonic() >= deadline:
                return
            time.sleep(_POLL_SECONDS)

    try:
        admit(list(members), signal.SIGTERM)
        settle(signal.SIGTERM, term_grace)
        if live:
            log.warning(
                "%s: %s ignored SIGTERM for %gs, sending SIGKILL: %s",
                label, count(len(live)), term_grace,
                ", ".join(describe(m.pid, m.name) for m in live.values()),
            )
            settle(signal.SIGKILL, kill_grace)
    except BaseException:
        table = read_table()
        for member in live.values():
            if table is not None and not _has_exited(member, table):
                send(member, signal.SIGKILL)
        raise

    def reason(pid: int) -> str:
        if pid in denied:
            return NOT_PERMITTED
        return DID_NOT_EXIT if table_readable else TABLE_UNAVAILABLE

    return [Survivor(m.pid, m.name, reason(m.pid)) for m in live.values()]
