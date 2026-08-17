"""The CI guard scripts, proven to fail on the cases they exist for.

`assert_junit_ran.py`, `render_worker_ci_config.py` and `await_worker_ready.py`
are the only things standing between a live lane and a silent no-op, a deploy
pointed at the wrong infrastructure, or a smoke run against the wrong Worker
version. A guard nobody tested is exactly the class of thing #909 was filed
about, so each rejection path below is exercised rather than asserted in prose.

The readiness gate earns its place here the hard way: it lived as inline shell
in `worker-deploy-smoke.yml` through two fixes, and the third failure
(run 30895709421) was a case neither fix covered and no test could reach.
"""

import http.server
import importlib.util
import itertools
import json
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load(script: str):
    path = REPO_ROOT / "scripts" / script
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE executing. A module that is not in `sys.modules` cannot
    # have its own annotations resolved, so `@dataclass` under
    # `from __future__ import annotations` dies in `_is_type` on a None module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


assert_junit_ran = _load("assert_junit_ran.py")
render_worker_ci_config = _load("render_worker_ci_config.py")
await_worker_ready = _load("await_worker_ready.py")


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


# ---------------------------------------------------------------------------
# await_worker_ready.py — the readiness gate
# ---------------------------------------------------------------------------
#
# The gate that shipped before this one polled `/health` for the release sha
# alone. Run 30895709421 (scheduled, 2026-08-04) cleared it on 5 consecutive
# matches with 0 resets and was then smoked against the version published by
# `wrangler deploy`, which carries the PREVIOUS run's secrets — 401 on /mcp,
# 500 on the round-trip. The sha cannot see that: it is a `var` baked into the
# rendered config, identical on both versions either side of `secret bulk`.
#
# So the fake Worker below serves the RIGHT sha throughout and rotates only the
# operator token, which is exactly the shape of the real failure.


