"""C22 GitOps deploy/update automation (#342), refactored onto the
`modastack deploy` primitive (DEPLOY_INTERFACE.md).

Covers the fleet-state primitive (`scripts/fleet.sh`), the provisioner's
identity stamps + ssh-push blank mode, and the structural invariants of the two
GitOps workflows. The shell helpers are exercised for real (bash subprocess with
a stubbed `fleet_exists`, so no Fly calls); the workflows are parsed and
asserted, so the load-bearing decisions break loudly if someone regresses them:

  * gitops-teams is a THIN CLIENT — the reconcile business logic lives in
    `modastack deploy` (idempotent provision-or-update), not the YAML;
  * each instance is stamped MODASTACK_FLEET + MODASTACK_INSTANCE (the
    SaaS-extensible fleet/tenant keys — the app name is only a hint);
  * --blank provisions a team-less instance whose entrypoint waits for an
    ssh-pushed team (the local-package delivery path);
  * the secret interface is one MODASTACK_ENV blob per GitHub Environment;
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
WF_TEAMS = REPO / ".github" / "workflows" / "gitops-teams.yml"
WF_RELEASE = REPO / ".github" / "workflows" / "gitops-release.yml"


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


def test_fleet_sh_passes_shellcheck():
    sc = subprocess.run(["shellcheck", str(FLEET_SH)], capture_output=True, text=True)
    assert sc.returncode == 0, sc.stdout + sc.stderr


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


# --- gitops-teams.yml invariants (thin client over `modastack deploy`) -------

def test_teams_triggered_by_push_to_main_on_config_paths():
    wf = _load(WF_TEAMS)
    on = wf.get("on", wf.get(True))  # PyYAML parses bare `on:` as boolean True.
    assert on["push"]["branches"] == ["main"]
    paths = on["push"]["paths"]
    assert any("deployments/" in p for p in paths)
    assert any("agents/" in p for p in paths)


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


def test_teams_deploy_binds_per_deployment_environment_and_env_blob():
    deploy = _jobs(_load(WF_TEAMS))["deploy"]
    # one GitHub Environment per deployment, computed from the matrix name
    assert deploy["environment"] == "${{ matrix.name }}"
    script = _step_scripts(deploy)
    # the single-blob secret interface, written umask-077, handed to the primitive
    assert "MODASTACK_ENV" in script
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


def test_teams_excludes_deletions_and_surfaces_orphans_for_human_destroy():
    wf = _load(WF_TEAMS)
    plan_script = _step_scripts(_jobs(wf)["plan"])
    assert "--diff-filter=d" in plan_script  # deletions never auto-deploy
    orphans = _step_scripts(_jobs(wf)["orphans"])
    # surfaces unmanaged apps for a human `modastack destroy`, never auto-destroys
    assert "modastack destroy" in orphans
    assert "apps destroy" not in orphans and "destroy-instance.sh" not in orphans


def test_teams_no_ops_without_fly_token():
    """Committing deployments/ before wiring the fleet must not break CI."""
    wf = _load(WF_TEAMS)
    deploy = _jobs(wf)["deploy"]
    assert "configured == 'true'" in deploy["if"]


# --- gitops-release.yml invariants ------------------------------------------

def test_release_triggers_on_published_release():
    wf = _load(WF_RELEASE)
    on = wf.get("on", wf.get(True))
    assert "published" in on["release"]["types"]


def test_release_builds_once_and_reuses_image_across_fleet():
    script = _step_scripts(_jobs(_load(WF_RELEASE))["rollout"])
    # enumerate the fleet by the stamp, build first, reuse --image for the rest
    assert "scripts/fleet.sh list" in script
    assert "fly image show" in script
    assert "--image" in script
    # round-trip live config so env/mounts/vm/volume survive the image swap.
    # `config save` writes to the path given by -c (NOT -o, which it rejects) —
    # both flag and image-ref shape were caught by the live e2e.
    assert "fly config save" in script
    assert "config save" in script and "-c " in script and " -o " not in script
    # image ref is constructed (Ref/Reference fields come back null from Fly)
    assert "registry.fly.io/" in script and "Digest" in script


def test_release_isolates_per_app_failures():
    script = _step_scripts(_jobs(_load(WF_RELEASE))["rollout"])
    assert "fails+=(" in script  # collect, don't abort the fleet
    # load-bearing flags from the provisioner (one-volume + zstd boot bug)
    assert "--ha=false" in script
    assert "--depot=false" in script
