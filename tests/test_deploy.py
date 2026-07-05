"""Unit tests for the `bobi deploy` engine (bobi/deploy.py).

The deployment PRIMITIVE: config precedence, delivery-mode selection, identity
naming, secret resolution/validation, and the idempotent provision-or-update
orchestration. Fly + the shell scripts are stubbed (deploy._run is monkeypatched
to record commands), so nothing here touches Fly or the network.

The workflow STRUCTURE (thin-client asserts) is in test_gitops_c22.py.
"""

import logging
import json
import os
import tarfile
from pathlib import Path

import pytest

from bobi import deploy as D


# --- fixtures ----------------------------------------------------------------

def _make_repo(tmp_path: Path) -> Path:
    """A minimal bobi source root: scripts/ + Dockerfile + a local team."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "provision-instance.sh").write_text("#!/usr/bin/env bash\n")
    (repo / "scripts" / "destroy-instance.sh").write_text("#!/usr/bin/env bash\n")
    (repo / "Dockerfile").write_text("FROM scratch\n")
    pkg = repo / "agents" / "eng-team"
    pkg.mkdir(parents=True)
    pkg.joinpath("agent.yaml").write_text(
        "agent: eng-team\nslack_token: ${SLACK_BOT_TOKEN}\n"
    )
    (repo / "deployments").mkdir()
    return repo


@pytest.fixture
def repo(tmp_path):
    return _make_repo(tmp_path)


@pytest.fixture
def recorder(monkeypatch):
    """Record every deploy._run command; stub fly_app_exists per-test."""
    calls = []

    def fake_run(cmd, *, cwd=None, check=True, input_bytes=None, extra_env=None,
                 secret=False):
        calls.append({"cmd": cmd, "cwd": cwd, "input": input_bytes,
                      "extra_env": extra_env, "secret": secret})
        class R:  # noqa: E306
            returncode = 0
        return R()

    monkeypatch.setattr(D, "_run", fake_run)
    monkeypatch.setattr(D, "_fly_bin", lambda: "fly")
    # Secret reconcile reads live names over `fly secrets list` (a subprocess that
    # captures output — not routed through _run). Default to "nothing live" so
    # existing orchestration tests don't shell out; per-test override as needed.
    monkeypatch.setattr(D, "fly_secrets_list", lambda app: set())
    return calls


# --- config precedence + naming ---------------------------------------------

def test_precedence_builtin_defaults_name_file_flags(repo):
    (repo / "deployments" / "defaults.yaml").write_text(
        "fleet: acme\nregion: sjc\n"
    )
    (repo / "deployments" / "eng.yaml").write_text(
        "team-url: https://r/eng.tar.gz\nmemory: 8gb\n"
    )
    cfg = D.load_deploy_config(repo, "eng", {"memory": "16gb", "region": None})
    assert cfg.app_name == "acme-eng"      # <fleet>-<name>
    assert cfg.fleet_stamp == "acme"
    assert cfg.region == "sjc"             # defaults.yaml (override was None → skipped)
    assert cfg.memory == "16gb"            # flag beats file
    assert cfg.cpus == 2                   # built-in
    assert cfg.delivery == "team-url"


def test_bare_name_falls_back_to_local_package_ssh_push(repo):
    cfg = D.load_deploy_config(repo, "eng-team")
    assert cfg.team == "eng-team"
    assert cfg.delivery == "ssh-push"
    assert cfg.app_name == "eng-team"      # no fleet → bare name
    assert cfg.fleet_stamp == "eng-team"


def test_fleet_stamp_and_app_name_with_fleet(repo):
    (repo / "deployments" / "defaults.yaml").write_text("fleet: moda\n")
    cfg = D.load_deploy_config(repo, "eng-team")
    assert cfg.app_name == "moda-eng-team"
    assert cfg.fleet_stamp == "moda"


def test_secrets_nested_mapping_is_flattened(repo):
    (repo / "deployments" / "x.yaml").write_text(
        "team-url: https://r/x.tar.gz\nsecrets:\n  env: prod\n  env-file: ./x.env\n"
    )
    cfg = D.load_deploy_config(repo, "x")
    assert cfg.secrets_env == "prod"
    assert cfg.secrets_env_file == "./x.env"


def test_structured_im_login_channel_is_normalized(repo):
    (repo / "deployments" / "x.yaml").write_text(
        "team-url: https://r/x.tar.gz\n"
        "login_channel:\n"
        "  type: im\n"
        "  user: zachkozick\n"
    )
    cfg = D.load_deploy_config(repo, "x")
    assert cfg.login_channel == "@zachkozick"


# --- validation --------------------------------------------------------------

def test_no_team_source_is_an_error(repo):
    (repo / "deployments" / "bad.yaml").write_text("region: iad\n")
    with pytest.raises(D.DeployError, match="no team source"):
        D.load_deploy_config(repo, "bad")


def test_both_team_sources_is_an_error(repo):
    with pytest.raises(D.DeployError, match="exactly one"):
        D.load_deploy_config(repo, "eng",
                             {"team": "eng-team", "team_url": "https://r/x.tgz"})


def test_bad_auth_is_an_error(repo):
    (repo / "deployments" / "x.yaml").write_text(
        "team-url: https://r/x.tgz\nauth: oauth\n"
    )
    with pytest.raises(D.DeployError, match="auth="):
        D.load_deploy_config(repo, "x")


# --- brain resolution --------------------------------------------------------

def test_team_url_brain_from_deployment_yaml(repo):
    """A team-url package isn't on disk at deploy time, so its brain can't be
    read from the tarball. The deployment yaml's `brain:` fills it, which selects
    the OpenAI auth key (not Anthropic) for an api_key-mode Codex canary."""
    (repo / "deployments" / "codex-smoke.yaml").write_text(
        "team-url: https://r/codex-smoke.tar.gz\nauth: api_key\nbrain: codex\n"
    )
    cfg = D.load_deploy_config(repo, "codex-smoke")
    assert cfg.brain == "codex"
    assert D._brain_api_key(cfg.brain) == "OPENAI_API_KEY"


def test_local_team_brain_wins_over_deployment_yaml(repo):
    """A LOCAL team's agent.yaml is the source of truth for its brain; a stray
    `brain:` in the deployment yaml never overrides it."""
    pkg = repo / "agents" / "codex-team"
    pkg.mkdir(parents=True)
    pkg.joinpath("agent.yaml").write_text("agent: codex-team\nbrain:\n  kind: codex\n")
    (repo / "deployments" / "codex-team.yaml").write_text(
        "team: codex-team\nbrain: claude\n")
    cfg = D.load_deploy_config(repo, "codex-team")
    assert cfg.brain == "codex"  # agent.yaml wins, not the yaml's claude


def test_bad_brain_is_an_error(repo):
    (repo / "deployments" / "x.yaml").write_text(
        "team-url: https://r/x.tgz\nbrain: gemini\n"
    )
    with pytest.raises(D.DeployError, match="brain="):
        D.load_deploy_config(repo, "x")


# --- repo + package resolution ----------------------------------------------

def test_find_repo_root_walks_up(repo):
    deep = repo / "deployments"
    assert D.find_repo_root(deep) == repo


def test_find_repo_root_raises_without_scripts(tmp_path):
    with pytest.raises(D.DeployError, match="not a bobi checkout"):
        D.find_repo_root(tmp_path)


def test_resolve_assets_source_mode_in_a_checkout(repo, tmp_path):
    """In a checkout, build from source (repo Dockerfile)."""
    a = D.resolve_assets(repo, tmp_path)
    assert a.mode == "source"
    assert a.build_context == repo
    assert a.dockerfile == repo / "Dockerfile"
    assert a.build_args == {}
    assert a.provision_sh == repo / "scripts" / "provision-instance.sh"


def test_resolve_assets_binary_mode_from_packaged(tmp_path, monkeypatch):
    """With no checkout, build from the bundled wheel assets (PyPI image)."""
    # A fake packaged _deploy dir (what the wheel ships).
    pkg = tmp_path / "_deploy"
    (pkg / "scripts").mkdir(parents=True)
    (pkg / "docker").mkdir()
    (pkg / "Dockerfile").write_text("FROM scratch\n")
    (pkg / "docker" / "docker-entrypoint.sh").write_text("#!/bin/sh\n")
    (pkg / "scripts" / "provision-instance.sh").write_text("#!/bin/sh\n")
    (pkg / "scripts" / "destroy-instance.sh").write_text("#!/bin/sh\n")
    monkeypatch.setattr(D, "find_repo_root",
                        lambda p=None: (_ for _ in ()).throw(D.DeployError("x")))
    monkeypatch.setattr(D, "_packaged_deploy_dir", lambda: pkg)
    monkeypatch.setattr(D, "_bobi_version", lambda: "9.9.9")

    staging = tmp_path / "staging"
    staging.mkdir()
    a = D.resolve_assets(tmp_path / "elsewhere", staging)
    assert a.mode == "binary"
    assert a.build_args == {"BOBI_BUILD": "pypi", "BOBI_VERSION": "9.9.9"}
    # build context assembled: Dockerfile + docker/ copied into staging
    assert (a.build_context / "Dockerfile").exists()
    assert (a.build_context / "docker" / "docker-entrypoint.sh").exists()
    assert a.provision_sh == pkg / "scripts" / "provision-instance.sh"


def test_local_package_dir_requires_agent_yaml(repo):
    with pytest.raises(D.DeployError, match="not found"):
        D.local_package_dir(repo, "nope")


def test_scan_required_vars_excludes_defaulted(tmp_path):
    y = tmp_path / "agent.yaml"
    y.write_text("a: ${REQUIRED}\nb: ${OPTIONAL:-x}\nc: literal\n")
    assert D.scan_required_vars(y) == ["REQUIRED"]


# --- secret resolution -------------------------------------------------------

def test_env_file_path_is_used_and_validated(repo, tmp_path):
    ef = tmp_path / "my.env"
    ef.write_text("SLACK_BOT_TOKEN=xoxb\nANTHROPIC_API_KEY=sk-ant\n")
    cfg = D.load_deploy_config(repo, "eng-team",
                              {"secrets_env_file": str(ef)})
    out = D.resolve_env_file(cfg, repo, tmp_path)
    vals = dict(l.split("=", 1) for l in out.read_text().splitlines())
    assert vals["SLACK_BOT_TOKEN"] == "xoxb"
    assert oct(out.stat().st_mode)[-3:] == "600"


def test_missing_required_secret_fails_loudly_for_local_team(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = D.load_deploy_config(repo, "eng-team")  # api_key default
    with pytest.raises(D.DeployError, match="missing required secret"):
        D.resolve_env_file(cfg, repo, tmp_path)


def test_declared_but_empty_optional_var_satisfies_validation(repo, tmp_path, monkeypatch):
    """A referenced-but-OPTIONAL var (e.g. eng-team's `channels: ${SLACK_CHANNELS}`,
    empty = whole workspace) must not block deploy when the operator declares it
    empty on purpose. Auth keys are still enforced at boot/provision."""
    monkeypatch.delenv("SLACK_CHANNELS", raising=False)
    pkg = repo / "agents" / "eng-team" / "agent.yaml"
    pkg.write_text(pkg.read_text() + "channels: ${SLACK_CHANNELS}\n")
    ef = tmp_path / "eng.env"
    ef.write_text("SLACK_BOT_TOKEN=xoxb\nANTHROPIC_API_KEY=sk\nSLACK_CHANNELS=\n")
    cfg = D.load_deploy_config(repo, "eng-team", {"secrets_env_file": str(ef)})
    out = D.resolve_env_file(cfg, repo, tmp_path)  # must NOT raise
    vals = dict(l.split("=", 1) for l in out.read_text().splitlines())
    assert vals.get("SLACK_CHANNELS", "MISSING") == ""  # present, intentionally empty


def test_secrets_materialized_from_process_env(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-env")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    cfg = D.load_deploy_config(repo, "eng-team")
    out = D.resolve_env_file(cfg, repo, tmp_path)
    vals = dict(l.split("=", 1) for l in out.read_text().splitlines())
    assert vals["SLACK_BOT_TOKEN"] == "xoxb-env"
    assert vals["ANTHROPIC_API_KEY"] == "sk-ant-env"


def test_bobi_vars_are_not_treated_as_required_secrets(repo, tmp_path, monkeypatch):
    """A package's ${BOBI_EVENT_SERVER} ref is identity the provisioner
    stamps into [env] from flags — never a secret to demand in the env-file."""
    monkeypatch.delenv("BOBI_EVENT_SERVER", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    pkg = repo / "agents" / "eng-team" / "agent.yaml"
    pkg.write_text(pkg.read_text() + "event_server: ${BOBI_EVENT_SERVER}\n")
    cfg = D.load_deploy_config(repo, "eng-team")
    out = D.resolve_env_file(cfg, repo, tmp_path)  # must NOT raise
    assert "BOBI_EVENT_SERVER" not in out.read_text()


def test_subscription_mode_rejects_anthropic_key(repo, tmp_path):
    ef = tmp_path / "sub.env"
    ef.write_text("SLACK_BOT_TOKEN=xoxb\nANTHROPIC_API_KEY=sk-ant\n")
    (repo / "deployments" / "dog.yaml").write_text(
        "team: eng-team\nauth: subscription\n"
    )
    cfg = D.load_deploy_config(repo, "dog", {"secrets_env_file": str(ef)})
    with pytest.raises(D.DeployError, match="ANTHROPIC_API_KEY"):
        D.resolve_env_file(cfg, repo, tmp_path)


# --- brain-aware auth (#485) -------------------------------------------------

def _codex_team(repo):
    """Add a local codex-brained team to the repo fixture."""
    pkg = repo / "agents" / "codex-team"
    pkg.mkdir(parents=True)
    pkg.joinpath("agent.yaml").write_text(
        "agent: codex-team\nbrain:\n  kind: codex\nslack_token: ${SLACK_BOT_TOKEN}\n"
    )


def test_brain_api_key_mapping():
    assert D._brain_api_key("codex") == "OPENAI_API_KEY"
    assert D._brain_api_key("claude") == "ANTHROPIC_API_KEY"
    assert D._brain_api_key("") == "ANTHROPIC_API_KEY"  # default
    assert D._brain_api_key("gateway") == ""  # no provider key (#655)


def _gateway_team(repo):
    pkg = repo / "agents" / "local-team"
    pkg.mkdir(parents=True)
    pkg.joinpath("agent.yaml").write_text(
        "agent: local-team\n"
        "brain:\n  kind: gateway\n  base_url: ${LLM_GATEWAY_URL}\n"
        "slack_token: ${SLACK_BOT_TOKEN}\n"
    )
    (repo / "deployments" / "lt.yaml").write_text("team: local-team\n")


def test_gateway_brain_is_a_valid_kind(repo):
    _gateway_team(repo)
    cfg = D.load_deploy_config(repo, "lt")
    assert cfg.brain == "gateway"


def test_gateway_subscription_is_an_error(repo):
    _gateway_team(repo)
    (repo / "deployments" / "lt.yaml").write_text(
        "team: local-team\nauth: subscription\n"
    )
    with pytest.raises(D.DeployError, match="gateway"):
        D.load_deploy_config(repo, "lt")


def test_gateway_requires_no_provider_key(repo, tmp_path, monkeypatch):
    """api_key mode demands no ANTHROPIC_API_KEY for a gateway team - its
    auth token is optional (Ollama serves unauthenticated)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("LLM_GATEWAY_URL", "http://localhost:4000")
    _gateway_team(repo)
    cfg = D.load_deploy_config(repo, "lt")
    out = D.resolve_env_file(cfg, repo, tmp_path)  # must NOT raise
    assert "ANTHROPIC_API_KEY" not in out.read_text()


