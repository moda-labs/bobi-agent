"""C22 GitOps deploy/update automation (#342), refactored onto the
`bobi deploy` primitive (docs/CONTAINERIZED_DEPLOYMENT.md).

Covers the fleet-state primitive (`scripts/fleet.sh`), the provisioner's
identity stamps + ssh-push blank mode, and the structural invariants of the two
GitOps workflows. The shell helpers are exercised for real (bash subprocess with
a stubbed `fleet_exists`, so no Fly calls); the workflows are parsed and
asserted, so the load-bearing decisions break loudly if someone regresses them:

  * deploy-agent-teams.yml.example is a THIN CLIENT for fleet repos — the
    reconcile business logic lives in `bobi deploy` (idempotent
    provision-or-update), not the YAML;
  * each instance is stamped BOBI_FLEET + BOBI_INSTANCE (the
    SaaS-extensible fleet/tenant keys — the app name is only a hint);
  * --blank provisions a team-less instance whose entrypoint waits for an
    ssh-pushed team (the local-package delivery path);
  * the secret interface is per-key `<TEAM>__<KEY>` secrets in a per-tenant
    GitHub Environment, filtered from toJSON(secrets) (#385 — no opaque blob);
  * deletions never auto-deploy — orphaned apps surface for human destroy;
  * release rollout is canary-specific in this repo; fleet repos own generic
    deployment reconciliation.

The deploy ENGINE itself (config precedence, delivery selection, secret
validation) is unit-tested in test_deploy.py.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
FLEET_SH = REPO / "scripts" / "fleet.sh"
PROVISION_SH = REPO / "scripts" / "provision-instance.sh"
DESTROY_SH = REPO / "scripts" / "destroy-instance.sh"
CANARY_SMOKE_SH = REPO / "scripts" / "canary-smoke.sh"
HOMEBREW_SMOKE_SH = REPO / "scripts" / "smoke-homebrew-bottles.sh"
WF_TEAMS = REPO / ".github" / "workflows" / "deploy-agent-teams.yml.example"
WF_RELEASE = REPO / ".github" / "workflows" / "release.yml"


# --- scripts/fleet.sh (exercised for real, Fly stubbed) ---------------------

def _run_fleet(snippet: str) -> subprocess.CompletedProcess:
    """Source fleet.sh and run a bash snippet against it.

    A `fleet_exists` stub treats any app named '<prefix>-known*' as existing, so
    classification can be tested without touching the Fly API.
    """
    stub = 'fleet_exists() { case "$1" in *-known*) return 0;; *) return 1;; esac; }\n'
    script = f'set -euo pipefail\nsource "{FLEET_SH}"\n{stub}{snippet}'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_fleet_app_builds_deterministic_name():
    out = _run_fleet('fleet_app moda eng-team')
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "moda-eng-team"


def test_classify_partitions_by_fly_state():
    out = _run_fleet('fleet_classify moda new-team known-team other-new')
    assert out.returncode == 0, out.stderr
    lines = dict(line.split("=", 1) for line in out.stdout.splitlines())
    assert lines["added"] == '["new-team","other-new"]'
    assert lines["changed"] == '["known-team"]'


def test_classify_empty_inputs_emit_empty_arrays():
    out = _run_fleet('fleet_classify moda')
    assert out.returncode == 0, out.stderr
    assert "added=[]" in out.stdout
    assert "changed=[]" in out.stdout


def test_classify_all_added_when_nothing_exists():
    out = _run_fleet('fleet_classify moda alpha beta')
    lines = dict(line.split("=", 1) for line in out.stdout.splitlines())
    assert lines["added"] == '["alpha","beta"]'
    assert lines["changed"] == "[]"


def test_deploy_scripts_pass_shellcheck():
    """fleet.sh + the provision/destroy scripts deploy drives — keep them clean."""
    for script in (FLEET_SH, PROVISION_SH, DESTROY_SH, CANARY_SMOKE_SH, HOMEBREW_SMOKE_SH):
        sc = subprocess.run(["shellcheck", str(script)], capture_output=True, text=True)
        assert sc.returncode == 0, f"{script.name}:\n{sc.stdout}{sc.stderr}"


# --- scripts/provision-instance.sh: identity stamps + ssh-push blank mode ----

def test_provisioner_stamps_fleet_and_defaults_from_app():
    text = PROVISION_SH.read_text()
    # The stamp goes into the [env] identity block read back for enumeration.
    assert 'ENV_VARS["BOBI_FLEET"]="$FLEET"' in text
    # --fleet is a real option, and defaults to the app name's leading segment.
    assert "--fleet) FLEET=" in text
    assert 'FLEET="${APP%%-*}"' in text


def test_provisioner_stamps_instance_and_defaults_from_slug():
    """BOBI_INSTANCE — the per-instance/SaaS-tenant key `bobi deploy`
    reads back to find an app for <name> (next to BOBI_FLEET)."""
    text = PROVISION_SH.read_text()
    assert 'ENV_VARS["BOBI_INSTANCE"]="$INSTANCE"' in text
    assert "--instance) INSTANCE=" in text
    # Defaults to the app name minus the "<fleet>-" prefix (the slug).
    assert 'INSTANCE="${APP#"$FLEET"-}"' in text


def test_provisioner_supports_blank_ssh_push_mode():
    """--blank provisions with NO team source so the entrypoint waits for a
    pushed team — the ssh-push delivery path `bobi deploy` uses for a
    local package."""
    text = PROVISION_SH.read_text()
    assert "--blank) BLANK=" in text
    # Exactly-one-of team/team-url/blank is enforced.
    assert "exactly one of --team / --team-url / --blank" in text


def test_entrypoint_waits_for_team_when_blank():
    """The C9-adjacent change: an empty volume with no team source polls for a
    pushed team instead of crashing (enables ssh-push)."""
    entry = (REPO / "docker" / "docker-entrypoint.sh").read_text()
    assert "waiting for" in entry.lower()
    assert "run/package/agent.yaml" in entry
    # It must NOT fatal on the no-team branch any more.
    assert "nothing to install" not in entry


def test_entrypoint_materializes_codex_api_key_auth_file():
    """Codex does not read OPENAI_API_KEY directly; the entrypoint must create
    auth.json for Codex-brained teams and auxiliary Codex tool users."""
    entry = (REPO / "docker" / "docker-entrypoint.sh").read_text()
    assert 'Path(os.environ["CODEX_CRED_DIR"]) / "auth.json"' in entry
    assert '{"OPENAI_API_KEY": os.environ["OPENAI_API_KEY"]}' in entry
    assert "chmod(0o600)" in entry
    assert 'materialize_codex_api_key_auth "${BRAIN_CRED_DIR}"' in entry
    assert 'materialize_codex_api_key_auth "${HOME}/.codex"' in entry
    assert 'if [ "${BOBI_AUTH:-api_key}" != "subscription" ]; then' in entry
    assert "leaving OPENAI_API_KEY out of Codex auth materialization" in entry
    assert '"BOBI_BRAIN=${ENTRYPOINT_BRAIN}"' in entry


def test_entrypoint_codex_auth_helper_writes_expected_file(tmp_path):
    """Exercise the entrypoint helper itself, not just its source text."""
    entry = (REPO / "docker" / "docker-entrypoint.sh").read_text()
    start = entry.index("materialize_codex_api_key_auth() {")
    end = entry.index("\n\nAUTH_VALIDATED=", start)
    helper = entry[start:end]
    cred_dir = tmp_path / "codex"

    script = f"""
