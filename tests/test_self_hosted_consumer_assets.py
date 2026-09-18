"""Keep the public self-host proof executable and free of private assumptions."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from scripts.render_worker_ci_config import load_jsonc
from tests.workflow_utils import load_workflow, workflow_on

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = REPO_ROOT / "examples" / "self-host"
TERRAFORM = EXAMPLE / "terraform"
KUBERNETES = EXAMPLE / "kubernetes"


def _text(path: Path) -> str:
    return path.read_text()


def test_terraform_matches_the_shipped_worker_contract():
    shipped = load_jsonc(REPO_ROOT / "event-server" / "worker" / "wrangler.jsonc")
    main = _text(TERRAFORM / "main.tf")
    versions = _text(TERRAFORM / "versions.tf")
    variables = _text(TERRAFORM / "variables.tf")

    assert 'source  = "cloudflare/cloudflare"' in versions
    assert 'version = "5.25.0"' in versions
    assert f'compatibility_date = "{shipped["compatibility_date"]}"' in main
    for flag in shipped["compatibility_flags"]:
        assert f'"{flag}"' in main
    migration = shipped["migrations"][0]
    assert f'new_tag            = "{migration["tag"]}"' in main
    for class_name in migration["new_sqlite_classes"]:
        assert f'new_sqlite_classes = ["{class_name}"]' in main
    durable_binding = shipped["durable_objects"]["bindings"][0]
    assert f'name       = "{durable_binding["name"]}"' in main
    assert f'class_name = "{durable_binding["class_name"]}"' in main
    assert 'name         = "EVENTS"' in main
    assert 'type         = "kv_namespace"' in main
    assert re.search(r"subdomain\s*=\s*\{", main)
    assert "enabled          = true" in main
    assert "previews_enabled = false" in main
    assert 'strategy    = "percentage"' in main
    assert "percentage = 100" in main
    assert 'resource "cloudflare_worker_version" "migration"' in main
    assert 'resource "cloudflare_workers_deployment" "migration"' in main
    assert "depends_on = [cloudflare_workers_deployment.migration]" in main
    migration_version = main.split(
        'resource "cloudflare_worker_version" "migration" {', 1
    )[1].split('resource "cloudflare_workers_deployment" "migration" {', 1)[0]
    serving_version = main.split(
        'resource "cloudflare_worker_version" "events" {', 1
    )[1].split('resource "cloudflare_workers_deployment" "events" {', 1)[0]
    assert 'name       = "DEPLOYMENT_SESSION"' not in migration_version
    assert 'new_sqlite_classes = ["DeploymentSession"]' in migration_version
    assert 'name       = "DEPLOYMENT_SESSION"' in serving_version
    assert "new_sqlite_classes" not in serving_version
    assert 'worker_name != "bobi-events"' in variables
    for secret in ("internal_do_secret", "fleet_operator_token"):
        assert f'variable "{secret}"' in variables
        block_start = variables.index(f'variable "{secret}"')
        block_end = variables.find("\n}", block_start)
        assert "sensitive   = true" in variables[block_start:block_end]


def test_terraform_state_and_worker_secrets_are_not_accidentally_published():
    ignored = _text(TERRAFORM / ".gitignore")
    assert ".terraform/" in ignored
    assert "*.tfstate" in ignored
    assert "*.tfstate.*" in ignored
    lock = _text(TERRAFORM / ".terraform.lock.hcl")
    assert 'provider "registry.terraform.io/cloudflare/cloudflare"' in lock
    assert 'version     = "5.25.0"' in lock


def test_kubernetes_example_uses_the_public_image_and_k8s_identity_contract():
    deployment = yaml.safe_load(_text(KUBERNETES / "deployment.yaml"))
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = {entry["name"]: entry for entry in container["env"]}

    assert pod["shareProcessNamespace"] is True
    assert container["image"] == "ghcr.io/moda-labs/bobi:latest"
    assert env["BOBI_BRAIN"]["value"] == "stub"
    assert env["BOBI_STUB_BRAIN"]["value"] == "1"
    assert env["ANTHROPIC_API_KEY"]["value"] == "stub-not-used"
    assert env["BOBI_HEALTH_BIND"]["value"] == "0.0.0.0"
    assert env["BOBI_HEALTH_PORT"]["value"] == "8081"
    assert env["POD_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name"
    assert env["NODE_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "spec.nodeName"
    assert container["readinessProbe"]["httpGet"]["path"] == "/ready"
    assert container["livenessProbe"]["httpGet"]["path"] == "/health"
    assert {volume["name"] for volume in pod["volumes"]} >= {"state", "team"}
    public_files = [
        path
        for path in EXAMPLE.rglob("*")
        if path.is_file() and ".terraform" not in path.parts
    ]
    assert all("moda-agents" not in _text(path) for path in public_files)


def test_team_fixture_is_deterministic_and_only_declares_public_runtime_inputs():
    team = yaml.safe_load(_text(KUBERNETES / "team" / "agent.yaml"))
    assert team["agent"] == "self-host-consumer"
    assert team["entry_point"] == "manager"
    assert team["brain"] == {"kind": "stub"}
    assert team["event_server"] == "${BOBI_EVENT_SERVER}"
    assert "ANTHROPIC_API_KEY" not in _text(KUBERNETES / "team" / "agent.yaml")


def test_workflow_is_live_gated_fork_safe_and_always_cleans_up():
    workflow = load_workflow("self-hosted-consumer.yml")
    on = workflow_on(workflow)
    assert "schedule" in on
    assert "workflow_dispatch" in on
    assert "labeled" in on["pull_request"]["types"]
    assert workflow["jobs"]["consumer-proof"]["timeout-minutes"] == 30

    steps = workflow["jobs"]["consumer-proof"]["steps"]
    names = [step.get("name") for step in steps]
    assert "Decide whether the live lane runs" in names
    assert "Create the disposable Kubernetes cluster" in names
    assert "Run the published image under the Kubernetes sidecar" in names
    assert "Assert the consumer proof actually ran" in names
    cleanup = next(
        step
        for step in steps
        if step.get("name") == "Destroy the scratch Cloudflare resources"
    )
    assert cleanup["if"].startswith("always()")
    gate = next(
        step
        for step in steps
        if step.get("name") == "Decide whether the live lane runs"
    )
    assert '"$PR_HEAD_REPO" != "$THIS_REPO"' in gate["run"]
    checkouts = [
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/checkout")
    ]
    assert len(checkouts) == 1
    assert "repository" not in checkouts[0].get("with", {})
    workflow_path = REPO_ROOT / ".github" / "workflows" / "self-hosted-consumer.yml"
    assert "moda-agents" not in _text(workflow_path)


def test_workflow_uses_public_image_and_checks_both_external_proofs():
    text = _text(REPO_ROOT / ".github" / "workflows" / "self-hosted-consumer.yml")
    assert 'image="ghcr.io/moda-labs/bobi:${version}"' in text
    assert "tr -d '[:space:]' < VERSION" in text
    assert 'worker="bobi-self-host-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in text
    assert "::add-mask::$internal" in text
    assert "::add-mask::$operator" in text
    assert "--expect-passed 2" in text
    assert "test_self_hosted_fleet_api_fails_closed_without_the_operator_token" in text
    assert "test_kubernetes_sidecar_heartbeats_and_restarts_from_outside_the_cluster" in text
    assert "terraform destroy" in text
