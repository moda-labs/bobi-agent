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
    FeedbackConfig,
    FeedbackContext,
    FeedbackError,
    IssueResult,
    build_issue_body,
    create_github_issue,
    load_feedback_config,
    resolve_destination,
    resolve_token,
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

    result = CliRunner().invoke(main, [
        "feedback", "feature", "--title", "Capability", "--body", "Need it",
        "--json",
    ])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
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
