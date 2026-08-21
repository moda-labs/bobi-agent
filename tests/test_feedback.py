"""Tests for the framework-owned feedback command and GitHub sink."""

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from bobi.cli import main
from bobi.config import Config
from bobi.feedback import (
    DEFAULT_FEEDBACK_REPO,
    MAX_RCA_BODY_CHARS,
    MAX_TITLE_CHARS,
    FeedbackConfig,
    FeedbackContext,
    FeedbackError,
    IssueMatch,
    IssueResult,
    build_issue_body,
    build_recurrence_comment,
    clamp_text,
    comment_on_issue,
    create_github_issue,
    duplicate_ratio,
    load_feedback_config,
    pick_duplicate,
    rca_enabled,
    rca_guide,
    rca_in_progress,
    resolve_destination,
    resolve_token,
    search_existing_issues,
    submit_feedback,
)


def _no_existing_issues(monkeypatch):
    """Keep a CLI test off the network when it is not about deduping."""
    monkeypatch.setattr(
        "bobi.feedback.search_existing_issues", lambda *a, **k: [],
    )


def _search_explodes(*args, **kwargs):
    """A duplicate check that cannot answer, the way a rate limit cannot."""
    raise FeedbackError("github_auth_failed", "rate limited")


_CONTEXT = FeedbackContext(
    bobi_version="1.2.3", agent_slot="eng", package="eng-team",
    package_version="0.4.0", brain_kind="claude", platform="Linux",
    python="3.11.x",
)


def test_destination_precedence():
    config = FeedbackConfig(repo="config/repo")

    assert resolve_destination("cli/repo", "env/repo", config) == "cli/repo"
    assert resolve_destination(None, "env/repo", config) == "env/repo"
    assert resolve_destination(None, None, config) == "config/repo"
    assert resolve_destination(None, None, FeedbackConfig()) == DEFAULT_FEEDBACK_REPO


def test_invalid_destination_fails_cleanly():
    with pytest.raises(FeedbackError) as exc:
        resolve_destination("not-a-repo", None, FeedbackConfig())

    assert exc.value.code == "invalid_feedback_repo"


def test_feedback_config_normalizes_labels():
    config = Config(feedback={
        "repo": "example/support",
        "labels": {"bug": "bug", "feature": ["request", ""]},
    })

    result = load_feedback_config(config)

    assert result.repo == "example/support"
    assert result.labels_for("bug") == ["bug"]
    assert result.labels_for("feature") == ["request"]


