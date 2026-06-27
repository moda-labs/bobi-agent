"""Tests for the deterministic setup actions.

These call the action functions directly — no SDK tool layer, no stage
gating. Actions own argument validation, the work, state mutation, and
checkpoint persistence; they raise ActionError for business-rule
refusals. (Stage gating lives in the caller — the web server — and is
tested in test_setup_server.py.)
"""

import os
from pathlib import Path

import pytest
import yaml

from bobi import paths
from bobi.setup import actions
from bobi.setup.actions import ActionError
from bobi.setup.state import SetupState, source_tree_hash


def _write_minimal_pack(pack_dir: Path, entry="manager", with_adhoc=True):
    (pack_dir / "roles" / entry).mkdir(parents=True, exist_ok=True)
    (pack_dir / "roles" / entry / "ROLE.md").write_text(f"# {entry}\nYou run things.")
    (pack_dir / "agent.md").write_text("# Team\nA test team.")
    (pack_dir / "agent.yaml").write_text(yaml.dump({
        "version": "1.0.0", "entry_point": entry,
        "services": [{"name": "github", "events": True}],
    }))
    wf = pack_dir / "workflows"
    wf.mkdir(exist_ok=True)
    if with_adhoc:
        (wf / "adhoc.yaml").write_text(yaml.dump({
            "name": "adhoc", "trigger": "Any ad-hoc task.",
            "description": "Open-ended task.",
            "steps": [{"name": "task", "prompt": "${{input.task}}"}],
        }))


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("BOBI_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def build_state():
    state = SetupState(team_name="my-team", source_dir="agents/my-team")
    state.spec.goal = "Triage incoming GitHub issues and assign owners."
    return state


# --- constants -----------------------------------------------------------

class TestConstants:
    def test_pack_slug_accepts_valid_and_rejects_invalid(self):
        assert actions.PACK_SLUG.match("sales-outreach")
        assert actions.PACK_SLUG.match("team1")
        assert not actions.PACK_SLUG.match("My Team!")
        assert not actions.PACK_SLUG.match("-leading-dash")

    def test_secret_shapes_matches_known_tokens(self):
        assert actions.SECRET_SHAPES.search("xoxb-abc")
        assert actions.SECRET_SHAPES.search("ghp_deadbeef")
        assert not actions.SECRET_SHAPES.search("a perfectly ordinary sentence")


class TestRedactSecrets:
    def test_redacts_provider_tokens(self):
        for secret in [
            # built from parts so no contiguous token literal sits in source
            # (GitHub push protection flags Slack-shaped literals)
            "xoxb-" + "1" * 12 + "-" + "a" * 16,
            "ghp_" + "a" * 36,
            "github_pat_" + "B" * 30,
            "sk-ant-api03-" + "c" * 40,
            "sk-" + "d" * 40,
            "lin_api_" + "e" * 30,
            "venn_" + "f" * 20,
            "AKIA" + "A" * 16,
            "AIza" + "z" * 35,
        ]:
            out, n = actions.redact_secrets(f"my key is {secret} ok")
            assert n >= 1, secret
            assert secret not in out, secret
            assert "[redacted]" in out

    def test_redacts_jwt_and_private_key_block(self):
        jwt = "eyJ" + "a" * 30 + ".bbb.ccc"
        out, n = actions.redact_secrets(f"token {jwt}")
        assert jwt not in out and n == 1
        pem = ("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\nxyz\n"
               "-----END RSA PRIVATE KEY-----")
        out, n = actions.redact_secrets(f"here:\n{pem}\ndone")
        assert "MIIabc" not in out and n == 1

    def test_redacts_keyword_value_pairs(self):
        out, n = actions.redact_secrets("password: hunter2 and API_KEY=shortish")
        assert "hunter2" not in out and "shortish" not in out
        assert n == 2

    def test_leaves_ordinary_prose_untouched(self):
        text = ("I want a team that triages GitHub issues and posts to Slack "
                "every morning, escalating blockers to me.")
        out, n = actions.redact_secrets(text)
        assert out == text and n == 0

    def test_is_idempotent(self):
        once, n1 = actions.redact_secrets("key sk-" + "x" * 40)
        twice, n2 = actions.redact_secrets(once)
        assert twice == once and n2 == 0


# --- env helpers ---------------------------------------------------------

class TestEnvHelpers:
    def test_write_then_read_roundtrip(self, project):
        actions.write_env(project, {"A": "1", "B": "2"})
        assert actions.read_env(project) == {"A": "1", "B": "2"}

    def test_read_missing_env_is_empty(self, project):
        assert actions.read_env(project) == {}

    def test_venn_key_prefers_environment_over_env_file(self, project, monkeypatch):
        actions.write_env(project, {"VENN_API_KEY": "from-file"})
        monkeypatch.delenv("VENN_API_KEY", raising=False)
        assert actions.venn_key(project) == "from-file"
        monkeypatch.setenv("VENN_API_KEY", "from-environ")
        assert actions.venn_key(project) == "from-environ"

    def test_mask_hides_middle_of_long_value(self):
        assert actions.mask("xoxb-very-long-secret") == "xoxb…cret"
        assert actions.mask("short") == "•••"


# --- installed_team_name -------------------------------------------------

class TestInstalledTeamName:
    def test_none_when_no_install(self, project):
        assert actions.installed_team_name(project) is None

    def test_reads_agent_name(self, project):
        from bobi import paths
        agent_yaml = paths.agent_yaml_path(project)
        agent_yaml.parent.mkdir(parents=True, exist_ok=True)
        agent_yaml.write_text(yaml.dump({"agent": "eng-team"}))
        assert actions.installed_team_name(project) == "eng-team"


# --- resolve_or_fetch ----------------------------------------------------

class TestResolveOrFetch:
    def test_resolves_local_pack_without_fetching(self, project, monkeypatch):
        _write_minimal_pack(project / "agents" / "local-team")
        def boom(*a, **k):
            raise AssertionError("should not fetch a locally-resolvable team")
        monkeypatch.setattr("bobi.registry.fetch", boom)
        resolved = actions.resolve_or_fetch("local-team", project)
        assert resolved == project / "agents" / "local-team"

    def test_returns_none_when_unresolvable(self, project, monkeypatch):
        monkeypatch.setattr("bobi.registry.fetch", lambda *a, **k: None)
        assert actions.resolve_or_fetch("ghost-team", project) is None


# --- save_credential -----------------------------------------------------

class TestSaveCredential:
    def test_writes_env_and_records_state_without_returning_value(self, project,
                                                                  monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "placeholder")
        monkeypatch.delenv("SLACK_BOT_TOKEN")
        state = SetupState()
        secret = "xoxb-very-secret-value-12345"
        payload = actions.save_credential(
            state, project, "SLACK_BOT_TOKEN", "slack", "paste it",
            prompt_fn=lambda v, s, i: secret)
        assert payload["saved"] is True
        assert secret not in str(payload)
        assert payload["masked"] == actions.mask(secret)
        assert f"SLACK_BOT_TOKEN={secret}" in actions.env_path(project).read_text()
        assert "SLACK_BOT_TOKEN" in state.credentials_saved
        # checkpointed to disk
        assert SetupState.load(project).credentials_saved == ["SLACK_BOT_TOKEN"]

    def test_empty_input_is_skip(self, project):
        state = SetupState()
        payload = actions.save_credential(
            state, project, "LINEAR_API_KEY", "linear", "",
            prompt_fn=lambda v, s, i: "")
        assert payload == {"saved": False, "skipped": True, "var": "LINEAR_API_KEY"}
        assert not actions.env_path(project).exists()

    def test_merges_with_existing_env(self, project, monkeypatch):
        monkeypatch.setenv("NEW_VAR", "placeholder")
        monkeypatch.delenv("NEW_VAR")
        actions.write_env(project, {"EXISTING": "keep"})
        actions.save_credential(state := SetupState(), project, "NEW_VAR", "", "",
                                prompt_fn=lambda v, s, i: "val")
        env = actions.read_env(project)
        assert env == {"EXISTING": "keep", "NEW_VAR": "val"}

    def test_refreshes_process_environment(self, project, monkeypatch):
        monkeypatch.setenv("LINEAR_API_KEY", "stale-value")
        actions.save_credential(SetupState(), project, "LINEAR_API_KEY",
                                "linear", "", prompt_fn=lambda v, s, i: "fresh")
        assert os.environ["LINEAR_API_KEY"] == "fresh"

    def test_bad_var_name_raises(self, project):
        with pytest.raises(ActionError):
            actions.save_credential(SetupState(), project, "not-a-var", "", "",
                                    prompt_fn=lambda v, s, i: "x")

    def test_framework_var_raises(self, project):
        with pytest.raises(ActionError):
            actions.save_credential(SetupState(), project,
                                    "BOBI_VENN_API_BASE", "", "",
                                    prompt_fn=lambda v, s, i: "x")


# --- validate_team / validate_pack ---------------------------------------

class TestValidateTeam:
    def test_passing_pack_freezes_validated_hash(self, project, build_state):
        pack = project / "agents" / "my-team"
        _write_minimal_pack(pack)
        result = actions.validate_team(build_state, project)
        assert result["passed"] is True
        assert result["failure_count"] == 0
        assert build_state.validated is True
        assert build_state.validated_hash == source_tree_hash(pack)
        assert SetupState.load(project).validated is True

    def test_missing_adhoc_fails_and_clears_hash(self, project, build_state):
        build_state.validated_hash = "left-over"
        _write_minimal_pack(project / "agents" / "my-team", with_adhoc=False)
        result = actions.validate_team(build_state, project)
        assert result["passed"] is False
        assert result["failure_count"] >= 1
        assert "adhoc.yaml" in result["report"]
        assert build_state.validated is False
        assert build_state.validated_hash == ""

    def test_saved_secret_value_detected(self, project, build_state):
        actions.write_env(project, {"STRIPE_KEY": "zq_live_totally_novel_shape_123"})
        pack = project / "agents" / "my-team"
        _write_minimal_pack(pack)
        (pack / "agent.md").write_text(
            "# Team\nUses key zq_live_totally_novel_shape_123 for Stripe.")
        result = actions.validate_team(build_state, project)
        assert "literal secret" in result["report"]
        assert result["passed"] is False

    def test_autonomous_behaviors_must_be_written(self, project, build_state):
        build_state.spec.autonomous = [
            {"description": "ping me about stale PRs each morning",
             "leash": "notify", "cadence": "1d"}]
        _write_minimal_pack(project / "agents" / "my-team")
        result = actions.validate_team(build_state, project)
        assert "defaults.yaml is missing" in result["report"]
        assert result["passed"] is False

    def test_nonexistent_pack_dir_fails(self, project, build_state):
        result = actions.validate_team(build_state, project)
        assert result["passed"] is False
        assert "does not exist" in result["report"]


# --- install_team --------------------------------------------------------

class TestInstallTeam:
    def test_installs_and_reports_missing_credentials(self, project, build_state):
        pack = project / "agents" / "my-team"
        _write_minimal_pack(pack)
        cfg = yaml.safe_load((pack / "agent.yaml").read_text())
        cfg["slack"] = {"bot_token": "${SLACK_BOT_TOKEN}"}
        (pack / "agent.yaml").write_text(yaml.dump(cfg))

        build_state.validated = True
        build_state.validated_hash = source_tree_hash(pack)

        payload = actions.install_team(build_state, project)
        assert payload["installed"] == "my-team"
        assert "SLACK_BOT_TOKEN" in payload["missing_credentials"]
        assert paths.agent_yaml_path(project).exists()
        assert paths.install_manifest_path(project).exists()
        assert build_state.installed is True
        assert SetupState.load(project).installed is True

    def test_stale_validation_raises_and_unsets_validated(self, project, build_state):
        _write_minimal_pack(project / "agents" / "my-team")
        build_state.validated = True
        build_state.validated_hash = "old-hash"
        with pytest.raises(ActionError) as exc:
            actions.install_team(build_state, project)
        assert "changed since" in str(exc.value)
        assert build_state.validated is False

    def test_missing_source_raises(self, project, build_state):
        with pytest.raises(ActionError) as exc:
            actions.install_team(build_state, project)
        assert "not found" in str(exc.value)


# --- run_preflight -------------------------------------------------------

class TestRunPreflight:
    def test_delegates_to_validate_config(self, project, monkeypatch):
        sentinel = object()
        seen = {}
        def fake_validate_config(p):
            seen["project"] = p
            return sentinel
        monkeypatch.setattr("bobi.validate.validate_config",
                            fake_validate_config)
        assert actions.run_preflight(project) is sentinel
        assert seen["project"] == project
