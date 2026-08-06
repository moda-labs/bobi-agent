"""The live CI lanes cannot be silently disabled.

This initiative (#909) exists because four separate lanes were GREEN while
proving nothing: `-m "not live"`, a retired self-hosted runner, workflows with
0 runs after a relocation, and a `skipif` on a secret nobody had added. Every
one of those failed open — quietly, and for months.

So the wiring itself is under test. A lane that stops running is worse than
one that never existed, because the green check keeps asserting that it did.
`release-fleet.yml`'s `smoked` output is the in-repo precedent for guarding a
gate this way.

Two things are covered here:

1. the workflow wiring — the triggers, the live steps, and the ran-assertions
   that make a skipped lane fail instead of pass;
2. the two guard scripts themselves, so the ran-assertion and the Worker
   isolation guard are proven non-vacuous here rather than trusted.
"""

import re
from pathlib import Path

import pytest

from tests.workflow_utils import load_workflow, workflow_on

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

CLAUDE_ROUNDTRIP = "test_image_ask_roundtrip"
CODEX_ROUNDTRIP = "test_image_codex_ask_roundtrip"
DEPLOY_HEALTH = "test_deployed_worker_health"
DEPLOY_ROUNDTRIP = "test_deployed_worker_publish_subscribe_roundtrip"
# The MCP route's only real proof: `nodejs_compat` cannot be exercised by the
# Worker unit suite, because vitest-pool-workers injects its own enable_nodejs_*
# flags. Drop this from the lane and the flag goes back to being unproven.
DEPLOY_MCP_CALL = "test_deployed_worker_mcp_tool_call"
DEPLOY_MCP_CLOSED = "test_deployed_worker_mcp_route_is_closed_without_a_token"
# The OTLP lane (#978) spends no credentials, but it is gated the same way -
# every test skips without a collector - so it carries the same ran-assertion.
OTEL_METRIC = "test_collector_accepts_a_metric_and_renders_its_identity"
OTEL_LOG = "test_collector_accepts_a_log_record"
OTEL_WRONG_TYPE = "test_collector_rejects_a_bare_resource_metrics_body"
OTEL_CLI = "test_the_cli_reaches_the_collector_end_to_end"


def _steps(workflow: str, job: str) -> list[dict]:
    return load_workflow(workflow)["jobs"][job]["steps"]


def _step(steps: list[dict], name: str) -> dict:
    for step in steps:
        if step.get("name") == name:
            return step
    raise AssertionError(
        f"step {name!r} is gone — the live lane it belongs to no longer runs. "
        f"Present: {[s.get('name') or s.get('uses') for s in steps]}"
    )


def _index(steps: list[dict], name: str) -> int:
    # Via _step so a renamed step reports which step is gone, rather than
    # surfacing a bare StopIteration.
    _step(steps, name)
    return next(i for i, s in enumerate(steps) if s.get("name") == name)


# ---------------------------------------------------------------------------
# container.yml — the brain lane (Phase 1)
# ---------------------------------------------------------------------------


def test_container_workflow_keeps_the_ci_live_label_trigger():
    """The `ci:live` label must still be able to fire a run.

    Default `pull_request` types do NOT include `labeled`, so dropping these
    turns the label into a no-op that looks configured.
    """
    on = workflow_on(load_workflow("container.yml"))
    assert "labeled" in on["pull_request"]["types"]
    assert "unlabeled" in on["pull_request"]["types"]
    assert "schedule" in on, "the nightly external-rot canary is gone"
    assert "workflow_dispatch" in on, "the manual live trigger is gone"


def test_live_brain_roundtrips_still_run_on_both_brains():
    steps = _steps("container.yml", "container-image")
    live = _step(steps, "Run the live brain round-trips (Claude + Codex)")

    run = live["run"]
    assert '-m "docker and live"' in run, (
        "the live marker expression changed — the round-trips may no longer be "
        "selected"
    )
    assert "--junitxml=live-roundtrips.xml" in run, (
        "no junit report means the ran-assertion has nothing to check"
    )

    env = live["env"]
    assert env["ANTHROPIC_API_KEY"] == "${{ secrets.ANTHROPIC_API_KEY }}"
    assert env["OPENAI_API_KEY"] == "${{ secrets.OPENAI_API_KEY }}"
    assert env["BOBI_TEST_IMAGE"] == "bobi:ci"


