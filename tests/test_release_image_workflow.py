"""Final consumer tags may only be written by the canary callback (#930)."""

from tests.workflow_utils import load_workflow, workflow_on


def test_final_tags_require_the_verified_canary_callback():
    workflow = load_workflow("release-image.yml")
    assert workflow_on(workflow)["repository_dispatch"]["types"] == [
        "bobi-image-proven-v1"
    ]
    promote = workflow["jobs"]["promote"]
    assert "repository_dispatch" in promote["if"]
    assert promote["concurrency"]["cancel-in-progress"] is False
    commands = [s.get("run", "") for s in promote["steps"]]
    assert any("release_image.py promote" in command for command in commands)
    assert not any("build-push-action" in s.get("uses", "") for s in promote["steps"])


def test_builds_write_only_run_unique_candidate_tags():
    workflow = load_workflow("release-image.yml")
    build = workflow["jobs"]["build-candidate"]
    step = next(s for s in build["steps"] if "build-push-action" in s.get("uses", ""))
    assert "candidate-" in step["with"]["tags"]
    assert "github.run_id" in step["with"]["tags"]
    assert "github.run_attempt" in step["with"]["tags"]
    assert "VERSION" not in step["with"]["tags"]
    assert "dry-run" in step["with"]["push"]
    assert step["with"]["provenance"] is False
    assert "BOBI_BUILD=pypi" in step["with"]["build-args"]


def test_dispatch_and_call_have_the_same_candidate_inputs():
    events = workflow_on(load_workflow("release-image.yml"))
    assert events["workflow_call"]["inputs"] == events["workflow_dispatch"]["inputs"]
    assert events["workflow_dispatch"]["inputs"]["dry-run"]["default"] is False


def test_cross_repo_pat_is_scoped_to_dispatch_and_proof_steps():
    jobs = load_workflow("release-image.yml")["jobs"]
    request = jobs["request-canary"]["steps"][0]
    assert request["env"]["GH_TOKEN"] == "${{ secrets.CROSS_REPO_PAT }}"
    promote = jobs["promote"]["steps"][-1]
    assert promote["env"]["CROSS_REPO_PAT"] == "${{ secrets.CROSS_REPO_PAT }}"
    assert promote["env"]["GHCR_TOKEN"] == "${{ github.token }}"
    for job in jobs.values():
        assert "CROSS_REPO_PAT" not in job.get("env", {})
        assert all("create-github-app-token" not in step.get("uses", "")
                   for step in job.get("steps", []))