class _FakeWorker(http.server.BaseHTTPRequestHandler):
    """A Worker that answers /health and /fleet/status from server state."""

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's interface
        state = self.server.state
        state["user_agents"].append(self.headers.get("User-Agent"))

        if self.path == "/health":
            state["health_hits"] += 1
            self._json(
                200,
                {
                    "status": "ok",
                    "release": {"version": "ci-smoke-1", "sha": state["sha"]},
                    "worker": {"version_id": state["version_id"]},
                },
            )
            return

        if self.path == "/fleet/status":
            state["fleet_hits"] += 1
            # `state["rotate_after"]` fake-rolls the edge over to the version
            # carrying this run's token, mid-poll, exactly as Cloudflare does.
            if state["rotate_after"] is not None and state["fleet_hits"] > state["rotate_after"]:
                state["token"] = state["new_token"]
                state["version_id"] = state["new_version_id"]
            presented = self.headers.get("Authorization", "")
            if not state["token"]:
                self._json(503, {"error": "fleet API not configured"})
            elif presented == f"Bearer {state['token']}":
                self._json(200, {"instances": []})
            else:
                self._json(401, {"error": "unauthorized"})
            return

        self._json(404, {"error": "not found"})

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def fake_worker():
    """A live HTTP Worker stand-in; yields (base_url, state)."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeWorker)
    server.state = {
        "sha": "c0ffee" * 6,
        "token": "the-previous-run-token",
        "new_token": "this-run-token",
        "version_id": "version-deploy",
        "new_version_id": "version-with-secrets",
        "rotate_after": None,
        "health_hits": 0,
        "fleet_hits": 0,
        "user_agents": [],
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server.state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _clock(step: float = 1.0):
    """A deterministic monotonic clock; each read advances by *step*.

    Real time would make the not-ready cases spin against the deadline for the
    whole timeout, which is both slow and load-sensitive. Advancing per read
    bounds every test at a fixed number of probes.
    """
    counter = itertools.count(0.0, step)
    return lambda: next(counter)


def _await(base_url, expected_sha, needed=5, timeout=12, token="this-run-token"):
    return await_worker_ready.await_ready(
        lambda: await_worker_ready.probe(base_url, expected_sha, token),
        needed=needed,
        interval=1,
        timeout=timeout,
        sleep=lambda _s: None,
        monotonic=_clock(),
    )


def test_gate_refuses_a_version_carrying_the_PREVIOUS_run_s_credentials(fake_worker):
    """THE regression. Right sha, wrong secrets — run 30895709421's exact shape.

    The deployed version answers /health with this commit's release sha, so the
    old sha-only gate cleared here and handed the smoke a Worker that 401s. The
    gate must not clear at all.
    """
    base_url, state = fake_worker

    with pytest.raises(await_worker_ready.NotReady) as excinfo:
        _await(base_url, state["sha"])

    assert "401" in str(excinfo.value)
    assert "predates the secret write" in str(excinfo.value)
    # It really was serving the right sha the whole time: the sha is NOT what
    # was wrong, which is precisely why the previous gate could not see it.
    assert state["health_hits"] > 0
    observation = await_worker_ready.probe(base_url, state["sha"], "this-run-token")
    assert observation.ready is False
    assert await_worker_ready.probe(base_url, state["sha"], state["token"]).ready is True


def test_gate_clears_once_the_secret_version_is_serving(fake_worker):
    base_url, state = fake_worker
    state["token"] = "this-run-token"
    state["version_id"] = "version-with-secrets"

    ready = _await(base_url, state["sha"])

    assert ready.version_id == "version-with-secrets"
    assert ready.probes == 5
    assert ready.resets == 0


def test_gate_waits_through_a_rollover_then_clears(fake_worker):
    """The real sequence: the deploy version answers first, the secret version
    takes over mid-poll, and the gate clears only on the far side."""
    base_url, state = fake_worker
    state["rotate_after"] = 3

    ready = _await(base_url, state["sha"])

    assert ready.version_id == "version-with-secrets"
    # 3 probes met the stale version before the flip, so clearing needs more
    # than `needed` probes — the streak could not have started early.
    assert ready.probes > 5


def test_gate_sends_a_user_agent_cloudflare_does_not_block(fake_worker):
    """`Python-urllib/x.y` is answered 403 by Cloudflare's managed bot rules at
    the edge, so a gate that forgot the header would never clear and the lane
    would fail as if the deploy were broken."""
    base_url, state = fake_worker
    state["token"] = "this-run-token"

    _await(base_url, state["sha"], needed=1)

    assert state["user_agents"], "no request reached the Worker"
    assert all(ua == await_worker_ready.SMOKE_USER_AGENT for ua in state["user_agents"])
    assert not any("urllib" in ua for ua in state["user_agents"])


def test_gate_reports_an_unconfigured_operator_token_distinctly(fake_worker):
    """503 (token unset on the script) is a different failure from 401 (wrong
    token), and the operator reading the log needs to know which."""
    base_url, state = fake_worker
    state["token"] = ""

    with pytest.raises(await_worker_ready.NotReady, match="FLEET_OPERATOR_TOKEN is unset"):
        _await(base_url, state["sha"])


def test_gate_refuses_a_stale_deploy(fake_worker):
    """The original guarantee, unweakened: a Worker serving a different commit
    never becomes ready, however valid its credentials."""
    base_url, state = fake_worker
    state["token"] = "this-run-token"
    state["sha"] = "a-different-commit"

    with pytest.raises(await_worker_ready.NotReady, match="release sha"):
        _await(base_url, "c0ffee" * 6)


def test_gate_cli_refuses_to_run_without_the_operator_token(monkeypatch, capsys):
    """No token means no credential check, which is a sha-only gate — the exact
    thing this script replaced. It must fail loudly, not quietly degrade."""
    monkeypatch.delenv(await_worker_ready.OPERATOR_TOKEN_ENV, raising=False)

    rc = await_worker_ready.main(["--url", "https://example.invalid", "--expect-sha", "abc"])

    assert rc == 1
    assert "::error::" in capsys.readouterr().err


def test_gate_cli_exits_nonzero_when_the_deployment_never_becomes_ready(
    fake_worker, monkeypatch, capsys
):
    """The CLI must surface NotReady as a non-zero exit with the reason, so the
    workflow step fails on it rather than falling through to the smoke."""
    base_url, state = fake_worker
    monkeypatch.setenv(await_worker_ready.OPERATOR_TOKEN_ENV, "this-run-token")

    rc = await_worker_ready.main(
        [
            "--url", base_url + "/",   # trailing slash must not double up the path
            "--expect-sha", state["sha"],
            "--needed", "5",
            "--interval", "0",
            "--timeout", "1",
        ]
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "::error::" in err
    assert "401" in err
    assert "this-run-token" not in err, "the minted token leaked into the log"
