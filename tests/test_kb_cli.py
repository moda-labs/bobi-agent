"""Unit tests for named KB CLI commands."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from bobi import paths
from bobi.cli import main

# Every KB CLI command drives the live store (sqlite-vec, from the optional [kb]
# extra). Skip the module cleanly on a `.[dev]`-only install instead of failing.
pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("sqlite_vec") is None,
    reason="kb extra not installed (pip install '.[kb]')",
)


@pytest.fixture(autouse=True)
def setup_project_root(tmp_path, monkeypatch):
    """Set project root and redirect KB storage for all CLI tests."""
    home = tmp_path / "home"
    root = home / "agents" / "test" / "run"
    (root / "package").mkdir(parents=True)
    (root / "package" / "agent.yaml").write_text("entry_point: test\n")
    (root / "state").mkdir()

    paths.bind_root(None)
    monkeypatch.setenv("BOBI_HOME", str(home))
    yield root
    paths.bind_root(None)


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# kb create
# ---------------------------------------------------------------------------

class TestKBCreate:
    def test_creates_kb(self, runner, setup_project_root):
        result = runner.invoke(main, ["agent", "test", "kb", "create", "docs"])
        assert result.exit_code == 0
        assert "Created KB 'docs'" in result.output

    def test_duplicate_fails(self, runner, setup_project_root):
        runner.invoke(main, ["agent", "test", "kb", "create", "docs"])
        result = runner.invoke(main, ["agent", "test", "kb", "create", "docs"])
        assert result.exit_code != 0
        assert "already exists" in result.output


# ---------------------------------------------------------------------------
# kb add
# ---------------------------------------------------------------------------

class TestKBAdd:
    def test_add_text(self, runner, setup_project_root):
        runner.invoke(main, ["agent", "test", "kb", "create", "docs"])
        with patch("bobi.kb.embedder.embed", return_value=[[0.1] * 384]):
            result = runner.invoke(main, ["agent", "test", "kb", "add", "docs", "--text", "Hello world"])
        assert result.exit_code == 0
        assert "Added" in result.output

    def test_add_file(self, runner, setup_project_root, tmp_path):
        runner.invoke(main, ["agent", "test", "kb", "create", "docs"])
        f = tmp_path / "test.md"
        f.write_text("File content here")
        with patch("bobi.kb.embedder.embed", return_value=[[0.1] * 384]):
            result = runner.invoke(main, ["agent", "test", "kb", "add", "docs", "--file", str(f)])
        assert result.exit_code == 0
        assert "Added" in result.output

    def test_add_no_options(self, runner, setup_project_root):
        runner.invoke(main, ["agent", "test", "kb", "create", "docs"])
        result = runner.invoke(main, ["agent", "test", "kb", "add", "docs"])
        assert result.exit_code != 0
        assert "Provide --file or --text" in result.output

    def test_add_nonexistent_kb(self, runner, setup_project_root):
        with patch("bobi.kb.embedder.embed"):
            result = runner.invoke(main, ["agent", "test", "kb", "add", "ghost", "--text", "hello"])
        assert result.exit_code != 0
        assert "does not exist" in result.output


# ---------------------------------------------------------------------------
# kb search
# ---------------------------------------------------------------------------

class TestKBSearch:
    def test_search_fts(self, runner, setup_project_root):
        runner.invoke(main, ["agent", "test", "kb", "create", "docs"])
        with patch("bobi.kb.embedder.embed", return_value=[[0.1] * 384]):
            runner.invoke(main, ["agent", "test", "kb", "add", "docs", "--text", "Python programming language"])
        result = runner.invoke(main, ["agent", "test", "kb", "search", "docs", "Python", "--mode", "fts"])
        assert result.exit_code == 0
        assert "Python" in result.output

    def test_search_no_results(self, runner, setup_project_root):
        runner.invoke(main, ["agent", "test", "kb", "create", "docs"])
        result = runner.invoke(main, ["agent", "test", "kb", "search", "docs", "xyznotfound", "--mode", "fts"])
        assert result.exit_code == 0
        assert "No results" in result.output

    def test_search_nonexistent_kb(self, runner, setup_project_root):
        with patch("bobi.kb.embedder.embed"):
            result = runner.invoke(main, ["agent", "test", "kb", "search", "ghost", "query", "--mode", "fts"])
        assert result.exit_code != 0
        assert "does not exist" in result.output


class TestRecallMemory:
    def test_recall_memory_searches_cold_kb(self, runner, setup_project_root):
        runner.invoke(main, ["agent", "test", "kb", "create", "long_term_memory"])
        with patch("bobi.kb.embedder.embed", return_value=[[0.1] * 384]):
            runner.invoke(
                main,
                [
                    "agent", "test", "kb", "add", "long_term_memory",
                    "--text", "Cold memory says use the release runbook",
                ],
            )
            result = runner.invoke(
                main,
                ["agent", "test", "recall-memory", "release runbook", "--limit", "3"],
            )

        assert result.exit_code == 0
        assert "release runbook" in result.output

    def test_recall_memory_without_index_is_empty(self, runner, setup_project_root):
        result = runner.invoke(main, ["agent", "test", "recall-memory", "anything"])

        assert result.exit_code == 0
        assert "No cold memory index yet" in result.output

    def test_recall_memory_rejects_negative_limit(self, runner, setup_project_root):
        result = runner.invoke(
            main,
            ["agent", "test", "recall-memory", "anything", "--limit", "-1"],
        )

        assert result.exit_code != 0
        assert "Invalid value for '--limit'" in result.output


# ---------------------------------------------------------------------------
# kb list
# ---------------------------------------------------------------------------

class TestKBList:
    def test_list_empty(self, runner, setup_project_root):
        result = runner.invoke(main, ["agent", "test", "kb", "list"])
        assert result.exit_code == 0
        assert "No knowledge bases" in result.output

    def test_list_with_kbs(self, runner, setup_project_root):
        runner.invoke(main, ["agent", "test", "kb", "create", "alpha"])
        runner.invoke(main, ["agent", "test", "kb", "create", "beta"])
        result = runner.invoke(main, ["agent", "test", "kb", "list"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" in result.output


# ---------------------------------------------------------------------------
# kb info
# ---------------------------------------------------------------------------

class TestKBInfo:
    def test_info(self, runner, setup_project_root):
        runner.invoke(main, ["agent", "test", "kb", "create", "docs"])
        result = runner.invoke(main, ["agent", "test", "kb", "info", "docs"])
        assert result.exit_code == 0
        assert "docs" in result.output
        assert "Entries:" in result.output

    def test_info_nonexistent(self, runner, setup_project_root):
        result = runner.invoke(main, ["agent", "test", "kb", "info", "ghost"])
        assert result.exit_code != 0
        assert "does not exist" in result.output


# ---------------------------------------------------------------------------
# kb remove
# ---------------------------------------------------------------------------

class TestKBRemove:
    def test_remove(self, runner, setup_project_root):
        runner.invoke(main, ["agent", "test", "kb", "create", "docs"])
        result = runner.invoke(main, ["agent", "test", "kb", "remove", "docs", "--yes"])
        assert result.exit_code == 0
        assert "Removed" in result.output

    def test_remove_nonexistent(self, runner, setup_project_root):
        result = runner.invoke(main, ["agent", "test", "kb", "remove", "ghost", "--yes"])
        assert result.exit_code != 0
        assert "does not exist" in result.output
