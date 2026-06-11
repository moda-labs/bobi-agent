"""Event-server launch must surface npm failures, not swallow them.

The v0.14.1 release gate failed inside `npm install` with
capture_output=True — the CalledProcessError carried no output, so the
manager.log showed a bare traceback and diagnosing the real cause
(ENOSPC) required SSHing to the runner and re-running npm by hand.
"""

import subprocess
from pathlib import Path

import pytest

from modastack.events import server as es


def test_npm_failure_surfaces_stderr(tmp_path, monkeypatch, caplog):
    es_dir = tmp_path / "event-server"
    es_dir.mkdir()
    (es_dir / "package.json").write_text("{}")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, returncode=1, stdout="",
            stderr="npm warn tar TAR_ENTRY_ERROR ENOSPC: no space left on device",
        )

    monkeypatch.setattr(es.subprocess, "run", fake_run)
    monkeypatch.setattr(es, "_find_event_server_dir", lambda: es_dir)
    monkeypatch.setattr(es, "health", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="ENOSPC"):
        es.ensure_running(8080, project_path=tmp_path)
