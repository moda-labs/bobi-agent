"""Tests for prompt assembly and spawning."""

from pathlib import Path

from dispatch.config import RepoConfig
from dispatch.scanner import WorkItem, WorkSource, Complexity
from dispatch.dispatcher import _build_context, _load_prompts, _slugify, _get_or_create_worktree


BRANCH = "agent/proj-1-abc123"


def _make_item(**kwargs) -> WorkItem:
    defaults = {
        "id": "PROJ-1",
        "source": WorkSource.LINEAR,
        "title": "Add user avatars",
        "body": "Users should see avatars in the sidebar.",
        "repo_config": RepoConfig(
            path=Path("/home/dev/myapp"),
            test_command="pytest -x",
            skills=["review", "ship"],
        ),
        "complexity": Complexity.MEDIUM,
    }
    defaults.update(kwargs)
    return WorkItem(**defaults)


def test_load_prompts_includes_all_sections():
    prompts = _load_prompts()
    assert "Running unattended" in prompts
    assert "Lifecycle" in prompts
    assert "Spec Phase" in prompts
    assert "Implementation Phase" in prompts
    assert "LINEAR_API_KEY" in prompts
    assert "gh pr create" in prompts


def test_build_context_includes_issue():
    item = _make_item()
    context = _build_context(item, BRANCH)

    assert "PROJ-1" in context
    assert "Add user avatars" in context
    assert "avatars in the sidebar" in context
    assert BRANCH in context


def test_build_context_includes_user_reply():
    item = _make_item()
    context = _build_context(item, BRANCH, user_reply="approved")

    assert "approved" in context
    assert "User reply" in context


def test_build_context_no_reply():
    item = _make_item()
    context = _build_context(item, BRANCH)

    assert "User reply" not in context


def test_build_context_includes_test_command():
    item = _make_item()
    context = _build_context(item, BRANCH)

    assert "pytest -x" in context


def test_slugify():
    assert _slugify("Add user avatars") == "add-user-avatars"
    assert _slugify("Fix the BUG!!!") == "fix-the-bug"
    assert _slugify("") == ""


def test_get_or_create_worktree_reuses_existing(tmp_path):
    # Create a fake existing worktree
    worktree = tmp_path / "worktrees" / "proj-1-add-user-avatars"
    worktree.mkdir(parents=True)

    result = _get_or_create_worktree(tmp_path, "PROJ-1", "Add user avatars")
    assert result == worktree
