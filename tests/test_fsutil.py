"""Tests for bobi.fsutil — the one durable-write path for bobi state files.

The crash-window test is the reason this module exists: it kills a REAL
process between the temp write and the rename and asserts the previous
state survives byte-for-byte.
"""

from __future__ import annotations

import ast
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

# Modules the durable-write guard cannot speak for. `fsutil.py` IS the
# implementation; the rest persist durable state without it and are outside
# D092's convergence, so converging them is its own change - not a licence to
# add a new one.
#
# This is a DENY-list of known debt, not the discovery mechanism: every entry
# is a writer the guard found and we consciously excluded. A durable writer
# that is not listed here is held to the helper, so a new one cannot land
# un-converged by simply not resembling the writers that already exist.
NOT_CONVERGED = frozenset({
    "bobi/fsutil.py",
    # Hand-rolled tmp+rename, pre-D092.
    "bobi/sdk.py",
    "bobi/launch_admission.py",
    "bobi/brain/instructions.py",
    # Bare serialized writes, pre-D092. `config.py save_deployment_state` is
    # the one this lane's PR body calls out by name; the rest are the same
    # shape and are listed so the count is honest rather than invisible.
    "bobi/build.py",
    "bobi/config.py",
    "bobi/events/artifact.py",
    "bobi/events/client.py",
    "bobi/events/server.py",
    "bobi/monitors/registry.py",
    "bobi/registry.py",
    "bobi/setup/webui/server.py",
})


def _bare_serialized_writes(source: str) -> list[int]:
    """Line numbers where a serialized document is written with a bare write.

    `path.write_text(json.dumps(...))` truncates the target before the new
    bytes land, so a kill mid-write leaves a document every reader parses as
    empty. The shape is matched on the AST rather than by substring so a
    module's plain-text writes (a `.gitignore`, a generated script) do not
    read as false positives.
    """
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute)
                and func.attr in ("write_text", "write_bytes")):
            continue
        for sub in ast.walk(node.args[0]):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in ("dumps", "dump")
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id in ("json", "yaml")):
                hits.append(node.lineno)
                break
    return hits


def _durable_writers() -> list[str]:
    """Every module under `bobi/` that persists durable state.

    DISCOVERED from source, not hand-listed: a module enrols by routing
    through `fsutil`, by publishing a file with its own temp + `os.replace`,
    or by writing a serialized document outright. A hand-typed roster guards
    only what someone remembered to type - it went stale the moment a durable
    writer landed without being added to it, which is exactly how a second
    atomic-write style survived inside an already converged subsystem.

    The third clause is what stops the discovery being circular. Enrolling on
    `fsutil`/`os.replace` alone reaches only writers that ALREADY publish
    atomically, so the un-converged shape the guard exists to catch - a bare
    `write_text(json.dumps(...))` - was the one shape that could never enrol.
    """
    found = []
    for path in sorted((REPO_ROOT / "bobi").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in NOT_CONVERGED:
            continue
        source = path.read_text()
        if ("fsutil" in source or "os.replace(" in source
                or _bare_serialized_writes(source)):
            found.append(rel)
    return found


DURABLE_WRITERS = _durable_writers()


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

    def test_the_roster_covers_the_modules_it_claims_to(self):
        """Discovery must actually reach the durable writers, not an empty set."""
        assert set(DURABLE_WRITERS) >= {
            "bobi/workflow/state.py",
            "bobi/setup/state.py",
            "bobi/brain/codex_config.py",
            "bobi/spend_governor.py",
            "bobi/monitors/scheduler.py",
            "bobi/monitors/script_cache_checks.py",
            "bobi/tool_library.py",
            "bobi/compose.py",
            "bobi/install.py",
        }

    def test_discovery_reaches_an_unconverged_writer(self, tmp_path, monkeypatch):
        """The shape the guard exists to catch must be able to ENROL.

        Enrolling on `fsutil`/`os.replace` alone only ever reached writers that
        already publish atomically, so a new module doing the un-converged
        thing - `write_text(json.dumps(...))` and nothing else - was invisible
        to the guard that was supposed to stop it. Discovery is only
        meaningful if it reaches a module resembling none of the writers
        already converged.
        """
        fake = tmp_path / "bobi" / "newcomer.py"
        fake.parent.mkdir(parents=True)
        fake.write_text(
            "import json\n"
            "from pathlib import Path\n"
            "def save(p, state):\n"
            "    Path(p).write_text(json.dumps(state))\n"
        )
        monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)

        assert "bobi/newcomer.py" in _durable_writers(), (
            "a durable writer that hand-rolls a bare serialized write never "
            "enrols, so the guard cannot see the regression it describes"
        )

    @pytest.mark.parametrize("relpath", DURABLE_WRITERS)
    def test_durable_writer_has_no_bare_serialized_write(self, relpath):
        lines = _bare_serialized_writes((REPO_ROOT / relpath).read_text())
        assert not lines, (
            f"{relpath} writes a serialized document without "
            f"fsutil.atomic_write_* (line(s) {lines})"
        )

    @pytest.mark.parametrize("relpath", DURABLE_WRITERS)
    def test_durable_writer_uses_the_shared_helper(self, relpath):
        source = (REPO_ROOT / relpath).read_text()
        assert "fsutil" in source, (
            f"{relpath} publishes state with its own tmp+os.replace instead of "
            "routing through bobi.fsutil"
        )