def test_token_precedence(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github-env")
    monkeypatch.setenv("GH_TOKEN", "gh-env")

    assert resolve_token("configured") == "configured"
    assert resolve_token() == "github-env"
    monkeypatch.delenv("GITHUB_TOKEN")
    assert resolve_token() == "gh-env"


def test_token_falls_back_to_existing_github_helper(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    with patch("bobi.feedback.github_token", return_value="local-auth"):
        assert resolve_token() == "local-auth"


def test_body_preserves_markdown_and_only_appends_allowlisted_context():
    body = "Repro:\n```sh\necho hi\n```\n\n| expected | actual |\n| ok | bad |\n---"
    context = FeedbackContext(
        bobi_version="0.57.0",
        agent_slot="eng-team",
        package="eng-team",
        package_version="1.2.3",
        brain_kind="codex",
        platform="Linux",
        python="3.13.x",
    )

    rendered = build_issue_body("bug", body, context)

    assert rendered.startswith(body)
    assert "- kind: bug" in rendered
    assert "- agent_slot: eng-team" in rendered
    assert "cwd" not in rendered
    assert "TOKEN" not in rendered


def test_github_sink_creates_issue_without_shelling_out():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(201, json={
            "html_url": "https://github.com/example/support/issues/42",
            "number": 42,
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = create_github_issue(
        "example/support", "bug", "Broken", "Details", ["bug"],
        token="secret-token", client=client,
    )

    request = seen["request"]
    assert request.url.path == "/repos/example/support/issues"
    assert request.headers["authorization"] == "Bearer secret-token"
    assert json.loads(request.content) == {
        "title": "Broken", "body": "Details", "labels": ["bug"],
    }
    assert result == IssueResult(
        url="https://github.com/example/support/issues/42",
        number=42, repo="example/support", kind="bug", title="Broken",
    )


@pytest.mark.parametrize(("status", "code"), [
    (401, "github_auth_failed"),
    (403, "github_auth_failed"),
    (404, "github_repo_not_found"),
    (422, "github_validation_failed"),
    (500, "github_request_failed"),
])
def test_github_sink_maps_failures(status, code):
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={"message": "rejected"}),
    ))

    with pytest.raises(FeedbackError) as exc:
        create_github_issue(
            "example/support", "feature", "Title", "Body", [],
            token="secret-token", client=client,
        )

    assert exc.value.code == code
    assert "secret-token" not in exc.value.message


def test_github_sink_requires_auth_before_network():
    with pytest.raises(FeedbackError) as exc:
        create_github_issue(
            "example/support", "bug", "Title", "Body", [], token="",
        )

    assert exc.value.code == "github_auth_missing"


def test_github_sink_rejects_an_issue_url_for_another_repo():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(201, json={
            "html_url": "https://github.com/other/repo/issues/42",
            "number": 42,
        }),
    ))

    with pytest.raises(FeedbackError) as exc:
        create_github_issue(
            "example/support", "bug", "Title", "Body", [],
            token="secret-token", client=client,
        )

    assert exc.value.code == "github_invalid_response"


def test_cli_text_success_and_labels_are_combined(monkeypatch):
    captured = {}

    def fake_create(repo, kind, title, body, labels, **kwargs):
        captured.update(repo=repo, kind=kind, title=title, body=body,
                        labels=labels, token=kwargs["token"])
        return IssueResult(
            url="https://github.com/example/support/issues/7",
            number=7, repo=repo, kind=kind, title=title,
        )

    config = Config(
        feedback={"repo": "example/support", "labels": {"bug": ["bug"]}},
        services=[],
    )
    monkeypatch.setattr("bobi.cli._try_detect_project_root", lambda: Path("/runtime"))
    monkeypatch.setattr("bobi.config.Config.load", lambda path: config)
    monkeypatch.setattr("bobi.feedback.create_github_issue", fake_create)
    monkeypatch.setattr("bobi.feedback.resolve_token", lambda token: "resolved-token")
    _no_existing_issues(monkeypatch)

    result = CliRunner().invoke(main, [
        "feedback", "bug", "--title", "Broken", "--body", "Details",
        "--label", "triage",
    ])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "https://github.com/example/support/issues/7"
    assert captured["labels"] == ["bug", "triage"]
    assert captured["token"] == "resolved-token"


def test_cli_json_success(monkeypatch):
    monkeypatch.setattr("bobi.cli._try_detect_project_root", lambda: None)
    monkeypatch.setattr(
        "bobi.feedback.create_github_issue",
        lambda repo, kind, title, body, labels, **kwargs: IssueResult(
            url="https://github.com/moda-labs/bobi-agent/issues/8",
            number=8, repo=repo, kind=kind, title=title,
        ),
    )
    monkeypatch.setattr("bobi.feedback.resolve_token", lambda token: "token")
    _no_existing_issues(monkeypatch)

    result = CliRunner().invoke(main, [
        "feedback", "feature", "--title", "Capability", "--body", "Need it",
        "--json",
    ])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "action": "created",
        "url": "https://github.com/moda-labs/bobi-agent/issues/8",
        "number": 8,
        "repo": "moda-labs/bobi-agent",
        "kind": "feature",
        "title": "Capability",
    }


