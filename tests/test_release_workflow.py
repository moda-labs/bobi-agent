"""Public release pipeline invariants (release.yml).

Post repo-split, release.yml owns only the public release: login smoke,
wheel build, PyPI publish, Homebrew bump. The fleet canary and GHCR base
image (the old `fleet`/`image` caller jobs and their release-fleet.yml /
release-image.yml files) live in the private deploy repo, whose own tests
guard them. What must hold here:

1. `publish` gates on the public proofs (login smoke + wheel) and nothing
   else - a dangling `needs` on a removed job would skip publish silently.
2. `publish` stays a native job in release.yml: PyPI trusted publishing
   rejects reusable workflows, so the OIDC step must live in the top-level
   file the publisher config names.
3. No job reaches for the private-bound workflow files.
"""

from tests.workflow_utils import load_workflow

PRIVATE_WORKFLOWS = ("release-fleet.yml", "release-image.yml")


def _release_jobs() -> dict:
    return load_workflow("release.yml")["jobs"]


def test_publish_gates_on_public_proofs():
    jobs = _release_jobs()
    publish = jobs["publish"]
    needs = publish["needs"]
    needs = [needs] if isinstance(needs, str) else list(needs)
    assert set(needs) == {"subscription-login-smoke", "build-wheel"}, (
        "publish must gate on exactly the public proofs; a stale `needs` "
        f"entry skips PyPI publish silently: {needs}"
    )
    for need in needs:
        assert need in jobs, f"publish needs unknown job {need!r}"


def test_publish_is_a_native_job_for_trusted_publishing():
    publish = _release_jobs()["publish"]
    assert "uses" not in publish, (
        "publish must stay a native job in release.yml - PyPI trusted "
        "publishing rejects reusable workflows (invalid-publisher)"
    )
    assert publish.get("environment") == "pypi"
    assert any(
        "pypa/gh-action-pypi-publish" in step.get("uses", "")
        for step in publish["steps"]
    )


def test_no_job_calls_private_bound_workflows():
    jobs = _release_jobs()
    offenders = [
        (name, job["uses"])
        for name, job in jobs.items()
        if any(private in job.get("uses", "") for private in PRIVATE_WORKFLOWS)
    ]
    assert not offenders, (
        "release.yml reaches for workflow files that moved to the private "
        f"deploy repo: {offenders}"
    )
