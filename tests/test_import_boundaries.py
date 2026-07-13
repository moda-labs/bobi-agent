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
import fnmatch
import json
import os
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BOBI_PACKAGE = REPO_ROOT / "bobi"
EVENT_SERVER = REPO_ROOT / "event-server"
EVENT_SERVER_SRC = EVENT_SERVER / "src"
EVENT_SERVER_CORE = EVENT_SERVER / "core"
EVENT_SERVER_CORE_SRC = EVENT_SERVER_CORE / "src"
EVENT_SERVER_TEST = EVENT_SERVER / "test"

# The worker adapter moved to the private deploy repo at the phase-2 cut
# (index.ts, deployment-session.ts, internal-auth.ts + wrangler.jsonc). The
# module names stay guarded here so a copy of the adapter reappearing under
# src/ is rejected as unclassified-private rather than silently public.
WORKER_ADAPTER_MODULES = {"index", "deployment-session", "internal-auth"}

# The only src/ modules allowed in the public repo. Together with
# WORKER_ADAPTER_MODULES this bounds src/ - every module present must be in
# one of the two sets and the worker set must stay ABSENT
# (test_src_modules_fully_classified), so new files cannot silently default
# to public. discord-gateway-local is the local runtime's Discord Gateway
# driver (#2) - Node `ws` client, no Cloudflare knowledge.
PUBLIC_LOCAL_MODULES = {"local", "discord-gateway-local"}


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


# Container-build tooling tokens that mark deploy IP. The scan is over EVERY
# file under bobi/ (not just .py) so a shell script or template cannot dodge
# it. Each allowlisted file carries the dual-use reason that keeps it public.
_CONTAINER_TOKEN_RE = re.compile(r"\b(docker|dockerfile|flyctl)\b", re.IGNORECASE)
CONTAINER_TOKEN_ALLOWLIST = {
    # Renders the team-deps hook script per the container contract (the
    # guide-dep bootstrap runs `python -m bobi.dep_bootstrap` INSIDE the
    # image, whose installed bobi is the public wheel). Renders shell, never
    # invokes docker.
    "bobi/build_render.py",
    # The public agent.yaml schema: BuildSpec documents the raw-Dockerfile
    # escape hatch a team may declare next to its agent.yaml.
    "bobi/config.py",
}


