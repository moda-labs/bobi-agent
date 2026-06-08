"""Unit tests for the modastack kb CLI commands."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from modastack.cli import main


@pytest.fixture(autouse=True)
def setup_project_root(tmp_path, monkeypatch):
    """Set project root and redirect KB storage for all CLI tests."""
    kb_dir = tmp_path / ".modastack" / "kb"
    kb_dir.mkdir(parents=True)
    state_dir = tmp_path / ".modastack" / "state"
    state_dir.mkdir(parents=True)
    monkeypatch.setattr("modastack.sdk._project_root", tmp_path)
    monkeypatch.setattr("modastack.kb.store._kb_dir", lambda: kb_dir)
    return tmp_path


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# kb create
# ---------------------------------------------------------------------------

class TestKBCreate:
    def test_creates_kb(self, runner, setup_project_root):
        result = runner.invoke(main, ["kb", "create", "docs"])
        assert result.exit_code == 0
        assert "Created KB 'docs'" in result.output

    def test_duplicate_fails(self, runner, setup_project_root):
        runner.invoke(main, ["kb", "create", "docs"])
        result = runner.invoke(main, ["kb", "create", "docs"])
        assert result.exit_code != 0
        assert "already exists" in result.output


# ---------------------------------------------------------------------------
# kb add
# ---------------------------------------------------------------------------

class TestKBAdd:
    def test_add_text(self, runner, setup_project_root):
        runner.invoke(main, ["kb", "create", "docs"])
        with patch("modastack.kb.embedder.embed", return_value=[[0.1] * 384]):
            result = runner.invoke(main, ["kb", "add", "docs", "--text", "Hello world"])
        assert result.exit_code == 0
        assert "Added" in result.output

    def test_add_file(self, runner, setup_project_root, tmp_path):
        runner.invoke(main, ["kb", "create", "docs"])
        f = tmp_path / "test.md"
        f.write_text("File content here")
        with patch("modastack.kb.embedder.embed", return_value=[[0.1] * 384]):
            result = runner.invoke(main, ["kb", "add", "docs", "--file", str(f)])
        assert result.exit_code == 0
        assert "Added" in result.output

    def test_add_no_options(self, runner, setup_project_root):
        runner.invoke(main, ["kb", "create", "docs"])
        result = runner.invoke(main, ["kb", "add", "docs"])
        assert result.exit_code != 0
        assert "Provide --file or --text" in result.output

    def test_add_nonexistent_kb(self, runner, setup_project_root):
        with patch("modastack.kb.embedder.embed"):
            result = runner.invoke(main, ["kb", "add", "ghost", "--text", "hello"])
        assert result.exit_code != 0
        assert "does not exist" in result.output


# ---------------------------------------------------------------------------
# kb search
# ---------------------------------------------------------------------------

class TestKBSearch:
    def test_search_fts(self, runner, setup_project_root):
        runner.invoke(main, ["kb", "create", "docs"])
        with patch("modastack.kb.embedder.embed", return_value=[[0.1] * 384]):
            runner.invoke(main, ["kb", "add", "docs", "--text", "Python programming language"])
        result = runner.invoke(main, ["kb", "search", "docs", "Python", "--mode", "fts"])
        assert result.exit_code == 0
        assert "Python" in result.output

    def test_search_no_results(self, runner, setup_project_root):
        runner.invoke(main, ["kb", "create", "docs"])
        result = runner.invoke(main, ["kb", "search", "docs", "xyznotfound", "--mode", "fts"])
        assert result.exit_code == 0
        assert "No results" in result.output

    def test_search_nonexistent_kb(self, runner, setup_project_root):
        with patch("modastack.kb.embedder.embed"):
            result = runner.invoke(main, ["kb", "search", "ghost", "query", "--mode", "fts"])
        assert result.exit_code != 0
        assert "does not exist" in result.output


# ---------------------------------------------------------------------------
# kb list
# ---------------------------------------------------------------------------

class TestKBList:
    def test_list_empty(self, runner, setup_project_root):
        result = runner.invoke(main, ["kb", "list"])
        assert result.exit_code == 0
        assert "No knowledge bases" in result.output

    def test_list_with_kbs(self, runner, setup_project_root):
        runner.invoke(main, ["kb", "create", "alpha"])
        runner.invoke(main, ["kb", "create", "beta"])
        result = runner.invoke(main, ["kb", "list"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" in result.output


# ---------------------------------------------------------------------------
# kb info
# ---------------------------------------------------------------------------

class TestKBInfo:
    def test_info(self, runner, setup_project_root):
        runner.invoke(main, ["kb", "create", "docs"])
        result = runner.invoke(main, ["kb", "info", "docs"])
        assert result.exit_code == 0
        assert "docs" in result.output
        assert "Entries:" in result.output

    def test_info_nonexistent(self, runner, setup_project_root):
        result = runner.invoke(main, ["kb", "info", "ghost"])
        assert result.exit_code != 0
        assert "does not exist" in result.output


# ---------------------------------------------------------------------------
# kb remove
# ---------------------------------------------------------------------------

class TestKBRemove:
    def test_remove(self, runner, setup_project_root):
        runner.invoke(main, ["kb", "create", "docs"])
        result = runner.invoke(main, ["kb", "remove", "docs", "--yes"])
        assert result.exit_code == 0
        assert "Removed" in result.output

    def test_remove_nonexistent(self, runner, setup_project_root):
        result = runner.invoke(main, ["kb", "remove", "ghost", "--yes"])
        assert result.exit_code != 0
        assert "does not exist" in result.output
