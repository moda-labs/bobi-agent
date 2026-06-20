"""Unit tests for the `modastack deploy` engine (modastack/deploy.py).

The deployment PRIMITIVE: config precedence, delivery-mode selection, identity
naming, secret resolution/validation, and the idempotent provision-or-update
orchestration. Fly + the shell scripts are stubbed (deploy._run is monkeypatched
to record commands), so nothing here touches Fly or the network.

The workflow STRUCTURE (thin-client asserts) is in test_gitops_c22.py.
"""

import os
import tarfile
from pathlib import Path

import pytest

from modastack import deploy as D


# --- fixtures ----------------------------------------------------------------

def _make_repo(tmp_path: Path) -> Path:
    """A minimal modastack source root: scripts/ + Dockerfile + a local team."""
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

    def fake_run(cmd, *, cwd=None, check=True, input_bytes=None):
        calls.append({"cmd": cmd, "cwd": cwd, "input": input_bytes})
        class R:  # noqa: E306
            returncode = 0
        return R()

    monkeypatch.setattr(D, "_run", fake_run)
    monkeypatch.setattr(D, "_fly_bin", lambda: "fly")
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


# --- repo + package resolution ----------------------------------------------

def test_find_repo_root_walks_up(repo):
    deep = repo / "deployments"
    assert D.find_repo_root(deep) == repo


def test_find_repo_root_raises_without_scripts(tmp_path):
    with pytest.raises(D.DeployError, match="not a modastack checkout"):
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
    monkeypatch.setattr(D, "_modastack_version", lambda: "9.9.9")

    staging = tmp_path / "staging"
    staging.mkdir()
    a = D.resolve_assets(tmp_path / "elsewhere", staging)
    assert a.mode == "binary"
    assert a.build_args == {"MODASTACK_BUILD": "pypi", "MODASTACK_VERSION": "9.9.9"}
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


def test_modastack_vars_are_not_treated_as_required_secrets(repo, tmp_path, monkeypatch):
    """A package's ${MODASTACK_EVENT_SERVER} ref is identity the provisioner
    stamps into [env] from flags — never a secret to demand in the env-file."""
    monkeypatch.delenv("MODASTACK_EVENT_SERVER", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    pkg = repo / "agents" / "eng-team" / "agent.yaml"
    pkg.write_text(pkg.read_text() + "event_server: ${MODASTACK_EVENT_SERVER}\n")
    cfg = D.load_deploy_config(repo, "eng-team")
    out = D.resolve_env_file(cfg, repo, tmp_path)  # must NOT raise
    assert "MODASTACK_EVENT_SERVER" not in out.read_text()


def test_subscription_mode_rejects_anthropic_key(repo, tmp_path):
    ef = tmp_path / "sub.env"
    ef.write_text("SLACK_BOT_TOKEN=xoxb\nANTHROPIC_API_KEY=sk-ant\n")
    (repo / "deployments" / "dog.yaml").write_text(
        "team: eng-team\nauth: subscription\n"
    )
    cfg = D.load_deploy_config(repo, "dog", {"secrets_env_file": str(ef)})
    with pytest.raises(D.DeployError, match="ANTHROPIC_API_KEY"):
        D.resolve_env_file(cfg, repo, tmp_path)


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
    assert any("modastack install \"https://r/eng.tar.gz\"" in c for c in joined)
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
    assert any("modastack install /data/incoming-team.tar.gz --non-interactive" in c
               for c in joined)
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
    assert any("modastack install /data/incoming-team.tar.gz" in c for c in joined)
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


def test_image_mode_deploys_by_ref_not_build(repo, recorder, monkeypatch):
    """C24: `image:` deploys a prebuilt team image — pass --image, never build."""
    monkeypatch.setattr(D, "fly_app_exists", lambda app: False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk")
    (repo / "deployments" / "eng.yaml").write_text(
        "team-url: https://r/eng.tar.gz\n"
        "image: ghcr.io/moda-labs/modastack-eng-team:latest\n"
    )
    D.deploy(repo, "eng")
    prov = next(c for c in _flat(recorder) if "provision-instance.sh" in c)
    assert "--image" in prov
    assert "ghcr.io/moda-labs/modastack-eng-team:latest" in prov
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


def test_binary_mode_pins_modastack_version_as_build_arg(repo, recorder, monkeypatch, tmp_path):
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
    monkeypatch.setattr(D, "_modastack_version", lambda: "1.2.3")
    D.deploy(repo, "eng")
    prov = next(c for c in _flat(recorder) if "provision-instance.sh" in c)
    assert "--build-arg" in prov and "MODASTACK_VERSION=1.2.3" in prov
    assert "MODASTACK_BUILD=pypi" in prov


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
    assert not any("modastack install \"https://r/eng.tar.gz\"" in c for c in joined)


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
