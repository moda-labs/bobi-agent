"""Tests for the shared crash-safe write helper (D092).

The load-bearing test here is the crash-window one: it really kills a child
process between the temp write and the rename, because that is the exact
window every adopting site is trying to survive. A mocked "crash" that just
raises would prove nothing about os.replace's atomicity.
"""

import json
import os
import subprocess
import sys
import textwrap
import threading

import pytest

from bobi.fsutil import atomic_write_json, atomic_write_text, file_lock


def _strays(path):
    """Temp siblings left beside *path* by the helper."""
    return [p.name for p in path.parent.iterdir()
            if p.name.startswith(f".{path.name}.") and p.name.endswith(".tmp")]


class TestAtomicWriteText:
    def test_writes_content(self, tmp_path):
        target = tmp_path / "state.json"
        assert atomic_write_text(target, "hello") == target
        assert target.read_text() == "hello"

    def test_creates_parent_directories(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "state.json"
        atomic_write_text(target, "hello")
        assert target.read_text() == "hello"

    def test_mkdir_false_does_not_create_parent(self, tmp_path):
        target = tmp_path / "missing" / "state.json"
        with pytest.raises(OSError):
            atomic_write_text(target, "hello", mkdir=False)

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "state.json"
        target.write_text("old")
        atomic_write_text(target, "new")
        assert target.read_text() == "new"

    def test_leaves_no_temp_file_behind(self, tmp_path):
        target = tmp_path / "state.json"
        atomic_write_text(target, "hello")
        assert _strays(target) == []
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_temp_file_removed_when_write_fails(self, tmp_path, monkeypatch):
        target = tmp_path / "state.json"
        target.write_text("old")

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("pathlib.Path.write_text", boom)
        with pytest.raises(OSError):
            atomic_write_text(target, "new")
        assert _strays(target) == []
        assert target.read_text() == "old"

    def test_temp_paths_are_unique_per_call(self, tmp_path, monkeypatch):
        """Two writers of one target must never share a temp path.

        A shared temp name lets writer A rename writer B's half-written file
        into place — a torn document published atomically.
        """
        seen = []
        real = os.replace

        def capture(src, dst):
            seen.append(str(src))
            real(src, dst)

        monkeypatch.setattr(os, "replace", capture)
        target = tmp_path / "state.json"
        atomic_write_text(target, "a")
        atomic_write_text(target, "b")
        assert len(set(seen)) == 2

    def test_executable_bit_set_before_rename(self, tmp_path):
        target = tmp_path / "runner.sh"
        atomic_write_text(target, "#!/bin/sh\n", executable=True)
        assert os.access(target, os.X_OK)

    def test_not_executable_by_default(self, tmp_path):
        target = tmp_path / "state.json"
        atomic_write_text(target, "{}")
        assert not os.access(target, os.X_OK)

    def test_fsync_still_writes(self, tmp_path):
        target = tmp_path / "state.json"
        atomic_write_text(target, "durable", fsync=True)
        assert target.read_text() == "durable"

    def test_encoding_round_trip(self, tmp_path):
        target = tmp_path / "state.json"
        atomic_write_text(target, "café ☕")
        assert target.read_text(encoding="utf-8") == "café ☕"


class TestIsAtomicTemp:
    """The predicate callers use to drop orphans from a content hash."""

    def test_recognizes_what_the_helper_generates(self, tmp_path, monkeypatch):
        from bobi.fsutil import _tmp_path, is_atomic_temp

        assert is_atomic_temp(_tmp_path(tmp_path / "setup.json"))

    def test_does_not_match_a_merely_hidden_tmp_file(self):
        """Narrow on purpose: a loose pattern silently drops real files.

        `source_tree_hash` skips these, so anything this matches stops being
        checked for changes between validate and install.
        """
        from bobi.fsutil import is_atomic_temp

        assert not is_atomic_temp(".notes.tmp")
        assert not is_atomic_temp(".x.abc.def.tmp")
        assert not is_atomic_temp("setup.json.4242.1785.tmp")   # not hidden
        assert not is_atomic_temp(".setup.json.4242.1785.tmp.bak")


class TestAtomicWriteJson:
    def test_round_trip(self, tmp_path):
        target = tmp_path / "state.json"
        atomic_write_json(target, {"a": [1, 2], "b": "x"})
        assert json.loads(target.read_text()) == {"a": [1, 2], "b": "x"}

    def test_unserializable_object_never_touches_disk(self, tmp_path):
        target = tmp_path / "state.json"
        target.write_text("old")
        with pytest.raises(TypeError):
            atomic_write_json(target, {"bad": object()})
        assert target.read_text() == "old"
        assert _strays(target) == []


# --- the crash window ------------------------------------------------------

_CRASH_CHILD = textwrap.dedent(
    """
    import os, signal, sys
    sys.path.insert(0, {repo!r})
    from bobi.fsutil import atomic_write_text

    target = {target!r}

    real_replace = os.replace
    def die(src, dst):
        # Killed with the temp file written and the rename not yet issued —
        # the exact window a truncating write would corrupt the target in.
        os.kill(os.getpid(), signal.SIGKILL)

    os.replace = die
    atomic_write_text(target, "NEW-CONTENT-THAT-MUST-NOT-LAND")
    """
)


class TestCrashWindow:
    """Phase 3 validation gate: kill between temp write and replace."""

    def test_old_state_survives_a_kill_mid_write(self, tmp_path):
        target = tmp_path / "state.json"
        old = json.dumps({"monitors": {"nightly": {"last_run": 123}}}, indent=2)
        target.write_text(old)

        repo_root = str(
            __import__("pathlib").Path(__file__).resolve().parent.parent
        )
        proc = subprocess.run(
            [sys.executable, "-c",
             _CRASH_CHILD.format(repo=repo_root, target=str(target))],
            capture_output=True,
        )

        # Really died by signal, not by a caught exception — otherwise this
        # test would pass without ever entering the window it names.
        assert proc.returncode == -9, (
            f"child did not SIGKILL itself: rc={proc.returncode} "
            f"stderr={proc.stderr.decode()[-2000:]}"
        )
        assert target.read_text() == old
        assert json.loads(target.read_text())["monitors"]["nightly"]["last_run"] == 123

    def test_a_bare_write_text_would_have_lost_it(self, tmp_path):
        """The mutant: the same kill against a non-atomic writer.

        Proves the previous test is measuring atomicity rather than a
        child process that never got far enough to write anything.
        """
        target = tmp_path / "state.json"
        target.write_text(json.dumps({"monitors": {"nightly": {"last_run": 123}}}))

        child = textwrap.dedent(
            f"""
            import os, signal
            fh = open({str(target)!r}, "w")     # truncates immediately
            fh.flush()
            os.kill(os.getpid(), signal.SIGKILL)
            """
        )
        proc = subprocess.run([sys.executable, "-c", child], capture_output=True)
        assert proc.returncode == -9
        assert target.read_text() == "", "expected the bare write to truncate"


class TestConcurrentWriters:
    def test_reader_never_sees_a_partial_document(self, tmp_path):
        target = tmp_path / "state.json"
        payload = {"entries": [{"i": i, "pad": "x" * 200} for i in range(400)]}
        atomic_write_json(target, payload)

        stop = threading.Event()
        torn: list[str] = []

        def read_forever():
            while not stop.is_set():
                try:
                    json.loads(target.read_text())
                except (json.JSONDecodeError, FileNotFoundError) as exc:
                    torn.append(repr(exc))
                    return

        reader = threading.Thread(target=read_forever)
        reader.start()
        try:
            for i in range(60):
                payload["round"] = i
                atomic_write_json(target, payload)
        finally:
            stop.set()
            reader.join(timeout=5)

        assert torn == []


class TestFileLock:
    def test_serializes_across_processes(self, tmp_path):
        target = tmp_path / "state.json"
        child = textwrap.dedent(
            f"""
            import fcntl, sys
            with open({str(tmp_path / "state.json.lock")!r}, "a+") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    sys.exit(7)          # held by the parent, as intended
                sys.exit(0)              # acquired — the lock is not real
            """
        )
        with file_lock(target):
            held = subprocess.run([sys.executable, "-c", child],
                                  capture_output=True)
        free = subprocess.run([sys.executable, "-c", child], capture_output=True)

        assert held.returncode == 7, "another process acquired a held lock"
        assert free.returncode == 0, "lock was not released on exit"

    def test_lock_file_is_a_sibling_not_the_target(self, tmp_path):
        target = tmp_path / "state.json"
        with file_lock(target):
            pass
        assert (tmp_path / "state.json.lock").exists()
        assert not target.exists()

    def test_released_on_exception(self, tmp_path):
        target = tmp_path / "state.json"
        with pytest.raises(RuntimeError):
            with file_lock(target):
                raise RuntimeError("boom")
        with file_lock(target):
            pass  # re-acquirable in-process