def test_gateway_auth_token_is_declared_not_required(repo, tmp_path, monkeypatch):
    """ANTHROPIC_AUTH_TOKEN reaches the instance when supplied (declared),
    without ever being demanded (required)."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("LLM_GATEWAY_URL", "http://localhost:4000")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "litellm-master")
    _gateway_team(repo)
    cfg = D.load_deploy_config(repo, "lt")
    out = D.resolve_env_file(cfg, repo, tmp_path)
    vals = dict(l.split("=", 1) for l in out.read_text().splitlines())
    assert vals["ANTHROPIC_AUTH_TOKEN"] == "litellm-master"


def test_gateway_team_url_backfills_auth_token(repo, tmp_path, monkeypatch):
    """A team-url gateway deployment has no local refs to scan, but its
    ANTHROPIC_AUTH_TOKEN must still backfill from the CI env or the deployed
    instance's sessions 401 at the gateway (#655 review finding)."""
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "litellm-master")
    (repo / "deployments" / "gw.yaml").write_text(
        "team-url: https://r/gw.tgz\nbrain: gateway\n"
    )
    cfg = D.load_deploy_config(repo, "gw")
    out = D.resolve_env_file(cfg, repo, tmp_path)
    vals = dict(l.split("=", 1) for l in out.read_text().splitlines())
    assert vals["ANTHROPIC_AUTH_TOKEN"] == "litellm-master"


