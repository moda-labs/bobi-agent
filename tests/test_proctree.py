"""``bobi.proctree`` against real processes: V1 to V10 of the #1122 plan.

What is alive is judged by ``tests/process_tree.py``, never by the module under
test. Every signal goes to a process the test started itself, each in a session
of its own, so a mutant that signals by group or session stays inside the test's
own processes. The parsing rows feed canned input and never reach ``terminate``.
"""

from __future__ import annotations

import errno
import itertools
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bobi import proctree
from bobi.proctree import (
    DID_NOT_EXIT,
    GONE,
    NOT_PERMITTED,
    TABLE_UNAVAILABLE,
    UNKNOWN,
    VERIFIED,
    Member,
    Proc,
    Survivor,
)
from tests.process_tree import ROLES, pid_running

SIGTERM, SIGKILL = signal.SIGTERM, signal.SIGKILL
PID_FILES = ["manager.pid", "event-server.pid", "embedding-sidecar.pid", "app.pid"]
# Long enough to see a tick: the command writes one every 50ms.
QUIET = 0.3


# --- real processes ----------------------------------------------------------

def _start_leaf(tree, *, on_term: str = "die") -> int:
    """One real process with no children: the fixture's command role on its own."""
    tree.root = subprocess.Popen(
        [sys.executable, str(tree.script),
         "--role", "command", "--on-term", on_term],
        start_new_session=True, stdin=subprocess.DEVNULL,
    )
    tree.wait_for(("command",))
    return tree.root.pid


def _make_zombie(tree) -> tuple[int, str]:
    """A child killed and not waited on. Returns its pid and its token from life."""
    pid = _start_leaf(tree)
    token = proctree.start_token(pid)
    tree.root.kill()
    _wait_until(lambda: _os_row(pid)[0] == "Z", f"pid {pid} is not a zombie")
    return pid, token


def _reaped_pid(tree) -> int:
    """The pid of a process that has fully gone."""
    pid = _start_leaf(tree)
    tree.root.kill()
    tree.root.wait()
    return pid


# One process with the fixture's roles as its children, all in its session and
# group. ``runner`` brings the tree under it, and any other role is a leaf.
_HOST = r"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

script, on_term, *roles = sys.argv[1:]
if on_term == "ignore":
    # A handler, not SIG_IGN: exec resets a handler, and children keep SIG_IGN.
    signal.signal(signal.SIGTERM, lambda *_: None)
temp = Path(script).with_name("host.pid.tmp")
temp.write_text(str(os.getpid()))
os.replace(temp, temp.with_name("host.pid"))
children = [subprocess.Popen([sys.executable, script, "--role", role],
                             stdin=subprocess.DEVNULL)
            for role in roles]
