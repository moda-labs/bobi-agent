"""C22 GitOps deploy/update automation (#342), refactored onto the
`modastack deploy` primitive (DEPLOY_INTERFACE.md).

Covers the fleet-state primitive (`scripts/fleet.sh`), the provisioner's
identity stamps + ssh-push blank mode, and the structural invariants of the two
GitOps workflows. The shell helpers are exercised for real (bash subprocess with
a stubbed `fleet_exists`, so no Fly calls); the workflows are parsed and
asserted, so the load-bearing decisions break loudly if someone regresses them:

  * deploy-agent-teams is a THIN CLIENT — the reconcile business logic lives in
    `modastack deploy` (idempotent provision-or-update), not the YAML;
  * each instance is stamped MODASTACK_FLEET + MODASTACK_INSTANCE (the
    SaaS-extensible fleet/tenant keys — the app name is only a hint);
  * --blank provisions a team-less instance whose entrypoint waits for an
    ssh-pushed team (the local-package delivery path);
  * the secret interface is per-key `<TEAM>__<KEY>` secrets in a per-tenant
    GitHub Environment, filtered from toJSON(secrets) (#385 — no opaque blob);
  * deletions never auto-deploy — orphaned apps surface for human destroy;
  * release rollout builds one image and reuses it across the fleet.

The deploy ENGINE itself (config precedence, delivery selection, secret
validation) is unit-tested in test_deploy.py.
"""

import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
FLEET_SH = REPO / "scripts" / "fleet.sh"
PROVISION_SH = REPO / "scripts" / "provision-instance.sh"
DESTROY_SH = REPO / "scripts" / "destroy-instance.sh"
WF_TEAMS = REPO / ".github" / "workflows" / "deploy-agent-teams.yml"
WF_RELEASE = REPO / ".github" / "workflows" / "release.yml"
WF_PUBLISH = REPO / ".github" / "workflows" / "publish-pypi.yml"


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
    for script in (FLEET_SH, PROVISION_SH, DESTROY_SH):
        sc = subprocess.run(["shellcheck", str(script)], capture_output=True, text=True)
        assert sc.returncode == 0, f"{script.name}:\n{sc.stdout}{sc.stderr}"


# --- scripts/provision-instance.sh: identity stamps + ssh-push blank mode ----

def test_provisioner_stamps_fleet_and_defaults_from_app():
    text = PROVISION_SH.read_text()
    # The stamp goes into the [env] identity block read back for enumeration.
    assert 'ENV_VARS["MODASTACK_FLEET"]="$FLEET"' in text
    # --fleet is a real option, and defaults to the app name's leading segment.
    assert "--fleet) FLEET=" in text
    assert 'FLEET="${APP%%-*}"' in text


def test_provisioner_stamps_instance_and_defaults_from_slug():
    """MODASTACK_INSTANCE — the per-instance/SaaS-tenant key `modastack deploy`
    reads back to find an app for <name> (next to MODASTACK_FLEET)."""
    text = PROVISION_SH.read_text()
    assert 'ENV_VARS["MODASTACK_INSTANCE"]="$INSTANCE"' in text
    assert "--instance) INSTANCE=" in text
    # Defaults to the app name minus the "<fleet>-" prefix (the slug).
    assert 'INSTANCE="${APP#"$FLEET"-}"' in text


def test_provisioner_supports_blank_ssh_push_mode():
    """--blank provisions with NO team source so the entrypoint waits for a
    pushed team — the ssh-push delivery path `modastack deploy` uses for a
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
    assert ".modastack/agent.yaml" in entry
    # It must NOT fatal on the no-team branch any more.
    assert "nothing to install" not in entry


# --- workflows: load + shared helpers ---------------------------------------

def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _jobs(wf: dict) -> dict:
    return wf["jobs"]


def _step_scripts(job: dict) -> str:
    return "\n".join(s.get("run", "") for s in job.get("steps", []))


def test_workflows_parse_and_actionlint_clean():
    # Sanity: both load. (actionlint runs in CI; this guards YAML at least.)
    assert _load(WF_TEAMS)["name"]
    assert _load(WF_RELEASE)["name"]


# --- deploy-agent-teams.yml invariants (thin client over `modastack deploy`) -------

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


def test_release_pipeline_deploys_teams_after_images():
    """The single gated pipeline: build-images (image roll) FIRST, then
    deploy-teams reconciles package content + secrets by CALLING the reusable
    deploy-agent-teams workflow. Image always precedes the package/secret reconcile."""
    jobs = _jobs(_load(WF_RELEASE))
    assert "build-images" in jobs
    deploy_teams = jobs["deploy-teams"]
    # ordered after the image roll
    assert deploy_teams["needs"] == "build-images"
    # reuses the standalone reconcile workflow (one implementation, two entry points)
    assert deploy_teams["uses"] == "./.github/workflows/deploy-agent-teams.yml"
    # forwards FLY_API_TOKEN + per-tenant Environment secrets to the called workflow
    assert deploy_teams["secrets"] == "inherit"


def test_teams_is_a_thin_client_no_business_logic_in_yaml():
    """The load-bearing refactor invariant: the reconcile business logic moved
    OUT of the workflow into the `modastack deploy` primitive. The Action must
    not call the provisioner or fleet.sh classify itself any more."""
    wf = _load(WF_TEAMS)
    all_scripts = "\n".join(_step_scripts(j) for j in _jobs(wf).values())
    assert "provision-instance.sh" not in all_scripts
    assert "fleet.sh classify" not in all_scripts
    # The deploy job drives the primitive instead.
    deploy_script = _step_scripts(_jobs(wf)["deploy"])
    assert "modastack deploy" in deploy_script


def test_teams_deploy_binds_tenant_environment_and_per_key_secrets():
    """#385: one GitHub Environment per TENANT (not per deployment), and per-key
    `<TEAM>__<KEY>` secrets filtered from toJSON(secrets) — no MODASTACK_ENV blob."""
    deploy = _jobs(_load(WF_TEAMS))["deploy"]
    # bound to the deployment's tenant Environment (from the plan matrix)
    assert deploy["environment"] == "${{ matrix.entry.tenant }}"
    script = _step_scripts(deploy)
    # per-key interface: dump all secrets, filter by the <TEAM>__ prefix
    assert "toJSON(secrets)" in script
    assert "startswith($p)" in script
    assert "MODASTACK_ENV" not in script   # the opaque blob is retired
    assert "umask 077" in script
    assert "--env-file" in script
    # installs the CLI so `modastack deploy` is available in CI
    assert "pip install" in script


def test_teams_deploy_is_idempotent_no_added_vs_changed_split():
    """`modastack deploy` is provision-or-update internally, so the workflow has
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
    # surfaces unmanaged apps for a human `modastack destroy`, never auto-destroys
    assert "modastack destroy" in orphans
    assert "apps destroy" not in orphans and "destroy-instance.sh" not in orphans