def test_cli_json_failure(monkeypatch):
    monkeypatch.setattr("bobi.cli._try_detect_project_root", lambda: None)
    monkeypatch.setattr("bobi.feedback.resolve_token", lambda token: "")

    result = CliRunner().invoke(main, [
        "feedback", "bug", "--title", "Broken", "--body", "Details", "--json",
    ])

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "error": "github_auth_missing",
        "message": (
            "No GitHub write credential configured. Set the github service "
            "token, GITHUB_TOKEN, or GH_TOKEN."
        ),
    }


def test_cli_body_file_from_stdin_dry_run():
    result = CliRunner().invoke(main, [
        "feedback", "bug", "--title", "Broken", "--body-file", "-",
        "--dry-run", "--json",
    ], input="Detailed repro\n")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["body"].startswith("Detailed repro\n\n---")


def test_cli_loads_runtime_feedback_config_and_allowlisted_context(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "agent.yaml").write_text(
        "agent: support-team\n"
        "version: 1.2.3\n"
        "brain:\n  kind: codex\n"
        "feedback:\n  repo: example/support\n"
    )

    result = CliRunner().invoke(main, [
        "feedback", "feature", "--title", "Capability", "--body", "Need it",
        "--dry-run", "--json",
    ], env={"BOBI_ROOT": str(tmp_path)})

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["repo"] == "example/support"
    assert "- agent_slot: " + tmp_path.name in payload["body"]
    assert "- package: support-team" in payload["body"]
    assert "- package_version: 1.2.3" in payload["body"]
    assert "- brain_kind: codex" in payload["body"]