time.sleep(120)
"""


def _start_host(tree, *roles: str, on_term: str = "die") -> dict[str, int]:
    """Start ``_HOST`` as a session leader. Returns every pid by role."""
    tree.root = subprocess.Popen(
        [sys.executable, "-c", _HOST, str(tree.script), on_term, *roles],
        start_new_session=True, stdin=subprocess.DEVNULL,
    )
    return tree.wait_for(("host", *ROLES, *roles))


def _tree_members(root: int) -> list[Member]:
    return proctree.kill_set(proctree.read_table(), root, ())


def _os_row(pid: int) -> tuple[str, int] | None:
    """(state, parent) read from the OS, not through bobi.proctree. None if gone."""
    if not Path("/proc/self/stat").exists():
        fields = subprocess.run(
            ["ps", "-o", "stat=", "-o", "ppid=", "-p", str(pid)],
            capture_output=True, text=True,
        ).stdout.split()
        return (fields[0][0], int(fields[1])) if fields else None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(errors="replace")
    except OSError:
        return None
    state, parent = stat[stat.rfind(")") + 2:].split()[:2]
    return state, int(parent)


def _parent_chain(pid: int) -> list[int]:
    """*pid* and each process up its parent chain, read from the OS."""
    chain = []
    while pid > 0:
        chain.append(pid)
        pid = _os_row(pid)[1]
    return chain


def _wait_until(condition, failure: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"{failure} after {timeout}s"
        time.sleep(0.02)


def _interrupt(_batch) -> None:
    raise KeyboardInterrupt


def _script_table_reads(monkeypatch, script: dict[int, object]) -> None:
    """Script chosen calls to ``read_table``, counting from 1.

    ``None`` is a failed read and an exception type is raised. Every other call
    reads the real table.
    """
    real_read_table = proctree.read_table
    calls = itertools.count(1)

    def read_table():
        call = next(calls)
        if call not in script:
            return real_read_table()
        if script[call] is None:
            return None
        raise script[call]

    monkeypatch.setattr(proctree, "read_table", read_table)


# --- V1, V2: the whole tree stops ----------------------------------------------

def test_terminate_stops_the_whole_tree(process_tree, signal_seam):
    """V1: the walk finds a command in its own session and group, and stops it.

    The root gets SIGTERM first and ``before_signal`` runs before any signal, so
    a caller's records are written while they can still say so.
    """
    tree = process_tree()
    root = tree.start()
    tree.assert_all_running()
    pids = [tree.pids()[role] for role in ROLES]
    members = _tree_members(root)
    assert [member.pid for member in members] == pids

    batches = []
    survivors = proctree.terminate(
        members,
        before_signal=lambda batch: batches.append(
            ([member.pid for member in batch], list(signal_seam.sent))),
    )

    assert survivors == []
    assert batches == [(pids, [])]
    assert signal_seam.sent == [(pid, SIGTERM) for pid in pids]
    tree.assert_stopped()


def test_member_ignoring_sigterm_is_killed(process_tree, signal_seam, caplog):
    """V2: what ignores SIGTERM gets SIGKILL once the grace is up, and it is named."""
    tree = process_tree()
    root = tree.start(on_term="ignore")
    command = tree.pids()["command"]

    with caplog.at_level(logging.WARNING, logger="bobi.proctree"):
        survivors = proctree.terminate(
            _tree_members(root), term_grace=0.3, kill_grace=10.0, label="wf-x",
        )

    assert survivors == []
    sent = signal_seam.sent
    assert sent.index((command, SIGTERM)) < sent.index((command, SIGKILL))
    tree.assert_stopped(quiet_for=QUIET)
    assert re.search(
        rf"wf-x: \d+ process(es)? ignored SIGTERM for 0\.3s, sending SIGKILL: "
        rf".*pid {command} \(",
        caplog.text,
    )


# --- V3: only descendants, never the caller's own chain --------------------------

def test_sibling_in_the_roots_session_and_group_survives(process_tree):
    """V3, I1: a tree is found by parent, never by session or group."""
    tree = process_tree()
    pids = _start_host(tree, "runner", "sibling")
    root, sibling = pids["runner"], pids["sibling"]
    assert os.getsid(sibling) == os.getsid(root)
    assert os.getpgid(sibling) == os.getpgid(root)
    members = _tree_members(root)
    assert [member.pid for member in members] == [pids[role] for role in ROLES]

    assert proctree.terminate(members) == []

    assert sorted(tree.running()) == ["host", "sibling"]


def test_is_self_or_ancestor_follows_the_real_parent_chain(process_tree):
    """V3, I5: true all the way up the chain, and for nothing off it."""
    pids = _start_host(process_tree(), "runner", "sibling")
    command, sibling = pids["command"], pids["sibling"]
    table = proctree.read_table()

    for pid in _parent_chain(os.getpid()):
        assert proctree.is_self_or_ancestor(table, pid), f"pid {pid} is above this test"

    chain = _parent_chain(command)
    assert chain[:6] == [command, pids["shell"], pids["cli"], pids["runner"],
                         pids["host"], os.getpid()]
    for pid in chain:
        assert proctree.is_self_or_ancestor(table, pid, start=command), f"pid {pid}"

    assert not proctree.is_self_or_ancestor(table, sibling)
    assert not proctree.is_self_or_ancestor(table, sibling, start=command)
    # A descendant is not an ancestor.
    assert not proctree.is_self_or_ancestor(table, command)
    assert not proctree.is_self_or_ancestor(table, command, start=pids["runner"])


def test_is_self_or_ancestor_with_no_usable_table():
    """Self and parent need no table, and without one nothing further is claimed."""
    me, parent = os.getpid(), os.getppid()
    elsewhere = max(me, parent) + 1

    assert proctree.is_self_or_ancestor(None, me)
    assert proctree.is_self_or_ancestor(None, parent)
    assert not proctree.is_self_or_ancestor(None, elsewhere)
    assert proctree.is_self_or_ancestor(None, elsewhere, start=elsewhere)
    assert not proctree.is_self_or_ancestor(None, me, start=elsewhere)


def test_a_cycle_in_the_table_ends_the_walk():
    """A table is read row by row, so a recycled pid can show a parent loop."""
    cycle = {10: Proc(10, 20, "S", "", 0.0, ""), 20: Proc(20, 10, "S", "", 0.0, "")}

    assert not proctree.is_self_or_ancestor(cycle, 30, start=10)
    assert [member.pid for member in proctree.kill_set(cycle, 10, ())] == [10, 20]


# --- V4: identity --------------------------------------------------------------

def test_verify_compares_the_token_and_signals_nothing(
        process_tree, signal_seam, tmp_path):
    """V4, I2: only an equal token verifies, and a mismatch leaves the process be."""
    alive = _start_leaf(process_tree("alive"))
    zombie, zombie_token = _make_zombie(process_tree("zombie"))
    reaped = _reaped_pid(process_tree("reaped"))
    token = proctree.start_token(alive)
    table = proctree.read_table()
    (tmp_path / "proc" / "100").mkdir(parents=True)
    (tmp_path / "proc" / "100" / "stat").write_text("100 (no start time) S 50 0\n")
    tokenless_table = proctree.read_table(tmp_path / "proc")

    assert {
        "equal token": proctree.verify(table, alive, token),
        "mismatched token": proctree.verify(table, alive, token + "0"),
        "token from the other source": proctree.verify(
            table, alive, "ps:Tue Nov 3 18:55:21 2026"),
        "process gone": proctree.verify(table, reaped, token),
        "zombie with an equal token": proctree.verify(table, zombie, zombie_token),
        "no recorded token": proctree.verify(table, alive, ""),
        "no token in the table": proctree.verify(tokenless_table, 100, token),
        "no table": proctree.verify(None, alive, token),
    } == {
        "equal token": VERIFIED,
        "mismatched token": GONE,
        "token from the other source": GONE,
        "process gone": GONE,
        "zombie with an equal token": GONE,
        "no recorded token": UNKNOWN,
        "no token in the table": UNKNOWN,
        "no table": UNKNOWN,
    }
    assert signal_seam.sent == []
    assert pid_running(alive)
    assert proctree.start_token(reaped) == ""
    assert proctree.start_token(0) == ""


@pytest.mark.parametrize("interrupted", [False, True],
                         ids=["during-the-wait", "on-an-interrupt"])
def test_member_whose_pid_changed_hands_gets_no_sigkill(
        process_tree, signal_seam, interrupted):
    """V4: the token is checked again before SIGKILL, so a recycled pid survives.

    The process ignores SIGTERM because the first SIGTERM goes out on the
    caller's own table read. Only SIGKILL compares the token again.
    """
    pid = _start_leaf(process_tree(), on_term="ignore")
    recycled = Member(pid, proctree.start_token(pid) + "0", "an earlier process")

    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            proctree.terminate([recycled], before_signal=_interrupt)
    else:
        assert proctree.terminate([recycled], term_grace=0.2, kill_grace=0.2) == []

    assert (pid, SIGKILL) not in signal_seam.sent
    assert pid_running(pid)


def test_pid_recycled_during_the_wait_gets_no_sigkill(
        process_tree, monkeypatch, signal_seam):
    """V4: every read of the SIGKILL round compares the token before it signals.

    The one edited table here: a real pid cannot be recycled on demand, so the
    real row takes a later process's token from the SIGKILL round on.
    """
    pid = _start_leaf(process_tree(), on_term="ignore")
    members = _tree_members(pid)
    real_read_table = proctree.read_table
    reads = itertools.count(1)

    def read_table():
        table = real_read_table()
        # With no grace the first read is the SIGTERM round's only poll.
        if next(reads) > 1:
            table[pid] = replace(table[pid], token="proc:a-later-process:1")
        return table

    monkeypatch.setattr(proctree, "read_table", read_table)

    assert proctree.terminate(members, term_grace=0.0, kill_grace=0.2) == []

    assert (pid, SIGKILL) not in signal_seam.sent
    assert pid_running(pid)


# --- V5, V6: what the wait has to notice -----------------------------------------

def test_zombie_root_counts_as_exited(process_tree, signal_seam):
    """V5: the root is this process's child and stays a zombie, with no escalation."""
    root = _start_leaf(process_tree())

    assert proctree.terminate(_tree_members(root), kill_grace=1.0) == []

    assert _os_row(root)[0] == "Z"
    assert signal_seam.sent == [(root, SIGTERM)]
    assert _tree_members(root) == []


