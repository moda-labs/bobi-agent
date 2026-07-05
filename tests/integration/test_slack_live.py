"""Live Slack gateway tests (#643) - the recurring soak for #190 Phase 2.

Runs the REAL outbound path against the REAL Slack Web API: `bobi reply`
signs with the instance bubble, POSTs to the real local Node event server,
which delivers through the Chat SDK Slack adapter to slack.com. This is the
only automated check that live Slack ACCEPTS what the gateway sends - in
particular `markdown_text` on chat.update, which the SDK assumes works.

Gated: skipped unless SLACK_BOT_TOKEN and SLACK_TEST_CHANNEL are set
(a dev-workspace bot with chat:write, files:write, channels:history, invited
to the sacrificial test channel). Locally:

    set -a; source .bobi-dogfood.env; set +a
    pytest tests/integration/test_slack_live.py -m live

Messages are deliberately left in the channel: the dogfood battery's
rendering eyeball step (Section 3c) inspects the thread this run creates.
Rendering fidelity is the one thing these tests cannot assert - they prove
API acceptance and content round-trip, not that the markdown looks right.
"""

import json
import os
import re
import signal
import socket
import time
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner

from bobi.config import save_bubble_state
from bobi.events.gateway import channels_history, channels_send
from bobi.events.server import _post_register, ensure_running
from bobi.events.signing import serialize_body, sign_headers