def test_cli_rejects_conflicting_body_sources(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("From file")

    result = CliRunner().invoke(main, [
        "feedback", "bug", "--title", "Broken", "--body", "Inline",
        "--body-file", str(report), "--dry-run",
    ])

    assert result.exit_code == 2
    assert "either --body or --body-file" in result.output


def test_base_prompt_includes_narrow_feedback_policy():
    prompt = (Path(__file__).parents[1] / "bobi" / "prompts" / "base.md").read_text()

    assert "bobi feedback bug" in prompt
    assert "ask for confirmation" in prompt
    assert "Never include secrets" in prompt


# ---------------------------------------------------------------------------
# Framework-bug RCA: the operator switch, the recursion guard, and the guide
# ---------------------------------------------------------------------------

def test_rca_is_on_unless_the_operator_turns_it_off(monkeypatch):
    monkeypatch.delenv("BOBI_FRAMEWORK_RCA", raising=False)
    assert rca_enabled() is True

    for value in ("off", "0", "false", "no"):
        monkeypatch.setenv("BOBI_FRAMEWORK_RCA", value)
        assert rca_enabled() is False, value

    monkeypatch.setenv("BOBI_FRAMEWORK_RCA", "on")
    assert rca_enabled() is True


def test_the_recursion_marker_is_separate_from_the_operator_switch(monkeypatch):
    """An RCA sub-agent must still be able to file what it found."""
    monkeypatch.delenv("BOBI_FRAMEWORK_RCA", raising=False)
    monkeypatch.setenv("BOBI_FRAMEWORK_RCA_ACTIVE", "1")

    assert rca_in_progress() is True
    assert rca_enabled() is True


def test_rca_guide_states_its_limits_and_stays_short():
    guide = rca_guide()

    assert "bobi feedback bug --rca" in guide
    assert str(MAX_TITLE_CHARS) in guide
    assert str(MAX_RCA_BODY_CHARS) in guide
    assert "Never run an RCA on an RCA" in guide
    # The guide is itself a prompt an agent pays for on every framework bug.
    assert len(guide) < 3000


def test_rca_guide_appends_the_failure_under_analysis():
    guide = rca_guide("bobi agent x restart exits 0 without restarting")

    assert guide.endswith("bobi agent x restart exits 0 without restarting")
    assert "## The failure to analyze" in guide


def test_clamp_trims_on_a_word_boundary_and_marks_the_cut():
    assert clamp_text("short enough", 40) == "short enough"

    clamped = clamp_text("word " * 100, 40)

    assert len(clamped) <= 40
    assert clamped.endswith("[truncated]")
    assert "wor [truncated]" not in clamped


# ---------------------------------------------------------------------------
# Deduplication: comment on the existing bug, never open the fifty-first issue
# ---------------------------------------------------------------------------

def test_duplicate_ratio_ignores_noise_words():
    # Significant words: {restart, exits, manager}. Two of the three are shared,
    # and none of "the"/"is"/"not"/"when"/"without" moves the number.
    assert duplicate_ratio(
        "restart exits 0 without the manager",
        "The manager is not restarted when restart is used",
    ) == pytest.approx(2 / 3)
    # Punctuation and case are not differences.
    assert duplicate_ratio("Restart wedges!", "restart, wedges") == 1.0
    assert duplicate_ratio("slack reply drops the thread", "restart wedges") == 0.0
    assert duplicate_ratio("", "anything") == 0.0
    # A title made only of noise words cannot claim to match anything.
    assert duplicate_ratio("is it the one that was?", "anything") == 0.0


def test_pick_duplicate_needs_a_real_overlap_not_just_a_top_rank():
    candidate = "restart exits 0 without restarting the manager"
    near_miss = IssueMatch(
        number=5, title="restart deletes a live worktree",
        url="u", state="open",
    )
    real = IssueMatch(
        number=9, title="manager restart exits without restarting",
        url="u9", state="open",
    )

    assert pick_duplicate(candidate, [near_miss]) is None
    assert pick_duplicate(candidate, [near_miss, real]) is real
    assert pick_duplicate(candidate, []) is None


def test_pick_duplicate_prefers_an_open_issue_over_a_closed_one():
    candidate = "manager restart exits without restarting"
    closed = IssueMatch(
        number=3, title="manager restart exits without restarting",
        url="u3", state="closed",
    )
    opened = IssueMatch(
        number=4, title="manager restart exits without restarting",
        url="u4", state="open",
    )

    assert pick_duplicate(candidate, [closed, opened]) is opened
    # A closed report is still the right home for a regression.
    assert pick_duplicate(candidate, [closed]) is closed


def test_search_scopes_the_query_to_the_repo_and_to_titles():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"items": [
            {"number": 12, "title": "manager restart exits", "state": "open"},
        ]})

    matches = search_existing_issues(
        "example/support", "manager restart exits without restarting",
        token="secret-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    query = seen["request"].url.params["q"]
    assert query.startswith("repo:example/support is:issue in:title ")
    assert "restarting" in query and "without" not in query
    assert matches == [IssueMatch(
        number=12, title="manager restart exits",
        url="https://github.com/example/support/issues/12", state="open",
    )]


def test_search_skips_the_network_when_a_title_has_no_significant_words():
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("searched with an empty query")

    assert search_existing_issues(
        "example/support", "is it a to do?", token="t",
        client=httpx.Client(transport=httpx.MockTransport(explode)),
    ) == []


def test_comment_rejects_a_url_for_another_issue():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(201, json={
            "html_url": "https://github.com/example/support/issues/99#issuecomment-1",
        }),
    ))

    with pytest.raises(FeedbackError) as exc:
        comment_on_issue("example/support", 12, "body", token="t", client=client)

    assert exc.value.code == "github_invalid_response"


def test_recurrence_comment_is_short_and_carries_no_second_footer():
    context = FeedbackContext(
        bobi_version="1.2.3", agent_slot="eng", package="eng-team",
        package_version="0.4.0", brain_kind="claude", platform="Linux",
        python="3.11.x",
    )

    comment = build_recurrence_comment("**What broke:** restart is a no-op.", context)

    assert comment.startswith("Seen again")
    assert "Generated context:" not in comment
    assert "agent_slot" not in comment
    assert len(comment) < 300


