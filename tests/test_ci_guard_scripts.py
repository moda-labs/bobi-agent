"""The two CI guard scripts, proven to fail on the cases they exist for.

`assert_junit_ran.py` and `render_worker_ci_config.py` are the only things
standing between a live lane and a silent no-op / a deploy pointed at the
wrong infrastructure. A guard nobody tested is exactly the class of thing
#909 was filed about, so each rejection path below is exercised rather than
asserted in prose.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(script: str):
    path = REPO_ROOT / "scripts" / script
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


assert_junit_ran = _load("assert_junit_ran.py")
render_worker_ci_config = _load("render_worker_ci_config.py")


# ---------------------------------------------------------------------------
# assert_junit_ran.py — the ran-assertion
# ---------------------------------------------------------------------------


def _junit(tmp_path: Path, cases: list[tuple[str, str | None]]) -> Path:
    """Write a junit report; each case is (name, outcome-tag-or-None)."""
    skipped = sum(1 for _, o in cases if o == "skipped")
    failures = sum(1 for _, o in cases if o == "failure")
    errors = sum(1 for _, o in cases if o == "error")
    body = "".join(
        f'<testcase name="{name}">'
        + (f"<{outcome} />" if outcome else "")
        + "</testcase>"
        for name, outcome in cases
    )
    xml = (
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite '
        f'name="pytest" tests="{len(cases)}" skipped="{skipped}" '
        f'failures="{failures}" errors="{errors}">{body}</testsuite></testsuites>'
    )
    path = tmp_path / "report.xml"
    path.write_text(xml)
    return path


def test_ran_assertion_passes_when_both_gated_tests_actually_passed(tmp_path):
    report = _junit(tmp_path, [("test_a", None), ("test_b", None)])
    assert_junit_ran.check(report, 2, ["test_a", "test_b"])


def test_ran_assertion_rejects_a_skip(tmp_path):
    """THE case this whole guard exists for: a missing credential skips the
    test and pytest exits 0."""
    report = _junit(tmp_path, [("test_a", None), ("test_b", "skipped")])
    with pytest.raises(assert_junit_ran.ReportError, match="SKIPPED"):
        assert_junit_ran.check(report, 2, ["test_a", "test_b"])


def test_ran_assertion_rejects_an_empty_selection(tmp_path):
    """A marker expression that stops matching selects 0 tests, exit 0."""
    report = _junit(tmp_path, [])
    with pytest.raises(assert_junit_ran.ReportError, match="0 tests were selected"):
        assert_junit_ran.check(report, 2, [])


def test_ran_assertion_rejects_a_missing_report(tmp_path):
    with pytest.raises(assert_junit_ran.ReportError, match="no junit report"):
        assert_junit_ran.check(tmp_path / "absent.xml", 2, [])


def test_ran_assertion_rejects_a_renamed_required_test(tmp_path):
    report = _junit(tmp_path, [("test_a", None), ("test_renamed", None)])
    with pytest.raises(assert_junit_ran.ReportError, match="did not pass"):
        assert_junit_ran.check(report, 2, ["test_a", "test_b"])


def test_ran_assertion_rejects_failures_and_wrong_counts(tmp_path):
    report = _junit(tmp_path, [("test_a", None), ("test_b", "failure")])
    with pytest.raises(assert_junit_ran.ReportError):
        assert_junit_ran.check(report, 2, ["test_a"])

    report = _junit(tmp_path, [("test_a", None)])
    with pytest.raises(assert_junit_ran.ReportError, match="expected exactly 2"):
        assert_junit_ran.check(report, 2, ["test_a"])


def test_ran_assertion_cli_exits_nonzero_on_a_skip(tmp_path):
    report = _junit(tmp_path, [("test_a", "skipped")])
    assert assert_junit_ran.main([str(report), "--expect-passed", "1"]) == 1


# ---------------------------------------------------------------------------
# render_worker_ci_config.py — the Worker isolation guard
# ---------------------------------------------------------------------------

SHIPPED = render_worker_ci_config.SOURCE_CONFIG


@pytest.fixture
def shipped() -> dict:
    return render_worker_ci_config.load_jsonc(SHIPPED)


def _render(shipped: dict, **overrides):
    kwargs = {
        "name": "bobi-events-ci-smoke",
        "kv_namespace_id": "0123456789abcdef0123456789abcdef",
        "release_version": "ci",
        "release_sha": "abc123",
    }
    kwargs.update(overrides)
    return render_worker_ci_config.render(shipped, **kwargs)


def test_the_shipped_worker_config_still_parses_as_jsonc():
    """The renderer derives from it, so a comment-syntax change that broke the
    parser would break every deploy."""
    config = render_worker_ci_config.load_jsonc(SHIPPED)
    assert config["name"] == render_worker_ci_config.PRODUCTION_WORKER_NAME
    assert config["migrations"][0]["tag"] == "v1"


def test_jsonc_comment_stripping_leaves_string_contents_alone():
    text = '{"a": "http://x//y", "b": "/* not a comment */"} // trailing'
    assert json.loads(render_worker_ci_config.strip_jsonc_comments(text)) == {
        "a": "http://x//y",
        "b": "/* not a comment */",
    }


def test_render_refuses_the_production_worker_name(shipped):
    with pytest.raises(render_worker_ci_config.ConfigError, match="bobi-events"):
        _render(shipped, name="bobi-events")


def test_render_refuses_the_placeholder_kv_id(shipped):
    """Deploying with the placeholder is the documented trap: wrangler
    auto-provisions a fresh empty namespace and the bus comes up empty."""
    with pytest.raises(render_worker_ci_config.ConfigError, match="placeholder"):
        _render(shipped, kv_namespace_id=render_worker_ci_config.KV_PLACEHOLDER)


def test_render_refuses_an_absent_kv_id(shipped):
    with pytest.raises(render_worker_ci_config.ConfigError, match="auto-provision"):
        _render(shipped, kv_namespace_id="   ")


def test_render_refuses_the_kv_id_hardcoded_in_the_shipped_config(shipped):
    """The CI KV id must come from CI configuration, not from the checked-in
    file — otherwise a future real id committed there would be reused here."""
    shipped_with_real_id = json.loads(json.dumps(shipped))
    shipped_with_real_id["kv_namespaces"][0]["id"] = "deadbeef" * 4
    with pytest.raises(render_worker_ci_config.ConfigError, match="hardcoded"):
        _render(shipped_with_real_id, kv_namespace_id="deadbeef" * 4)


def test_render_preserves_the_recipe_under_test(shipped):
    """The point of deriving from the shipped config: the CI deploy must
    exercise the SAME migration, DO binding and compatibility date production
    uses, or it proves a fork of the recipe."""
    config = _render(shipped)

    assert config["migrations"] == shipped["migrations"]
    assert config["durable_objects"] == shipped["durable_objects"]
    assert config["compatibility_date"] == shipped["compatibility_date"]
    assert config["main"] == shipped["main"]


def test_render_isolates_name_kv_and_stamps_the_release(shipped):
    config = _render(shipped, release_sha="c0ffee")

    assert config["name"] == "bobi-events-ci-smoke"
    assert config["workers_dev"] is True
    events = next(b for b in config["kv_namespaces"] if b["binding"] == "EVENTS")
    assert events["id"] == "0123456789abcdef0123456789abcdef"
    # The health smoke's only real discriminator between this deploy and any
    # other Worker answering 200.
    assert config["vars"]["BOBI_RELEASE_SHA"] == "c0ffee"
    assert config["vars"]["BOBI_RELEASE_SHA"] != shipped["vars"]["BOBI_RELEASE_SHA"]


def test_render_cli_exits_nonzero_on_an_unsafe_config(tmp_path):
    out = tmp_path / "wrangler.ci.jsonc"
    rc = render_worker_ci_config.main(
        [
            "--name",
            "bobi-events",
            "--kv-namespace-id",
            "abc",
            "--out",
            str(out),
        ]
    )
    assert rc == 1
    assert not out.exists(), "an unsafe config must never be written to disk"


def test_render_refuses_a_padded_production_name(shipped):
    """`"bobi-events "` must not slip past the name guards on whitespace."""
    with pytest.raises(render_worker_ci_config.ConfigError, match="bobi-events"):
        _render(shipped, name=" bobi-events ")
