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

from tests.workflow_utils import load_workflow, workflow_on

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


def test_release_build_provisions_node_and_smokes_the_exact_wheel():
    steps = _release_jobs()["build-wheel"]["steps"]
    node_index = next(
        index
        for index, step in enumerate(steps)
        if "actions/setup-node" in step.get("uses", "")
    )
    build_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Build wheel + sdist"
    )
    test_dependencies_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Install event-server test dependencies"
    )
    assert node_index < build_index
    assert node_index < test_dependencies_index < build_index
    assert steps[node_index]["with"]["node-version"] == "20"
    assert steps[test_dependencies_index]["working-directory"] == "event-server"
    assert (
        steps[test_dependencies_index]["run"]
        == "npm ci --no-audit --no-fund"
    )

    build = steps[build_index]["run"]
    assert 'pip install -e ".[dev]"' in build
    assert "python -m build" in build

    smoke = next(
        step
        for step in steps
        if step.get("name") == "Smoke the exact immutable wheel"
    )["run"]
    assert 'wheel="$(realpath dist/*.whl)"' in smoke
    assert 'BOBI_TEST_WHEEL="$wheel"' in smoke
    assert (
        "tests/integration/test_packaged_event_server.py::"
        "test_installed_wheel_starts_without_mutating_frozen_event_server"
    ) in smoke
    assert "/tmp/whl/bin/bobi --version" in smoke


def _meta_step_run() -> str:
    return next(
        step
        for step in _release_jobs()["build-wheel"]["steps"]
        if step.get("id") == "meta"
    )["run"]


def test_meta_publishes_the_pins_it_resolves():
    """The resolved pins must be READABLE, not just passed between jobs.

    `release-image.yml` takes `claude-version` as a required dispatch input,
    and the repo split moved its caller out of release.yml - so the output is
    consumed by nobody and the value existed only inside a shell variable.
    Dispatching the image then meant re-deriving the floating `stable`
    channel by hand, which defeats the entire point of pinning it once.
    """
    run = _meta_step_run()
    assert "$GITHUB_STEP_SUMMARY" in run, (
        "the meta step must publish the resolved pins to the run summary; "
        "without it the claude-version release-image.yml requires is "
        "unreadable and has to be guessed"
    )
    assert "::notice" in run, "the pins should also reach the run log"

    # The published value must be the SAME variable written to GITHUB_OUTPUT.
    # Publishing a separately re-derived value would be worse than nothing:
    # it would look authoritative while being able to disagree with what the
    # release actually baked.
    for var in ("$claude_version", "$version", "$source_sha"):
        assert run.count(var) >= 2, (
            f"{var} should be written to GITHUB_OUTPUT and published; "
            "publish the resolved variable, never a re-derivation"
        )


def test_summary_names_every_required_release_image_input():
    """If release-image.yml grows a required input, the summary must grow too.

    The summary prints a ready-to-paste dispatch command. An input added
    there but not here yields a command that fails at dispatch time, during
    a release, which is the worst moment to discover it.
    """
    dispatch = workflow_on(load_workflow("release-image.yml"))["workflow_dispatch"]
    required = {
        name
        for name, spec in dispatch["inputs"].items()
        if spec.get("required")
    }
    assert required, "expected release-image.yml to have required inputs"

    run = _meta_step_run()
    summary = run[run.index("$GITHUB_STEP_SUMMARY") - 800:]
    for name in required:
        assert f"-f {name}=" in run, (
            f"release-image.yml requires input {name!r}, but release.yml's "
            f"run summary does not print it: {summary[:200]}"
        )