class TestNoContainerBuildInPublicPackage:
    """The image engine is deploy IP (#707): everything that assembles a
    docker build context or invokes docker lives in the deploy plugin. New
    container-build code cannot silently land public - either it belongs in
    bobi_deploy/, or it is genuinely dual-use and gets an explicit allowlist
    entry here with its reason."""

    def _files(self):
        files = [f for f in sorted(BOBI_PACKAGE.rglob("*"))
                 if f.is_file() and "__pycache__" not in f.parts]
        assert files, f"no files found under {BOBI_PACKAGE}"
        return files

    def test_no_container_build_tokens(self):
        offenders = []
        for f in self._files():
            rel = str(f.relative_to(REPO_ROOT))
            if rel in CONTAINER_TOKEN_ALLOWLIST:
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
            for lineno, line in enumerate(text.splitlines(), 1):
                if _CONTAINER_TOKEN_RE.search(line):
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")
        assert not offenders, (
            "container-build tokens in the public package - move the code to "
            "bobi_deploy/, or allowlist the file here with its dual-use "
            "reason:\n" + "\n".join(offenders)
        )

    def test_allowlist_entries_stay_justified(self):
        """Keep the allowlist honest in both directions: every entry must
        still exist AND still contain a token - a stale entry is a hole the
        next offender hides in."""
        for rel in sorted(CONTAINER_TOKEN_ALLOWLIST):
            f = REPO_ROOT / rel
            assert f.is_file(), f"allowlisted {rel} no longer exists - remove it"
            assert _CONTAINER_TOKEN_RE.search(
                f.read_text(encoding="utf-8", errors="ignore")
            ), f"allowlisted {rel} has no container tokens left - remove it"


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
    event-server source file. Bare package specifiers resolve through
    node_modules and cannot smuggle a cross-boundary path; they are checked
    separately via _ts_bare_specifiers."""
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


def _ts_bare_specifiers(ts_file: Path) -> list[tuple[int, str]]:
    """(lineno, specifier) for each bare (non-relative) import in an
    event-server source file."""
    text = ts_file.read_text(encoding="utf-8")
    return [
        (text.count("\n", 0, m.start()) + 1, m.group(1))
        for m in _TS_IMPORT_RE.finditer(text)
        if not m.group(1).startswith(".")
    ]


def _package_of(spec: str) -> str:
    """The package portion of a bare specifier (drops any subpath)."""
    parts = spec.split("/")
    return "/".join(parts[:2]) if spec.startswith("@") else parts[0]


def _ts_sources(*roots: Path) -> list[Path]:
    files = [
        ts for root in roots for glob in _TS_SOURCE_GLOBS for ts in sorted(root.rglob(glob))
    ]
    assert files, f"no sources found under {', '.join(str(r) for r in roots)}"
    return files


def _import_offenders(files, predicate) -> list[str]:
    """Offender lines for every relative import whose resolved target
    violates *predicate*. Shared by all boundary scans so they can never
    drift apart in specifier handling or reporting."""
    return [
        f"{ts.relative_to(REPO_ROOT)}:{lineno}: imports {spec!r}"
        for ts in files
        for lineno, spec, target in _ts_import_targets(ts)
        if predicate(target)
    ]


def _core_manifest() -> dict:
    return json.loads((EVENT_SERVER_CORE / "package.json").read_text(encoding="utf-8"))


class TestEventServerCoreNeverImportsWorkerAdapter:
    def test_no_public_source_imports_worker_adapter(self):
        files = [
            ts
            for ts in _ts_sources(EVENT_SERVER_SRC, EVENT_SERVER_CORE_SRC)
            if _src_module(ts) not in WORKER_ADAPTER_MODULES
        ]
        offenders = _import_offenders(
            files, lambda target: _src_module(target) in WORKER_ADAPTER_MODULES
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
        tsconfigs = []
        for root, dirs, names in os.walk(EVENT_SERVER):
            dirs[:] = [d for d in dirs if d != "node_modules"]
            tsconfigs += [
                Path(root) / n for n in names if fnmatch.fnmatch(n, "tsconfig*.json")
            ]
        assert tsconfigs, f"no tsconfig found under {EVENT_SERVER}"
        for tsconfig in tsconfigs:
            text = tsconfig.read_text(encoding="utf-8")
            for key in ('"paths"', '"baseUrl"'):
                assert key not in text, (
                    f"{tsconfig.relative_to(REPO_ROOT)} gained {key}; extend the "
                    "import boundary scanner to resolve aliased specifiers"
                )

    def test_src_modules_fully_classified(self):
        """Default-deny for new files under src/: post-cut, src/ holds
        exactly the public local runtime modules. A worker-adapter module
        reappearing here, or any unclassified new module, fails loudly
        instead of silently landing in the public repo."""
        modules = {_src_module(ts) for ts in _ts_sources(EVENT_SERVER_SRC)}
        assert modules == PUBLIC_LOCAL_MODULES, (
            "unexpected module under event-server/src/ - the worker adapter "
            "lives in the private deploy repo; a genuinely public module "
            "must be added to PUBLIC_LOCAL_MODULES in this test"
        )


class TestEventsCorePackageBoundary:
    """The events-core package boundary is real in both directions: consumers
    reach it only by package name, and it reaches nothing outside itself.
    Either direction of relative-path leakage would silently break the phase-2
    cut, where the worker adapter consumes a pinned published events-core from
    the private repo."""

    def test_no_relative_import_into_core(self):
        core = EVENT_SERVER_CORE.resolve()
        offenders = _import_offenders(
            _ts_sources(EVENT_SERVER_SRC, EVENT_SERVER_TEST),
            lambda target: target.is_relative_to(core),
        )
        assert not offenders, (
            "consume the events-core package by its package name "
            "(@moda-labs/bobi-events-core), never by relative path:\n"
            + "\n".join(offenders)
        )

    def test_core_never_imports_outside_itself(self):
        core = EVENT_SERVER_CORE.resolve()
        offenders = _import_offenders(
            _ts_sources(EVENT_SERVER_CORE_SRC),
            lambda target: not target.is_relative_to(core),
        )
        assert not offenders, (
            "events-core must stay self-contained (publishable): no relative "
            "imports may escape event-server/core/:\n" + "\n".join(offenders)
        )

    def test_consumer_specifiers_match_core_manifest(self):
        """Every import of the events-core package must use the exact name
        the manifest declares and a subpath its exports map serves - a typo
        or stale subpath resolves nowhere at the next npm install, and after
        the phase-2 cut the private repo's pinned install fails the same way."""
        manifest = _core_manifest()
        name, exports = manifest["name"], manifest["exports"]
        offenders, core_imports = [], 0
        for ts in _ts_sources(EVENT_SERVER_SRC, EVENT_SERVER_TEST):
            for lineno, spec in _ts_bare_specifiers(ts):
                if not spec.startswith("@moda-labs/"):
                    continue
                core_imports += 1
                subpath = "." + spec.removeprefix(name)
                if _package_of(spec) != name or subpath not in exports:
                    offenders.append(
                        f"{ts.relative_to(REPO_ROOT)}:{lineno}: imports {spec!r}"
                    )
        assert core_imports, "no events-core imports found - scan is vacuous"
        assert not offenders, (
            f"specifier does not match {name}'s manifest name + exports "
            f"({sorted(exports)}):\n" + "\n".join(offenders)
        )

    def test_core_bare_imports_are_declared_dependencies(self):
        """Publishability, dependency half: hoisting lets core sources
        resolve any package the workspace root happens to install, so an
        undeclared import passes tsc and vitest here but breaks the
        published package's consumers. Runtime builtins (node:, cloudflare:)
        are deliberately not allowed either - core is the runtime-agnostic
        tier."""
        declared = set(_core_manifest().get("dependencies", {}))
        offenders = [
            f"{ts.relative_to(REPO_ROOT)}:{lineno}: imports {spec!r}"
            for ts in _ts_sources(EVENT_SERVER_CORE_SRC)
            for lineno, spec in _ts_bare_specifiers(ts)
            if _package_of(spec) not in declared
        ]
        assert not offenders, (
            "events-core imports packages its own manifest does not declare "
            f"(declared: {sorted(declared)}):\n" + "\n".join(offenders)
        )