def test_brain_kind_resolved_from_team_agent_yaml(repo):
    _codex_team(repo)
    (repo / "deployments" / "ct.yaml").write_text("team: codex-team\n")
    cfg = D.load_deploy_config(repo, "ct")
    assert cfg.brain == "codex"


def test_brain_kind_resolved_from_composed_team(repo):
    core = repo / "agents" / "codex-core"
    core.mkdir()
    core.joinpath("agent.yaml").write_text(
        "agent: codex-core\nbrain:\n  kind: codex\n"
    )
    leaf = repo / "agents" / "codex-leaf"
    leaf.mkdir()
    leaf.joinpath("agent.yaml").write_text(
        "from: codex-core\nagent: codex-leaf\nslack_token: ${SLACK_BOT_TOKEN}\n"
    )
    (repo / "deployments" / "ct.yaml").write_text("team: codex-leaf\n")

    cfg = D.load_deploy_config(repo, "ct")

    assert cfg.brain == "codex"


def test_codex_api_key_mode_requires_openai_key(repo, tmp_path, monkeypatch):
    _codex_team(repo)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ef = tmp_path / "ct.env"
    ef.write_text("SLACK_BOT_TOKEN=xoxb\n")  # no OPENAI_API_KEY
    (repo / "deployments" / "ct.yaml").write_text("team: codex-team\nauth: api_key\n")
    cfg = D.load_deploy_config(repo, "ct", {"secrets_env_file": str(ef)})
    with pytest.raises(D.DeployError, match="missing required secret"):
        D.resolve_env_file(cfg, repo, tmp_path)
    # With the OpenAI key present it resolves cleanly.
    ef.write_text("SLACK_BOT_TOKEN=xoxb\nOPENAI_API_KEY=sk-openai\n")
    out = D.resolve_env_file(cfg, repo, tmp_path)
    assert "OPENAI_API_KEY=sk-openai" in out.read_text()


def test_codex_subscription_rejects_openai_key(repo, tmp_path):
    _codex_team(repo)
    ef = tmp_path / "sub.env"
    ef.write_text("SLACK_BOT_TOKEN=xoxb\nOPENAI_API_KEY=sk-openai\n")
    (repo / "deployments" / "ct.yaml").write_text(
        "team: codex-team\nauth: subscription\n"
    )
    cfg = D.load_deploy_config(repo, "ct", {"secrets_env_file": str(ef)})
    with pytest.raises(D.DeployError, match="OPENAI_API_KEY"):
        D.resolve_env_file(cfg, repo, tmp_path)


