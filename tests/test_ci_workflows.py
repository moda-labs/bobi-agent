from tests.workflow_utils import load_workflow


def _ci_workflow() -> dict:
    return load_workflow("ci.yml")


def test_integration_fast_model_download_is_bounded_without_hf_xet():
    workflow = _ci_workflow()
    job = workflow["jobs"]["integration-fast"]
    steps = job["steps"]
    cache = next(step for step in steps if step.get("name") == "Cache embedding model")
    predownload = next(step for step in steps if step.get("name") == "Pre-download embedding model")
    pytest_step = next(step for step in steps if step.get("name") == "Run all non-Claude integration tests")

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