def test_member_already_gone_is_not_an_error(process_tree, monkeypatch):
    """A member can exit between the table read and its signal."""
    pid = _reaped_pid(process_tree())

    def no_such_process(_pid, _sig):
        # What os.kill answers. Not sent for real: the pid is no longer this test's.
        raise ProcessLookupError

    monkeypatch.setattr(proctree, "_send", no_such_process)

    assert proctree.terminate([Member(pid, "a token", "gone")]) == []
    assert _tree_members(pid) == []


def test_child_forked_during_the_wait_is_stopped(process_tree, signal_seam):
    """V6: the command forks on SIGTERM and outlives a poll, so a re-read finds it."""
    tree = process_tree()
    root = _start_leaf(tree, on_term="fork")
    batches = []

    survivors = proctree.terminate(
        _tree_members(root),
        before_signal=lambda batch: batches.append(
            ([member.pid for member in batch], list(signal_seam.sent))),
    )

    assert survivors == []
    late = tree.pids()["late"]
    assert [pids for pids, _ in batches] == [[root], [late]]
    assert (late, SIGTERM) not in batches[1][1], "recorded only after it was signalled"
    assert (late, SIGTERM) in signal_seam.sent
    tree.assert_stopped(quiet_for=QUIET)


# --- V7: the sources of the table, and a table that cannot be read ---------------

