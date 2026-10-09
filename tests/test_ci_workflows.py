from pathlib import Path
import os
import subprocess

from tests.workflow_utils import load_workflow

PACKAGED_EVENT_SERVER_TEST = (
    Path(__file__).parent / "integration" / "test_packaged_event_server.py"
)


def _ci_workflow() -> dict:
    return load_workflow("ci.yml")


def test_integration_fast_model_download_is_bounded_without_hf_xet():
    workflow = _ci_workflow()
    job = workflow["jobs"]["integration-fast"]
    steps = job["steps"]
    cache = next(step for step in steps if step.get("name") == "Cache embedding model")
    predownload = next(step for step in steps if step.get("name") == "Pre-download embedding model")
    pytest_step = next(
        step
        for step in steps
        if step.get("name") == "Run all other non-Claude integration tests"
    )

    env = job["env"]
    assert env["HF_HUB_DISABLE_XET"] == "1"
    assert int(env["HF_HUB_DOWNLOAD_TIMEOUT"]) <= 120
    assert env["FASTEMBED_CACHE_PATH"] == "${{ github.workspace }}/.fastembed-cache"

    assert cache["with"]["path"] == env["FASTEMBED_CACHE_PATH"]
    assert predownload["timeout-minutes"] <= 10
    run = predownload["run"]
    assert "timeout 120 python" in run
    assert "_FASTEMBED_MODEL" in run
    assert "_resolve_cache_dir()" in run
    assert "TextEmbedding" in run
    assert "embedding model download failed after 3 attempts" in run
    assert "embedding model cache is empty after warmup" in run
    assert "FASTEMBED_CACHE_PATH" not in pytest_step.get("env", {})


def test_ci_builds_and_runs_the_packaged_event_server_contract():
    workflow = _ci_workflow()
    event_steps = workflow["jobs"]["event-server"]["steps"]
    install = next(
        step for step in event_steps if step.get("name") == "Install dependencies"
    )
    bundle = next(
        step
        for step in event_steps
        if step.get("name") == "Build embedded local-server artifact"
    )
    assert install["run"] == "npm ci --no-audit --no-fund"
    assert bundle["working-directory"] == "event-server"
    assert bundle["run"] == "npm run build:local"

    integration_steps = workflow["jobs"]["integration-fast"]["steps"]
    packaged = next(
        step
        for step in integration_steps
        if step.get("name") == "Run packaged event-server regression"
    )
    remaining = next(
        step
        for step in integration_steps
        if step.get("name") == "Run all other non-Claude integration tests"
    )
    assert "tests/integration/test_packaged_event_server.py" in packaged["run"]
    assert '--timeout=180' in packaged["run"]
    assert "pytestmark = pytest.mark.timeout" not in (
        PACKAGED_EVENT_SERVER_TEST.read_text()
    )
    assert "--ignore=tests/integration/test_packaged_event_server.py" in remaining["run"]
    assert '--timeout=180' in remaining["run"]


def test_unit_tests_gate_is_a_stable_required_check_that_cannot_false_green():
    """The required check standing in for the unit-test matrix.

    A matrixed job whose job-level `if:` is false never expands its matrix -
    GitHub publishes one check run named `Unit tests`, not `Unit tests (3.12)`
    / `(3.13)`. Requiring the expanded names left every docs/plans-only PR
    waiting on statuses that could not arrive. This gate is what branch
    protection requires instead, so the properties that make it safe are
    load-bearing and asserted here rather than left to a comment.
    """
    workflow = _ci_workflow()
    job = workflow["jobs"]["unit-tests-gate"]

    # No matrix: the whole point is a check name that is stable whether the
    # suite runs or skips. A matrix here would reintroduce the original bug.
    assert "strategy" not in job
    assert job["name"] == "Unit tests (gate)"
    assert job["needs"] == "unit-tests"

    # `always()`, NOT the implicit form. GitHub skips dependents when a
    # dependency FAILS, and branch protection reads a skipped required check
    # as passing - so letting this job skip would report green on a genuinely
    # failing unit suite, which is the exact bug class this gate exists to
    # close. `!cancelled()` is also insufficient: it would skip (and so pass)
    # a cancelled run.
    assert job["if"] == "always()"

    # Only success and skipped may pass; every other result - failure,
    # cancelled - must exit non-zero.
    run = "".join(step.get("run", "") for step in job["steps"])
    assert "needs.unit-tests.result" in "".join(
        str(step.get("env", "")) for step in job["steps"]
    )
    assert "success)" in run
    assert "skipped)" in run
    assert "exit 1" in run


