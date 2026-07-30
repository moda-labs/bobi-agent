"""Every line Bobi appends to a runtime log carries a full date, once (#851).

`state/manager.log` is append-only and retained across days. Stamped with a
bare wall clock it does not just lose information, it asserts something false:
four once-a-day monitor fires land adjacent and read as a runaway loop firing
four times inside 30 seconds. That misreading drove a wrong-cause postmortem on
2026-07-25 and the same misreading on 2026-07-12.
"""

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from bobi import logs
from bobi.monitors.scheduler import _append_manager_log, _append_monitor_output

# 2026-07-25T15:00:19.417-07:00 [INFO] message
STAMPED = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2} \[[A-Z]+\] "
)


def _record(msg: str = "Monitor sales-call-manager due", level: int = logging.INFO,
            created: float | None = None) -> logging.LogRecord:
    record = logging.LogRecord("bobi.test", level, __file__, 1, msg, None, None)
    if created is not None:
        record.created = created
    return record


class TestFormatter:
    def test_stamp_carries_the_date_and_offset(self):
        line = logs.formatter().format(_record())
        assert STAMPED.match(line), line
        assert line.endswith("[INFO] Monitor sales-call-manager due")

    def test_same_clock_time_on_different_days_reads_differently(self):
        """The issue's evidence: four 15:00 fires that are four separate days."""
        fmt = logs.formatter()
        day = 24 * 60 * 60
        first = fmt.format(_record(created=1_753_484_419.0))
        next_day = fmt.format(_record(created=1_753_484_419.0 + day))

        assert first != next_day
        # A bare "%H:%M:%S" stamp made these two lines byte-identical.
        assert first.split("T")[0] != next_day.split("T")[0]

    def test_stamp_is_fixed_and_ignores_datefmt(self):
        """One shape across every sink, so one grep spans all of them."""
        line = logging.Formatter.format(
            logs.IsoFormatter(logs.LOG_FORMAT, datefmt="%H:%M:%S"), _record()
        )
        assert STAMPED.match(line), line

    def test_stamped_matches_what_the_formatter_emits(self):
        """Direct appenders and handler records are indistinguishable on disk."""
        assert STAMPED.match(logs.stamped("ERROR", "spawn failed")), logs.stamped(
            "ERROR", "spawn failed")
        assert logs.stamped("ERROR", "spawn failed").endswith("[ERROR] spawn failed")