def test_codex_brain_passed_to_provisioner(repo):
    _codex_team(repo)
    (repo / "deployments" / "ct.yaml").write_text("team: codex-team\n")
    cfg = D.load_deploy_config(repo, "ct")
    args = D._provision_args(cfg, Path("/tmp/x.env"))
    assert "--brain" in args and "codex" in args


def test_inherited_codex_brain_uses_openai_auth(repo, tmp_path, monkeypatch):
    core = repo / "agents" / "codex-core"
    core.mkdir()
    core.joinpath("agent.yaml").write_text(
        "agent: codex-core\nbrain:\n  kind: codex\n"
    )
    leaf = repo / "agents" / "codex-leaf"
    leaf.mkdir()
    leaf.joinpath("agent.yaml").write_text(
        "from: codex-core\nagent: codex-leaf\nslack_token: ${SLACK_BOT_TOKEN}\n"
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ef = tmp_path / "ct.env"
    ef.write_text("SLACK_BOT_TOKEN=xoxb\n")
    (repo / "deployments" / "ct.yaml").write_text("team: codex-leaf\nauth: api_key\n")
    cfg = D.load_deploy_config(repo, "ct", {"secrets_env_file": str(ef)})
    args = D._provision_args(cfg, Path("/tmp/x.env"))

    assert cfg.brain == "codex"
    assert "--brain" in args and "codex" in args
    with pytest.raises(D.DeployError, match="OPENAI_API_KEY"):
        D.resolve_env_file(cfg, repo, tmp_path)


# --- secret reconcile against live Fly (#385) --------------------------------

def test_declared_filter_drops_undeclared_keys_with_warning(repo, tmp_path, caplog):
    """Only secrets the team REFERENCES reach Fly. A CI-dump key (FLY_API_TOKEN)
    or a typo'd name is dropped + warned — never silently provisioned."""
    ef = tmp_path / "e.env"
    ef.write_text("SLACK_BOT_TOKEN=xoxb\nANTHROPIC_API_KEY=sk\n"
                  "FLY_API_TOKEN=fly\nSLAKC_BOT_TOKEN=typo\n")
    cfg = D.load_deploy_config(repo, "eng-team", {"secrets_env_file": str(ef)})
    with caplog.at_level(logging.WARNING):
        vals = D.resolve_secret_values(cfg, repo)
    assert vals["SLACK_BOT_TOKEN"] == "xoxb"          # declared, kept
    assert "FLY_API_TOKEN" not in vals                # undeclared CI dump, dropped
    assert "SLAKC_BOT_TOKEN" not in vals              # typo, dropped
    assert "dropping undeclared secret" in caplog.text.lower()


def test_live_secret_satisfies_required_on_update(repo, monkeypatch):
    """An already-live Fly secret satisfies the required check on an update — no
    need to re-supply it. Without a live store (provision), the same call fails."""
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = D.load_deploy_config(repo, "eng-team")  # api_key
    vals = D.resolve_secret_values(
        cfg, repo, live={"SLACK_BOT_TOKEN", "ANTHROPIC_API_KEY"})
    assert vals == {}  # live covers it; nothing to set, no raise
    with pytest.raises(D.DeployError, match="missing required secret"):
        D.resolve_secret_values(cfg, repo, live=None)  # provision: must supply


def test_optional_declared_var_is_kept_but_not_required(repo):
    """A ${VAR:-default} ref (e.g. codex's OPENAI_API_KEY) is DECLARED (so it's
    never pruned) but OPTIONAL (so its absence doesn't block a deploy)."""
    pkg = repo / "agents" / "eng-team" / "agent.yaml"
    pkg.write_text(pkg.read_text() + "openai: ${OPENAI_API_KEY:-}\n")
    req, decl = D._secret_sets(D.load_deploy_config(repo, "eng-team"), repo)
    assert "OPENAI_API_KEY" in decl       # declared → survives prune
    assert "OPENAI_API_KEY" not in req    # optional → not required


def test_outage_unset_required_secret_is_restored_on_update(repo, recorder, monkeypatch):
    """#385 regression (failing-first): ANTHROPIC_API_KEY manually unset on a live
    api_key instance is RESTORED on the next deploy, not perpetuated (the eng-team
    outage). The plain-update path never re-ran the provisioner, so secrets drifted
    — fixed by reconciling live secrets directly."""
    monkeypatch.setattr(D, "fly_app_exists", lambda app: True)
    monkeypatch.setattr(D, "fly_instance_running", lambda app: True)
    monkeypatch.setattr(D, "_fly_machine_ids", lambda app: ["m1"])
    monkeypatch.setattr(D, "fly_secrets_list", lambda app: {"SLACK_BOT_TOKEN"})  # ANTHROPIC unset!
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")  # supplied by CI/Environment
    D.deploy(repo, "eng-team")
    sets = [c for c in recorder if "secrets" in c["cmd"] and "set" in c["cmd"]]
    assert sets, "expected a `fly secrets set` to restore the unset key"
    assert any("ANTHROPIC_API_KEY=sk-ant" in arg for c in sets for arg in c["cmd"])
    assert all(c["secret"] for c in sets)  # secret args logged redacted


def test_existing_app_syncs_reconciled_secrets_to_volume_env(repo, recorder,
                                                            monkeypatch):
    """A rotated Fly secret must also refresh the persisted volume .env. Codex
    tool shells can lose inherited env and fall back to run/.env, so Fly
    secrets alone are not enough (#501)."""
    monkeypatch.setattr(D, "fly_app_exists", lambda app: True)
    monkeypatch.setattr(D, "fly_instance_running", lambda app: True)
    monkeypatch.setattr(D, "_fly_machine_ids", lambda app: ["m1"])
    monkeypatch.setattr(D, "fly_secrets_list", lambda app: {
        "SLACK_BOT_TOKEN", "ANTHROPIC_API_KEY"})
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-new")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")

    D.deploy(repo, "eng-team")

    syncs = [
        c for c in recorder
        if "ssh" in c["cmd"] and "/opt/venv/bin/python -c" in " ".join(c["cmd"])
    ]
    assert syncs, "expected deploy to refresh the volume run/.env"
    payload = json.loads(syncs[0]["input"].decode())
    assert payload["set"]["SLACK_BOT_TOKEN"] == "xoxb-new"
    assert payload["set"]["ANTHROPIC_API_KEY"] == "sk-ant"
    assert payload["unset"] == []
    assert syncs[0]["secret"] is True


