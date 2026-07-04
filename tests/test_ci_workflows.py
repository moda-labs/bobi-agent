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