class TestRootWritesTo:
    """Both routes a record can already be reaching the file by.

    A predicate that knows only one of them still lets the other double every
    line, which is the whole defect.
    """

    def test_sees_a_stream_handler_on_a_redirected_stderr(
            self, tmp_path, monkeypatch):
        """How spawn_team and _spawn_monitor_agent launch their children."""
        target = tmp_path / "manager.log"
        target.touch()
        with open(target, "a") as handle:
            monkeypatch.setattr(logging.getLogger(), "handlers",
                                [logging.StreamHandler(handle)])
            assert logs.root_writes_to(target) is True

    def test_sees_a_file_handler_on_the_same_file(self, tmp_path, monkeypatch):
        """The systemd / container shape: stderr elsewhere, handler on disk."""
        target = tmp_path / "manager.log"
        target.touch()
        handler = logs.file_handler(target)
        try:
            monkeypatch.setattr(logging.getLogger(), "handlers", [handler])
            assert logs.root_writes_to(target) is True
        finally:
            handler.close()

    def test_matches_through_a_different_spelling_of_the_same_file(
            self, tmp_path, monkeypatch):
        """Inode identity, not string equality - a redirect is an open fd."""
        target = tmp_path / "manager.log"
        target.touch()
        with open(target, "a") as handle:
            monkeypatch.setattr(logging.getLogger(), "handlers",
                                [logging.StreamHandler(handle)])
            assert logs.root_writes_to(tmp_path / "." / "manager.log") is True

    def test_false_for_a_handler_on_a_different_file(self, tmp_path, monkeypatch):
        target = tmp_path / "manager.log"
        target.touch()
        other = tmp_path / "other.log"
        other.touch()
        with open(other, "a") as handle:
            monkeypatch.setattr(logging.getLogger(), "handlers",
                                [logging.StreamHandler(handle)])
            assert logs.root_writes_to(target) is False

    def test_false_when_the_stream_is_a_pipe(self, tmp_path, monkeypatch):
        target = tmp_path / "manager.log"
        target.touch()
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(write_fd, "w") as handle:
                monkeypatch.setattr(logging.getLogger(), "handlers",
                                    [logging.StreamHandler(handle)])
                assert logs.root_writes_to(target) is False
        finally:
            os.close(read_fd)

    def test_false_when_the_stream_has_no_fileno(self, tmp_path, monkeypatch):
        """Under a capturing test harness; never drop a line over uncertainty."""
        import io

        target = tmp_path / "manager.log"
        target.touch()
        monkeypatch.setattr(logging.getLogger(), "handlers",
                            [logging.StreamHandler(io.StringIO())])
        assert logs.root_writes_to(target) is False

    def test_false_when_there_are_no_handlers(self, tmp_path, monkeypatch):
        target = tmp_path / "manager.log"
        target.touch()
        monkeypatch.setattr(logging.getLogger(), "handlers", [])
        assert logs.root_writes_to(target) is False

    def test_false_when_the_target_does_not_exist(self, tmp_path, monkeypatch):
        missing = tmp_path / "not-yet.log"
        with open(tmp_path / "real.log", "w") as handle:
            monkeypatch.setattr(logging.getLogger(), "handlers",
                                [logging.StreamHandler(handle)])
            assert logs.root_writes_to(missing) is False


class TestConfigureRoot:
    def test_installs_the_house_format_over_an_existing_handler(
            self, monkeypatch):
        """A plugin import or a dependency's basicConfig gets there first.

        Plain `basicConfig` stands down when root already has a handler, which
        silently reverts the log to the undated lines #851 is about.
        """
        root = logging.getLogger()
        monkeypatch.setattr(root, "handlers", [logging.StreamHandler()])
        monkeypatch.setattr(root, "level", logging.WARNING)

        logs.configure_root()

        assert len(root.handlers) == 1
        assert root.level == logging.INFO
        line = root.handlers[0].format(_record())
        assert STAMPED.match(line), line


class TestCliWiring:
    def test_the_cli_stamps_its_stream_logs(self, monkeypatch):
        """The real `bobi` entry point, not a hand-built handler.

        `--version` would not do: it is eager and exits during parsing, before
        the group callback that configures logging ever runs.
        """
        from click.testing import CliRunner

        from bobi.cli import main

        # monkeypatch, not assignment: the CLI leaves a handler behind and an
        # unrestored root logger leaks into every later test in the session.
        monkeypatch.setattr(logging.getLogger(), "handlers", [])
        CliRunner().invoke(main, ["agent"])

        handlers = logging.getLogger().handlers
        assert handlers, "the CLI configured no handler"
        line = handlers[0].format(_record())
        assert STAMPED.match(line), line


