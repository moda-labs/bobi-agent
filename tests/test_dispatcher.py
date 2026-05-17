"""Tests for prompt assembly."""

from pathlib import Path

from dispatch.config import RepoConfig
from dispatch.scanner import WorkItem, WorkSource, Complexity
from dispatch.dispatcher import build_prompt


BRANCH = "agent/proj-1-abc123"


def _make_item(complexity: Complexity = Complexity.MEDIUM, **kwargs) -> WorkItem:
    defaults = {
        "id": "PROJ-1",
        "source": WorkSource.LINEAR,
        "title": "Add user avatars",
        "body": "Users should see avatars in the sidebar. Use Gravatar as fallback.",
        "repo_config": RepoConfig(
            path=Path("/home/dev/myapp"),
            test_command="pytest -x",
            skills=["review", "ship"],
        ),
        "complexity": complexity,
    }
    defaults.update(kwargs)
    return WorkItem(**defaults)


def test_trivial_prompt_has_branch_and_pr():
    item = _make_item(Complexity.TRIVIAL)
    prompt = build_prompt(item, BRANCH)

    assert "Add user avatars" in prompt
    assert BRANCH in prompt
    assert "gh pr create" in prompt
    assert "git push" in prompt


def test_medium_prompt_has_plan_step():
    item = _make_item(Complexity.MEDIUM)
    prompt = build_prompt(item, BRANCH)

    assert "plan" in prompt.lower()
    assert "CLAUDE.md" in prompt
    assert "pytest -x" in prompt
    assert BRANCH in prompt
    assert "gh pr create" in prompt


def test_heavy_prompt_has_skills():
    item = _make_item(Complexity.HEAVY)
    prompt = build_prompt(item, BRANCH)

    assert "/review" in prompt
    assert "/ship" in prompt
    assert "CLAUDE.md" in prompt
    assert "pytest -x" in prompt
    assert "gh pr create" in prompt


def test_prompt_includes_issue_body():
    item = _make_item(body="Use Gravatar as fallback for missing avatars.")
    prompt = build_prompt(item, BRANCH)

    assert "Gravatar" in prompt


def test_no_test_command_shows_placeholder():
    item = _make_item()
    item.repo_config.test_command = ""
    prompt = build_prompt(item, BRANCH)

    assert "no test command configured" in prompt


def test_prompt_includes_issue_id():
    item = _make_item()
    prompt = build_prompt(item, BRANCH)

    assert "PROJ-1" in prompt