def test_live_lane_installs_the_wrangler_harness_it_needs():
    """Without `npm ci`, `_has_wrangler()` is false and BOTH round-trips skip.

    That is the single most likely way this lane reverts to a silent no-op,
    because it still exits 0.
    """
    steps = _steps("container.yml", "container-image")
    install = _step(steps, "Install the wrangler harness")
    assert install["working-directory"] == "event-server"
    assert "npm ci" in install["run"]

    # wrangler refuses Node < 22; the job pins 20 for the wheel build, so the
    # swap has to happen after it and before the harness install.
    node_swaps = [
        i
        for i, s in enumerate(steps)
        if str(s.get("uses", "")).startswith("actions/setup-node")
        and s.get("with", {}).get("node-version") == "22"
    ]
    assert node_swaps, "the Node 22 swap is gone — wrangler cannot start on 20"
    assert node_swaps[0] < _index(steps, "Install the wrangler harness")
    assert _index(steps, "Build the wheel into the build context") < node_swaps[0], (
        "Node was raised to 22 before the wheel build, which requires exactly 20"
    )


@pytest.mark.parametrize(
    "workflow,job,step_name,junit,expect_passed,required",
    [
        (
            "container.yml",
            "container-image",
            "Assert the live round-trips actually ran",
            "live-roundtrips.xml",
            2,
            (CLAUDE_ROUNDTRIP, CODEX_ROUNDTRIP),
        ),
        (
            "worker-deploy-smoke.yml",
            "deploy-smoke",
            "Assert the deploy smoke actually ran",
            "worker-deploy-smoke.xml",
            4,
            (DEPLOY_HEALTH, DEPLOY_ROUNDTRIP, DEPLOY_MCP_CALL, DEPLOY_MCP_CLOSED),
        ),
        (
            "container.yml",
            "container-image",
            "Assert the collector round-trip actually ran",
            "otel-collector.xml",
            4,
            (OTEL_METRIC, OTEL_LOG, OTEL_WRONG_TYPE, OTEL_CLI),
        ),
    ],
)
def test_each_live_lane_asserts_it_ran(workflow, job, step_name, junit, expect_passed, required):
    """Ran-assertion, layer (ii), on every live lane.

    Exiting 0 is not evidence: every test in these lanes carries a `skipif`.

    The count is pinned alongside the names so that ADDING a test to a lane
    without raising `--expect-passed` fails here: the named tests would still
    pass while the new one skipped silently, which is the exact shape this
    whole guard exists to catch.
    """
    run = _step(_steps(workflow, job), step_name)["run"]
    assert "scripts/assert_junit_ran.py" in run
    assert junit in run
    assert f"--expect-passed {expect_passed}" in run
    for name in required:
        assert f"--require {name}" in run, f"{name} is no longer a required pass"


@pytest.mark.parametrize(
    "workflow,job,step_name,secrets",
    [
        (
            "container.yml",
            "container-image",
            "Fail fast when the live credentials are missing",
            ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
        ),
        (
            "worker-deploy-smoke.yml",
            "deploy-smoke",
            "Fail fast when the Cloudflare configuration is missing",
            ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "CI_SMOKE_KV_NAMESPACE_ID"),
        ),
    ],
)
def test_each_live_lane_fails_fast_on_a_missing_credential(
    workflow, job, step_name, secrets
):
    """Ran-assertion, layer (i): a renamed or never-added secret is loud."""
    step = _step(_steps(workflow, job), step_name)
    for secret in secrets:
        assert step["env"][secret] == "${{ secrets.%s }}" % secret
        assert secret in step["run"]
    assert "exit 1" in step["run"]


def test_required_test_names_exist_in_the_suites_they_gate():
    """A rename would make `--require` unsatisfiable — catch it here, not in a
    nightly run three weeks later."""
    container = (REPO_ROOT / "tests" / "integration" / "test_container_image.py").read_text()
    for name in (CLAUDE_ROUNDTRIP, CODEX_ROUNDTRIP):
        assert f"def {name}(" in container

    smoke = (REPO_ROOT / "tests" / "integration" / "test_worker_deploy_smoke.py").read_text()
    for name in (DEPLOY_HEALTH, DEPLOY_ROUNDTRIP):
        assert f"def {name}(" in smoke

    collector = (REPO_ROOT / "tests" / "integration" / "test_otel_collector.py").read_text()
    for name in (OTEL_METRIC, OTEL_LOG, OTEL_WRONG_TYPE, OTEL_CLI):
        assert f"def {name}(" in collector


def test_live_marker_is_still_defined():
    markers = (REPO_ROOT / "pyproject.toml").read_text()
    assert '"live: test hits a live external API' in markers, (
        "the `live` marker is gone; `-m \"docker and live\"` would select nothing"
    )


# ---------------------------------------------------------------------------
# Fork safety (Q5's non-negotiable compensating control)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "workflow,job",
    [("container.yml", "container-image"), ("worker-deploy-smoke.yml", "deploy-smoke")],
)
def test_live_lanes_are_inert_on_fork_pull_requests(workflow, job):
    gate = _step(_steps(workflow, job), "Decide whether the live lane runs")
    run = gate["run"]
    assert gate["id"] == "live"
    assert "ci:live" in gate["env"]["HAS_LIVE_LABEL"]
    assert gate["env"]["PR_HEAD_REPO"] == "${{ github.event.pull_request.head.repo.full_name }}"
    assert gate["env"]["THIS_REPO"] == "${{ github.repository }}"
    assert '"$PR_HEAD_REPO" != "$THIS_REPO"' in run, (
        "the fork check is gone — a labelled fork PR could spend credentials"
    )
    assert "schedule|workflow_dispatch" in run