def test_prune_removes_undeclared_live_secret(repo, recorder, monkeypatch):
    """A live, non-BOBI_ secret absent from the declared set is pruned; the
    declared keys and BOBI_* identity are left alone."""
    monkeypatch.setattr(D, "fly_app_exists", lambda app: True)
    monkeypatch.setattr(D, "fly_instance_running", lambda app: True)
    monkeypatch.setattr(D, "_fly_machine_ids", lambda app: ["m1"])
    monkeypatch.setattr(D, "fly_secrets_list", lambda app: {
        "SLACK_BOT_TOKEN", "ANTHROPIC_API_KEY", "STALE_KEY", "BOBI_FLEET"})
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    D.deploy(repo, "eng-team")
    unsets = [c for c in recorder if "secrets" in c["cmd"] and "unset" in c["cmd"]]
    assert unsets, "expected the undeclared live secret to be pruned"
    flat = [a for c in unsets for a in c["cmd"]]
    assert "STALE_KEY" in flat
    assert "BOBI_FLEET" not in flat   # identity — never pruned
    assert "SLACK_BOT_TOKEN" not in flat   # declared — kept
    syncs = [
        c for c in recorder
        if "ssh" in c["cmd"] and "/opt/venv/bin/python -c" in " ".join(c["cmd"])
    ]
    assert syncs, "expected pruned keys to be removed from volume .env"
    payload = json.loads(syncs[0]["input"].decode())
    assert payload["unset"] == ["STALE_KEY"]


def test_no_prune_override_skips_pruning(repo, recorder, monkeypatch):
    monkeypatch.setattr(D, "fly_app_exists", lambda app: True)
    monkeypatch.setattr(D, "fly_instance_running", lambda app: True)
    monkeypatch.setattr(D, "_fly_machine_ids", lambda app: ["m1"])
    monkeypatch.setattr(D, "fly_secrets_list", lambda app: {
        "SLACK_BOT_TOKEN", "ANTHROPIC_API_KEY", "STALE_KEY"})
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    D.deploy(repo, "eng-team", {"no_prune": True})
    unsets = [c for c in recorder if "secrets" in c["cmd"] and "unset" in c["cmd"]]
    assert not unsets


# --- orchestration: provision-vs-update × delivery mode ----------------------

def _flat(calls):
    return ["\n".join(c["cmd"]) for c in calls]


