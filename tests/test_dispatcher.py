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


def test_prompt_includes_preamble():
    item = _make_item()
    prompt = build_prompt(item, BRANCH)

    assert "Running unattended" in prompt
    assert ".dispatch/state.md" in prompt


def test_prompt_includes_lifecycle():
    item = _make_item()
    prompt = build_prompt(item, BRANCH)

    assert "Phase 1: Spec" in prompt
    assert "Phase 2: Implement" in prompt
    assert "Exit cleanly" in prompt


def test_prompt_includes_tools():
    item = _make_item()
    prompt = build_prompt(item, BRANCH)

    assert "LINEAR_API_KEY" in prompt
    assert "gh pr create" in prompt


def test_prompt_includes_spec_methodology():
    item = _make_item()
    prompt = build_prompt(item, BRANCH)

    assert "Scope guards" in prompt
    assert "Size verdict" in prompt
    assert "Verification Plan" in prompt


def test_prompt_includes_implement_methodology():
    item = _make_item()
    prompt = build_prompt(item, BRANCH)

    assert "staff engineer" in prompt.lower()
    assert "Tests are not optional" in prompt


def test_prompt_includes_issue_context():
    item = _make_item()
    prompt = build_prompt(item, BRANCH)

    assert "PROJ-1" in prompt
    assert "Add user avatars" in prompt
    assert "Gravatar" in prompt
    assert BRANCH in prompt


def test_prompt_includes_user_reply_when_provided():
    item = _make_item()
    prompt = build_prompt(item, BRANCH, user_reply="approved, go ahead")

    assert "approved, go ahead" in prompt
    assert "User reply" in prompt


def test_prompt_without_reply_has_no_reply_section():
    item = _make_item()
    prompt = build_prompt(item, BRANCH)

    assert "User reply" not in prompt


def test_prompt_includes_test_command():
    item = _make_item()
    prompt = build_prompt(item, BRANCH)

    assert "pytest -x" in prompt


def test_no_test_command_shows_placeholder():
    item = _make_item()
    item.repo_config.test_command = ""
    prompt = build_prompt(item, BRANCH)

    assert "none configured" in prompt
