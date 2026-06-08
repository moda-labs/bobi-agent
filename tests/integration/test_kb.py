"""Integration tests for modastack kb — end-to-end with real embedding model.

Exercises the full KB lifecycle: create, add (text + file), search (FTS +
hybrid), info, list, and remove. The embedding sidecar auto-starts on first
add/search and uses the real sentence-transformers model.
"""

import os
import signal
import time
from pathlib import Path

import pytest


class TestKBLifecycle:
    """Full create → add → search → info → remove lifecycle."""

    def test_create_kb(self, cli_run):
        result = cli_run("kb", "create", "test-docs")
        assert result.returncode == 0
        assert "Created KB" in result.stdout

    def test_create_duplicate_fails(self, cli_run):
        cli_run("kb", "create", "dup-test")
        result = cli_run("kb", "create", "dup-test")
        assert result.returncode != 0
        assert "already exists" in result.stderr

    def test_add_text(self, cli_run):
        cli_run("kb", "create", "add-test")
        result = cli_run(
            "kb", "add", "add-test",
            "--text", "Python is a popular programming language used for web development and data science",
            timeout=60,
        )
        assert result.returncode == 0
        assert "Added" in result.stdout

    def test_add_file(self, cli_run, modastack_env):
        cli_run("kb", "create", "file-test")
        doc = modastack_env.project_path / "test-doc.md"
        doc.write_text(
            "# Architecture\n\n"
            "The system uses event-driven architecture with pub/sub messaging.\n\n"
            "Each agent subscribes to topics and reacts to events autonomously.\n\n"
            "Workflows are defined as YAML DAGs with three step types."
        )
        result = cli_run(
            "kb", "add", "file-test", "--file", str(doc),
            timeout=60,
        )
        assert result.returncode == 0
        assert "Added" in result.stdout

    def test_add_file_dedup(self, cli_run, modastack_env):
        cli_run("kb", "create", "dedup-test")
        doc = modastack_env.project_path / "dedup-doc.md"
        doc.write_text("Content that should not be duplicated on re-add.")
        cli_run("kb", "add", "dedup-test", "--file", str(doc), timeout=60)
        result = cli_run("kb", "add", "dedup-test", "--file", str(doc), timeout=60)
        assert result.returncode == 0
        assert "unchanged" in result.stdout.lower()

    def test_search_fts(self, cli_run):
        cli_run("kb", "create", "search-fts")
        cli_run(
            "kb", "add", "search-fts",
            "--text", "Kubernetes orchestrates containerized applications across clusters",
            timeout=60,
        )
        result = cli_run(
            "kb", "search", "search-fts", "Kubernetes", "--mode", "fts",
        )
        assert result.returncode == 0
        assert "Kubernetes" in result.stdout

    def test_search_fts_no_results(self, cli_run):
        cli_run("kb", "create", "search-empty")
        result = cli_run(
            "kb", "search", "search-empty", "nonexistent", "--mode", "fts",
        )
        assert result.returncode == 0
        assert "No results" in result.stdout

    def test_search_hybrid(self, cli_run):
        cli_run("kb", "create", "search-hybrid")
        cli_run(
            "kb", "add", "search-hybrid",
            "--text", "Machine learning models require training data and compute resources",
            timeout=60,
        )
        result = cli_run(
            "kb", "search", "search-hybrid", "AI training", "--mode", "hybrid",
            timeout=60,
        )
        assert result.returncode == 0
        assert "machine learning" in result.stdout.lower() or "training" in result.stdout.lower()

    def test_info(self, cli_run):
        cli_run("kb", "create", "info-test")
        cli_run(
            "kb", "add", "info-test",
            "--text", "Some content for info test",
            timeout=60,
        )
        result = cli_run("kb", "info", "info-test")
        assert result.returncode == 0
        assert "info-test" in result.stdout
        assert "Entries:" in result.stdout

    def test_list(self, cli_run):
        cli_run("kb", "create", "list-alpha")
        cli_run("kb", "create", "list-beta")
        result = cli_run("kb", "list")
        assert result.returncode == 0
        assert "list-alpha" in result.stdout
        assert "list-beta" in result.stdout

    def test_list_empty(self, cli_run, modastack_env):
        kb_dir = modastack_env.project_path / ".modastack" / "kb"
        had_files = list(kb_dir.glob("list-*")) if kb_dir.exists() else []
        result = cli_run("kb", "list")
        assert result.returncode == 0

    def test_remove(self, cli_run):
        cli_run("kb", "create", "remove-test")
        result = cli_run("kb", "remove", "remove-test", "--yes")
        assert result.returncode == 0
        assert "Removed" in result.stdout

    def test_remove_nonexistent(self, cli_run):
        result = cli_run("kb", "remove", "ghost-kb", "--yes")
        assert result.returncode != 0
        assert "does not exist" in result.stderr

    def test_search_nonexistent_kb(self, cli_run):
        result = cli_run("kb", "search", "nope-kb", "query", "--mode", "fts")
        assert result.returncode != 0
        assert "does not exist" in result.stderr


class TestEmbeddingSidecar:
    """Verify the sidecar auto-starts and can be stopped."""

    def test_sidecar_starts_on_add(self, cli_run, modastack_env):
        cli_run("kb", "create", "sidecar-test")
        result = cli_run(
            "kb", "add", "sidecar-test",
            "--text", "Testing sidecar auto-start",
            timeout=60,
        )
        assert result.returncode == 0

        pid_file = modastack_env.state_dir / "embedding-sidecar.pid"
        port_file = modastack_env.state_dir / "embedding-sidecar.port"
        assert pid_file.exists()
        assert port_file.exists()

        pid = int(pid_file.read_text().strip())
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        assert alive, "Sidecar should still be running after add"

    def test_sidecar_survives_between_commands(self, cli_run, modastack_env):
        cli_run("kb", "create", "survive-test")
        cli_run(
            "kb", "add", "survive-test",
            "--text", "First command starts sidecar",
            timeout=60,
        )

        pid_file = modastack_env.state_dir / "embedding-sidecar.pid"
        first_pid = int(pid_file.read_text().strip())

        cli_run(
            "kb", "add", "survive-test",
            "--text", "Second command reuses sidecar",
            timeout=60,
        )

        second_pid = int(pid_file.read_text().strip())
        assert first_pid == second_pid, "Sidecar should be reused, not restarted"

    def test_stop_kills_sidecar(self, cli_run, modastack_env):
        cli_run("kb", "create", "stop-test")
        cli_run(
            "kb", "add", "stop-test",
            "--text", "Content before stop",
            timeout=60,
        )

        pid_file = modastack_env.state_dir / "embedding-sidecar.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            result = cli_run("stop")
            time.sleep(1)

            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            assert not alive, "Sidecar should be dead after modastack stop"
