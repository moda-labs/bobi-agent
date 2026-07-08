"""Release pipeline invariants across the split workflow files (repo-split
phase 1): release.yml keeps the public jobs (smoke, wheel, PyPI, Homebrew) and
calls release-fleet.yml (event-server deploy + canary gate) and
release-image.yml (GHCR base image) - the two private-bound files that move
whole at cut time."""

from tests.workflow_utils import load_workflow, workflow_on


def _release_jobs() -> dict:
    return load_workflow("release.yml")["jobs"]


def _fleet() -> dict:
    return load_workflow("release-fleet.yml")


def _image_jobs() -> dict:
    return load_workflow("release-image.yml")["jobs"]


def _build_step(job: dict) -> dict:
    return next(
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("docker/build-push-action")
    )


def test_fleet_and_image_are_the_called_private_bound_workflows():
    # The cut-time contract: the Fly/Worker/image halves are whole files, and
    # release.yml only calls them. The fleet call must inherit secrets
    # (CLOUDFLARE_*, FLY_API_TOKEN live in the caller's repo).
    jobs = _release_jobs()
    assert jobs["fleet"]["uses"] == "./.github/workflows/release-fleet.yml"
    assert jobs["fleet"]["secrets"] == "inherit"
    assert jobs["image"]["uses"] == "./.github/workflows/release-image.yml"
    for name in ("release-fleet.yml", "release-image.yml"):
        assert "workflow_call" in workflow_on(load_workflow(name)), (
            f"{name} must be callable-only - it moves private at cut time"
        )


def test_pypi_publish_is_gated_on_the_fleet_gate_not_the_image():
    # PyPI is irreversible: publish waits on the canary gate. The GHCR image
    # is deliberately NOT in its needs - an image failure never blocks PyPI.
    jobs = _release_jobs()
    assert jobs["publish"]["needs"] == "fleet"
    assert "image" not in str(jobs["publish"].get("needs"))


def test_image_publish_requires_a_canary_that_actually_ran():
    jobs = _release_jobs()
    job = jobs["image"]
    assert "fleet" in job["needs"]
    # Plain job success is not enough: build-canary exits 0 without smoking
    # anything when FLY_API_TOKEN is unset.
    assert "needs.fleet.outputs.smoked == 'true'" in job["if"]
    fleet = _fleet()
    smoked = workflow_on(fleet)["workflow_call"]["outputs"]["smoked"]
    assert smoked["value"] == "${{ jobs.build-canary.outputs.smoked }}"
    canary = fleet["jobs"]["build-canary"]
    assert canary["outputs"]["smoked"] == "${{ steps.canary.outputs.smoked }}"


def test_image_publish_refuses_non_release_refs():
    # A bare dispatch from a branch would bake an unreleased wheel whose
    # version collides with an already-published tag.
    job = _release_jobs()["image"]
    assert "github.event_name == 'release'" in job["if"]
    assert "startsWith" in job["if"]
    assert any(
        step.get("name") == "Assert the ref matches the wheel version"
        for step in _image_jobs()["publish-image"]["steps"]
    )


def test_image_builds_natively_per_arch_never_under_qemu():
    # The runtime stage executes the fetched binaries (claude --version et
    # al); the Bun-compiled claude binary segfaults under qemu-user.
    job = _image_jobs()["publish-image"]
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
    job = _image_jobs()["publish-image"]
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
    jobs = _release_jobs()
    # Everything flows from build-wheel outputs: version from the wheel
    # filename, claude pin resolved once so canary + all arches bake the same
    # CLI. The called workflows receive them ONLY as inputs.
    assert jobs["build-wheel"]["outputs"]["source-sha"] == "${{ steps.meta.outputs.source-sha }}"
    meta = next(
        step
        for step in jobs["build-wheel"]["steps"]
        if step.get("name") == "Resolve release version + claude CLI pin"
    )
    assert "source-sha=$(git rev-parse HEAD)" in meta["run"]

    for caller in ("fleet", "image"):
        assert "build-wheel" in jobs[caller]["needs"]
        assert jobs[caller]["with"] == {
            "version": "${{ needs.build-wheel.outputs.version }}",
            "claude-version": "${{ needs.build-wheel.outputs.claude-version }}",
            "source-sha": "${{ needs.build-wheel.outputs.source-sha }}",
        }

    fleet_jobs = _fleet()["jobs"]
    called_jobs = [
        fleet_jobs["deploy-event-server"],
        fleet_jobs["build-canary"],
        _image_jobs()["publish-image"],
    ]
    for job in called_jobs:
        checkout = next(
            step
            for step in job["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout")
        )
        assert checkout["with"]["ref"] == "${{ inputs.source-sha }}"
    homebrew_checkout = next(
        step
        for step in jobs["update-homebrew"]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout")
    )
    assert homebrew_checkout["with"]["ref"] == "${{ needs.build-wheel.outputs.source-sha }}"

    build = _build_step(_image_jobs()["publish-image"])
    assert "CLAUDE_VERSION=${{ inputs.claude-version }}" in build["with"]["build-args"]
    assert "inputs.claude-version" in str(fleet_jobs["build-canary"]["env"])

    dispatch = next(
        step
        for step in jobs["update-homebrew"]["steps"]
        if str(step.get("uses", "")).startswith("peter-evans/repository-dispatch")
    )
    assert "needs.build-wheel.outputs.version" in dispatch["with"]["client-payload"]


def test_event_server_deploy_stamps_and_verifies_fleet_url_before_canary():
    fleet_jobs = _fleet()["jobs"]
    deploy = fleet_jobs["deploy-event-server"]

    stamp = next(
        step
        for step in deploy["steps"]
        if step.get("name") == "Stamp event-server release metadata"
    )
    assert "BOBI_RELEASE_VERSION" in stamp["run"]
    assert "BOBI_RELEASE_SHA" in stamp["run"]
    assert "inputs.version" in stamp["run"]
    assert "inputs.source-sha" in stamp["run"]

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
    assert "deploy-event-server" in fleet_jobs["build-canary"]["needs"]


def test_latest_moves_only_for_the_repos_latest_release():
    image_jobs = _image_jobs()
    manifest = next(
        step
        for step in image_jobs["publish-manifest"]["steps"]
        if "imagetools create" in step.get("run", "")
    )
    run = manifest["run"]
    # The decision input is releases/latest (excludes prereleases and drafts),
    # and :latest is only added behind that comparison.
    assert "releases/latest" in run
    assert '"${IMAGE}:latest"' in run
    # No unconditional :latest anywhere in the per-arch build tags.
    build = _build_step(image_jobs["publish-image"])
    assert ":latest" not in build["with"]["tags"]


def test_publish_jobs_are_time_bounded():
    image_jobs = _image_jobs()
    assert image_jobs["publish-image"]["timeout-minutes"] <= 45
    assert image_jobs["publish-manifest"]["timeout-minutes"] <= 15
