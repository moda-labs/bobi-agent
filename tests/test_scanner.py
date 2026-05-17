"""Tests for scanner classification logic."""

from dispatch.scanner import classify_complexity, Complexity
from dispatch.config import RepoConfig


def _make_config(**kwargs) -> RepoConfig:
    from pathlib import Path
    defaults = {
        "path": Path("/tmp"),
        "complexity_rules": {
            "trivial": "label:typo OR label:docs",
            "heavy": "label:feature OR estimate>3",
        },
    }
    defaults.update(kwargs)
    return RepoConfig(**defaults)


def test_classify_trivial_by_label():
    config = _make_config()
    issue = {"estimate": None}
    labels = ["typo", "agent"]

    assert classify_complexity(issue, labels, config) == Complexity.TRIVIAL


def test_classify_trivial_docs():
    config = _make_config()
    issue = {"estimate": None}
    labels = ["docs"]

    assert classify_complexity(issue, labels, config) == Complexity.TRIVIAL


def test_classify_heavy_by_label():
    config = _make_config()
    issue = {"estimate": None}
    labels = ["feature", "agent"]

    assert classify_complexity(issue, labels, config) == Complexity.HEAVY


def test_classify_heavy_by_estimate():
    config = _make_config()
    issue = {"estimate": 5}
    labels = ["agent"]

    assert classify_complexity(issue, labels, config) == Complexity.HEAVY


def test_classify_medium_default():
    config = _make_config()
    issue = {"estimate": 2}
    labels = ["agent", "backend"]

    assert classify_complexity(issue, labels, config) == Complexity.MEDIUM


def test_classify_no_rules():
    config = _make_config(complexity_rules={})
    issue = {"estimate": 8}
    labels = ["feature"]

    assert classify_complexity(issue, labels, config) == Complexity.MEDIUM