def test_proc_stat_lines_a_process_can_cause(tmp_path):
    """V7: a process can name itself anything, and a short line must not raise."""
    root = tmp_path / "proc"
    lines = {
        100: b"100 (manager with spaces) S 50 0 0 0 0 0 0 0 0 0 0 0\n",
        101: b"101 (tool (1) x) R 50 0\n",
        102: b"102 (not utf-8 \xff) D 50 0\n",
        103: b"103 (cut short)\n",
        104: b"104 (no parent) S\n",
    }
    for pid, line in lines.items():
        (root / str(pid)).mkdir(parents=True)
        (root / str(pid) / "stat").write_bytes(line)
    (root / "self").mkdir()

    table = proctree.read_table(root)

    assert sorted(table) == [100, 101, 102]
    assert table[100] == Proc(100, 50, "S", "", 0.0, "manager with spaces")
    assert (table[101].parent, table[101].name) == (50, "tool (1) x")
    assert (table[102].state, table[102].parent) == ("D", 50)


def test_proc_stat_gives_the_token_and_start_time(tmp_path):
    """V7: the token is the boot id and field 22, which never changes in a life."""
    root = tmp_path / "proc"
    (root / "100").mkdir(parents=True)
    # Every field its own value, so only field 22 can land in the token.
    fields = ["S", "50", *map(str, range(5, 22)), "7700", "23", "24"]
    (root / "100" / "stat").write_text(f"100 (x) {' '.join(fields)}\n")
    (root / "sys" / "kernel" / "random").mkdir(parents=True)
    (root / "sys" / "kernel" / "random" / "boot_id").write_text("boot-1\n")
    (root / "stat").write_text("cpu  1 2 3\nbtime 1000\nprocesses 5\n")

    assert proctree.read_table(root) == {
        100: Proc(100, 50, "S", "proc:boot-1:7700",
                  1000 + 7700 / os.sysconf("SC_CLK_TCK"), "x"),
    }


@pytest.fixture
def fake_ps(tmp_path, monkeypatch):
    """Put a ``ps`` script with this body first on PATH. None leaves no ``ps`` at all."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def install(body: str | None) -> None:
        if body is None:
            monkeypatch.setenv("PATH", str(bin_dir))
            return
        script = bin_dir / "ps"
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    return install


_CANNED_PS = """\
      1       0 Ss   Tue Nov  3 18:55:21 2026 /sbin/init
    100       1 S+   Wed Nov  4 04:00:00 2026 /usr/local/bin/my tool
    200     100 Z    Wed Nov  4 04:00:01 2026 python3
    300     100 R    Wed Nov  4 04:00:02 2026
not a process table row
    abc       1 S    Wed Nov  4 04:00:03 2026 bad-pid
    400       1 S    Wed Nov 99 04:00:04 2026 bad-date
