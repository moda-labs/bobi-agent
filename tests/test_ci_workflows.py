from pathlib import Path

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


def test_ci_proves_the_shipped_bundle_survives_a_newer_node_major():
    """#857 loosened the build gate to any Node 20+ host.

    That is sound only because the host Node major cannot reach the bundle
    bytes, and nothing revalidates it downstream, so this guard is the enforcing
    control - it must not be quietly dropped or reduced to a bare esbuild run.
    """
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

    # The digest is captured while the job is still on the release major, and
    # the newer major is only introduced afterwards.
    assert bundle_index < record_index < newer_node_index < compare_index
    assert str(event_steps[newer_node_index]["with"]["node-version"]) != "20"

    # A missing or unhashable bundle must fail the step, not compare "" to "".
    assert "set -euo pipefail" in record["run"]
    assert "set -euo pipefail" in compare["run"]
    assert "test -s dist/local.js" in record["run"]

    # The guard has to exercise the real packaging path - a bare `npm run
    # build:local` would skip the build gate this change touched.
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
