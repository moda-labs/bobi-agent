"""Subscription-login bootstrap smoke (#388).

A Layer-1 integration smoke that drives the FULL subscription-login bootstrap
path with the *real* Slack adapter output shape and a FAKED `claude auth login`
subprocess (driven over a real PTY). It would have caught the `_extract_code`
bug fixed in #386 (commit 42f96be).

The bug: `modastack/auth_bootstrap.py::_extract_code` read the message text from
`event["fields"]["text"]`, but the live Slack adapter
(`event-server/src/adapters/slack.ts`) puts the text at the event TOP LEVEL and
in `payload["text"]` — `fields` carries only channel/channel_type/user_id/ts.
The old unit tests passed because they hand-built events with `text` inside
`fields`, a shape the real system never emits (a classic integration gap).

What this smoke does differently:

  * It GENERATES the auth-code event by running the adapter's OWN logic
    (`normalizeSlackWebhook` from slack.ts) through Node + esbuild, rather than
    hand-authoring the event dict. So the bootstrap extractor and the adapter
    cannot drift apart again — if slack.ts moves `text`, this test regenerates
    against the new shape and the extractor must keep up (or the explicit
    shape-pin assertions below fail loudly).

  * It exercises the real PTY driver end-to-end: a stand-in `claude auth login`
    process is spawned on a PTY that prints the OAuth URL, then BLOCKS reading a
    line from its stdin and only writes the credentials file once the pasted
    code arrives on that stdin. So a green run proves: pasted code (real adapter
    shape) -> _extract_code -> _write_line -> subprocess stdin ->
    credentials file written -> result posted to Slack.

Run locally with:  pytest tests/integration/test_login_bootstrap_smoke.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from modastack import auth_bootstrap as ab


# --- generate the event from the real adapter (slack.ts) --------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
SLACK_TS = REPO_ROOT / "event-server" / "src" / "adapters" / "slack.ts"

# The code the human "pastes" back, and the DM conversation id it arrives on.
AUTH_CODE = "abc123#xyz789"
LOGIN_DM = "D0B51JP1N4C"


def _esbuild_dir() -> Path | None:
    """Locate a usable esbuild install, or install one on demand.

    Prefers the event-server's own esbuild (a devDependency present after
    `npm ci`); otherwise installs esbuild into a cached temp dir. Returns the
    node_modules-parent dir to require esbuild from, or None if Node/npm or
    network are unavailable (the caller then skips).
    """
    if not shutil.which("node"):
        return None
    es_local = REPO_ROOT / "event-server"
    if (es_local / "node_modules" / "esbuild" / "package.json").exists():
        return es_local
    import tempfile

    cache = Path(tempfile.gettempdir()) / "mds388-esbuild"
    if (cache / "node_modules" / "esbuild" / "package.json").exists():
        return cache
    if not shutil.which("npm"):
        return None
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "package.json").write_text('{"private":true}\n')
    try:
        subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund", "--silent", "esbuild"],
            cwd=str(cache), check=True, capture_output=True, timeout=180,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutError, OSError):
        return None
    if (cache / "node_modules" / "esbuild" / "package.json").exists():
        return cache
    return None


def _generate_adapter_event(code: str, channel: str) -> dict:
    """Run slack.ts::normalizeSlackWebhook on a real DM webhook and return the
    normalized event the live system would emit for that pasted code."""
    if not SLACK_TS.exists():  # pragma: no cover - sanity
        pytest.skip(f"slack.ts not found at {SLACK_TS}")
    es_dir = _esbuild_dir()
    if es_dir is None:
        pytest.skip("Node/npm/esbuild unavailable — cannot generate event from slack.ts")

    driver = textwrap.dedent(
        """
        const esbuild = require("esbuild");
        const fs = require("fs");
        const src = fs.readFileSync(process.argv[1], "utf8");
        // Transpile the REAL adapter (TS type-erasure only) and run its logic.
        const out = esbuild.transformSync(src, { loader: "ts", format: "esm" }).code;
        const mod = "data:text/javascript;base64," + Buffer.from(out).toString("base64");
        import(mod).then((m) => {
          // A genuine Slack event_callback webhook for a DM carrying the code.
          const webhook = {
            type: "event_callback",
            team_id: "T0TESTTEAM",
            event_id: "Ev0BOOTSTRAP",
            event: {
              type: "message",
              channel_type: "im",
              channel: process.argv[2],
              user: "U0HUMAN",
              text: process.argv[3],
              ts: "1779500000.000100",
            },
          };
          const res = m.normalizeSlackWebhook(webhook, "B0SELFBOT");
          process.stdout.write(JSON.stringify(res.event));
        });
        """
    )
    proc = subprocess.run(
        ["node", "--input-type=commonjs", "-e", driver, str(SLACK_TS), channel, code],
        cwd=str(es_dir), capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def adapter_event() -> dict:
    return _generate_adapter_event(AUTH_CODE, LOGIN_DM)


# --- pin the generated event to the real adapter shape ----------------------

def test_generated_event_matches_real_adapter_shape(adapter_event):
    """Document + pin the live shape: text TOP-LEVEL and in payload, NEVER in
    fields. If slack.ts moves text, this fails loudly so the extractor below
    cannot silently drift again."""
    ev = adapter_event
    assert ev["source"] == "slack"
    assert ev["type"] == "slack.dm"
    # The crux of the #386 bug: text lives at the top level and in payload...
    assert ev["text"] == AUTH_CODE
    assert ev["payload"]["text"] == AUTH_CODE
    # ...and NOT in fields (fields holds only routing metadata).
    assert "text" not in ev["fields"]
    assert ev["fields"]["channel"] == LOGIN_DM
    assert ev["payload"]["channel"] == LOGIN_DM


def test_extract_code_handles_real_adapter_event(adapter_event):
    """The extractor must pull the code out of the REAL adapter event. This is
    the assertion that FAILS against the pre-#386 extractor (which read only
    fields.text, absent here) and PASSES on current code."""
    assert ab._extract_code(adapter_event, LOGIN_DM) == AUTH_CODE


# --- full bootstrap path with a PTY-faked `claude auth login` ----------------

# Stand-in for `claude auth login --claudeai`: prints the OAuth URL (so
# _read_until_url scrapes it), then BLOCKS reading one line from stdin and only
# writes the credentials file once the pasted code arrives. Proves the code
# reaches the subprocess stdin AND that creds appear only after it does.
_FAKE_CLAUDE = textwrap.dedent(
    """
    import os, sys, json, pathlib
    creds = pathlib.Path(sys.argv[1])
    sys.stdout.write(
        "Opening browser to sign in\\r\\n"
        "If the browser didn't open, visit: "
        "https://claude.com/cai/oauth/authorize?code=true&client_id=test&state=s\\r\\n"
        "Paste code here > "
    )
    sys.stdout.flush()
    code = sys.stdin.readline().strip()   # blocks until _write_line feeds stdin
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(json.dumps({"received_code": code, "claudeAiOauth": {"accessToken": "x"}}))
    sys.stdout.write("Login successful.\\r\\n")
    sys.stdout.flush()
    """
)


def test_full_bootstrap_smoke_real_event_through_pty(adapter_event, tmp_path, monkeypatch):
    """End-to-end: real adapter event -> _extract_code -> _write_line ->
    fake `claude auth login` stdin (PTY) -> credentials written -> success
    posted. Faked at the seams the design declares injectable (spawn_login,
    post_message, wait_for_code) but with a REAL pty subprocess and the REAL
    extractor, so the integration gap that #386 fixed is actually exercised."""
    from modastack import paths

    monkeypatch.setattr(paths, "_root", None, raising=False)

    # Isolated project with a Slack bot_token (run_bootstrap requires it).
    project = tmp_path / "proj"
    (project / ".modastack").mkdir(parents=True)
    (project / ".modastack" / "agent.yaml").write_text(
        "agent: test\n"
        "event_server_url: wss://example\n"
        "services:\n"
        "  - name: slack\n"
        "    credentials:\n"
        "      bot_token: xoxb-test\n"
    )
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv(ab.LOGIN_CHANNEL_ENV, LOGIN_DM)

    creds_file = home / ".claude" / ".credentials.json"
    fake_script = tmp_path / "fake_claude.py"
    fake_script.write_text(_FAKE_CLAUDE)

    posts: list[tuple] = []

    def fake_spawn(home_arg: Path):
        """Spawn the stand-in `claude auth login` on a REAL pty, exactly like
        _spawn_login does for the real binary."""
        import pty

        master, slave = pty.openpty()
        proc = subprocess.Popen(
            [sys.executable, str(fake_script), str(creds_file)],
            stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, close_fds=True,
        )
        os.close(slave)
        return proc, master

    def fake_post(token, channel, text):
        posts.append((token, channel, text))

    def fake_wait(project_path, channel, timeout):
        """The Worker/event-bus is the one seam we can't run in-process here, so
        we stand in for _wait_for_code's network — but feed it the GENERATED
        real-adapter event and run the REAL _extract_code, so the extraction
        under test is the production code path on the production event shape."""
        code = ab._extract_code(adapter_event, channel)
        assert code is not None, "real-adapter event must yield a code"
        return code

    ok = ab.run_bootstrap(
        project,
        spawn_login=fake_spawn,
        post_message=fake_post,
        wait_for_code=fake_wait,
    )

    # Full-path assertions.
    assert ok is True, "bootstrap should report success once creds land"
    assert creds_file.is_file(), "credentials file must be written by the subprocess"
    written = json.loads(creds_file.read_text())
    # The decisive assertion: the code extracted from the REAL adapter event
    # reached the subprocess's stdin (the subprocess echoed it into the creds).
    assert written["received_code"] == AUTH_CODE
    # URL was posted first; success posted last; both to the login DM.
    assert any("oauth/authorize" in p[2] for p in posts)
    assert any("complete" in p[2] for p in posts)
    assert all(p[1] == LOGIN_DM for p in posts)
