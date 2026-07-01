"""Container-safety contract for path/CLI assumptions (containerized-1, #332).

A bobi instance runs in a Linux container with a volume-mounted ``$HOME``
and the pinned ``claude`` CLI on ``PATH`` (see
``docs/CONTAINERIZED_DEPLOYMENT.md`` The image). Two things must hold:

1. The ``claude`` CLI is resolved from ``PATH``; the only absolute fallback is
   a macOS dev-machine convenience, never used on Linux.
2. No module hardcodes a ``/Users/...`` or ``/opt/homebrew/...`` path outside a
   macOS-guarded fallback — those would break the moment ``$HOME`` moves to a
   volume on Linux.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from bobi import sdk

PACKAGE_ROOT = Path(sdk.__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent


class TestClaudeCliResolution:
    def test_prefers_path(self, monkeypatch):
        monkeypatch.setattr(sdk.shutil, "which", lambda name: "/somewhere/bin/claude")
        assert sdk.get_cli_path() == "/somewhere/bin/claude"

    def test_linux_fallback_is_path_relative(self, monkeypatch):
        """When ``claude`` isn't found and we're not on macOS, fall back to the
        bare name so exec resolves it via ``PATH`` at spawn time — never a
        macOS-specific absolute path that doesn't exist in the container."""
        monkeypatch.setattr(sdk.shutil, "which", lambda name: None)
        monkeypatch.setattr(sdk.platform, "system", lambda: "Linux")
        resolved = sdk.get_cli_path()
        assert resolved == "claude"
        assert "/opt/homebrew" not in resolved

    def test_macos_fallback_kept_for_dev(self, monkeypatch):
        monkeypatch.setattr(sdk.shutil, "which", lambda name: None)
        monkeypatch.setattr(sdk.platform, "system", lambda: "Darwin")
        assert sdk.get_cli_path() == "/opt/homebrew/bin/claude"


def _macos_path_hits() -> list[tuple[Path, int, str]]:
    """Every source line in the package mentioning a macOS-absolute path."""
    hits = []
    for py in PACKAGE_ROOT.rglob("*.py"):
        for lineno, line in enumerate(py.read_text().splitlines(), 1):
            if "/opt/homebrew" in line or "/Users/" in line:
                hits.append((py, lineno, line))
    return hits


def _enclosing_function(tree: ast.AST, lineno: int) -> ast.AST | None:
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                if best is None or node.lineno > best.lineno:
                    best = node
    return best


class TestNoUnguardedMacosPaths:
    def test_macos_paths_are_darwin_guarded(self):
        """Any macOS-absolute path literal must live inside a function that
        also branches on the platform (``Darwin`` / ``platform.system``), so it
        can never be reached on Linux."""
        offenders = []
        for py, lineno, line in _macos_path_hits():
            tree = ast.parse(py.read_text())
            fn = _enclosing_function(tree, lineno)
            guarded = fn is not None and (
                "Darwin" in ast.get_source_segment(py.read_text(), fn)
                or "platform.system" in ast.get_source_segment(py.read_text(), fn)
            )
            if not guarded:
                offenders.append(f"{py.relative_to(PACKAGE_ROOT.parent)}:{lineno}: {line.strip()}")
        assert not offenders, "unguarded macOS-absolute paths:\n" + "\n".join(offenders)


class TestContainerCliPath:
    def test_bobi_cli_is_on_codex_sanitized_path(self):
        """Codex tool shells keep /usr/local/bin but may drop /opt/venv/bin."""
        dockerfile = (REPO_ROOT / "Dockerfile").read_text()
        assert "> /usr/local/bin/bobi" in dockerfile
        assert "/home/bobi/.local/bin/bobi" in dockerfile
        assert 'exec /opt/venv/bin/bobi "$@"' in dockerfile


class TestContainerFastembedCache:
    def test_dockerfile_uses_volume_fastembed_cache_path(self):
        """fastembed ignores HF_HOME, so first-use downloads must land in a
        writable persistent cache path."""
        dockerfile = (REPO_ROOT / "Dockerfile").read_text()
        assert "FASTEMBED_CACHE_PATH=/data/.bobi/cache/fastembed" in dockerfile
        assert "FROM python:3.11-slim AS model-baker" not in dockerfile
        assert "COPY --from=model-baker" not in dockerfile

    def test_entrypoint_prepares_fastembed_cache_path(self):
        entrypoint = (REPO_ROOT / "docker" / "docker-entrypoint.sh").read_text()
        assert '"${FASTEMBED_CACHE_PATH:-${BOBI_HOME}/cache/fastembed}"' in entrypoint
        assert '"${HF_HOME:-${BOBI_HOME}/cache/huggingface}"' in entrypoint
        assert "chown \"${APP_USER}:${APP_USER}\" \"${DATA_DIR}\" \"${BOBI_HOME}\" \"${RUN_ROOT}\"" in entrypoint
        assert '"${FASTEMBED_CACHE_PATH:-${BOBI_HOME}/cache/fastembed}"' in entrypoint