def test_submit_comments_on_the_match_and_never_creates(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "bobi.feedback.search_existing_issues",
        lambda *a, **k: [IssueMatch(
            number=7, title="manager restart exits without restarting",
            url="https://github.com/example/support/issues/7", state="open",
        )],
    )
    monkeypatch.setattr(
        "bobi.feedback.create_github_issue",
        lambda *a, **k: calls.append("created"),
    )
    monkeypatch.setattr(
        "bobi.feedback.comment_on_issue",
        lambda repo, number, body, **k: (
            calls.append(("commented", number, body))
            or "https://github.com/example/support/issues/7#issuecomment-1"
        ),
    )

    outcome = submit_feedback(
        "example/support", "bug", "manager restart exits without restarting",
        "**What broke:** restart is a no-op.", ["bug"], _CONTEXT, token="t",
    )

    assert outcome.action == "commented"
    assert outcome.number == 7
    assert outcome.comment_url.endswith("#issuecomment-1")
    assert [c[0] for c in calls] == ["commented"]


def test_a_failed_comment_never_falls_back_to_opening_a_duplicate(monkeypatch):
    """The match was found, so a new issue would be the duplicate we forbade."""
    monkeypatch.setattr(
        "bobi.feedback.search_existing_issues",
        lambda *a, **k: [IssueMatch(
            number=7, title="manager restart exits without restarting",
            url="https://github.com/example/support/issues/7", state="open",
        )],
    )
    monkeypatch.setattr(
        "bobi.feedback.comment_on_issue",
        lambda *a, **k: (_ for _ in ()).throw(
            FeedbackError("github_auth_failed", "no write access"),
        ),
    )
    monkeypatch.setattr(
        "bobi.feedback.create_github_issue",
        lambda *a, **k: pytest.fail("opened a duplicate after a failed comment"),
    )

    with pytest.raises(FeedbackError) as exc:
        submit_feedback(
            "example/support", "bug", "manager restart exits without restarting",
            "body", [], _CONTEXT, token="t",
        )

    assert exc.value.code == "github_auth_failed"


def test_submit_aborts_a_robot_filing_when_the_duplicate_check_fails(monkeypatch):
    monkeypatch.setattr("bobi.feedback.search_existing_issues", _search_explodes)
    monkeypatch.setattr(
        "bobi.feedback.create_github_issue",
        lambda *a, **k: pytest.fail("filed blind with no duplicate protection"),
    )

    with pytest.raises(FeedbackError) as exc:
        submit_feedback(
            "example/support", "bug", "restart is a no-op", "body", [],
            _CONTEXT, token="t", dedupe_required=True,
        )

    assert exc.value.code == "dedupe_unavailable"


def test_submit_cannot_be_talked_out_of_deduping_a_robot_filing(monkeypatch):
    monkeypatch.setattr("bobi.feedback.search_existing_issues", _search_explodes)

    with pytest.raises(FeedbackError) as exc:
        submit_feedback(
            "example/support", "bug", "restart is a no-op", "body", [],
            _CONTEXT, token="t", dedupe=False, dedupe_required=True,
        )

    assert exc.value.code == "dedupe_unavailable"


def test_submit_warns_but_still_files_a_human_report(monkeypatch):
    monkeypatch.setattr("bobi.feedback.search_existing_issues", _search_explodes)
    monkeypatch.setattr(
        "bobi.feedback.create_github_issue",
        lambda repo, kind, title, body, labels, **k: IssueResult(
            url="https://github.com/example/support/issues/8", number=8,
            repo=repo, kind=kind, title=title,
        ),
    )

    outcome = submit_feedback(
        "example/support", "bug", "restart is a no-op", "body", [],
        _CONTEXT, token="t",
    )

    assert outcome.action == "created"
    assert "Duplicate check failed" in outcome.warning


def test_no_dedupe_skips_the_search_for_an_interactive_filing(monkeypatch):
    monkeypatch.setattr("bobi.feedback.search_existing_issues", _search_explodes)
    monkeypatch.setattr(
        "bobi.feedback.create_github_issue",
        lambda repo, kind, title, body, labels, **k: IssueResult(
            url="https://github.com/example/support/issues/8", number=8,
            repo=repo, kind=kind, title=title,
        ),
    )

    outcome = submit_feedback(
        "example/support", "bug", "restart is a no-op", "body", [],
        _CONTEXT, token="t", dedupe=False,
    )

    assert outcome.action == "created" and outcome.warning == ""


