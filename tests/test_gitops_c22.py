"""C22 GitOps deploy/update automation (#342).

Covers the fleet-state primitive (`scripts/fleet.sh`) and the structural
invariants of the two GitOps workflows + the provisioner's fleet stamp. The
shell helpers are exercised for real (bash subprocess with a stubbed
`fleet_exists`, so no Fly calls); the workflows are parsed and asserted, so the
load-bearing decisions break loudly if someone regresses them:

  * added vs changed is classified by Fly STATE, not git status;
  * a changed URL-sourced team is updated with `modastack install <url>`,
    NOT `modastack agents update` (which resolves via the registry, not a URL);
  * each provisioned instance is stamped MODASTACK_FLEET (the SaaS-extensible
    fleet-membership key — name is only a hint);
  * the secret interface is one MODASTACK_ENV blob per GitHub Environment;
  * release rollout builds one image and reuses it across the fleet.
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


# --- scripts/provision-instance.sh: the MODASTACK_FLEET stamp ----------------

def test_provisioner_stamps_fleet_and_defaults_from_app():
    text = PROVISION_SH.read_text()
    # The stamp goes into the [env] identity block read back for enumeration.
    assert 'ENV_VARS["MODASTACK_FLEET"]="$FLEET"' in text
    # --fleet is a real option, and defaults to the app name's leading segment.
    assert "--fleet) FLEET=" in text
    assert 'FLEET="${APP%%-*}"' in text


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


# --- gitops-teams.yml invariants --------------------------------------------

def test_teams_triggered_by_team_packages_completion():
    wf = _load(WF_TEAMS)
    # PyYAML parses the bare `on:` key as boolean True.
    on = wf.get("on", wf.get(True))
    assert on["workflow_run"]["workflows"] == ["Team packages"]
    assert "completed" in on["workflow_run"]["types"]


def test_teams_diff_gates_on_main_success():
    diff = _jobs(_load(WF_TEAMS))["diff"]
    cond = diff["if"]
    assert "success" in cond and "main" in cond


def test_teams_diff_excludes_deletions_and_classifies_via_fleet():
    script = _step_scripts(_jobs(_load(WF_TEAMS))["diff"])
    # deletions excluded (human destroy only), classification delegates to fleet.sh
    assert "--diff-filter=d" in script
    assert "scripts/fleet.sh classify" in script


def test_teams_provision_binds_per_team_environment_and_env_blob():
    prov = _jobs(_load(WF_TEAMS))["provision"]
    # one GitHub Environment per team, computed from the matrix
    assert prov["environment"] == "${{ matrix.team }}"
    script = _step_scripts(prov)
    # the single-blob secret interface (token-broker seam), written umask-077
    assert "MODASTACK_ENV" in script
    assert "umask 077" in script
    # provisions with the fleet stamp + the rolling teams-latest tarball
    assert "--fleet" in script
    assert "releases/download/teams-latest/" in script


def test_teams_update_uses_install_url_not_agents_update():
    """The single highest-value correctness invariant for C22."""
    upd = _jobs(_load(WF_TEAMS))["update"]
    script = _step_scripts(upd)
    assert "modastack install" in script
    assert "modastack agents update" not in script
    # in-place reload after the workspace-safe reinstall
    assert "machine restart" in script
    # update needs no GitHub Environment (secrets already on the volume)
    assert "environment" not in upd


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
