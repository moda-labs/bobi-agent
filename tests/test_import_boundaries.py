"""Import-direction guard for the public/private split (#690 phase 1).

The dependency rule is one-way: private code imports and pins public code,
never the reverse. Concretely:

1. The public ``bobi`` package must never import ``bobi_deploy`` - the deploy
   plugin reaches into ``bobi`` (``bobi.build``, ``bobi.config``, the
   ``bobi.commands`` entry points), not the other way around.
2. The event server's public surfaces must never import the worker adapter
   (``index.ts`` / ``deployment-session.ts`` / ``internal-auth.ts`` under
   ``event-server/src/``).
3. The events-core package (``event-server/core/``) is a real package
   boundary: everything outside it consumes it by its package name
   (``@moda-labs/bobi-events-core``), never by relative path, and its own
   sources never reach outside the package by relative path. This is what
   lets the worker adapter move to the private repo consuming a pinned
   published events-core.

All boundaries are clean today; this test keeps them that way, and survives
the repo split as the public repo's permanent guard (the private-side files it
names simply stop existing, but nothing public may ever import those names).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOBI_PACKAGE = REPO_ROOT / "bobi"
EVENT_SERVER = REPO_ROOT / "event-server"
EVENT_SERVER_SRC = EVENT_SERVER / "src"
EVENT_SERVER_CORE = EVENT_SERVER / "core"
EVENT_SERVER_CORE_SRC = EVENT_SERVER_CORE / "src"
EVENT_SERVER_TEST = EVENT_SERVER / "test"

# The worker adapter: the only event-server sources allowed to know they run
# on Cloudflare (internal-auth is the Worker<->Durable Object handshake, so it
# moves private with the adapter). Module names relative to event-server/src/,
# extension stripped (see _src_module). Anchored against wrangler.jsonc below
# so a rename of the worker entry cannot silently make this guard vacuous.
WORKER_ADAPTER_MODULES = {"index", "deployment-session", "internal-auth"}


def _parsed_bobi_modules():
    """Yield ``(path, ast_tree)`` for every module in the public package.

    Shared by both Python-side guards so they can never scan different file
    sets, and asserts the scan is non-vacuous.
    """
    files = sorted(BOBI_PACKAGE.rglob("*.py"))
    assert files, f"no Python sources found under {BOBI_PACKAGE}"
    for py in files:
        yield py, ast.parse(py.read_text(encoding="utf-8"), filename=str(py))


class TestBobiNeverImportsBobiDeploy:
    def test_no_static_imports(self):
        offenders = []
        for py, tree in _parsed_bobi_modules():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if name == "bobi_deploy" or name.startswith("bobi_deploy."):
                        offenders.append(
                            f"{py.relative_to(REPO_ROOT)}:{node.lineno}: imports {name}"
                        )
        assert not offenders, (
            "public bobi/ must never import bobi_deploy "
            "(private imports public, never the reverse):\n" + "\n".join(offenders)
        )

    def test_no_dynamic_import_strings(self):
        """Catch ``importlib.import_module("bobi_deploy...")``-style references:
        a string literal that IS the private package's module path is a
        disguised import. Prose mentioning the package is not flagged - the
        whole literal must equal ``bobi_deploy`` or start with ``bobi_deploy.``."""
        offenders = []
        for py, tree in _parsed_bobi_modules():
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                if node.value == "bobi_deploy" or node.value.startswith("bobi_deploy."):
                    offenders.append(
                        f"{py.relative_to(REPO_ROOT)}:{node.lineno}: "
                        f"string literal {node.value!r}"
                    )
        assert not offenders, (
            "public bobi/ must not reference the bobi_deploy package, "
            "even dynamically:\n" + "\n".join(offenders)
        )


# Every compile unit wrangler/tsc would accept from src/ (allowJs + jsx are
# enabled in tsconfig), so a stray .js or .tsx file cannot dodge the scan.
_TS_SOURCE_GLOBS = ("*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.mts")

# Matches the specifier of every ESM import form: static imports and
# re-exports (`from "x"`, including `export * from "x"`), side-effect imports
# (`import "x"`), and dynamic imports (`import("x")`). Applied to the whole
# file text, so Prettier-wrapped specifiers on their own line still match.
_TS_IMPORT_RE = re.compile(r"""\b(?:import|from)\s*\(?\s*["']([^"']+)["']""")

_TS_EXTENSION_RE = re.compile(r"\.(?:d\.ts|tsx?|mts|mjs|jsx?)$")


def _src_module(path: Path) -> str | None:
    """Module name relative to ``event-server/src/``, extension stripped;
    ``None`` when the path lives outside src/ (and so cannot be the worker
    adapter)."""
    resolved = path.resolve()
    src = EVENT_SERVER_SRC.resolve()
    if not resolved.is_relative_to(src):
        return None
    return _TS_EXTENSION_RE.sub("", resolved.relative_to(src).as_posix())


def _ts_import_targets(ts_file: Path) -> list[tuple[int, str, Path]]:
    """(lineno, specifier, resolved-path) for each relative import in an
    event-server source file. Bare package specifiers are skipped - they
    resolve through node_modules and cannot smuggle a cross-boundary path."""
    text = ts_file.read_text(encoding="utf-8")
    targets = []
    for match in _TS_IMPORT_RE.finditer(text):
        spec = match.group(1)
        if not spec.startswith("."):
            continue
        lineno = text.count("\n", 0, match.start()) + 1
        # Bundler loader suffixes ("./x.ts?raw") address the same module.
        bare = spec.split("?")[0].split("#")[0]
        target = (ts_file.parent / bare).resolve()
        if target.is_dir():
            # Directory imports ("."/".."/"./adapters") resolve to its index.
            target = target / "index"
        targets.append((lineno, spec, target))
    return targets


def _ts_sources(*roots: Path) -> list[Path]:
    files = [
        ts for root in roots for glob in _TS_SOURCE_GLOBS for ts in sorted(root.rglob(glob))
    ]
    assert files, f"no sources found under {', '.join(str(r) for r in roots)}"
    return files


class TestEventServerCoreNeverImportsWorkerAdapter:
    def test_no_public_source_imports_worker_adapter(self):
        files = [
            ts
            for ts in _ts_sources(EVENT_SERVER_SRC, EVENT_SERVER_CORE_SRC)
            if _src_module(ts) not in WORKER_ADAPTER_MODULES
        ]
        offenders = []
        for ts in files:
            for lineno, spec, target in _ts_import_targets(ts):
                if _src_module(target) in WORKER_ADAPTER_MODULES:
                    offenders.append(
                        f"{ts.relative_to(REPO_ROOT)}:{lineno}: imports {spec!r}"
                    )
        assert not offenders, (
            "event-server core/local surfaces must never import the worker "
            "adapter (index.ts / deployment-session.ts / internal-auth.ts):\n"
            + "\n".join(offenders)
        )

    def test_no_path_aliases(self):
        """The scanner only resolves relative specifiers. That is sound only
        while no tsconfig defines path aliases; if aliases ever appear, this
        fails loudly so the scanner is taught about them instead of silently
        missing aliased adapter imports."""
        tsconfigs = [
            p
            for p in EVENT_SERVER.rglob("tsconfig*.json")
            if "node_modules" not in p.parts
        ]
        assert tsconfigs, f"no tsconfig found under {EVENT_SERVER}"
        for tsconfig in tsconfigs:
            text = tsconfig.read_text(encoding="utf-8")
            for key in ('"paths"', '"baseUrl"'):
                assert key not in text, (
                    f"{tsconfig.relative_to(REPO_ROOT)} gained {key}; extend the "
                    "import boundary scanner to resolve aliased specifiers"
                )

    def test_events_core_package_name_matches_imports(self):
        """The specifier every consumer uses must be the name the core
        package actually declares, or workspace resolution silently breaks
        at the next npm install."""
        manifest = json.loads(
            (EVENT_SERVER_CORE / "package.json").read_text(encoding="utf-8")
        )
        assert manifest["name"] == "@moda-labs/bobi-events-core"

    def test_worker_adapter_set_matches_wrangler_entry(self):
        """Keep WORKER_ADAPTER_MODULES honest: the worker entry wrangler
        actually deploys must be in the set, so renaming the adapter cannot
        leave this guard green while enforcing nothing."""
        wrangler = EVENT_SERVER / "wrangler.jsonc"
        if not wrangler.exists():
            pytest.skip("worker adapter moved to the private repo")
        match = re.search(
            r'"main"\s*:\s*"src/([^"]+)"', wrangler.read_text(encoding="utf-8")
        )
        assert match, "could not find the worker entry in wrangler.jsonc"
        assert _TS_EXTENSION_RE.sub("", match.group(1)) in WORKER_ADAPTER_MODULES


class TestEventsCorePackageBoundary:
    """The events-core package boundary is real in both directions: consumers
    reach it only by package name, and it reaches nothing outside itself.
    Either direction of relative-path leakage would silently break the phase-2
    cut, where the worker adapter consumes a pinned published events-core from
    the private repo."""

    def test_no_relative_import_into_core(self):
        core = EVENT_SERVER_CORE.resolve()
        offenders = []
        for ts in _ts_sources(EVENT_SERVER_SRC, EVENT_SERVER_TEST):
            for lineno, spec, target in _ts_import_targets(ts):
                if target.is_relative_to(core):
                    offenders.append(
                        f"{ts.relative_to(REPO_ROOT)}:{lineno}: imports {spec!r}"
                    )
        assert not offenders, (
            "consume the events-core package by its package name "
            "(@moda-labs/bobi-events-core), never by relative path:\n"
            + "\n".join(offenders)
        )

    def test_core_never_imports_outside_itself(self):
        core = EVENT_SERVER_CORE.resolve()
        offenders = []
        for ts in _ts_sources(EVENT_SERVER_CORE_SRC):
            for lineno, spec, target in _ts_import_targets(ts):
                if not target.is_relative_to(core):
                    offenders.append(
                        f"{ts.relative_to(REPO_ROOT)}:{lineno}: imports {spec!r}"
                    )
        assert not offenders, (
            "events-core must stay self-contained (publishable): no relative "
            "imports may escape event-server/core/:\n" + "\n".join(offenders)
        )