def test_ci_proves_the_shipped_bundle_survives_a_newer_node_major():
    event_steps = _ci_workflow()["jobs"]["event-server"]["steps"]

    def index_of(predicate):
        return next(
            index for index, step in enumerate(event_steps) if predicate(step)
        )

    bundle_index = index_of(
        lambda step: step.get("name") == "Build embedded local-server artifact"
    )
    record_index = index_of(
        lambda step: step.get("name") == "Record the release-major bundle digest"
    )
    newer_node_index = index_of(
        lambda step: step.get("uses", "").startswith("actions/setup-node")
        and str(step.get("with", {}).get("node-version")) != "20"
    )
    compare_index = index_of(
        lambda step: step.get("name")
        == "Wheel builds on a newer Node major and ships an identical bundle"
    )
    record = event_steps[record_index]
    compare = event_steps[compare_index]

    assert bundle_index < record_index < newer_node_index < compare_index
    assert compare_index == len(event_steps) - 1
    assert "set -euo pipefail" in record["run"]
    assert "test -s dist/local.js" in record["run"]
    assert "set -euo pipefail" in compare["run"]
    assert "test -n \"$RELEASE_BUNDLE_SHA256\"" in compare["run"]
    assert "python -m build --wheel" in compare["run"]
    assert "bobi/event-server/dist/local.js" in compare["run"]
    assert "exit 1" in compare["run"]


def test_promote_dev_advances_only_on_fully_green_main_push():
    """#740 Track A: `dev` is the pre-release channel the private deploy repo
    consumes, so it must only ever point at a main commit the WHOLE CI matrix
    proved — and must move by fast-forward, never force."""
    workflow = _ci_workflow()
    jobs = workflow["jobs"]
    job = jobs["promote-dev"]

    # Gated on every other job in this workflow: adding a CI job without
    # gating the channel on it would silently weaken what "green" means.
    assert set(job["needs"]) == set(jobs) - {"promote-dev"}

    # Push-to-main only — never PRs, nightly cron, or manual dispatch.
    condition = job["if"]
    assert "github.event_name == 'push'" in condition
    assert "github.ref == 'refs/heads/main'" in condition

    # Job-scoped write permission (the rest of CI stays read-only) and
    # serialized promotions.
    assert job["permissions"] == {"contents": "write"}
    assert job["concurrency"]["group"] == "promote-dev"
    assert job["concurrency"]["cancel-in-progress"] is False

    push = next(s for s in job["steps"] if "refs/heads/dev" in s.get("run", ""))
    # Fast-forward only: no force flag, and out-of-order completions no-op
    # via the ancestor check instead of rewinding the channel.
    assert "--force" not in push["run"]
    assert "merge-base --is-ancestor" in push["run"]
    # The ancestor check needs history; a shallow checkout would break it.
    checkout = next(s for s in job["steps"] if "checkout" in s.get("uses", ""))
    assert checkout["with"]["fetch-depth"] == 0


def test_diy_install_lane_uses_one_wheel_across_supported_hosts():
    workflow = _ci_workflow()
    jobs = workflow["jobs"]
    unit_steps = jobs["unit-tests"]["steps"]
    assert not any(step.get("uses", "").startswith("actions/setup-node") for step in unit_steps)

    build = jobs["diy-install-build"]
    build_node = next(step for step in build["steps"] if step.get("uses", "").startswith("actions/setup-node"))
    assert str(build_node["with"]["node-version"]) == "20"
    producer = next(step for step in build["steps"] if step.get("name") == "Build wheel and manifest")["run"]
    assert '"sha256": digest' in producer
    assert "tests/diy_install/test_diy_install.py" in producer

    consumer = jobs["diy-install"]
    assert not any(step.get("uses", "").startswith("actions/checkout") for step in consumer["steps"])
    assert consumer["strategy"]["fail-fast"] is False
    assert consumer["strategy"]["matrix"]["include"] == [
        {"os": os_name, "python-version": version}
        for os_name in ("ubuntu-latest", "macos-latest")
        for version in ("3.11", "3.12", "3.13")
    ]
    node = next(step for step in consumer["steps"] if step.get("uses", "").startswith("actions/setup-node"))
    assert str(node["with"]["node-version"]) == "18"
    smoke = next(step for step in consumer["steps"] if step.get("name") == "Install and smoke test the wheel outside the checkout")["run"]
    assert "type -P node" in smoke and "v18.*" in smoke
    assert "command -v node" not in smoke
    assert "BOBI_TEST_MANIFEST" in smoke
    assert "--expect-passed 9" in smoke
    assert "subject-venv" not in smoke


def test_diy_install_gate_fails_closed():
    gate = _ci_workflow()["jobs"]["diy-install-gate"]
    script = next(step for step in gate["steps"] if step.get("name") == "Require every DIY install cell")["run"]

    def run(**values):
        env = os.environ | {key: value for key, value in values.items()}
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True).returncode

    assert run(CODE="true", CHANGE_RESULT="success", BUILD_RESULT="success", CONSUMER_RESULT="success") == 0
    assert run(CODE="false", CHANGE_RESULT="success", BUILD_RESULT="skipped", CONSUMER_RESULT="skipped") == 0
    assert run(CODE="true", CHANGE_RESULT="success", BUILD_RESULT="failure", CONSUMER_RESULT="skipped") != 0
    assert run(CODE="true", CHANGE_RESULT="success", BUILD_RESULT="success", CONSUMER_RESULT="cancelled") != 0
    assert run(CODE="", CHANGE_RESULT="success", BUILD_RESULT="skipped", CONSUMER_RESULT="skipped") != 0
