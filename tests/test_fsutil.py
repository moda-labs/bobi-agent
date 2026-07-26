"""Tests for bobi.fsutil — the one durable-write path for bobi state files.

The crash-window test is the reason this module exists: it kills a REAL
process between the temp write and the rename and asserts the previous
state survives byte-for-byte.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from bobi import fsutil

REPO_ROOT = Path(__file__).resolve().parent.parent

# The five in-scope durable-state writers that must route through fsutil
# rather than re-implementing tmp+rename (or writing bare). Guards D092:
# one atomic-write implementation, not six.
DURABLE_WRITERS = (
    "bobi/workflow/state.py",
    "bobi/setup/state.py",
    "bobi/brain/codex_config.py",
    "bobi/spend_governor.py",
    "bobi/monitors/scheduler.py",
)


def kill_mid_write(monkeypatch, *, keep: int = 12) -> None:
    """Simulate a process killed mid-write (Ctrl-C, OOM, supervisor restart).

    Partial bytes land, then the write dies. Faithful to the real thing: a
    bare ``write_text`` has already truncated its target by then, while an
    atomic write is only part-way through a temp file. Shared by every
    durable-state test module, so the simulation itself has one definition.
    """
    real_write_text = Path.write_text

    def killed(self, data, *a, **kw):
        real_write_text(self, data[:keep], *a, **kw)
        raise KeyboardInterrupt("simulated kill mid-write")

    monkeypatch.setattr(Path, "write_text", killed)


class TestAtomicWriteText:
    def test_writes_content_and_returns_path(self, tmp_path):
        target = tmp_path / "state.json"
        assert fsutil.atomic_write_text(target, "hello") == target
        assert target.read_text() == "hello"

    def test_creates_missing_parents(self, tmp_path):
        target = tmp_path / "a" / "b" / "state.json"
        fsutil.atomic_write_text(target, "hi")
        assert target.read_text() == "hi"

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "state.json"
        target.write_text("old")
        fsutil.atomic_write_text(target, "new")
        assert target.read_text() == "new"

    def test_leaves_no_temp_files_behind(self, tmp_path):
        target = tmp_path / "state.json"
        fsutil.atomic_write_text(target, "x")
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_failed_write_leaves_target_and_dir_clean(self, tmp_path):
        """A write that raises must not touch the target nor orphan a temp."""
        target = tmp_path / "state.json"
        target.write_text("old")

        with pytest.MonkeyPatch.context() as mp:
            kill_mid_write(mp, keep=3)
            with pytest.raises(KeyboardInterrupt):
                fsutil.atomic_write_text(target, "brand new content")

        assert target.read_text() == "old"
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_concurrent_writers_do_not_share_a_temp(self, tmp_path):
        """Two writers racing on one target must not clobber each other's temp.

        A shared/fixed temp name lets writer B's rename consume (or delete)
        writer A's temp, so A's rename then fails on a file it did write.
        """
        target = tmp_path / "state.json"
        real_replace = os.replace
        seen = {"nested": False}

        def hooked(src, dst):
            if not seen["nested"]:
                seen["nested"] = True
                fsutil.atomic_write_text(target, "from B")  # B runs inside A's window
            return real_replace(src, dst)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "replace", hooked)
            fsutil.atomic_write_text(target, "from A")

        assert seen["nested"] is True
        assert target.read_text() == "from A"  # A's rename landed last, intact
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


class TestModeIsPreserved:
    """The replacement inherits the target's mode, so a restrictive one survives.

    `os.replace` swaps in a brand-new inode, so without this the temp file's
    umask-default mode (typically 0644) silently becomes the target's. That
    would strip `chmod 600` from files bobi fills with resolved credentials -
    `~/.codex/config.toml` carries live MCP tokens.
    """

    @pytest.mark.parametrize("write", [
        lambda p: fsutil.atomic_write_text(p, "new"),
        lambda p: fsutil.atomic_write_json(p, {"new": True}),
    ])
    def test_restrictive_mode_on_an_existing_target_survives(self, tmp_path, write):
        target = tmp_path / "config.toml"
        target.write_text("old")
        os.chmod(target, 0o600)
        write(target)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_executable_bit_survives(self, tmp_path):
        target = tmp_path / "check.sh"
        target.write_text("old")
        os.chmod(target, 0o750)
        fsutil.atomic_write_text(target, "new")
        assert stat.S_IMODE(target.stat().st_mode) == 0o750

    def test_a_new_file_keeps_the_process_default_mode(self, tmp_path):
        """Creation is unchanged: only an EXISTING target's mode is preserved.

        A bare `write_text` created the file at the umask default too, so
        matching it is what keeps the atomic-write seam a pure swap-in.
        """
        plain = tmp_path / "plain.json"
        plain.write_text("x")
        fsutil.atomic_write_text(tmp_path / "atomic.json", "x")
        assert stat.S_IMODE((tmp_path / "atomic.json").stat().st_mode) == \
            stat.S_IMODE(plain.stat().st_mode)


class TestAtomicWriteJson:
    def test_round_trip(self, tmp_path):
        target = tmp_path / "state.json"
        fsutil.atomic_write_json(target, {"a": [1, 2], "b": "x"})
        assert json.loads(target.read_text()) == {"a": [1, 2], "b": "x"}

    def test_indent_is_configurable(self, tmp_path):
        target = tmp_path / "state.json"
        fsutil.atomic_write_json(target, {"a": 1}, indent=None)
        assert target.read_text() == '{"a": 1}'


# --- the crash window ------------------------------------------------------

CRASH_CHILD = """
import os, sys
from bobi import fsutil
target = sys.argv[1]
os.replace = lambda *a, **kw: os._exit(9)   # killed before the rename lands
fsutil.atomic_write_json(target, {"generation": 2})
"""


class TestCrashWindow:
    def test_kill_between_temp_write_and_rename_preserves_old_state(self, tmp_path):
        """A real process killed in the atomic-write window loses nothing.

        The child writes its temp file and is hard-killed (os._exit) before
        os.replace runs — the exact window a bare write_text would spend
        with the target truncated.
        """
        target = tmp_path / "state.json"
        fsutil.atomic_write_json(target, {"generation": 1})
        before = target.read_bytes()

        proc = subprocess.run(
            [sys.executable, "-c", CRASH_CHILD, str(target)],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        assert proc.returncode == 9, proc.stderr  # the child really died mid-write

        assert target.read_bytes() == before
        assert json.loads(target.read_text()) == {"generation": 1}

        # The next successful write still lands (crash residue can't block it).
        fsutil.atomic_write_json(target, {"generation": 2})
        assert json.loads(target.read_text()) == {"generation": 2}


class TestFileLock:
    def test_serializes_threads(self, tmp_path):
        target = tmp_path / "state.json"
        events: list[tuple[str, int]] = []

        def worker(i: int) -> None:
            with fsutil.file_lock(target):
                events.append(("enter", i))
                time.sleep(0.05)
                events.append(("exit", i))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(events) == 8
        for i in range(0, 8, 2):
            assert events[i][0] == "enter" and events[i + 1][0] == "exit"
            assert events[i][1] == events[i + 1][1]  # no interleaving

    def test_lock_file_is_a_sibling_not_the_target(self, tmp_path):
        target = tmp_path / "state.json"
        with fsutil.file_lock(target):
            pass
        assert not target.exists()
        assert (tmp_path / "state.json.lock").exists()

    def test_creates_missing_parents(self, tmp_path):
        with fsutil.file_lock(tmp_path / "deep" / "nested" / "state.json"):
            pass
        assert (tmp_path / "deep" / "nested").is_dir()

    def test_releases_on_exception(self, tmp_path):
        target = tmp_path / "state.json"
        with pytest.raises(ValueError):
            with fsutil.file_lock(target):
                raise ValueError("boom")
        # A second acquisition must not block (the lock was released).
        done = threading.Event()

        def worker():
            with fsutil.file_lock(target):
                done.set()

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=5)
        assert done.is_set()


class TestSingleImplementation:
    """D092: one atomic-write helper, adopted by every durable-state writer."""

    @pytest.mark.parametrize("relpath", DURABLE_WRITERS)
    def test_durable_writer_has_no_bare_write_text(self, relpath):
        source = (REPO_ROOT / relpath).read_text()
        assert ".write_text(" not in source, (
            f"{relpath} writes durable state without fsutil.atomic_write_*"
        )

    @pytest.mark.parametrize("relpath", DURABLE_WRITERS)
    def test_durable_writer_uses_the_shared_helper(self, relpath):
        source = (REPO_ROOT / relpath).read_text()
        assert "fsutil" in source, f"{relpath} does not route through bobi.fsutil"
