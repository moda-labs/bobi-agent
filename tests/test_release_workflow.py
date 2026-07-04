from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent


def _release_workflow() -> dict:
    return yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text()
    )


def test_publish_image_is_gated_on_the_canary():
    job = _release_workflow()["jobs"]["publish-image"]
    assert job["needs"] == "build-canary"


def test_publish_image_can_write_packages():
    job = _release_workflow()["jobs"]["publish-image"]
    assert job["permissions"]["packages"] == "write"


def test_publish_image_builds_from_the_proven_wheel():
    steps = _release_workflow()["jobs"]["publish-image"]["steps"]

    download = next(
        step for step in steps if step.get("uses", "").startswith("actions/download-artifact")
    )
    assert download["with"]["name"] == "dist"
    assert download["with"]["path"] == "dist/"

    build = next(
        step for step in steps if step.get("uses", "").startswith("docker/build-push-action")
    )
    assert "BOBI_BUILD=wheel" in build["with"]["build-args"]


def test_publish_image_pushes_versioned_and_latest_tags():
    steps = _release_workflow()["jobs"]["publish-image"]["steps"]
    build = next(
        step for step in steps if step.get("uses", "").startswith("docker/build-push-action")
    )
    assert build["with"]["push"] is True
    assert build["with"]["platforms"] == "linux/amd64,linux/arm64"

    tags = build["with"]["tags"]
    assert "ghcr.io/moda-labs/bobi:${{ steps.version.outputs.version }}" in tags
    # :latest must move only on real release events.
    assert "github.event_name == 'release' && 'ghcr.io/moda-labs/bobi:latest'" in tags
