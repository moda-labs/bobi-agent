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


def test_worker_deploy_waits_for_the_deployment_before_smoking():
    """The readiness gate must stay, and must stay a GATE.

    The first real run of this lane failed on a rollover race — `wrangler
    secret put` publishes another version and requests during the swap 500.
    A plain sleep, or a retry that accepts any 200, would paper over a stale
    or broken deploy; this one requires the commit's own release sha.
    """
    steps = _steps("worker-deploy-smoke.yml", "deploy-smoke")
    wait = _step(steps, "Wait for the deployment to serve this commit")
    # Backslashes are literal: `run: |` is a block scalar, so the shell's
    # own quoting survives into the parsed string.
    assert r'\"sha\":\"$GITHUB_SHA\"' in wait["run"], (
        "the readiness gate no longer pins the deployed sha — it would accept "
        "a stale Worker"
    )
    assert "exit 1" in wait["run"]
    assert _index(steps, "Set the deployed Worker's secrets") < _index(
        steps, "Wait for the deployment to serve this commit"
    ), "the wait must come after the LAST step that publishes a new version"
    assert _index(steps, "Wait for the deployment to serve this commit") < _index(
        steps, "Smoke the DEPLOYED Worker"
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
    assert "wrangler secret put FLEET_OPERATOR_TOKEN" in put, (
        "the lane no longer sets FLEET_OPERATOR_TOKEN — /mcp would 503 and the "
        "MCP smoke would prove nothing"
    )
    assert "::add-mask::" in put, "the minted operator token is not masked in the log"

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

    Asserted structurally rather than by substring: a streak is only a streak if
    a non-match RESETS it, so this pins the reset, the threshold comparison, and
    the fact that success is reached through the threshold — not merely that the
    word "streak" appears somewhere.
    """
    wait = _step(
        _steps("worker-deploy-smoke.yml", "deploy-smoke"),
        "Wait for the deployment to serve this commit",
    )
    run = wait["run"]
    assert 'streak=$(( streak + 1 ))' in run, "nothing counts consecutive matches"
    assert '[ "$streak" -ge "$needed" ]' in run, (
        "success is not gated on reaching the consecutive-match threshold"
    )

    # The THRESHOLD VALUE, not merely that a threshold is declared. `needed=1`
    # satisfies "needed= appears" while being exactly the bug this test exists
    # to prevent.
    declared = re.search(r"^\s*needed=(\d+)", run, re.MULTILINE)
    assert declared, "the gate declares no consecutive-match threshold"
    assert int(declared.group(1)) >= 2, (
        f"the gate clears on {declared.group(1)} observation(s) — a single match "
        "is the original bug (run 30740272285)"
    )

    # THE load-bearing line: the reset must live in the MISMATCH branch. Testing
    # for `streak=0` anywhere also matches the initializer at the top, so
    # deleting the reset entirely would still pass. Scope it to the default case.
    #
    # Matched by regex, not by splitting on a literal indent: `run` is a YAML
    # block scalar, so it comes back DEDENTED and any hard-coded leading
    # whitespace would never match — the assertion would then fail for the wrong
    # reason and "catch" mutants it is not actually testing for.
    default_case = re.search(r"^[ \t]*\*\)[ \t]*$(.*?)^[ \t]*;;", run,
                             re.MULTILINE | re.DOTALL)
    assert default_case, (
        "the gate has no default (non-matching) case — nothing can reset the streak"
    )
    assert "streak=0" in default_case.group(1), (
        "a non-matching response does not reset the streak — the count would be "
        "cumulative rather than consecutive, and a flapping rollover would still "
        "clear the gate"
    )
    # And the only `exit 0` must sit behind that threshold, so no earlier path
    # can short-circuit to ready.
    ready = run.split('[ "$streak" -ge "$needed" ]', 1)[0]
    assert "exit 0" not in ready, (
        "the gate can exit ready before the consecutive-match threshold"
    )
