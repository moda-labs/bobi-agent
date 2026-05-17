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


# Spec phase tests

def test_spec_prompt_does_not_implement():
    item = _make_item()
    prompt = build_prompt(item, BRANCH, phase="spec")

    assert "SPEC.md" in prompt
    assert "Do NOT write any implementation code" in prompt
    assert "principal" in prompt.lower()


def test_spec_prompt_includes_task():
    item = _make_item()
    prompt = build_prompt(item, BRANCH, phase="spec")

    assert "Add user avatars" in prompt
    assert "Gravatar" in prompt


def test_spec_prompt_has_scope_assessment():
    item = _make_item()
    prompt = build_prompt(item, BRANCH, phase="spec")

    assert "Scope Assessment" in prompt
    assert "multiple tickets" in prompt.lower()


# Implementation phase tests

def test_implement_prompt_has_branch_and_pr():
    item = _make_item()
    prompt = build_prompt(item, BRANCH, phase="implement", spec="The approved spec here.")

    assert BRANCH in prompt
    assert "gh pr create" in prompt
    assert "git push" in prompt
    assert "The approved spec here." in prompt


def test_implement_prompt_has_review():
    item = _make_item()
    prompt = build_prompt(item, BRANCH, phase="implement", spec="spec")

    assert "/review" in prompt
    assert "PROJ-1" in prompt


def test_implement_prompt_has_test_command():
    item = _make_item()
    prompt = build_prompt(item, BRANCH, phase="implement", spec="spec")

    assert "pytest -x" in prompt


def test_no_test_command_shows_placeholder():
    item = _make_item()
    item.repo_config.test_command = ""
    prompt = build_prompt(item, BRANCH, phase="implement", spec="spec")

    assert "no test command configured" in prompt