def test_no_workflow_uses_pull_request_target():
    """`pull_request_target` runs a fork's PR with the base repo's secrets —
    the one trigger that would defeat every fork guard above.

    Checked against the parsed `on:` block, not a text search: the workflows
    that carry these guards also *explain* them, and a grep would flag the
    prose that says the trigger is never used.
    """
    offenders = [
        path.name
        for path in sorted(WORKFLOWS.glob("*.yml"))
        if "pull_request_target" in (workflow_on(load_workflow(path.name)) or {})
    ]
    assert offenders == [], f"pull_request_target found in: {offenders}"


# ---------------------------------------------------------------------------
# worker-deploy-smoke.yml — the deploy lane (Phase 2)
# ---------------------------------------------------------------------------


def test_worker_deploy_renders_an_isolated_config_before_deploying():
    steps = _steps("worker-deploy-smoke.yml", "deploy-smoke")
    render = _step(steps, "Render the ci-smoke Worker config")
    assert "scripts/render_worker_ci_config.py" in render["run"]
    assert "--kv-namespace-id" in render["run"]
    assert render["env"]["CI_SMOKE_KV_NAMESPACE_ID"] == (
        "${{ secrets.CI_SMOKE_KV_NAMESPACE_ID }}"
    ), "the CI KV id must come from CI configuration, never a literal"

    assert _index(steps, "Render the ci-smoke Worker config") < _index(
        steps, "Deploy the ci-smoke Worker"
    ), "the isolation guard must run BEFORE the deploy, not after"


def test_worker_deploy_never_targets_the_production_worker_name():
    workflow = load_workflow("worker-deploy-smoke.yml")
    name = workflow["env"]["CI_WORKER_NAME"]
    assert name != "bobi-events", (
        "the CI lane is pointed at production's Worker name — smoke traffic "
        "would write live event state"
    )
    assert "ci-smoke" in name


def test_worker_deploy_smokes_the_deployed_url_not_a_dev_server():
    step = _step(_steps("worker-deploy-smoke.yml", "deploy-smoke"), "Smoke the DEPLOYED Worker")
    assert step["env"]["BOBI_SMOKE_EVENT_SERVER_URL"] == "${{ steps.deploy.outputs.url }}"
    assert step["env"]["BOBI_SMOKE_EXPECTED_SHA"] == "${{ github.sha }}", (
        "without the expected sha the health check cannot tell this deploy "
        "from a stale one"
    )


GATE_STEP = "Wait for the deployment to serve this commit and this run's credentials"


def test_worker_deploy_waits_for_the_deployment_before_smoking():
    """The readiness gate must stay, and must stay a GATE.

    Its BEHAVIOUR is proven in tests/test_ci_guard_scripts.py, against a Worker
    that fakes the rollover; what belongs here is the wiring that behaviour
    depends on. The gate must run the script, be told which commit to expect,
    and sit between the last step that publishes a Worker version and the smoke.
    """
    steps = _steps("worker-deploy-smoke.yml", "deploy-smoke")
    wait = _step(steps, GATE_STEP)

    assert "scripts/await_worker_ready.py" in wait["run"], (
        "the readiness gate no longer runs the tested guard script"
    )
    assert '--expect-sha "$GITHUB_SHA"' in wait["run"], (
        "the readiness gate no longer pins the deployed sha — it would accept "
        "a stale Worker"
    )
    assert _index(steps, "Set the deployed Worker's secrets") < _index(steps, GATE_STEP), (
        "the wait must come after the LAST step that publishes a new version"
    )
    assert _index(steps, GATE_STEP) < _index(steps, "Smoke the DEPLOYED Worker")