# ---------------------------------------------------------------------------
# CLI wiring for the RCA path
# ---------------------------------------------------------------------------

def test_cli_rca_prints_nothing_to_follow_when_disabled(monkeypatch):
    monkeypatch.setenv("BOBI_FRAMEWORK_RCA", "off")

    result = CliRunner().invoke(main, ["feedback", "rca"])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert "disabled" in result.stderr


def test_cli_rca_filing_is_refused_before_any_work_when_disabled(monkeypatch):
    monkeypatch.setenv("BOBI_FRAMEWORK_RCA", "off")
    monkeypatch.setattr(
        "bobi.feedback.submit_feedback",
        lambda *a, **k: pytest.fail("filed with the operator switch off"),
    )

    result = CliRunner().invoke(main, [
        "feedback", "bug", "--rca", "--json", "--title", "x", "--body", "y",
    ])

    assert result.exit_code == 0
    assert json.loads(result.output) == {"action": "skipped", "status": "disabled"}


def test_cli_rca_clamps_the_report_before_it_reaches_the_sink(monkeypatch):
    captured = {}
    monkeypatch.setattr("bobi.cli._try_detect_project_root", lambda: None)
    monkeypatch.setattr("bobi.feedback.resolve_token", lambda token: "token")
    _no_existing_issues(monkeypatch)
    monkeypatch.setattr(
        "bobi.feedback.create_github_issue",
        lambda repo, kind, title, body, labels, **k: captured.update(
            title=title, body=body,
        ) or IssueResult(
            url="https://github.com/moda-labs/bobi-agent/issues/9", number=9,
            repo=repo, kind=kind, title=title,
        ),
    )

    result = CliRunner().invoke(main, [
        "feedback", "bug", "--rca", "--json",
        "--title", "restart " + "and again " * 40,
        "--body", "narrating my reasoning. " * 200,
    ])

    assert result.exit_code == 0, result.output
    assert len(captured["title"]) <= MAX_TITLE_CHARS
    assert captured["title"].endswith("[truncated]")
    assert "[truncated]" in captured["body"]


def test_cli_rca_reports_a_filing_failure_without_failing_the_caller(monkeypatch):
    monkeypatch.setattr("bobi.cli._try_detect_project_root", lambda: None)
    monkeypatch.setattr("bobi.feedback.search_existing_issues", _search_explodes)

    result = CliRunner().invoke(main, [
        "feedback", "bug", "--rca", "--json", "--title", "Broken",
        "--body", "Details",
    ])

    # The task that hit the bug must not inherit the RCA's exit code.
    assert result.exit_code == 0
    assert json.loads(result.output)["error"] == "dedupe_unavailable"


def test_cli_bug_filing_still_fails_loudly_without_rca(monkeypatch):
    monkeypatch.setattr("bobi.cli._try_detect_project_root", lambda: None)
    monkeypatch.setattr("bobi.feedback.resolve_token", lambda token: "")

    result = CliRunner().invoke(main, [
        "feedback", "bug", "--json", "--title", "Broken", "--body", "Details",
    ])

    assert result.exit_code == 1
    assert json.loads(result.output)["error"] == "github_auth_missing"


def test_base_prompt_teaches_the_whole_rca_loop():
    prompt = (Path(__file__).parents[1] / "bobi" / "prompts" / "base.md").read_text()

    assert "bobi feedback rca" in prompt
    assert "bobi feedback bug --rca" in prompt
    assert "BOBI_FRAMEWORK_RCA=off" in prompt
    assert "BOBI_FRAMEWORK_RCA_ACTIVE=1" in prompt
    assert "comments on an existing report" in prompt