"""


def _utc(day: int, hour: int, minute: int, second: int) -> float:
    return datetime(2026, 11, day, hour, minute, second,
                    tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("proc_root", ["no-proc", "empty-proc"])
def test_ps_source_parses_its_output(tmp_path, monkeypatch, fake_ps, proc_root):
    """V7: with no ``/proc`` to read, and whatever the caller's locale and zone."""
    seen_by_ps = tmp_path / "seen-by-ps"
    fake_ps(f"""printf '%s %s' "$LC_ALL" "$TZ" > '{seen_by_ps}'
cat <<'EOF'
{_CANNED_PS}EOF""")
    (tmp_path / "empty-proc").mkdir()
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("TZ", "Asia/Tokyo")

    assert proctree.read_table(tmp_path / proc_root) == {
        1: Proc(1, 0, "S", "ps:Tue Nov 3 18:55:21 2026", _utc(3, 18, 55, 21), "init"),
        100: Proc(100, 1, "S", "ps:Wed Nov 4 04:00:00 2026", _utc(4, 4, 0, 0), "my tool"),
        200: Proc(200, 100, "Z", "ps:Wed Nov 4 04:00:01 2026", _utc(4, 4, 0, 1), "python3"),
        300: Proc(300, 100, "R", "ps:Wed Nov 4 04:00:02 2026", _utc(4, 4, 0, 2), ""),
    }
    assert seen_by_ps.read_text() == "C UTC"

    monkeypatch.setattr(proctree, "PROC_ROOT", tmp_path / "no-proc")
    assert proctree.start_token(100) == "ps:Wed Nov 4 04:00:00 2026"
    assert proctree.start_token(999) == ""


@pytest.mark.parametrize(
    "body",
    ["exit 1", "exit 0", "echo not a process table", "exec sleep 60", None],
    ids=["fails", "no-output", "no-rows", "hangs-past-its-5s-cap", "not-installed"],
)
def test_ps_that_cannot_answer_gives_no_table(tmp_path, fake_ps, body):
    """V7: no table, which is unknown, and never an empty table, which is all gone."""
    fake_ps(body)

    assert proctree.read_table(tmp_path / "no-proc") is None


@pytest.fixture
def canned_proc(tmp_path):
    root = tmp_path / "proc"
    fields = ["S", "50", *map(str, range(5, 22)), "7700", "23", "24"]
    for pid in (100, 200):
        (root / str(pid)).mkdir(parents=True)
        (root / str(pid) / "stat").write_text(f"{pid} (x) {' '.join(fields)}\n")
    (root / "sys" / "kernel" / "random").mkdir(parents=True)
    (root / "sys" / "kernel" / "random" / "boot_id").write_text("boot-1\n")
    return root


def _fail_reads(patch, suffix: str, code: int) -> None:
    """Make a read of any file whose path ends in *suffix* fail with errno *code*."""
    real_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if str(path).endswith(suffix):
            raise OSError(code, os.strerror(code))
        return real_read_text(path, *args, **kwargs)

    patch.setattr(Path, "read_text", read_text)


@pytest.mark.parametrize("failure", ["listing", "one-row", "boot-id", "every-row-gone"])
def test_failed_proc_read_gives_no_table_and_never_asks_ps(
        canned_proc, monkeypatch, fake_ps, failure):
    """V7: a hole in the table reads as a process gone, and so would every ``ps`` token."""
    fake_ps(f"cat <<'EOF'\n{_CANNED_PS}EOF")

    def listing_fails(_path):
        raise OSError(errno.EMFILE, os.strerror(errno.EMFILE))

    if failure == "every-row-gone":
        for pid in (100, 200):
            (canned_proc / str(pid) / "stat").unlink()
    with monkeypatch.context() as patch:
        if failure == "listing":
            patch.setattr(Path, "iterdir", listing_fails)
        elif failure == "one-row":
            _fail_reads(patch, "200/stat", errno.EMFILE)
        elif failure == "boot-id":
            _fail_reads(patch, "boot_id", errno.EMFILE)
        assert proctree.read_table(canned_proc) is None


@pytest.mark.parametrize("code", [errno.ENOENT, errno.ESRCH, errno.EACCES],
                         ids=["exited", "exiting", "not-ours-to-read"])
def test_proc_row_that_is_gone_or_hidden_leaves_the_rest_of_the_table(
        canned_proc, monkeypatch, code):
    """V7: a process can exit mid-read, and hidepid hides another user's."""
    with monkeypatch.context() as patch:
        _fail_reads(patch, "200/stat", code)
        assert sorted(proctree.read_table(canned_proc)) == [100]