set -euo pipefail
APP_USER="$(id -un)"
log() {{ :; }}
chown() {{ :; }}
{helper}
OPENAI_API_KEY="sk-test" materialize_codex_api_key_auth "{cred_dir}"
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    auth_file = cred_dir / "auth.json"
    assert json.loads(auth_file.read_text()) == {"OPENAI_API_KEY": "sk-test"}
    assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600


def test_entrypoint_codex_auth_helper_noops_without_api_key(tmp_path):
    """Subscription auth has no OPENAI_API_KEY; the helper must leave existing
    OAuth auth.json alone and return successfully."""
    entry = (REPO / "docker" / "docker-entrypoint.sh").read_text()
    start = entry.index("materialize_codex_api_key_auth() {")
    end = entry.index("\n\nAUTH_VALIDATED=", start)
    helper = entry[start:end]
    cred_dir = tmp_path / "codex"
    cred_dir.mkdir()
    auth_file = cred_dir / "auth.json"
    auth_file.write_text('{"tokens":"subscription"}\n')

    script = f"""
set -euo pipefail
APP_USER="$(id -un)"
log() {{ :; }}
chown() {{ :; }}
{helper}
unset OPENAI_API_KEY
materialize_codex_api_key_auth "{cred_dir}"
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert auth_file.read_text() == '{"tokens":"subscription"}\n'


def test_entrypoint_subscription_removes_codex_api_key_auth(tmp_path):
    entry = (REPO / "docker" / "docker-entrypoint.sh").read_text()
    start = entry.index("codex_auth_uses_api_key() {")
    end = entry.index("\n\nAUTH_VALIDATED=", start)
    helper = entry[start:end]
    cred_dir = tmp_path / "codex"
    cred_dir.mkdir()
    auth_file = cred_dir / "auth.json"
    auth_file.write_text('{"OPENAI_API_KEY":"sk-stale"}\n')

    script = f"""