pytestmark = [
    pytest.mark.live,
    pytest.mark.timeout(180),
    pytest.mark.skipif(
        not (os.environ.get("SLACK_BOT_TOKEN")
             and os.environ.get("SLACK_TEST_CHANNEL")),
        reason="live Slack not configured (SLACK_BOT_TOKEN, SLACK_TEST_CHANNEL)",
    ),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for(predicate, timeout: float = 15.0, interval: float = 1.0):
    """Poll ``predicate`` until it returns a truthy value (returned) or the
    timeout elapses (returns the last falsy value) - Slack history reads are
    eventually consistent."""
    deadline = time.monotonic() + timeout
    result = predicate()
    while not result and time.monotonic() < deadline:
        time.sleep(interval)
        result = predicate()
    return result


@pytest.fixture(scope="module")
def live_env(tmp_path_factory):
    """A real local event server pointed at the REAL Slack API, with the dev
    workspace token registered, and one thread in the test channel that all
    tests in this module post into."""
    token = os.environ["SLACK_BOT_TOKEN"]
    channel = os.environ["SLACK_TEST_CHANNEL"]
    # Must be the channel ID, not a #name: conversation refs always carry IDs
    # (they come from webhooks), and chat.update rejects names with the
    # misleading error channel_not_found even though chat.postMessage resolves
    # them.
    assert re.fullmatch(r"[CG][A-Z0-9]+", channel), (
        f"SLACK_TEST_CHANNEL must be a channel ID (C...), got {channel!r} - "
        "copy it from the channel's About tab in Slack"
    )

    # Token present but invalid must FAIL, not skip.
    auth = httpx.post(
        "https://slack.com/api/auth.test",
        headers={"Authorization": f"Bearer {token}"}, timeout=15,
    ).json()
    assert auth.get("ok"), f"SLACK_BOT_TOKEN rejected by auth.test: {auth}"
    team_id = auth["team_id"]

    base = tmp_path_factory.mktemp("slack-live")
    project = base / "run"
    (project / "package").mkdir(parents=True)
    (project / "state").mkdir(parents=True)

    port = _free_port()
    es_url = f"http://localhost:{port}"
    (project / "package" / "agent.yaml").write_text(
        f"entry_point: manager\nevent_server: {es_url}\n")

    # Make sure no stub redirect leaks in from another suite's environment.
    os.environ.pop("BOBI_ES_SLACK_API_URL", None)
    status = ensure_running(port, project_path=project)
    assert status in ("started", "connected")

    minted = _post_register(es_url, "slack-live-bootstrap", ["_bootstrap"])
    save_bubble_state(project, minted["bubble_id"], minted["bubble_key"])

    # Real registration: the server verifies the token against live auth.test
    # and stores the bubble-scoped send credential.
    body = serialize_body({"workspace_id": team_id, "bot_token": token})
    headers = {"Content-Type": "application/json"}
    headers.update(sign_headers(
        minted["bubble_id"], minted["bubble_key"],
        "POST", "/slack/workspaces", body,
    ))
    resp = httpx.post(f"{es_url}/slack/workspaces", content=body,
                      headers=headers, timeout=30)
    assert resp.status_code == 200, resp.text

    # One labeled thread per run keeps the sacrificial channel browsable.
    marker = f"soak-{int(time.time())}"
    root = channels_send(
        project, f"slack:{team_id}:channel:{channel}",
        f"Live gateway soak `{marker}` (tests/integration/test_slack_live.py)",
    )
    root_ts = root.get("ts")
    assert root_ts, f"root post returned no ts: {root}"

    yield SimpleNamespace(
        project=project,
        channel=channel,
        team_id=team_id,
        es_url=es_url,
        marker=marker,
        thread=f"slack:{team_id}:channel:{channel}:thread:{root_ts}",
    )

    pid_file = project / "state" / "event-server.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass


@pytest.fixture
def live(live_env, monkeypatch):
    monkeypatch.setenv("BOBI_ROOT", str(live_env.project))
    return live_env


def _thread_messages(env) -> list[dict]:
    return channels_history(env.project, env.thread)


class TestLiveSlackGateway:
    def test_placeholder_edit_accepts_markdown_text(self, live):
        """The soak's core question: does real Slack accept markdown_text on
        chat.update? Post a placeholder (the exact call the drain loop's
        SlackInputChannel makes), then resolve it with `bobi reply --edit`."""
        placeholder = channels_send(live.project, live.thread, "Evaluating…")
        placeholder_ts = placeholder.get("ts")
        assert placeholder_ts, f"placeholder post returned no ts: {placeholder}"

        token = f"edit-{live.marker}"
        markdown = (
            f"# Gateway soak {token}\n"
            "**bold**, _italic_, `inline code`, and a list:\n"
            "- markdown_text on chat.update\n"
            "- rendered by Slack, not converted client-side\n"
            "```python\nprint('gateway')\n```"
        )
        from bobi.cli import main
        result = CliRunner().invoke(main, [
            "reply", live.thread, "--edit", placeholder_ts, markdown,
        ])
        assert result.exit_code == 0, result.output

        def edited():
            for msg in _thread_messages(live):
                if msg["ts"] == placeholder_ts and token in msg["text"]:
                    return msg
            return None

        msg = _wait_for(edited)
        assert msg, (
            f"edited placeholder {placeholder_ts} never showed {token}: "
            f"{json.dumps(_thread_messages(live), indent=2)}"
        )
        assert "Evaluating" not in msg["text"]

    def test_reply_file_upload(self, live, tmp_path):
        token = f"file-{live.marker}"
        f = tmp_path / "soak-report.txt"
        f.write_text(f"live gateway file upload {token}\n")

        from bobi.cli import main
        result = CliRunner().invoke(main, [
            "reply", live.thread, "--file", str(f), f"Attaching {token}",
        ])
        assert result.exit_code == 0, result.output

        def uploaded():
            for msg in _thread_messages(live):
                if any(fl["name"] == "soak-report.txt"
                       for fl in msg.get("files", [])):
                    return msg
            return None

        # File attachment propagation is the slowest read-after-write here.
        assert _wait_for(uploaded, timeout=30), (
            f"soak-report.txt never appeared in thread history: "
            f"{json.dumps(_thread_messages(live), indent=2)}"
        )

    def test_edit_with_file_replaces_text_then_attaches(self, live, tmp_path):
        placeholder = channels_send(live.project, live.thread, "Uploading…")
        placeholder_ts = placeholder.get("ts")
        assert placeholder_ts

        token = f"editfile-{live.marker}"
        f = tmp_path / "soak-chart.txt"
        f.write_text("chart bytes\n")

        from bobi.cli import main
        result = CliRunner().invoke(main, [
            "reply", live.thread,
            "--edit", placeholder_ts, "--file", str(f), f"Done: {token}",
        ])
        assert result.exit_code == 0, result.output

        def resolved():
            msgs = _thread_messages(live)
            edited = any(m["ts"] == placeholder_ts and token in m["text"]
                         for m in msgs)
            attached = any(fl["name"] == "soak-chart.txt"
                           for m in msgs for fl in m.get("files", []))
            return edited and attached

        assert _wait_for(resolved, timeout=30), (
            f"edit+file never resolved for {placeholder_ts}: "
            f"{json.dumps(_thread_messages(live), indent=2)}"
        )

    def test_over_budget_reply_arrives_whole_across_chunks(self, live):
        """A >12k reply goes out as multiple real Slack messages with nothing
        truncated (#651): the head and the tail sentinel both arrive, no chunk
        carries the truncation marker, and every chunk clears Slack's limit."""
        token = f"chunk-{live.marker}"
        sentinel = "TAIL-BEYOND-BUDGET"
        paragraphs = "\n\n".join(
            f"paragraph {i} " + ("x" * 300) for i in range(45))
        long_text = f"Chunking soak {token}\n\n{paragraphs}\n\n{sentinel}"
        assert len(long_text) > 12000

        from bobi.cli import main
        result = CliRunner().invoke(main, ["reply", live.thread, long_text])
        assert result.exit_code == 0, result.output

        def delivered():
            msgs = [m for m in _thread_messages(live)
                    if token in m["text"] or sentinel in m["text"]
                    or "paragraph " in m["text"]]
            joined = "\n".join(m["text"] for m in msgs)
            return msgs if (token in joined and sentinel in joined) else None

        msgs = _wait_for(delivered, timeout=30)
        assert msgs, f"chunked message {token} never fully appeared"
        assert len(msgs) > 1, "over-budget reply should span multiple messages"
        for m in msgs:
            assert "(truncated)" not in m["text"]