class TestRuntimeLogHandler:
    def test_attaches_a_stamped_handler(self, bobi_install, monkeypatch):
        import io

        from bobi.cli import _attach_runtime_log

        monkeypatch.setattr(sys, "stderr", io.StringIO())
        root = logging.getLogger()
        monkeypatch.setattr(root, "handlers", [])
        _attach_runtime_log(bobi_install.repo_path)

        assert root.handlers, "no runtime log handler attached"
        line = root.handlers[0].format(_record())
        assert STAMPED.match(line), line

    def test_stands_down_when_stderr_is_already_the_log(
            self, bobi_install, monkeypatch):
        """Otherwise every record lands in manager.log twice and counts inflate."""
        from bobi.cli import _attach_runtime_log

        log_path = bobi_install.state_dir / "manager.log"
        log_path.touch()
        root = logging.getLogger()
        with open(log_path, "a") as handle:
            monkeypatch.setattr(root, "handlers",
                                [logging.StreamHandler(handle)])
            _attach_runtime_log(bobi_install.repo_path)

            assert len(root.handlers) == 1

    def test_attaches_only_once(self, bobi_install, monkeypatch):
        import io

        from bobi.cli import _attach_runtime_log

        monkeypatch.setattr(sys, "stderr", io.StringIO())
        root = logging.getLogger()
        monkeypatch.setattr(root, "handlers", [])
        _attach_runtime_log(bobi_install.repo_path)
        _attach_runtime_log(bobi_install.repo_path)

        assert len(root.handlers) == 1

    def test_a_real_process_writes_each_record_once_and_dated(self, tmp_path):
        """Both halves of #851 on the bytes that actually reach manager.log.

        `spawn_team` launches `bobi agent <name> start --foreground` with its
        stderr redirected into manager.log, and that child then binds its
        runtime - which attaches a handler to the same file. Both writers are
        live at once, and only a real process holds the two together: the
        sibling tests hand-patch `root.handlers` and cannot show it.

        Before the fix this file got the record twice, once stamped with a
        bare clock (`21:01:51 [WARNING] Monitor sales due`) and once with no
        stamp at all - the exact pair in the live log #851 was filed from.

        One subprocess covers both claims deliberately: each costs ~15s of
        interpreter startup and `bobi.cli` import, against CI's per-test
        `--timeout=30`.
        """
        log_file = tmp_path / "state" / "manager.log"
        probe = tmp_path / "probe.py"
        probe.write_text(
            "import logging, sys\n"
            "from pathlib import Path\n"
            "from bobi.cli import _attach_runtime_log, main\n"
            "try:\n"
            "    main.main(['agent'], standalone_mode=False)\n"
            "except BaseException:\n"
            "    pass\n"
            "_attach_runtime_log(Path(sys.argv[1]))\n"
            "logging.getLogger('bobi.probe').warning('Monitor sales due')\n"
            "for h in logging.getLogger().handlers:\n"
            "    h.flush()\n"
        )
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "w") as handle:
            subprocess.run(
                [sys.executable, str(probe), str(tmp_path)],
                stdout=handle, stderr=handle,
                cwd=str(Path(__file__).resolve().parents[1]),
                timeout=20, check=True,
            )

        text = log_file.read_text()
        assert text.count("Monitor sales due") == 1, text
        line = next(ln for ln in text.splitlines() if "Monitor sales due" in ln)
        assert STAMPED.match(line), line


class TestLogPathOnAFreshRuntime:
    """`state/` does not exist until something makes it.

    Every caller of `manager_log_path` opens the result for write, so the
    non-creating `state_path` form raises `FileNotFoundError` into
    `bobi agent <name> ...` and is swallowed whole by the scheduler's
    best-effort appends - dropping the line the fix exists to write. The
    `bobi_install` fixture pre-creates `state/`, so this uses a bare root.
    """

    def test_the_path_brings_its_directory_with_it(self, tmp_path):
        from bobi import paths

        assert not (tmp_path / "state").exists()
        assert paths.manager_log_path(tmp_path).parent.is_dir()

    def test_attaching_the_runtime_log_survives_a_missing_state_dir(
            self, tmp_path, monkeypatch):
        import io

        from bobi.cli import _attach_runtime_log

        monkeypatch.setattr(sys, "stderr", io.StringIO())
        monkeypatch.setattr(logging.getLogger(), "handlers", [])
        _attach_runtime_log(tmp_path)

        logging.getLogger("bobi.probe").warning("Monitor due")
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert "Monitor due" in (tmp_path / "state" / "manager.log").read_text()


