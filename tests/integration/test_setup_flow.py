"""Integration test for `bobi setup` — a real Claude-backed create flow
driven through the HTTP API against an isolated tmp project.

The web server's `build_app` is exercised with Starlette's TestClient and
the *real* LLM source (no injected fake): a Design message routes into the
spec via the digestion brain, the Build pour authors the pack with the
file-authoring prompts, and validate + install leave the real artifacts —
a valid team source and a frozen install image.

v1 is the create-only spine; open mode (use-as-is) and Venn CLI discovery
are deferred to M2, so the old REPL-driven cases are gone with the REPL.
"""

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from bobi import paths
from bobi.setup.state import SetupState
from bobi.setup.webui import server
from .conftest import requires_claude

pytestmark = pytest.mark.claude

NONCE = "integration-nonce"


def _fresh_project(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("BOBI_HOME", str(home))
    project = home / "agents" / "setup-integration" / "run"
    project.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=project, capture_output=True, check=True)
    return project


def _client(state, project):
    app = server.build_app(state, project, nonce=NONCE)
    c = TestClient(app, base_url="http://127.0.0.1")
    c.headers.update({server.NONCE_HEADER: NONCE})
    return c


def _events(sse_text: str) -> list[tuple[str, dict]]:
    out = []
    for block in sse_text.split("\n\n"):
        if not block.strip():
            continue
        ev, data = "message", ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        try:
            out.append((ev, json.loads(data)))
        except json.JSONDecodeError:
            out.append((ev, {}))
    return out


@requires_claude
@pytest.mark.timeout(900)
class TestCreateFlow:
    def test_idea_to_installed_pack(self, tmp_path, monkeypatch):
        project = _fresh_project(tmp_path, monkeypatch)
        state = SetupState()
        c = _client(state, project)

        # 1. Design — one clear message; the brain routes it into the spec.
        msg = ("Build a team that triages incoming GitHub issues for a small "
               "open-source project: label each new issue and draft a first "
               "reply. One role, a 'triager'. It uses GitHub. Nothing "
               "proactive. Keep it simple.")
        r = c.post("/api/message", json={"text": msg})
        assert r.status_code == 200
        events = _events(r.text)
        assert any(ev == "delta" for ev, _ in events), "bob never replied"
        final = next(d for ev, d in events if ev == "state")
        assert final["spec"]["goal"].strip(), "goal slot stayed empty"

        # 2. Build — the real pour authors the pack to disk.
        r = c.post("/api/build")
        assert r.status_code == 200
        build_events = _events(r.text)
        authored = {d["path"] for ev, d in build_events if ev == "file_start"}
        assert "agent.yaml" in authored and "agent.md" in authored
        assert any(p.startswith("roles/") for p in authored)

        team = state.team_name
        pack = Path(state.source_dir)
        cfg = yaml.safe_load((pack / "agent.yaml").read_text())
        entry = cfg["entry_point"]
        role_text = (pack / "roles" / entry / "ROLE.md").read_text()
        assert len(role_text) > 200, "role prompt looks like a placeholder"

        from bobi.workflow.schema import load_workflow
        load_workflow(pack / "workflows" / "adhoc.yaml")

        # 3. Validate — the real structural validator must pass.
        v = c.post("/api/validate").json()
        assert v["passed"] is True, v["report"]

        # 4. Install — freeze the image.
        i = c.post("/api/install").json()
        assert i["installed"] == team
        installed = yaml.safe_load(
            paths.agent_yaml_path(project).read_text())
        assert installed.get("agent") == team
        assert paths.install_manifest_path(project).exists()
        assert (paths.package_dir(project) / ".gitignore").exists()
