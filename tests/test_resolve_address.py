"""`ask`/`message` must find the coordinator by the installed entry_point.

_resolve_address("manager") used to look up the literal role "manager" —
any pack whose entry point has a different role name (research_manager,
director, ...) had a broken interactive loop: the session was running
with a live inbox, but `modastack ask` reported "No active manager
session found". Found live-testing the market-research pack.
"""

import json
import os
from dataclasses import asdict

import pytest

from modastack.sdk import SessionEntry, set_project_root


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / ".modastack" / "sessions").mkdir(parents=True)
    (tmp_path / ".modastack" / "agent.yaml").write_text(
        "agent: market-research\nentry_point: research_manager\nservices: []\n"
    )
    monkeypatch.chdir(tmp_path)
    set_project_root(tmp_path)
    import modastack.sdk as sdk
    sdk._registry = None
    yield tmp_path
    set_project_root(None)
    sdk._registry = None


def _register(project, name, role):
    session_dir = project / ".modastack" / "sessions" / name
    session_dir.mkdir(parents=True)
    entry = SessionEntry(
        name=name, session_id="sess-x", role=role, run_key="",
        phase="", status="running", pid=os.getpid(), inbox_port=12345,
    )
    (session_dir / "state.json").write_text(json.dumps(asdict(entry)))


def test_resolves_entry_point_role(project):
    from modastack.cli import _resolve_address
    _register(project, "moda-research_manager-proj", "research_manager")
    assert _resolve_address("manager") == "moda-research_manager-proj"
    assert _resolve_address(None) == "moda-research_manager-proj"


def test_literal_manager_role_still_resolves(project):
    from modastack.cli import _resolve_address
    (project / ".modastack" / "agent.yaml").write_text(
        "agent: eng\nentry_point: manager\nservices: []\n")
    _register(project, "moda-manager-proj", "manager")
    assert _resolve_address("manager") == "moda-manager-proj"


def test_exact_name_passthrough(project):
    from modastack.cli import _resolve_address
    assert _resolve_address("some-exact-session") == "some-exact-session"