def test_readiness_gate_proves_the_serving_version_carries_this_run_s_token():
    """The gate must check the CREDENTIAL, not just the commit.

    Run 30895709421 (scheduled, 2026-08-04) is why. `wrangler deploy` and
    `wrangler secret bulk` publish two versions that report the SAME
    `BOBI_RELEASE_SHA` — it is a `var` baked in at render time — so a sha-only
    gate cannot tell them apart. It cleared on 5 consecutive matches with 0
    resets while the edge still served the deploy version, which inherits the
    PREVIOUS run's secrets, and the smoke took a 401 on /mcp and a 500 on the
    round-trip.

    Without the token in this step's env the script exits 1 by design, so this
    pins the one input that makes the gate more than a sha check.
    """
    wait = _step(_steps("worker-deploy-smoke.yml", "deploy-smoke"), GATE_STEP)

    assert wait["env"]["BOBI_SMOKE_FLEET_OPERATOR_TOKEN"] == (
        "${{ steps.secrets.outputs.operator_token }}"
    ), (
        "the gate no longer receives this run's minted operator token — it "
        "would fall back to proving only that the commit is live, which is the "
        "exact hole run 30895709421 fell through"
    )
    # The SAME token the smoke uses, from the SAME step that put it on the
    # script. A gate that proved a different credential live would prove nothing.
    smoke = _step(_steps("worker-deploy-smoke.yml", "deploy-smoke"), "Smoke the DEPLOYED Worker")
    assert smoke["env"]["BOBI_SMOKE_FLEET_OPERATOR_TOKEN"] == (
        wait["env"]["BOBI_SMOKE_FLEET_OPERATOR_TOKEN"]
    )


def test_worker_deploy_mints_the_operator_token_on_the_deployed_script():
    """The MCP smoke must run against a route that is actually OPEN to it.

    `/mcp` fails closed when `FLEET_OPERATOR_TOKEN` is unset — `requireOperator`
    answers 503 before the handler runs. A lane that deployed without minting
    the token would never reach the MCP handler at all, so the tool call could
    not prove `nodejs_compat` no matter how green it looked.

    The token is passed to pytest from the same step that puts it on the script,
    so the two cannot drift apart.
    """
    steps = _steps("worker-deploy-smoke.yml", "deploy-smoke")
    put = _step(steps, "Set the deployed Worker's secrets")["run"]
    assert "FLEET_OPERATOR_TOKEN" in put, (
        "the lane no longer sets FLEET_OPERATOR_TOKEN — /mcp would 503 and the "
        "MCP smoke would prove nothing"
    )
    assert "::add-mask::" in put, "the minted operator token is not masked in the log"

    # ONE write, so one new Worker version. Every secret write publishes a
    # version and Cloudflare's rollover is not atomic, so a second write widens
    # the window in which the smoke can be answered by the PREVIOUS version —
    # observed as a 404 from a version predating /mcp on this lane's first live
    # run. `secret bulk` sets both in a single request.
    assert "secret bulk" in put, (
        "secrets are no longer set in one request — each `secret put` publishes "
        "another version and widens the non-atomic rollover window"
    )
    assert "secret put" not in put, (
        "a `wrangler secret put` crept back in alongside `secret bulk`; that is "
        "an extra version publish and an extra rollover"
    )

    smoke = _step(steps, "Smoke the DEPLOYED Worker")
    assert "BOBI_SMOKE_FLEET_OPERATOR_TOKEN" in smoke["env"], (
        "the smoke step no longer receives the operator token"
    )


def test_readiness_gate_requires_consecutive_matches_not_one():
    """The gate must clear on a RUN of matches, never a single observation.

    Run 30740272285 — the first SCHEDULED run of this lane — passed the gate on
    one match and was then smoked against the PREVIOUS run's version.
    Cloudflare's rollover is not atomic, so while propagation finishes,
    successive requests to the same URL can land on either version; one match
    predicts nothing about the next request.

    The streak logic itself is exercised in tests/test_ci_guard_scripts.py.
    What this pins is the THRESHOLD VALUE the workflow passes: `--needed 1`
    satisfies "the flag is present" while being exactly the bug this test
    exists to prevent.
    """
    wait = _step(_steps("worker-deploy-smoke.yml", "deploy-smoke"), GATE_STEP)

    declared = re.search(r"--needed\s+(\d+)", wait["run"])
    assert declared, "the gate declares no consecutive-match threshold"
    assert int(declared.group(1)) >= 2, (
        f"the gate clears on {declared.group(1)} observation(s) — a single match "
        "is the original bug (run 30740272285)"
    )


def test_the_deploy_lane_serializes_on_the_shared_worker():
    """One deployment, so one run at a time.

    Every trigger deploys the same `bobi-events-ci-smoke` name and mints fresh
    secrets onto it, so two overlapping runs stomp each other's credentials and
    each smokes against the other's — stale-credential failures arriving from a
    direction no readiness gate can distinguish. Not keyed by ref: the Worker
    is shared across every ref, so the serialization must be too.
    """
    workflow = load_workflow("worker-deploy-smoke.yml")
    concurrency = workflow.get("concurrency")
    assert concurrency, (
        "the deploy lane has no concurrency group — two runs can deploy to the "
        "same Worker and overwrite each other's minted secrets"
    )
    assert "${{" not in concurrency["group"], (
        "the concurrency group is templated per-ref, but the Worker it protects "
        "is shared across refs"
    )
    assert concurrency.get("cancel-in-progress") is False, (
        "cancelling mid-deploy leaves the shared Worker half-rolled-over"
    )