@pytest.mark.parametrize("code", [errno.ENOENT, errno.EACCES], ids=["missing", "masked"])
def test_boot_id_the_host_does_not_offer_means_no_tokens(canned_proc, monkeypatch, code):
    """V7: no identity on this host, which is C4, and not a failed read."""
    assert {row.token for row in proctree.read_table(canned_proc).values()} == {
        "proc:boot-1:7700"}
    with monkeypatch.context() as patch:
        _fail_reads(patch, "boot_id", code)
        assert {row.token for row in proctree.read_table(canned_proc).values()} == {""}


def test_start_token_of_a_failed_read_is_empty(canned_proc, monkeypatch):
    """V7: a registry write must not fail because a token could not be read."""
    monkeypatch.setattr(proctree, "PROC_ROOT", canned_proc)
    assert proctree.start_token(100) == "proc:boot-1:7700"
    with monkeypatch.context() as patch:
        _fail_reads(patch, "100/stat", errno.EMFILE)
        assert proctree.start_token(100) == ""


@pytest.mark.skipif(shutil.which("ps") is None,
                    reason="no ps on this host; the CI runners have one")
def test_ps_source_stops_a_real_tree(process_tree, tmp_path, monkeypatch, signal_seam):
    """V7: kill set, tokens and the wait, all from a real ``ps``."""
    tree = process_tree()
    root = tree.start(on_term="ignore")
    pids = tree.pids()
    no_proc = tmp_path / "no-proc"
    real_read_table = proctree.read_table
    monkeypatch.setattr(proctree, "read_table", lambda: real_read_table(no_proc))
    monkeypatch.setattr(proctree, "PROC_ROOT", no_proc)

    table = proctree.read_table()
    members = proctree.kill_set(table, root, ())

    assert [member.pid for member in members] == [pids[role] for role in ROLES]
    assert all(member.token.startswith("ps:") for member in members)
    assert proctree.start_token(root) == table[root].token
    # A clock read in the wrong zone would be off by hours, and the 1h stale-file
    # rule would then read a live daemon's pid file as stale.
    assert abs(table[root].started_at - time.time()) < 300

    assert proctree.terminate(members, term_grace=0.3, kill_grace=10.0) == []

    assert (pids["command"], SIGKILL) in signal_seam.sent
    tree.assert_stopped(quiet_for=QUIET)


def test_failed_table_read_never_counts_as_exited(
        process_tree, monkeypatch, signal_seam):
    """V7: every member stays a survivor, and SIGKILL waits for a checkable identity."""
    tree = process_tree()
    root = tree.start(on_term="ignore")
    pids = tree.pids()
    members = _tree_members(root)
    # terminate reads no table before its first signal, so this is every read after it.
    monkeypatch.setattr(proctree, "read_table", lambda: None)

    survivors = proctree.terminate(members, term_grace=0.2, kill_grace=0.2)

    assert survivors == [Survivor(member.pid, member.name, TABLE_UNAVAILABLE)
                         for member in members]
    assert signal_seam.sent == [(pids[role], SIGTERM) for role in ROLES]
    assert "command" in tree.running()


@pytest.mark.parametrize(
    ("failed_read", "term_grace"),
    [
        pytest.param(1, 0.2, id="during-the-wait"),
        # With no grace the first read is the SIGTERM round's only poll, so the
        # second is the first read of the SIGKILL round.
        pytest.param(2, 0.0, id="at-the-escalation"),
    ],
)
def test_table_read_that_fails_once_still_ends_in_sigkill(
        process_tree, monkeypatch, signal_seam, failed_read, term_grace):
    """V7: one failed read costs a poll. It must not cost the SIGKILL."""
    tree = process_tree()
    pid = _start_leaf(tree, on_term="ignore")
    members = _tree_members(pid)
    _script_table_reads(monkeypatch, {failed_read: None})

    survivors = proctree.terminate(members, term_grace=term_grace, kill_grace=5.0)

    assert survivors == []
    assert (pid, SIGKILL) in signal_seam.sent
    tree.assert_stopped(quiet_for=QUIET)


# --- V8: pid files ---------------------------------------------------------------

def _pid_file(directory: Path, name: str, pid: int) -> Path:
    path = directory / name
    path.write_text(str(pid))
    return path


