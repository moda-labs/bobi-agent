"""Tests for `bobi deploy-init` scaffolding (bobi/scaffold.py)."""

from pathlib import Path

import yaml
import pytest
from click.testing import CliRunner

from bobi import scaffold
from bobi.cli import main


AGENT_YAML = """\
version: "1.0.0"
entry_point: director
event_server: ${BOBI_EVENT_SERVER}
services:
  - name: slack
    channels: ${SLACK_CHANNELS}
    credentials:
      bot_token: ${SLACK_BOT_TOKEN}
  - name: github
    credentials:
      token: ${GH_TOKEN}
requires:
  - name: codex
    check: 'test -n "${OPENAI_API_KEY:-}"'
"""


def _make_team(root: Path, name: str, body: str = AGENT_YAML) -> Path:
    d = root / "agents" / name
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(body)
    return d


# --- pure helpers ------------------------------------------------------------

def test_prefix_mirrors_tr():
    assert scaffold.prefix_for("eng-team") == "ENG_TEAM__"
    assert scaffold.prefix_for("market-research") == "MARKET_RESEARCH__"
    assert scaffold.prefix_for("smoke") == "SMOKE__"


def test_discover_teams(tmp_path):
    _make_team(tmp_path, "eng-team")
    _make_team(tmp_path, "support")
    (tmp_path / "agents" / "not-a-team").mkdir()  # no agent.yaml → ignored
    assert scaffold.discover_teams(tmp_path) == ["eng-team", "support"]


def test_discover_teams_no_agents_dir(tmp_path):
    assert scaffold.discover_teams(tmp_path) == []


def test_secret_keys_api_key_adds_anthropic_drops_bobi(tmp_path):
    _make_team(tmp_path, "eng-team")
    keys = scaffold.secret_keys_for(tmp_path, "eng-team", "api_key")
    # declared service/runtime vars, BOBI_* excluded, ANTHROPIC overlaid
    assert set(keys) == {
        "SLACK_CHANNELS", "SLACK_BOT_TOKEN", "GH_TOKEN",
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
    assert "BOBI_EVENT_SERVER" not in keys


def test_secret_keys_subscription_omits_anthropic(tmp_path):
    _make_team(tmp_path, "eng-team")
    keys = scaffold.secret_keys_for(tmp_path, "eng-team", "subscription")
    assert "ANTHROPIC_API_KEY" not in keys
    assert "SLACK_BOT_TOKEN" in keys


def test_subscription_drops_explicitly_declared_anthropic(tmp_path):
    body = "services:\n  - name: x\n    credentials:\n      k: ${ANTHROPIC_API_KEY}\n"
    _make_team(tmp_path, "t", body)
    assert scaffold.secret_keys_for(tmp_path, "t", "subscription") == []


# --- generated workflow ------------------------------------------------------

def test_render_workflow_pins_version_and_uses_pypi():
    wf = scaffold.render_workflow("9.9.9")
    assert "__BOBI_VERSION__" not in wf
    assert 'BOBI_VERSION: "9.9.9"' in wf
    assert 'pip install "bobi==${BOBI_VERSION}"' in wf
    assert "pip install -e ." not in wf          # standalone, not the framework copy
    assert "fly apps list --json" in wf          # orphans enumerates inline, no fleet.sh
    doc = yaml.safe_load(wf)
    assert set(doc["jobs"]) == {"plan", "deploy", "orphans"}


# --- scaffold() --------------------------------------------------------------

def test_scaffold_writes_expected_files(tmp_path):
    _make_team(tmp_path, "eng-team")
    res = scaffold.scaffold(
        tmp_path, teams=["eng-team"], fleet="acme", tenant="prod",
        event_server=None, auth="api_key", force=False, version="1.2.3")
    rels = {p.relative_to(tmp_path).as_posix() for p in res.written}
    assert rels == {
        ".github/workflows/deploy-agent-teams.yml",
        "deployments/defaults.yaml",
        "deployments/eng-team.yaml"}
    defaults = yaml.safe_load((tmp_path / "deployments/defaults.yaml").read_text())
    assert defaults["fleet"] == "acme" and defaults["tenant"] == "prod"
    dep = yaml.safe_load((tmp_path / "deployments/eng-team.yaml").read_text())
    assert dep["team"] == "eng-team" and dep["secrets"]["env"] == "eng-team"
    assert "auth" not in dep  # api_key is the default → no explicit line


def test_scaffold_is_non_destructive_then_force(tmp_path):
    _make_team(tmp_path, "eng-team")
    (tmp_path / "deployments").mkdir()
    (tmp_path / "deployments/defaults.yaml").write_text("fleet: existing\n")
    res = scaffold.scaffold(
        tmp_path, teams=["eng-team"], fleet="acme", tenant="prod",
        event_server=None, auth="api_key", force=False, version="1.2.3")
    assert (tmp_path / "deployments/defaults.yaml") in res.skipped
    assert (tmp_path / "deployments/defaults.yaml").read_text() == "fleet: existing\n"

    res2 = scaffold.scaffold(
        tmp_path, teams=["eng-team"], fleet="acme", tenant="prod",
        event_server=None, auth="api_key", force=True, version="1.2.3")
    assert (tmp_path / "deployments/defaults.yaml") in res2.written
    assert "fleet: acme" in (tmp_path / "deployments/defaults.yaml").read_text()


def test_scaffold_subscription_writes_auth_line(tmp_path):
    _make_team(tmp_path, "eng-team")
    scaffold.scaffold(
        tmp_path, teams=["eng-team"], fleet="acme", tenant="prod",
        event_server="https://ev.example.com", auth="subscription",
        force=False, version="1.2.3")
    dep = yaml.safe_load((tmp_path / "deployments/eng-team.yaml").read_text())
    assert dep["auth"] == "subscription"
    assert "https://ev.example.com" in (tmp_path / "deployments/defaults.yaml").read_text()


def test_next_steps_lists_every_secret(tmp_path):
    _make_team(tmp_path, "eng-team")
    res = scaffold.scaffold(
        tmp_path, teams=["eng-team"], fleet="acme", tenant="prod",
        event_server=None, auth="api_key", force=False, version="1.2.3")
    steps = scaffold.next_steps(res)
    assert "gh secret set FLY_API_TOKEN" in steps
    assert "gh api -X PUT repos/" in steps and "/environments/prod" in steps
    for key in ("SLACK_BOT_TOKEN", "GH_TOKEN", "ANTHROPIC_API_KEY"):
        assert f"gh secret set ENG_TEAM__{key} --env prod" in steps
    assert "git tag deploy-1" in steps


# --- CLI end-to-end ----------------------------------------------------------

def test_cli_deploy_init_scaffolds(tmp_path, monkeypatch):
    _make_team(tmp_path, "eng-team")
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(main, ["deploy-init", "eng-team",
                                    "--fleet", "acme", "--tenant", "prod"])
    assert res.exit_code == 0, res.output
    assert "wrote   .github/workflows/deploy-agent-teams.yml" in res.output
    assert "gh secret set ENG_TEAM__SLACK_BOT_TOKEN --env prod" in res.output
    assert (tmp_path / ".github/workflows/deploy-agent-teams.yml").is_file()


def test_cli_deploy_init_unknown_team(tmp_path, monkeypatch):
    _make_team(tmp_path, "eng-team")
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(main, ["deploy-init", "nope"])
    assert res.exit_code != 0
    assert "no agents/nope/agent.yaml" in res.output


def test_cli_deploy_init_no_teams(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(main, ["deploy-init"])
    assert res.exit_code != 0
    assert "no teams found" in res.output