def test_teams_no_ops_without_fly_token():
    """Committing deployments/ before wiring the fleet must not break CI."""
    wf = _load(WF_TEAMS)
    deploy = _jobs(wf)["deploy"]
    assert "configured == 'true'" in deploy["if"]


# --- release.yml invariants (build-images job) ------------------------------

def test_release_triggers_on_published_release():
    wf = _load(WF_RELEASE)
    on = wf.get("on", wf.get(True))
    assert "published" in on["release"]["types"]


def test_release_builds_once_and_reuses_image_across_fleet():
    script = _step_scripts(_jobs(_load(WF_RELEASE))["build-images"])
    # enumerate the fleet by the stamp, build the generic image once, reuse --image
    assert "scripts/fleet.sh list" in script
    assert "fly image show" in script
    assert "--image" in script
    # round-trip live config so env/mounts/vm/volume survive the image swap.
    # `config save` writes to the path given by -c (NOT -o, which it rejects) —
    # both flag and image-ref shape were caught by the live e2e.
    assert "config save" in script and "-c " in script and " -o " not in script
    # image ref is constructed (Ref/Reference fields come back null from Fly)
    assert "registry.fly.io/" in script and "Digest" in script


def test_release_is_team_aware():
    """A framework release must REBUILD a team-flavored instance's own image
    (its TEAM_DEPS hook) instead of rolling the generic image onto it, which
    would strip its baked tools and break dispatch (C24 #368)."""
    script = _step_scripts(_jobs(_load(WF_RELEASE))["build-images"])
    # the renderer decides generic-vs-team per app, and team apps build with TEAM_DEPS
    assert "render-team-deps.py" in script
    assert "TEAM_DEPS=" in script
    # the canary functional gate (replaces the retired EC2 release smoke)
    assert "CANARY-OK" in script


def test_release_functional_canary_gate():
    script = _step_scripts(_jobs(_load(WF_RELEASE))["build-images"])
    # build + smoke the canary BEFORE rolling the rest: a blocking `ask` proving
    # the new image's agent answers, asserted by a marker.
    assert "modastack ask" in script and "CANARY-OK" in script
    assert "aborting rollout" in script  # a failed smoke stops the roll


def test_release_isolates_per_app_failures():
    script = _step_scripts(_jobs(_load(WF_RELEASE))["build-images"])
    assert "fails+=(" in script  # collect, don't abort the fleet
    # load-bearing flags from the provisioner (one-volume + zstd boot bug)
    assert "--ha=false" in script
    assert "--depot=false" in script


# --- publish-pypi.yml invariants (gate the irreversible publish) ------------

def test_publish_triggers_on_release_not_bare_tag():
    """Publish fires on a published Release (the same gate as release.yml), NOT a
    raw `v*` tag push — so a bare `git push --tags` can't ship an unverified build,
    and the publish lines up behind the same event as the fleet roll."""
    on = _load(WF_PUBLISH).get("on", _load(WF_PUBLISH).get(True))
    assert "published" in on["release"]["types"]
    assert "push" not in on  # the old tag-push trigger is retired


def test_publish_is_gated_on_a_functional_wheel_smoke():
    """PyPI is irreversible, so the upload job depends on a job that builds the
    wheel, installs THAT artifact into a clean venv, and runs it — test the bytes,
    then ship the bytes (the publish step does NOT rebuild)."""
    jobs = _jobs(_load(WF_PUBLISH))
    assert jobs["publish"]["needs"] == "build-and-smoke"
    smoke = _step_scripts(jobs["build-and-smoke"])
    assert "python -m build" in smoke
    assert "dist/*.whl" in smoke           # installs the built artifact, not from PyPI
    assert "modastack --version" in smoke  # functional: entry point + version
    assert "modastack skill" in smoke      # functional: package_data shipped
    # publish reuses the tested artifact instead of rebuilding it
    publish_steps = jobs["publish"].get("steps", [])
    assert any("download-artifact" in (s.get("uses") or "") for s in publish_steps)
    assert not any("python -m build" in (s.get("run") or "") for s in publish_steps)