def test_team_url_new_app_provisions_with_url(repo, recorder, monkeypatch):
    monkeypatch.setattr(D, "fly_app_exists", lambda app: False)
    (repo / "deployments" / "defaults.yaml").write_text("fleet: moda\n")
    (repo / "deployments" / "eng.yaml").write_text(
        "team-url: https://r/eng.tar.gz\n"
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")  # api_key default needs it… team-url skips local validation
    cfg = D.deploy(repo, "eng")
    joined = _flat(recorder)
    prov = next(c for c in joined if "provision-instance.sh" in c)
    assert "--team-url" in prov and "https://r/eng.tar.gz" in prov
    assert "--app\nmoda-eng" in prov
    assert "--instance\neng" in prov
    assert "--blank" not in prov


def test_team_url_existing_app_updates_in_place(repo, recorder, monkeypatch):
    monkeypatch.setattr(D, "fly_app_exists", lambda app: True)
    monkeypatch.setattr(D, "fly_instance_running", lambda app: True)
    monkeypatch.setattr(D, "_fly_machine_ids", lambda app: ["48ed1234"])
    (repo / "deployments" / "eng.yaml").write_text(
        "team-url: https://r/eng.tar.gz\n"
    )
    D.deploy(repo, "eng")
    joined = _flat(recorder)
    assert not any("provision-instance.sh" in c for c in joined)  # no re-provision
    assert any("bobi agents install https://r/eng.tar.gz" in c
               and "--name \"$BOBI_INSTANCE\"" in c
               and "--non-interactive" in c for c in joined)
    assert any("machine\nrestart\n48ed1234" in c for c in joined)


def test_team_url_rebuild_flag_rebuilds_before_update(repo, recorder, monkeypatch):
    """--rebuild must also refresh an existing team-url image before the
    in-place package reinstall. This is the release canary path."""
    monkeypatch.setattr(D, "fly_app_exists", lambda app: True)
    monkeypatch.setattr(D, "fly_instance_running", lambda app: True)
    monkeypatch.setattr(D, "_fly_machine_ids", lambda app: ["48ed1234"])
    (repo / "deployments" / "eng.yaml").write_text(
        "team-url: https://r/eng.tar.gz\n"
    )
    D.deploy(repo, "eng", {"rebuild": True})
    joined = _flat(recorder)
    assert any("provision-instance.sh" in c
               and "--team-url" in c
               and "https://r/eng.tar.gz" in c for c in joined)
    assert any("bobi agents install https://r/eng.tar.gz" in c
               and "--name \"$BOBI_INSTANCE\"" in c
               and "--non-interactive" in c for c in joined)
    assert any("machine\nrestart\n48ed1234" in c for c in joined)


def test_ssh_push_new_app_provisions_blank_then_pushes(repo, recorder, monkeypatch, tmp_path):
    monkeypatch.setattr(D, "fly_app_exists", lambda app: False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    (repo / "deployments" / "defaults.yaml").write_text("fleet: moda\n")
    cfg = D.deploy(repo, "eng-team")           # bare name → local package, ssh-push
    joined = _flat(recorder)
    prov = next(c for c in joined if "provision-instance.sh" in c)
    assert "--blank" in prov
    assert "--team" not in prov.replace("--team-url", "")  # neither team nor team-url
    # team pushed: base64 onto the volume, then install from the tarball
    assert any("base64 -d" in c for c in joined)
    assert any("bobi agents install /data/incoming-team.tar.gz" in c
               and "--name \"$BOBI_INSTANCE\"" in c
               and "--non-interactive" in c for c in joined)
    # NEW provision releases the waiting entrypoint — no restart.
    assert not any("machine\nrestart" in c for c in joined)


def test_ssh_push_existing_app_pushes_and_restarts(repo, recorder, monkeypatch):
    monkeypatch.setattr(D, "fly_app_exists", lambda app: True)
    monkeypatch.setattr(D, "fly_instance_running", lambda app: True)
    monkeypatch.setattr(D, "_fly_machine_ids", lambda app: ["48ed1234"])
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    D.deploy(repo, "eng-team")
    joined = _flat(recorder)
    assert not any("provision-instance.sh" in c for c in joined)
    assert any("bobi agents install /data/incoming-team.tar.gz" in c
               and "--name \"$BOBI_INSTANCE\"" in c for c in joined)
    # reload after reinstall — `fly machine restart` needs an explicit machine ID
    # non-interactively (a bare `-a <app>` errors; caught in the live e2e).
    assert any("machine\nrestart\n48ed1234" in c for c in joined)


def test_restart_requires_resolved_machine_id(repo, recorder, monkeypatch):
    """Regression: a bare `fly machine restart -a <app>` is rejected outside a
    TTY ('a machine ID must be specified'); deploy must resolve + pass IDs."""
    monkeypatch.setattr(D, "_fly_machine_ids", lambda app: ["m1", "m2"])
    D.restart_app("acme-eng")
    restarts = [c for c in _flat(recorder) if "machine\nrestart" in c]
    assert restarts and all(any(mid in c for mid in ("m1", "m2")) for c in restarts)
    # never a bare `-a` with no id
    assert not any(c.endswith("restart\n-a\nacme-eng") for c in _flat(recorder))


def test_provision_passes_build_context_and_dockerfile(repo, recorder, monkeypatch):
    """Every provision passes the build context + Dockerfile (source mode = repo)."""
    monkeypatch.setattr(D, "fly_app_exists", lambda app: False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    (repo / "deployments" / "eng.yaml").write_text("team-url: https://r/eng.tar.gz\n")
    D.deploy(repo, "eng")
    prov = next(c for c in _flat(recorder) if "provision-instance.sh" in c)
    assert "--build-context" in prov and str(repo) in prov
    assert "--dockerfile" in prov


# --- #387: macOS / Docker-Desktop local image build -------------------------
# Fly's remote builder is unreliable from a Docker-Desktop laptop (flyctl
# v0.4.59 mis-parses the daemon host); the tell is the standard
# /var/run/docker.sock being ABSENT (present on Linux/CI). When absent we build
# with local buildkit (--local-only, gzip) and point DOCKER_HOST at the real
# Docker Desktop socket from `docker context inspect`.

def test_resolve_local_build_keeps_remote_when_default_socket_present(monkeypatch):
    """Linux / GitHub-Actions: /var/run/docker.sock exists → remote build stays."""
    monkeypatch.setattr(D, "_default_docker_socket_present", lambda: True)
    monkeypatch.setattr(D, "_docker_context_host", lambda: "unix:///nope.sock")
    assert D._resolve_local_build() == (False, None)


def test_resolve_local_build_resolves_desktop_socket(monkeypatch, tmp_path):
    """macOS Docker Desktop: no default socket → local build + DOCKER_HOST from
    the active docker context (only when that socket actually exists)."""
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(D, "_default_docker_socket_present", lambda: False)
    monkeypatch.setattr(D, "_docker_context_host", lambda: f"unix://{sock}")
    assert D._resolve_local_build() == (True, f"unix://{sock}")


def test_resolve_local_build_respects_explicit_docker_host(monkeypatch):
    """An operator-set DOCKER_HOST wins: local build, no overlay (the subprocess
    already inherits it)."""
    monkeypatch.setenv("DOCKER_HOST", "unix:///custom.sock")
    monkeypatch.setattr(D, "_default_docker_socket_present", lambda: False)
    assert D._resolve_local_build() == (True, None)


def test_resolve_local_build_local_host_but_socket_unresolved(monkeypatch):
    """No default socket and the context yields nothing usable → still a local
    build (deploy warns with the manual export), but no host to inject."""
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(D, "_default_docker_socket_present", lambda: False)
    monkeypatch.setattr(D, "_docker_context_host", lambda: "")
    assert D._resolve_local_build() == (True, None)


def test_provision_local_build_passes_flag_and_docker_host(repo, recorder, monkeypatch):
    """On a Docker-Desktop laptop the provision is --local-build with DOCKER_HOST
    injected into the provision subprocess env (#387)."""
    monkeypatch.setattr(D, "fly_app_exists", lambda app: False)
    monkeypatch.setattr(D, "_resolve_local_build",
                        lambda: (True, "unix:///me/.docker/run/docker.sock"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    (repo / "deployments" / "eng.yaml").write_text("team-url: https://r/eng.tar.gz\n")
    D.deploy(repo, "eng")
    prov = next(c for c in recorder if "provision-instance.sh" in "\n".join(c["cmd"]))
    assert "--local-build" in prov["cmd"]
    assert prov["extra_env"] == {"DOCKER_HOST": "unix:///me/.docker/run/docker.sock"}


def test_provision_remote_build_default_passes_no_local_flag(repo, recorder, monkeypatch):
    """Linux/CI (remote build): no --local-build, no DOCKER_HOST overlay."""
    monkeypatch.setattr(D, "fly_app_exists", lambda app: False)
    monkeypatch.setattr(D, "_resolve_local_build", lambda: (False, None))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    (repo / "deployments" / "eng.yaml").write_text("team-url: https://r/eng.tar.gz\n")
    D.deploy(repo, "eng")
    prov = next(c for c in recorder if "provision-instance.sh" in "\n".join(c["cmd"]))
    assert "--local-build" not in prov["cmd"]
    assert not prov["extra_env"]


def test_image_mode_never_requests_local_build(repo, recorder, monkeypatch):
    """--image mode pulls a prebuilt ref (no build) → never --local-build, even
    on a laptop."""
    monkeypatch.setattr(D, "fly_app_exists", lambda app: False)
    monkeypatch.setattr(D, "_resolve_local_build", lambda: (True, "unix:///x.sock"))
    (repo / "deployments" / "eng.yaml").write_text(
        "team-url: https://r/eng.tar.gz\nimage: registry.fly.io/x@sha256:abc\n")
    D.deploy(repo, "eng")
    prov = next(c for c in recorder if "provision-instance.sh" in "\n".join(c["cmd"]))
    assert "--local-build" not in prov["cmd"]


def test_image_mode_deploys_by_ref_not_build(repo, recorder, monkeypatch):
    """C24: `image:` deploys a prebuilt team image — pass --image, never build."""
    monkeypatch.setattr(D, "fly_app_exists", lambda app: False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    (repo / "deployments" / "eng.yaml").write_text(
        "team-url: https://r/eng.tar.gz\n"
        "image: ghcr.io/moda-labs/bobi-eng-team:latest\n"
    )
    D.deploy(repo, "eng")
    prov = next(c for c in _flat(recorder) if "provision-instance.sh" in c)
    assert "--image" in prov
    assert "ghcr.io/moda-labs/bobi-eng-team:latest" in prov
    # build path is skipped entirely in image mode
    assert "--build-context" not in prov
    assert "--dockerfile" not in prov
    assert "--build-arg" not in prov


def test_build_spec_team_renders_team_deps_build_arg(repo, recorder, monkeypatch):
    """C24: a team with a `build:` spec gets its team-deps hook rendered into the
    build context and passed as --build-arg TEAM_DEPS (built on Fly during deploy)."""
    monkeypatch.setattr(D, "fly_app_exists", lambda app: False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    pkg = repo / "agents" / "eng-team" / "agent.yaml"
    pkg.write_text(pkg.read_text() + "build:\n  npm: [bun]\n  verify: requires\n")
    D.deploy(repo, "eng-team")
    prov = next(c for c in _flat(recorder) if "provision-instance.sh" in c)
    assert "--build-arg" in prov and "TEAM_DEPS=dist/team-deps/eng-team.sh" in prov
    assert (repo / "dist" / "team-deps" / "eng-team.sh").exists()


def test_no_build_spec_team_passes_no_team_deps(repo, recorder, monkeypatch):
    """A team with no `build:` spec deploys on the generic image — no TEAM_DEPS."""
    monkeypatch.setattr(D, "fly_app_exists", lambda app: False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    D.deploy(repo, "eng-team")  # fixture team has no build: block
    prov = next(c for c in _flat(recorder) if "provision-instance.sh" in c)
    assert "TEAM_DEPS" not in prov


# --- #379: deps-drift guard on the in-place ssh-push update path -------------

def _with_build_spec(repo):
    """Give the fixture team a `build:` spec so it has a real deps identity."""
    pkg = repo / "agents" / "eng-team" / "agent.yaml"
    pkg.write_text(pkg.read_text() + "build:\n  npm: [bun]\n  verify: requires\n")


def _running_app(monkeypatch):
    monkeypatch.setattr(D, "fly_app_exists", lambda app: True)
    monkeypatch.setattr(D, "fly_instance_running", lambda app: True)
    monkeypatch.setattr(D, "_fly_machine_ids", lambda app: ["m1"])
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")


def test_ssh_push_deps_drift_rebuilds_in_place(repo, recorder, monkeypatch):
    """#379: editing a live team's build: deps then re-deploying must NOT silently
    hot-push (the rebuilt tools would never land). Detect the drift and rebuild
    the image in place, then refresh the definition."""
    _running_app(monkeypatch)
    _with_build_spec(repo)
    monkeypatch.setattr(D, "_running_team_deps_hash", lambda app: "stalehash0000")
    D.deploy(repo, "eng-team")
    joined = _flat(recorder)
    # rebuilds the image on the existing app (idempotent provision, blank)…
    assert any("provision-instance.sh" in c and "--blank" in c for c in joined)
    # …then refreshes the definition + reloads
    assert any("bobi agents install /data/incoming-team.tar.gz" in c
               and "--name \"$BOBI_INSTANCE\"" in c for c in joined)


def test_ssh_push_deps_match_takes_fast_path(repo, recorder, monkeypatch):
    """Deps unchanged → the in-place hot-push fast path is correct (no rebuild)."""
    _running_app(monkeypatch)
    _with_build_spec(repo)
    from bobi.build_render import load_team_config, team_deps_hash
    h = team_deps_hash(load_team_config(repo / "agents" / "eng-team").build)
    monkeypatch.setattr(D, "_running_team_deps_hash", lambda app: h)
    D.deploy(repo, "eng-team")
    joined = _flat(recorder)
    assert not any("provision-instance.sh" in c for c in joined)  # no rebuild
    assert any("bobi agents install /data/incoming-team.tar.gz" in c
               and "--name \"$BOBI_INSTANCE\"" in c for c in joined)


def test_ssh_push_deps_unknown_stamp_hot_pushes(repo, recorder, monkeypatch):
    """An image built before the #379 stamp carries no hash — can't tell deps
    apart, so take the hot-push path (warn); --rebuild forces it."""
    _running_app(monkeypatch)
    _with_build_spec(repo)
    monkeypatch.setattr(D, "_running_team_deps_hash", lambda app: "")
    D.deploy(repo, "eng-team")
    joined = _flat(recorder)
    assert not any("provision-instance.sh" in c for c in joined)  # no rebuild
    assert any("incoming-team.tar.gz" in c for c in joined)


def test_ssh_push_rebuild_flag_forces_rebuild(repo, recorder, monkeypatch):
    """--rebuild forces an in-place image rebuild even when deps haven't drifted
    (covers the unknown-stamp case where the operator knows deps changed)."""
    _running_app(monkeypatch)
    _with_build_spec(repo)
    # stamp MATCHES → no drift; the flag forces a rebuild anyway
    from bobi.build_render import load_team_config, team_deps_hash
    h = team_deps_hash(load_team_config(repo / "agents" / "eng-team").build)
    monkeypatch.setattr(D, "_running_team_deps_hash", lambda app: h)
    D.deploy(repo, "eng-team", {"rebuild": True})
    assert any("provision-instance.sh" in c and "--blank" in c for c in _flat(recorder))


def test_generic_team_skips_deps_probe_entirely(repo, recorder, monkeypatch):
    """A team with no build: spec has no baked deps — never ssh-probe, never
    rebuild (keeps existing generic deploys untouched)."""
    _running_app(monkeypatch)  # fixture team has NO build: block
    probed = []
    monkeypatch.setattr(D, "_running_team_deps_hash",
                        lambda app: probed.append(app) or "")
    D.deploy(repo, "eng-team")
    joined = _flat(recorder)
    assert probed == []  # short-circuited before any ssh probe
    assert not any("provision-instance.sh" in c for c in joined)  # no rebuild
    assert any("incoming-team.tar.gz" in c for c in joined)


# --- #428 Stage 3: declared dependency-set drift + guide-dep guard ----------

def _with_pinned_dep(repo):
    """Give the fixture team a pinned tool_library dep (a dep-set + a build)."""
    pkg = repo / "agents" / "eng-team" / "agent.yaml"
    pkg.write_text(pkg.read_text()
                   + "tool_library:\n  - name: mytool\n    success: 'mytool -v'\n"
                     "    install:\n      npm: ['mytool@1.0.0']\n")


def test_dep_list_drift_rebuilds_in_place(repo, recorder, monkeypatch):
    """#428: a changed DECLARED dependency set must rebuild (re-bootstrap), even
    when the resolved build-deps hash still matches the running image."""
    _running_app(monkeypatch)
    _with_pinned_dep(repo)
    from bobi.build_render import load_composed_team_config, team_deps_hash
    spec = load_composed_team_config(repo / "agents" / "eng-team", repo).build
    # build-deps stamp MATCHES (no #379 drift); only the dep-set stamp is stale.
    monkeypatch.setattr(D, "_running_team_deps_hash", lambda app: team_deps_hash(spec))
    monkeypatch.setattr(D, "_running_dep_list_hash", lambda app: "stale00000000")
    D.deploy(repo, "eng-team")
    assert any("provision-instance.sh" in c and "--blank" in c
               for c in _flat(recorder))  # rebuilt in place


def test_dep_list_match_takes_fast_path(repo, recorder, monkeypatch):
    """Dep-set unchanged and build-deps unchanged → hot-push, no rebuild."""
    _running_app(monkeypatch)
    _with_pinned_dep(repo)
    from bobi.build_render import load_composed_team_config, team_deps_hash
    from bobi.tool_library import (
        dependency_list_hash,
        resolve_team_dependencies,
    )
    team = repo / "agents" / "eng-team"
    spec = load_composed_team_config(team, repo).build
    monkeypatch.setattr(D, "_running_team_deps_hash", lambda app: team_deps_hash(spec))
    monkeypatch.setattr(D, "_running_dep_list_hash",
                        lambda app: dependency_list_hash(
                            resolve_team_dependencies(team, repo)))
    D.deploy(repo, "eng-team")
    joined = _flat(recorder)
    assert not any("provision-instance.sh" in c for c in joined)  # no rebuild
    assert any("incoming-team.tar.gz" in c for c in joined)


def test_guide_only_dep_deploy_is_refused(repo, monkeypatch):
    """#428: `bobi deploy` never runs the bootstrap agent, so it must refuse to
    source-build a team with a guide-only dep instead of silently omitting it."""
    monkeypatch.setattr(D, "fly_app_exists", lambda app: False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    pkg = repo / "agents" / "eng-team" / "agent.yaml"
    pkg.write_text(pkg.read_text()
                   + "tool_library:\n  - name: gtool\n    guide: 'g'\n    success: 's'\n")
    with pytest.raises(D.DeployError, match="gtool.*bootstrap agent|guide-only"):
        D.deploy(repo, "eng-team")


def test_binary_mode_pins_bobi_version_as_build_arg(repo, recorder, monkeypatch, tmp_path):
    """Binary mode builds the PyPI image pinned to the installed version."""
    monkeypatch.setattr(D, "fly_app_exists", lambda app: False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    (repo / "deployments" / "eng.yaml").write_text("team-url: https://r/eng.tar.gz\n")
    # Force binary mode regardless of the fixture being a checkout.
    pkg = tmp_path / "_deploy"
    (pkg / "scripts").mkdir(parents=True); (pkg / "docker").mkdir()
    (pkg / "Dockerfile").write_text("FROM scratch\n")
    (pkg / "docker" / "x").write_text("")
    (pkg / "scripts" / "provision-instance.sh").write_text("#!/bin/sh\n")
    (pkg / "scripts" / "destroy-instance.sh").write_text("#!/bin/sh\n")
    monkeypatch.setattr(D, "find_repo_root",
                        lambda p=None: (_ for _ in ()).throw(D.DeployError("x")))
    monkeypatch.setattr(D, "_packaged_deploy_dir", lambda: pkg)
    monkeypatch.setattr(D, "_bobi_version", lambda: "1.2.3")
    D.deploy(repo, "eng")
    prov = next(c for c in _flat(recorder) if "provision-instance.sh" in c)
    assert "--build-arg" in prov and "BOBI_VERSION=1.2.3" in prov
    assert "BOBI_BUILD=pypi" in prov


# --- Fly onboarding preflight ------------------------------------------------

def test_preflight_flags_missing_flyctl(monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda _: None)
    problems = D.fly_preflight()
    assert problems and "isn't installed" in problems[0]
    assert "fly.io/install.sh" in problems[0]


def test_preflight_flags_not_logged_in(monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda _: "/usr/local/bin/fly")
    monkeypatch.setattr(D.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})())
    problems = D.fly_preflight()
    assert problems and "not signed in" in problems[0]
    assert "fly auth signup" in problems[0] and "fly auth login" in problems[0]


def test_preflight_passes_when_ready(monkeypatch):
    monkeypatch.setattr(D.shutil, "which", lambda _: "/usr/local/bin/fly")
    monkeypatch.setattr(D.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "me@x", "stderr": ""})())
    assert D.fly_preflight() == []


def test_local_package_dir_accepts_a_path(repo, tmp_path):
    team = tmp_path / "somewhere" / "myteam"
    team.mkdir(parents=True)
    (team / "agent.yaml").write_text("agent: myteam\n")
    assert D.local_package_dir(repo, str(team)) == team.resolve()


def test_half_provisioned_app_reprovisions_not_ssh_updates(repo, recorder, monkeypatch):
    """Regression: an app that exists but has no started machine (a deploy that
    failed mid-build) must RE-PROVISION, not take the ssh update path (which
    errors 'no started VMs'). Caught in the binary-only e2e."""
    monkeypatch.setattr(D, "fly_app_exists", lambda app: True)          # app exists…
    monkeypatch.setattr(D, "fly_instance_running", lambda app: False)   # …but not running
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    (repo / "deployments" / "eng.yaml").write_text("team-url: https://r/eng.tar.gz\n")
    D.deploy(repo, "eng")
    joined = _flat(recorder)
    # re-provisions (rebuilds the image) — provision-instance.sh is idempotent
    assert any("provision-instance.sh" in c and "--team-url" in c for c in joined)
    # does NOT try to ssh-install into a dead app
    assert not any("bobi agents install https://r/eng.tar.gz" in c for c in joined)


def test_destroy_resolves_app_and_passes_yes(repo, recorder, monkeypatch):
    (repo / "deployments" / "defaults.yaml").write_text("fleet: moda\n")
    app = D.destroy(repo, "eng-team", assume_yes=True)
    assert app == "moda-eng-team"
    joined = _flat(recorder)
    dcall = next(c for c in joined if "destroy-instance.sh" in c)
    assert "--app\nmoda-eng-team" in dcall
    assert "--yes" in dcall


def test_push_team_builds_single_dir_tarball(repo, tmp_path):
    pkg = repo / "agents" / "eng-team"
    out = D._build_team_tarball(pkg, tmp_path)
    with tarfile.open(out) as t:
        names = t.getnames()
    # extracts to a single eng-team/ dir holding agent.yaml
    assert "eng-team/agent.yaml" in names
    assert all(n == "eng-team" or n.startswith("eng-team/") for n in names)