class TestSchedulerAppends:
    MESSAGE = "Failed to spawn check for monitor sales"

    def test_appended_lines_are_stamped(self, bobi_install, monkeypatch):
        monkeypatch.setattr(logging.getLogger(), "handlers", [])
        _append_manager_log(self.MESSAGE, "ERROR")

        line = (bobi_install.state_dir / "manager.log").read_text().strip()
        assert STAMPED.match(line), line
        assert line.endswith(f"[ERROR] {self.MESSAGE}")

    def test_stands_down_when_stderr_is_already_the_log(
            self, bobi_install, monkeypatch):
        """The scheduler's own `log.error` already put this record on disk."""
        log_path = bobi_install.state_dir / "manager.log"
        log_path.touch()
        with open(log_path, "a") as handle:
            monkeypatch.setattr(logging.getLogger(), "handlers",
                                [logging.StreamHandler(handle)])
            _append_manager_log(self.MESSAGE, "ERROR")

        assert log_path.read_text() == ""

    def test_stands_down_when_a_file_handler_already_writes_the_log(
            self, bobi_install, monkeypatch):
        """The systemd shape: stderr is the journal, the handler is on disk.

        Checking only stderr missed this and wrote the record twice - the
        count inflation #851 is about, reintroduced by the fix for it.
        """
        log_path = bobi_install.state_dir / "manager.log"
        log_path.touch()
        handler = logs.file_handler(log_path)
        try:
            monkeypatch.setattr(logging.getLogger(), "handlers", [handler])
            logging.getLogger("bobi.monitors.scheduler").error(self.MESSAGE)
            _append_manager_log(self.MESSAGE, "ERROR")
            handler.flush()

            assert log_path.read_text().count(self.MESSAGE) == 1
        finally:
            handler.close()

    def test_still_writes_when_no_handler_reaches_the_log(
            self, bobi_install, monkeypatch):
        """A container's PID 1: `start --foreground` strips the file handler,
        so this direct append is the only copy that reaches manager.log."""
        import io

        monkeypatch.setattr(logging.getLogger(), "handlers",
                            [logging.StreamHandler(io.StringIO())])
        _append_manager_log("Check for monitor sales exceeded 900s - killed",
                            "ERROR")

        assert "exceeded 900s" in (
            bobi_install.state_dir / "manager.log").read_text()


class TestMonitorOutputTee:
    def test_the_teed_blob_is_announced_with_a_stamp(self, bobi_install):
        """The agent's raw output stays verbatim; a header dates its arrival."""
        _append_monitor_output("sales-call-manager", "check",
                               '{"success": true, "finding": false}\n')

        text = (bobi_install.state_dir / "manager.log").read_text()
        header, blob = text.splitlines()[0], text.splitlines()[1]
        assert STAMPED.match(header), header
        assert "sales-call-manager" in header
        assert blob == '{"success": true, "finding": false}'

    def test_header_and_blob_go_out_as_one_write(self, bobi_install,
                                                 monkeypatch):
        """A waiter thread runs per monitor and they share this file.

        Two writes could be split by another thread's, filing the blob under
        someone else's header.
        """
        writes = []
        real_open = open

        def counting_open(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            if str(path).endswith("manager.log"):
                original = handle.write

                def record(text):
                    writes.append(text)
                    return original(text)

                handle.write = record
            return handle

        monkeypatch.setattr("builtins.open", counting_open)
        _append_monitor_output("sales", "check", '{"success": true}')

        assert len(writes) == 1, writes


class TestSidecarSink:
    def test_the_sidecar_configures_the_house_format(self, tmp_path,
                                                     monkeypatch):
        """`state/embedding-sidecar.log` is retained across days too.

        Driven through the real `main()` rather than grepping its source: a
        `configure_root()` call sitting in a dead branch would pass that.
        """
        from bobi.kb import sidecar

        called = []
        monkeypatch.setattr(logs, "configure_root",
                            lambda *a, **k: called.append(True))
        # No fastembed -> main() exits right after configuring logging.
        monkeypatch.setitem(sys.modules, "fastembed", None)
        monkeypatch.setattr(sys, "argv",
                            ["sidecar", "--project-root", str(tmp_path)])

        with pytest.raises(SystemExit):
            sidecar.main()

        assert called, "the sidecar did not configure the house log format"