set -euo pipefail
APP_USER="$(id -un)"
log() {{ :; }}
chown() {{ :; }}
{helper}
codex_auth_uses_api_key "{cred_dir}" && rm -f "{auth_file}"
"""
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert not auth_file.exists()


def test_dockerfile_supports_wheel_build_mode():
    """The release pipeline builds the image from a PREBUILT wheel (builder-wheel),
    so the canary smokes — and the fleet runs — the exact bytes published to PyPI."""
    df = (REPO / "Dockerfile").read_text()
    assert "builder-wheel" in df
    assert "COPY dist/" in df  # the staged prebuilt wheel
    # .dockerignore excludes dist/ but must re-include the wheel for wheel-mode.
    assert "!dist/*.whl" in (REPO / ".dockerignore").read_text()


# --- workflows: load + shared helpers ---------------------------------------

def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _jobs(wf: dict) -> dict:
    return wf["jobs"]


def _step_scripts(job: dict) -> str:
    return "\n".join(s.get("run", "") for s in job.get("steps", []))


def test_workflows_parse_and_actionlint_clean():
    # Sanity: release workflow and example workflow both load. (actionlint runs
    # in CI for active workflows; this guards YAML at least.)
    assert _load(WF_TEAMS)["name"]
    assert _load(WF_RELEASE)["name"]


# --- deploy-agent-teams.yml.example invariants (thin client over `bobi deploy`) -

def test_framework_repo_does_not_register_generic_deploy_workflow():
    """The framework repo release path owns only ci-canary. Generic
    deployments/*.yaml reconciliation is example-only here and belongs in
    fleet-owning repos such as moda-agents."""
    assert not (REPO / ".github" / "workflows" / "deploy-agent-teams.yml").exists()
    assert WF_TEAMS.exists()

def test_teams_is_callable_and_not_release_triggered():
    """The reconcile is invoked by the release pipeline via workflow_call (so the
    image roll can precede it), NOT directly on a release. Standalone image-free
    team updates still run via a `deploy-*` tag or workflow_dispatch."""
    wf = _load(WF_TEAMS)
    on = wf.get("on", wf.get(True))  # PyYAML parses bare `on:` as boolean True.
    # Reusable: release.yml calls it as its final step.
    assert "workflow_call" in on
    # No longer fires directly on a release — release.yml owns that ordering.
    assert "release" not in on
    # Standalone, image-free path for a team-definition/secret edit.
    assert any(t.startswith("deploy-") for t in on["push"]["tags"])
    assert "workflow_dispatch" in on
    # A push to a branch must NOT auto-deploy — only the call / tag / dispatch.
    assert "branches" not in (on.get("push") or {})


def test_release_pipeline_does_not_run_generic_deployment_matrix():
    """The framework release is canary-specific. It must not call the generic
    deployments/*.yaml reconciler, because example/experimental deployments in
    this repo should not silently become release gates."""
    jobs = _jobs(_load(WF_RELEASE))
    assert "roll-fleet" not in jobs
    assert "deploy-teams" not in jobs
    assert "deploy-agent-teams.yml" not in yaml.dump(jobs)


def test_teams_is_a_thin_client_no_business_logic_in_yaml():
    """The load-bearing refactor invariant: the reconcile business logic moved
    OUT of the workflow into the `bobi deploy` primitive. The Action must
    not call the provisioner or fleet.sh classify itself any more."""
    wf = _load(WF_TEAMS)
    all_scripts = "\n".join(_step_scripts(j) for j in _jobs(wf).values())
    assert "provision-instance.sh" not in all_scripts
    assert "fleet.sh classify" not in all_scripts
    # The deploy job drives the primitive instead.
    deploy_script = _step_scripts(_jobs(wf)["deploy"])
    assert "bobi deploy" in deploy_script


def test_teams_deploy_binds_tenant_environment_and_per_key_secrets():
    """#385: one GitHub Environment per TENANT (not per deployment), and per-key
    `<TEAM>__<KEY>` secrets filtered from toJSON(secrets) — no BOBI_ENV blob."""
    deploy = _jobs(_load(WF_TEAMS))["deploy"]
    # bound to the deployment's tenant Environment (from the plan matrix)
    assert deploy["environment"] == "${{ matrix.entry.tenant }}"
    script = _step_scripts(deploy)
    # per-key interface: dump all secrets, filter by the <TEAM>__ prefix
    assert "toJSON(secrets)" in script
    assert "startswith($p)" in script
    assert "BOBI_ENV" not in script   # the opaque blob is retired
    assert "umask 077" in script
    assert "--env-file" in script
    # installs the CLI so `bobi deploy` is available in CI
    assert "pip install" in script


def test_teams_deploy_is_idempotent_no_added_vs_changed_split():
    """`bobi deploy` is provision-or-update internally, so the workflow has
    a single deploy path — no separate provision/update jobs keyed on Fly state."""
    jobs = _jobs(_load(WF_TEAMS))
    assert "provision" not in jobs and "update" not in jobs
    assert "deploy" in jobs


def test_teams_reconciles_active_set_and_skips_inactive():
    """A release reconciles every active deployments/<name>.yaml; defaults is
    excluded and inactive (.example) files are never picked up."""
    plan_script = _step_scripts(_jobs(_load(WF_TEAMS))["plan"])
    assert "deployments/*.yaml" in plan_script
    assert 'b" = "defaults"' in plan_script or '"defaults"' in plan_script


def test_teams_surfaces_orphans_for_human_destroy():
    orphans = _step_scripts(_jobs(_load(WF_TEAMS))["orphans"])
    # surfaces unmanaged apps for a human `bobi destroy`, never auto-destroys
    assert "bobi destroy" in orphans
    assert "apps destroy" not in orphans and "destroy-instance.sh" not in orphans


def test_teams_no_ops_without_fly_token():
    """Committing deployments/ before wiring the fleet must not break CI."""
    wf = _load(WF_TEAMS)
    deploy = _jobs(wf)["deploy"]
    assert "configured == 'true'" in deploy["if"]


# --- release.yml invariants (build-wheel → build-canary → publish/roll) ------

def _steps(job: dict) -> list:
    return job.get("steps", [])


def _uses_blob(job: dict) -> str:
    """All `uses:` refs in a job, joined — for asserting actions like up/download."""
    return "\n".join(s.get("uses") or "" for s in _steps(job))


def test_release_triggers_on_published_release():
    wf = _load(WF_RELEASE)
    on = wf.get("on", wf.get(True))
    assert "published" in on["release"]["types"]


def test_release_builds_the_wheel_once_and_uploads_it():
    """The wheel is built ONCE (build-wheel) and uploaded, so the canary and PyPI
    run the exact same artifact."""
    jobs = _jobs(_load(WF_RELEASE))
    bw = jobs["build-wheel"]
    assert "python -m build" in _step_scripts(bw)
    assert "upload-artifact" in _uses_blob(bw)


def test_release_canary_is_built_from_the_wheel_and_smoked():
    """THE gate: the canary image is built FROM the prebuilt wheel (not source) and
    smoked with a functional ask — so we prove the exact bytes we publish boot and
    answer end-to-end."""
    canary = _jobs(_load(WF_RELEASE))["build-canary"]
    # consumes the built wheel and builds the image in wheel mode
    assert "download-artifact" in _uses_blob(canary)
    script = _step_scripts(canary)
    assert "BOBI_BUILD=wheel" in script
    # functional smoke with an abort-on-failure gate — the ask loop lives in
    # canary-smoke.sh (cold-boot-robust), invoked from the workflow.
    assert "scripts/canary-smoke.sh" in script
    smoke = CANARY_SMOKE_SH.read_text()
    assert 'bobi agent \\"\\$BOBI_INSTANCE\\" ask' in smoke and "CANARY-OK" in smoke
    assert "aborting release" in smoke
    # round-trips live config and swaps only the canary image.
    assert "config save" in script and "-c " in script and " -o " not in script


def test_release_publish_is_gated_on_the_canary():
    """PyPI is irreversible, so publish waits on the canary gate. Publish reuses
    the SAME artifact the canary ran (no rebuild)."""
    jobs = _jobs(_load(WF_RELEASE))
    publish = jobs["publish"]
    assert publish["needs"] == "build-canary"      # gated on the canary
    assert publish["environment"] == "pypi"        # trusted-publishing env
    # publishes the proven bytes: downloads the artifact, never rebuilds
    assert "download-artifact" in _uses_blob(publish)
    assert "python -m build" not in _step_scripts(publish)
    assert any("pypi-publish" in (s.get("uses") or "") for s in _steps(publish))


def test_release_targets_only_the_named_brain_canaries():
    """The framework repo should build/smoke the permanent brain canaries by name,
    not scan the whole ci fleet and pick arbitrary apps."""
    script = _step_scripts(_jobs(_load(WF_RELEASE))["build-canary"])
    assert '-canary' in script
    assert "scripts/fleet.sh list" not in script
    assert "render-team-deps.py" not in script
    assert "scripts/canary-smoke.sh" in script
    # UI reachability gate, per-canary in the loop (deployment name + its app).
    assert 'bobi agent "$name" ui --app "$app" --check' in script
    # load-bearing flags from the provisioner (one-volume + zstd boot bug)
    assert "--ha=false" in script
    assert "--depot=false" in script


def test_release_smokes_both_brain_canaries_at_parity():
    """#428: Codex is a hard release gate at parity with Claude. Both canaries are
    named explicitly and smoked from the same wheel — ci-canary (Claude) is
    `required`, ci-codex-smoke (Codex) is `bootstrap` (a one-time warn+skip window
    until it is provisioned, then a hard gate)."""
    script = _step_scripts(_jobs(_load(WF_RELEASE))["build-canary"])
    assert "-canary:required" in script          # Claude canary, mandatory
    assert "-codex-smoke:bootstrap" in script    # Codex canary, gate-once-live
    # Both go through the ONE build+smoke loop (same wheel, same base image).
    assert "for spec in" in script


def test_release_publishes_to_pypi_only_after_the_canary():
    """Publish + its wheel-dependent downstream (Homebrew) are part of THIS pipeline
    (trusted publishing can't run from a reusable workflow), all behind the canary.
    The event server (a Cloudflare Worker, no dependency on the published wheel)
    deploys BEFORE the canary so event-server-only fixes are live when the canary
    runs its functional gate against the live event bus."""
    jobs = _jobs(_load(WF_RELEASE))
    assert jobs["deploy-event-server"]["needs"] == ["subscription-login-smoke", "build-wheel"]
    assert "deploy-event-server" in jobs["build-canary"]["needs"]
    assert jobs["update-homebrew"]["needs"] == "publish"
    assert "roll-fleet" not in jobs
    assert "deploy-teams" not in jobs


def test_release_smokes_homebrew_bottle_urls_after_dispatch():
    """The release gate must catch malformed tap bottle URLs before users do.

    Issue #493: the tap formula had a root_url whose release tag segment included
    `.arm64_sequoia.bottle.tar`, so Homebrew constructed a 404 bottle URL.
    """
    script = _step_scripts(_jobs(_load(WF_RELEASE))["update-homebrew"])
    assert "actions/checkout" in _uses_blob(_jobs(_load(WF_RELEASE))["update-homebrew"])
    assert "Smoke Homebrew bottle URLs" in "\n".join(
        step.get("name", "") for step in _steps(_jobs(_load(WF_RELEASE))["update-homebrew"])
    )
    assert "scripts/smoke-homebrew-bottles.sh" in script


def _run_homebrew_smoke(formula: str, tmp_path: Path) -> subprocess.CompletedProcess:
    formula_path = tmp_path / "bobi.rb"
    formula_path.write_text(formula)
    env = {
        **os.environ,
        "BOBI_HOMEBREW_FORMULA_FILE": str(formula_path),
        "BOBI_HOMEBREW_SKIP_HEAD": "1",
        "BOBI_HOMEBREW_SMOKE_ATTEMPTS": "1",
        "BOBI_HOMEBREW_SMOKE_SLEEP": "0",
    }
    return subprocess.run(
        [str(HOMEBREW_SMOKE_SH), "0.33.0"],
        capture_output=True,
        text=True,
        env=env,
    )


def test_homebrew_smoke_accepts_valid_bottle_formula(tmp_path):
    formula = """
class Bobi < Formula
  url "https://files.pythonhosted.org/packages/bobi-0.33.0.tar.gz"
  bottle do
    root_url "https://github.com/moda-labs/homebrew-bobi-agent/releases/download/bobi-0.33.0"
    sha256 cellar: :any_skip_relocation, arm64_sequoia: "aaaaaaaa"
    sha256 cellar: :any_skip_relocation, arm64_sonoma: "bbbbbbbb"
  end
end
"""
    result = _run_homebrew_smoke(formula, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "bobi-0.33.0.arm64_sequoia.bottle.tar.gz" in result.stdout
    assert "bobi-0.33.0.arm64_sonoma.bottle.tar.gz" in result.stdout


def test_homebrew_smoke_rejects_current_version_malformed_root_url(tmp_path):
    formula = """
class Bobi < Formula
  url "https://files.pythonhosted.org/packages/bobi-0.33.0.tar.gz"
  bottle do
    root_url "https://github.com/moda-labs/homebrew-bobi-agent/releases/download/bobi-0.33.0.arm64_sequoia.bottle.tar"
    sha256 cellar: :any_skip_relocation, arm64_sequoia: "aaaaaaaa"
  end
end
"""
    result = _run_homebrew_smoke(formula, tmp_path)
    assert result.returncode == 1
    assert "root_url is malformed" in result.stdout


def test_homebrew_smoke_waits_for_incomplete_bottle_formula(tmp_path):
    formula = """
class Bobi < Formula
  url "https://files.pythonhosted.org/packages/bobi-0.33.0.tar.gz"
end
"""
    result = _run_homebrew_smoke(formula, tmp_path)
    assert result.returncode == 1
    assert "Timed out waiting" in result.stdout