def test_pid_files_protect_their_processes_and_subtrees(
        process_tree, tmp_path, signal_seam):
    """V8, I3: all four files, read together as a cancel reads them.

    The first file's daemon has a whole tree under it. The host outlives the
    SIGTERM round, so the walk of every later read meets the daemons too.
    """
    tree = process_tree()
    daemons = dict(zip(PID_FILES, ("runner", "daemon-2", "daemon-3", "daemon-4")))
    pids = _start_host(tree, *daemons.values(), "sibling", on_term="ignore")
    spared = {*ROLES, *daemons.values()}
    table = proctree.read_table()

    protected = proctree.protected_pids(
        [_pid_file(tmp_path, name, pids[role]) for name, role in daemons.items()],
        table,
    )
    members = proctree.kill_set(table, pids["host"], protected)

    assert protected == {pids[role]: name for name, role in daemons.items()}
    assert sorted(member.pid for member in members) == sorted(
        [pids["host"], pids["sibling"]])

    survivors = proctree.terminate(
        members, term_grace=0.3, kill_grace=10.0, protected=protected)

    assert survivors == []
    assert set(tree.running()) == spared
    assert not {pids[role] for role in spared} & {pid for pid, _ in signal_seam.sent}


def test_target_under_a_protected_parent_is_stopped(process_tree, tmp_path):
    """V8, I3: every run the reactor dispatches is the manager's child."""
    tree = process_tree()
    tree.start()
    pids = tree.pids()
    table = proctree.read_table()
    protected = proctree.protected_pids(
        [_pid_file(tmp_path, "manager.pid", pids["runner"])], table)

    members = proctree.kill_set(table, pids["cli"], protected)

    assert [member.pid for member in members] == [
        pids["cli"], pids["shell"], pids["command"]]
    assert proctree.terminate(members, protected=protected) == []
    assert list(tree.running()) == ["runner"]


@pytest.mark.parametrize(
    ("written", "booted", "protects"),
    [
        pytest.param(-3660, -7200, False, id="written-over-1h-before-its-process"),
        pytest.param(-3540, -7200, True, id="written-under-1h-before-its-process"),
        pytest.param(-60, -30, False, id="written-before-this-boot"),
    ],
)
def test_pid_file_stale_rules(process_tree, tmp_path, monkeypatch,
                              written, booted, protects):
    """V8: times are seconds from the process's real start.

    Boot is moved, and only boot, so that exactly one rule can decide each row on
    any host. A CI runner up for minutes has no real file that is both after boot
    and over 1h older than a process.
    """
    pid = _start_leaf(process_tree())
    table = proctree.read_table()
    started = table[pid].started_at
    path = _pid_file(tmp_path, "event-server.pid", pid)
    os.utime(path, (started + written, started + written))
    monkeypatch.setattr(proctree, "_boot_time", lambda *_: started + booted)

    expected = {pid: "event-server.pid"} if protects else {}
    assert proctree.protected_pids([path], table) == expected


def _btime() -> float:
    if not Path("/proc/stat").exists():
        pytest.skip("no /proc/stat on this host")
    for line in Path("/proc/stat").read_text().splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    raise AssertionError("/proc/stat has no btime")


@pytest.mark.parametrize("boot_source", ["btime", "pid-1-without-proc-stat"])
def test_pid_file_from_before_the_real_boot_protects_nothing(
        tmp_path, monkeypatch, boot_source):
    """V8: the file names pid 1, which started at boot, so only the boot rule fires."""
    table = proctree.read_table()
    if 1 not in table:
        pytest.skip("pid 1 is hidden on this host")
    if boot_source == "btime":
        booted = _btime()
    else:
        booted = table[1].started_at
        monkeypatch.setattr(proctree, "_boot_time", lambda *_: 0.0)
    path = _pid_file(tmp_path, "app.pid", 1)
    os.utime(path, (booted - 60, booted - 60))

    assert proctree.protected_pids([path], table) == {}


@pytest.mark.parametrize(
    "content",
    [None, "not a pid", "", "0", "-1"],
    ids=["no-file", "garbage", "empty", "zero-pid", "negative-pid"],
)
def test_pid_file_with_no_usable_pid_protects_nothing(tmp_path, content):
    """V8: with no table too, which is the only place the zero-pid check can show."""
    path = tmp_path / "event-server.pid"
    if content is not None:
        path.write_text(content)

    assert proctree.protected_pids([path], proctree.read_table()) == {}
    assert proctree.protected_pids([path], None) == {}


def test_pid_file_naming_no_running_process_protects_nothing(process_tree, tmp_path):
    """V8: neither a process that is missing nor a zombie is anything to spare."""
    zombie, _ = _make_zombie(process_tree("zombie"))
    reaped = _reaped_pid(process_tree("reaped"))
    files = [_pid_file(tmp_path, "manager.pid", zombie),
             _pid_file(tmp_path, "app.pid", reaped)]

    assert proctree.protected_pids(files, proctree.read_table()) == {}


