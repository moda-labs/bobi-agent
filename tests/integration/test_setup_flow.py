"""Integration tests for `modastack setup` — real Claude sessions driving
the full onboarding state machine against an isolated tmp project.

Each test runs run_repl in-process with a scripted input queue and a
non-interactive secret prompt, then asserts on the artifacts setup is
supposed to leave behind: a valid team source, a frozen install image,
manifest, and gitignore.
"""

import asyncio
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from modastack.setup.repl import run_repl
from .conftest import requires_claude

PACKAGE_ROOT = Path(__file__).parent.parent.parent

# A drained queue keeps nudging the model to wrap up rather than pausing.
NUDGE = ("Proceed with your best judgment, do not ask me anything else, "
         "and finish the setup.")


def _fresh_project(tmp_path: Path) -> Path:
    project = tmp_path / "setup-project"
    project.mkdir()
    subprocess.run(["git", "init"], cwd=project, capture_output=True, check=True)
    return project


def _drive_setup(project: Path, first_message: str, secrets: dict | None = None,
                 nudges: int = 30) -> int:
    queue = [first_message] + [NUDGE] * nudges

    def input_fn():
        return queue.pop(0) if queue else None

    def secret_prompt(var, service, instructions):
        return (secrets or {}).get(var, "")

    return asyncio.run(run_repl(project, input_fn=input_fn,
                                secret_prompt_fn=secret_prompt))


def _assert_installed_image(project: Path, team: str):
    installed = yaml.safe_load((project / ".modastack" / "agent.yaml").read_text())
    assert installed.get("agent") == team
    assert (project / ".modastack" / "install-manifest.json").exists()
    gitignore = (project / ".modastack" / ".gitignore").read_text()
    assert "roles/" in gitignore  # local-source install: image is an artifact


@requires_claude
@pytest.mark.timeout(900)
class TestBuildYourOwn:
    def test_idea_to_runnable_pack(self, tmp_path):
        project = _fresh_project(tmp_path)
        first = (
            "I want to build an agent team named exactly 'support-triage'. "
            "Purpose: triage GitHub issues for a small open-source project. "
            "Roles: a single 'triager' role that labels new issues and "
            "drafts a first reply. Services: github only. Chat: none. "
            "No scheduled jobs. Event triggers: only GitHub issue opens "
            "(native webhooks, no monitors needed). No approval gates. "
            "Skip discovery (no venn services) and skip any credentials. "
            "Do not ask me any questions — make every remaining decision "
            "yourself, generate the team, validate it, install it, and "
            "finish the setup."
        )
        code = _drive_setup(project, first)
        assert code == 0, "setup did not reach finish_setup"

        pack = project / "agents" / "support-triage"
        cfg = yaml.safe_load((pack / "agent.yaml").read_text())
        entry = cfg["entry_point"]
        assert (pack / "roles" / entry / "ROLE.md").exists()
        role_text = (pack / "roles" / entry / "ROLE.md").read_text()
        assert len(role_text) > 200, "role prompt is a placeholder"

        from modastack.workflow.schema import load_workflow
        load_workflow(pack / "workflows" / "adhoc.yaml")

        _assert_installed_image(project, "support-triage")
        # setup state cleared on success
        assert not (project / ".modastack" / "state" / "setup.json").exists()


@requires_claude
@pytest.mark.timeout(600)
class TestUseAsIs:
    def test_existing_team_straight_to_install(self, tmp_path):
        project = _fresh_project(tmp_path)
        src = PACKAGE_ROOT / "agents" / "dogfood-content-review"
        shutil.copytree(src, project / "agents" / "dogfood-content-review")

        first = (
            "Install the existing team 'dogfood-content-review' exactly "
            "as it is. Skip every credential (leave them blank) and do not "
            "ask me anything — install it and finish the setup."
        )
        code = _drive_setup(project, first)
        assert code == 0, "setup did not reach finish_setup"

        _assert_installed_image(project, "dogfood-content-review")
        # use-as-is generates nothing new
        assert (project / ".modastack" / "roles").is_dir()


class _VennStub(BaseHTTPRequestHandler):
    """Minimal Venn REST stub: every POST answers list_servers."""

    def do_POST(self):
        body = json.dumps({
            "success": True,
            "result": {"servers": [
                {"server_id": "work-gmail", "server_name": "gmail",
                 "connected": True},
            ]},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


FAKE_VENN = r'''#!/bin/bash
# Replays recorded venn CLI output for setup discovery tests.
case "$*" in
  "help list_servers"*)
    echo '[{"server_id": "work-gmail", "server_name": "gmail", "connected": true}]' ;;
  *"tools search"*)
    echo '[{"server_id": "work-gmail", "tool": "list_messages", "rank": 1}]' ;;
  *"tools describe"*)
    echo '{"name": "list_messages", "args": {"maxResults": "int", "q": "string"}}' ;;
  *"tools execute"*)
    echo '[{"id": "msg-1", "subject": "Hello"}, {"id": "msg-2", "subject": "Re: Hi"}]' ;;
  *)
    echo "unknown venn invocation: $*" >&2; exit 1 ;;
esac
'''


@requires_claude
@pytest.mark.timeout(900)
class TestVennDiscovery:
    def test_discovery_records_tested_command_monitor(self, tmp_path, monkeypatch):
        project = _fresh_project(tmp_path)

        # Fake venn binary on PATH + REST stub for check_venn.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        venn = bin_dir / "venn"
        venn.write_text(FAKE_VENN)
        venn.chmod(0o755)
        import os
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

        server = HTTPServer(("127.0.0.1", 0), _VennStub)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setenv("MODASTACK_VENN_API_BASE",
                           f"http://127.0.0.1:{server.server_port}")
        try:
            first = (
                "I want to build an agent team named exactly 'inbox-watch'. "
                "Purpose: watch my email inbox and summarize important "
                "unread messages. Roles: a single 'watcher' role. Services: "
                "email (via venn). Chat: none. No scheduled jobs. Event "
                "trigger: new unread emails, polled via a venn command "
                "monitor — discover the right venn tool, test it, and "
                "record a command monitor for it. No approval gates. When "
                "you collect the venn API key with save_credential I will "
                "provide it. Do not ask me any questions — make every "
                "remaining decision yourself, generate the team including "
                "monitors/defaults.yaml, validate it, install it, and "
                "finish the setup."
            )
            code = _drive_setup(project, first,
                                secrets={"VENN_API_KEY": "venn-test-key"})
        finally:
            server.shutdown()

        assert code == 0, "setup did not reach finish_setup"

        pack = project / "agents" / "inbox-watch"
        monitors = yaml.safe_load((pack / "monitors" / "defaults.yaml").read_text())
        records = monitors["monitors"]
        assert any("venn" in (m.get("command") or "") for m in records), (
            f"no venn command monitor recorded: {records}")

        env_text = (project / ".modastack" / ".env").read_text()
        assert "VENN_API_KEY=venn-test-key" in env_text

        _assert_installed_image(project, "inbox-watch")
