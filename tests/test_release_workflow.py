from tests.workflow_utils import load_workflow


def _jobs() -> dict:
    return load_workflow("release.yml")["jobs"]


def _build_step(job: dict) -> dict:
    return next(
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("docker/build-push-action")
    )


def test_image_publish_requires_a_canary_that_actually_ran():
    jobs = _jobs()
    job = jobs["publish-image"]
    assert "build-canary" in job["needs"]
    # Plain job success is not enough: build-canary exits 0 without smoking
    # anything when FLY_API_TOKEN is unset.
    assert "needs.build-canary.outputs.smoked == 'true'" in job["if"]
    assert jobs["build-canary"]["outputs"]["smoked"] == "${{ steps.canary.outputs.smoked }}"


def test_image_publish_refuses_non_release_refs():
    # A bare dispatch from a branch would bake an unreleased wheel whose
    # version collides with an already-published tag.
    job = _jobs()["publish-image"]
    assert "github.event_name == 'release'" in job["if"]
    assert "startsWith" in job["if"]
    assert any(
        step.get("name") == "Assert the ref matches the wheel version"
        for step in job["steps"]
    )


def test_image_builds_natively_per_arch_never_under_qemu():
    # The runtime stage executes the fetched binaries (claude --version et
    # al); the Bun-compiled claude binary segfaults under qemu-user.
    job = _jobs()["publish-image"]
    runners = {
        entry["platform"]: entry["runner"]
        for entry in job["strategy"]["matrix"]["include"]
    }
    assert runners == {
        "linux/amd64": "ubuntu-latest",
        "linux/arm64": "ubuntu-24.04-arm",
    }
    assert not any("qemu" in str(step.get("uses", "")) for step in job["steps"])


def test_image_builds_from_the_proven_wheel_in_the_checkout_context():
    job = _jobs()["publish-image"]
    download = next(
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/download-artifact")
    )
    assert download["with"] == {"name": "dist", "path": "dist/"}

    build = _build_step(job)
    # The action's default Git context would not contain the downloaded dist/.
    assert build["with"]["context"] == "."
    assert "BOBI_BUILD=wheel" in build["with"]["build-args"]
    assert build["with"]["push"] is True


def test_version_and_claude_pin_have_a_single_source_of_truth():
    jobs = _jobs()
    # Both come from build-wheel outputs: version from the wheel filename,
    # claude pin resolved once so canary + all arches bake the same CLI.
    assert "build-wheel" in jobs["publish-image"]["needs"]
    assert jobs["build-wheel"]["outputs"]["source-sha"] == "${{ steps.meta.outputs.source-sha }}"
    meta = next(
        step
        for step in jobs["build-wheel"]["steps"]
        if step.get("name") == "Resolve release version + claude CLI pin"
    )
    assert "source-sha=$(git rev-parse HEAD)" in meta["run"]

    for job_name in ("build-canary", "publish-image", "deploy-event-server", "update-homebrew"):
        checkout = next(
            step
            for step in jobs[job_name]["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout")
        )
        assert checkout["with"]["ref"] == "${{ needs.build-wheel.outputs.source-sha }}"

    build = _build_step(jobs["publish-image"])
    assert (
        "CLAUDE_VERSION=${{ needs.build-wheel.outputs.claude-version }}"
        in build["with"]["build-args"]
    )
    assert "needs.build-wheel.outputs.claude-version" in str(
        jobs["build-canary"]["env"]
    )

    dispatch = next(
        step
        for step in jobs["update-homebrew"]["steps"]
        if str(step.get("uses", "")).startswith("peter-evans/repository-dispatch")
    )
    assert "needs.build-wheel.outputs.version" in dispatch["with"]["client-payload"]


def test_event_server_deploy_stamps_and_verifies_fleet_url_before_canary():
    jobs = _jobs()
    deploy = jobs["deploy-event-server"]
    assert "build-wheel" in deploy["needs"]

    stamp = next(
        step
        for step in deploy["steps"]
        if step.get("name") == "Stamp event-server release metadata"
    )
    assert "BOBI_RELEASE_VERSION" in stamp["run"]
    assert "BOBI_RELEASE_SHA" in stamp["run"]
    assert "needs.build-wheel.outputs.version" in stamp["run"]
    assert "needs.build-wheel.outputs.source-sha" in stamp["run"]

    guard = next(
        step
        for step in deploy["steps"]
        if step.get("name") == "Verify fleet event server is the deployed Worker"
    )
    assert guard["env"]["FLEET_EVENT_SERVER_URL"] == "${{ vars.FLEET_EVENT_SERVER_URL }}"
    assert "FLEET_EVENT_SERVER_URL" in guard["run"]
    assert "deployments/defaults.yaml" in guard["run"]
    assert 'event_server="${event_server%/}"' in guard["run"]
    assert "/health" in guard["run"]
    assert "expected_sha" in guard["run"]
    assert "actual_sha" in guard["run"]
    assert "exit 1" in guard["run"]

    stamp_index = deploy["steps"].index(stamp)
    deploy_index = next(
        i for i, step in enumerate(deploy["steps"])
        if step.get("name") == "Deploy to Cloudflare"
    )
    guard_index = deploy["steps"].index(guard)
    assert stamp_index < deploy_index < guard_index
    assert "deploy-event-server" in jobs["build-canary"]["needs"]


def test_latest_moves_only_for_the_repos_latest_release():
    jobs = _jobs()
    manifest = next(
        step
        for step in jobs["publish-manifest"]["steps"]
        if "imagetools create" in step.get("run", "")
    )
    run = manifest["run"]
    # The decision input is releases/latest (excludes prereleases and drafts),
    # and :latest is only added behind that comparison.
    assert "releases/latest" in run
    assert '"${IMAGE}:latest"' in run
    # No unconditional :latest anywhere in the per-arch build tags.
    build = _build_step(jobs["publish-image"])
    assert ":latest" not in build["with"]["tags"]


def test_publish_jobs_are_time_bounded():
    jobs = _jobs()
    assert jobs["publish-image"]["timeout-minutes"] <= 45
    assert jobs["publish-manifest"]["timeout-minutes"] <= 15