def test_with_no_table_a_fresh_pid_file_counts(tmp_path):
    """V8: wrongly sparing a process beats wrongly stopping a daemon."""
    path = _pid_file(tmp_path, "manager.pid", os.getpid())

    assert proctree.protected_pids([path], None) == {os.getpid(): "manager.pid"}


# --- V9: what cannot be stopped ---------------------------------------------------

def test_member_that_cannot_be_signalled_is_reported_within_both_graces(
        process_tree, signal_seam):
    """V9: the wait is capped by each grace, and neither is cut short."""
    tree = process_tree()
    root = tree.start()
    command = tree.pids()["command"]
    members = _tree_members(root)
    name = next(member.name for member in members if member.pid == command)
    signal_seam.denied.add(command)
    term_grace, kill_grace = 1.5, 1.0

    started = time.monotonic()
    survivors = proctree.terminate(members, term_grace=term_grace, kill_grace=kill_grace)
    elapsed = time.monotonic() - started

    assert survivors == [Survivor(command, name, NOT_PERMITTED)]
    assert list(tree.running()) == ["command"]
    # The margin is for a loaded box, and keeps each default grace out of range.
    assert term_grace + kill_grace <= elapsed < term_grace + kill_grace + 3.5


def test_member_surviving_sigkill_is_named_as_such(process_tree, monkeypatch):
    """A process stuck in the kernel takes SIGKILL and stays, so the seam drops it."""
    pid = _start_leaf(process_tree(), on_term="ignore")
    members = _tree_members(pid)
    real_send = proctree._send
    monkeypatch.setattr(
        proctree, "_send",
        lambda target, sig: None if sig == SIGKILL else real_send(target, sig),
    )

    survivors = proctree.terminate(members, term_grace=0.2, kill_grace=0.2)

    assert survivors == [Survivor(pid, members[0].name, DID_NOT_EXIT)]


@pytest.mark.parametrize("refusal", [ProcessLookupError, PermissionError])
def test_signal_unverified_sends_one_sigterm_and_tolerates_a_refusal(
        process_tree, monkeypatch, refusal):
    """C4: the process may be gone or not ours, and that is not cancel's failure."""
    pid = _start_leaf(process_tree())
    sent = []

    def refuse(target, sig):
        sent.append((target, sig))
        raise refusal

    monkeypatch.setattr(proctree, "_send", refuse)

    proctree.signal_unverified(pid)

    assert sent == [(pid, SIGTERM)]


# --- V10: interrupts --------------------------------------------------------------

def test_interrupt_before_any_signal_kills_every_member(process_tree, signal_seam):
    """V10: records were being written, so nothing may be left running untracked."""
    tree = process_tree()
    root = tree.start(on_term="ignore")
    pids = tree.pids()

    with pytest.raises(KeyboardInterrupt):
        proctree.terminate(_tree_members(root), before_signal=_interrupt)

    assert signal_seam.sent == [(pids[role], SIGKILL) for role in ROLES]
    tree.assert_stopped_soon()


def test_interrupt_with_no_readable_table_sends_no_sigkill(
        process_tree, monkeypatch, signal_seam):
    """V10: identity cannot be checked, so nothing is killed, and it still propagates."""
    tree = process_tree()
    pid = _start_leaf(tree)
    members = _tree_members(pid)
    monkeypatch.setattr(proctree, "read_table", lambda: None)

    with pytest.raises(KeyboardInterrupt):
        proctree.terminate(members, before_signal=_interrupt)

    assert signal_seam.sent == []
    assert pid_running(pid)


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit],
                         ids=["ctrl-c", "manager-sigterm"])
def test_interrupt_during_the_wait_kills_live_members(
        process_tree, monkeypatch, signal_seam, interrupt):
    """V10: on the second poll, with most of the default grace still to run.

    SystemExit is what the manager's SIGTERM handler raises during the sweep.
    """
    tree = process_tree()
    pid = _start_leaf(tree, on_term="ignore")
    members = _tree_members(pid)
    _script_table_reads(monkeypatch, {2: interrupt})

    with pytest.raises(interrupt):
        proctree.terminate(members)

    assert signal_seam.sent == [(pid, SIGTERM), (pid, SIGKILL)]
    tree.assert_stopped_soon()
