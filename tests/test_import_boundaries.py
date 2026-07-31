"""Import-direction guard for the event-server package boundaries.

Re-aimed by the repo reorg (``plans/2026-07-29-repo-reorg.md``, Lane 1), which
brought the Cloudflare Worker adapter back into this repo as a third workspace
package. The guard previously encoded the repo split itself - "the worker
adapter is not here" - which the reorg makes false. What replaces it is the
boundary that actually earns its keep, and it is stricter, not weaker: three
sibling packages under ``event-server/`` with a one-way dependency rule.

1. The public ``bobi`` package must never import ``bobi_deploy`` - the deploy
   plugin reaches into ``bobi`` (``bobi.build``, ``bobi.config``, the
   ``bobi.commands`` entry points), not the other way around.
2. ``event-server/`` holds three packages and the dependency arrows run one
   way only::

       src/     (local runtime variants)  ─┐
                                           ├─> core/  (runtime-agnostic protocol)
       worker/  (Cloudflare adapter)      ─┘

   Neither leaf may import the other: the local variants must never import the
   Worker adapter, and the Worker must never import the local variants. Their
   only shared vocabulary is ``core``.
3. The events-core package (``event-server/core/``) is a real package boundary:
   everything outside it consumes it by its package name
   (``@moda-labs/bobi-events-core``), never by relative path, and its own
   sources never reach outside the package by relative path. That is what keeps
   core independently publishable - the property the Worker relied on while it
   lived in the private repo, and which must survive its return.

Both leaf packages are classified default-deny: every module under ``src/`` and
under ``worker/src/`` must appear in its package's expected set, so a new file
cannot silently land on the wrong side of the boundary.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import os
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
EVENT_SERVER_WORKER = EVENT_SERVER / "worker"
EVENT_SERVER_WORKER_SRC = EVENT_SERVER_WORKER / "src"
EVENT_SERVER_WORKER_TEST = EVENT_SERVER_WORKER / "test"

# The Cloudflare Worker adapter's modules, which live in their own workspace
# package (event-server/worker/) and nowhere else. Two properties are asserted
# against this set: worker/src/ contains exactly these
# (test_worker_modules_fully_classified), and none of them may reappear under
# src/, where they would be running in the local Node server that has no
# Cloudflare runtime (test_src_modules_fully_classified).
WORKER_ADAPTER_MODULES = {"index", "deployment-session", "internal-auth", "fleet"}

# The only src/ modules allowed - the local runtime variants. Every module
# present must be in this exact set (test_src_modules_fully_classified), so a
# new file cannot silently default into the local server. The two gateway
# drivers and their shared socket scaffolding are local-runtime Node modules
# with no Cloudflare knowledge.
PUBLIC_LOCAL_MODULES = {
    "local",
    "discord-gateway-local",
    "slack-socket-local",
    "socket-driver-common",
}


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


# Container-build ACTIONS. Restated by the repo reorg (Lane 1, Phase 2).
#
# This guard used to ban the words `docker|dockerfile|flyctl` anywhere under
# bobi/. That encoded #707's premise - "the image engine is deploy IP; the
# public product builds no docker images" - which the reorg reverses: the
# reference image recipe becomes public and the supervisor sidecar ships in
# this package, naming container runtimes as ordinary runtime values
# (`identity.py` returns the literal "docker" as a platform label, and several
# comments describe how docker-entrypoint.sh behaves).
#
# Bolting exceptions onto a word-ban would have hollowed it out, so the guard
# is restated to ban what actually matters: code that ASSEMBLES A BUILD CONTEXT
# OR INVOKES a container/deploy CLI. Merely naming a runtime is permitted. That
# is a narrower literal match but a stronger property - `subprocess.run(
# ["docker", "build", ...])` was never caught by the old prose regex at all,
# and is caught now by the AST pass below.
#
# The regex scan is over EVERY file under bobi/ (not just .py) so a shell
# script or template cannot dodge it; the AST pass adds call-shape detection
# that no regex over split argv lists could do.
_CONTAINER_BUILD_RE = re.compile(
    r"""
      \bdockerfile\b                                        # names a build recipe
    | \bdocker\s+(?:build|buildx|push|compose|image|save|load)\b
    | \bbuildx\b
    | \bflyctl\b
    | \bfly\s+deploy\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# argv[0] values that mean "a container/deploy CLI is being invoked".
_CONTAINER_CLIS = {"docker", "podman", "nerdctl", "buildah", "flyctl"}

# Call targets whose first argument is a command (or an argv list).
_INVOKING_CALLS = {
    "run", "Popen", "call", "check_call", "check_output",
    "which", "execvp", "execv", "execvpe", "spawnvp",
}

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


def _container_cli_invocations(tree: ast.AST) -> list[tuple[int, str]]:
    """(lineno, cli) for every call that runs a container/deploy CLI.

    Catches both argument shapes a build would use: a bare command
    (``shutil.which("docker")``) and an argv list
    (``subprocess.run(["docker", "build", ...])``). A string literal that is
    merely a value - ``return "docker"`` as a platform label - is not a call
    and is deliberately not flagged.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None
        )
        if attr not in _INVOKING_CALLS:
            continue
        # The command may arrive positionally OR as `args=`/`cmd=` - checking
        # only node.args[0] let `subprocess.run(args=["docker", "build"])`
        # through.
        candidates = list(node.args[:1])
        candidates += [
            kw.value for kw in node.keywords if kw.arg in {"args", "cmd", "command"}
        ]
        for first in candidates:
            if isinstance(first, (ast.List, ast.Tuple)):
                first = first.elts[0] if first.elts else None
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            # A shell=True call passes the whole command line as one string
            # ("docker run x"), so the CLI is the first token, not the whole
            # value. Path().name strips any directory the caller spelled out.
            token = first.value.strip().split()[0] if first.value.strip() else ""
            name = Path(token).name
            if name in _CONTAINER_CLIS:
                found.append((node.lineno, name))
                break
    return found


class TestNoContainerBuildInPublicPackage:
    """The public package may NAME a container runtime; it may not BUILD for
    one. Everything that assembles a build context or shells out to
    docker/flyctl belongs in the deploy engine, whose job that is. New
    container-build code cannot silently land here - either it moves to the
    engine, or it is genuinely dual-use and gets an explicit allowlist entry
    with its reason."""

    def _files(self):
        files = [f for f in sorted(BOBI_PACKAGE.rglob("*"))
                 if f.is_file() and "__pycache__" not in f.parts]
        assert files, f"no files found under {BOBI_PACKAGE}"
        return files

    def test_no_build_context_assembly(self):
        offenders = []
        for f in self._files():
            rel = str(f.relative_to(REPO_ROOT))
            if rel in CONTAINER_TOKEN_ALLOWLIST:
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
            for lineno, line in enumerate(text.splitlines(), 1):
                if _CONTAINER_BUILD_RE.search(line):
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")
        assert not offenders, (
            "the public package assembles a container build context - move it "
            "to the deploy engine, or allowlist the file here with its "
            "dual-use reason:\n" + "\n".join(offenders)
        )

    def test_no_container_cli_invocation(self):
        """The half a regex cannot see: an argv list whose first element is a
        container CLI. `subprocess.run(["docker", "build", "."])` splits the
        command across list elements, so the line never contains the string
        "docker build" that the scan above looks for."""
        offenders = []
        for py, tree in _parsed_bobi_modules():
            rel = str(py.relative_to(REPO_ROOT))
            if rel in CONTAINER_TOKEN_ALLOWLIST:
                continue
            for lineno, cli in _container_cli_invocations(tree):
                offenders.append(f"{rel}:{lineno}: invokes {cli!r}")
        assert not offenders, (
            "the public package shells out to a container/deploy CLI - that is "
            "the deploy engine's job:\n" + "\n".join(offenders)
        )

    def test_allowlist_entries_stay_justified(self):
        """Keep the allowlist honest in both directions: every entry must
        still exist AND still trip the scan - a stale entry is a hole the next
        offender hides in."""
        for rel in sorted(CONTAINER_TOKEN_ALLOWLIST):
            f = REPO_ROOT / rel
            assert f.is_file(), f"allowlisted {rel} no longer exists - remove it"
            assert _CONTAINER_BUILD_RE.search(
                f.read_text(encoding="utf-8", errors="ignore")
            ), f"allowlisted {rel} no longer assembles a build context - remove it"


# Every compile unit wrangler/tsc would accept from src/ (allowJs + jsx are
# enabled in tsconfig), so a stray .js or .tsx file cannot dodge the scan.
_TS_SOURCE_GLOBS = ("*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.mts")

# Matches the specifier of every ESM import form: static imports and
# re-exports (`from "x"`, including `export * from "x"`), side-effect imports
# (`import "x"`), and dynamic imports (`import("x")`). Applied to the whole
# file text, so Prettier-wrapped specifiers on their own line still match.
_TS_IMPORT_RE = re.compile(r"""\b(?:import|from)\s*\(?\s*["']([^"']+)["']""")

_TS_EXTENSION_RE = re.compile(r"\.(?:d\.ts|tsx?|mts|mjs|jsx?)$")


def _module_under(path: Path, root: Path) -> str | None:
    """Module name relative to *root*, extension stripped; ``None`` when the
    path lives outside *root*."""
    resolved = path.resolve()
    root = root.resolve()
    if not resolved.is_relative_to(root):
        return None
    return _TS_EXTENSION_RE.sub("", resolved.relative_to(root).as_posix())


def _src_module(path: Path) -> str | None:
    """Module name relative to ``event-server/src/`` (the local variants)."""
    return _module_under(path, EVENT_SERVER_SRC)


def _worker_module(path: Path) -> str | None:
    """Module name relative to ``event-server/worker/src/`` (the adapter)."""
    return _module_under(path, EVENT_SERVER_WORKER_SRC)


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


class TestLeafPackagesNeverImportEachOther:
    """``src/`` (local variants) and ``worker/`` (Cloudflare adapter) are
    sibling leaves over the same core. Neither may reach into the other: the
    local server has no Cloudflare runtime (no Durable Objects, no KV, no
    ``cloudflare:`` modules), and the Worker has no Node runtime. An import
    either way typechecks in its own compile unit and fails at runtime in the
    other, so it has to be caught here."""

    def test_local_variants_never_import_the_worker(self):
        worker_root = EVENT_SERVER_WORKER.resolve()
        offenders = _import_offenders(
            _ts_sources(EVENT_SERVER_SRC, EVENT_SERVER_CORE_SRC, EVENT_SERVER_TEST),
            lambda target: target.is_relative_to(worker_root),
        )
        assert not offenders, (
            "event-server core/local surfaces must never import the Cloudflare "
            "Worker adapter under event-server/worker/ - they share `core`, "
            "nothing else:\n" + "\n".join(offenders)
        )

    def test_worker_never_imports_the_local_variants(self):
        src_root = EVENT_SERVER_SRC.resolve()
        test_root = EVENT_SERVER_TEST.resolve()
        offenders = _import_offenders(
            _ts_sources(EVENT_SERVER_WORKER_SRC, EVENT_SERVER_WORKER_TEST),
            lambda target: target.is_relative_to(src_root)
            or target.is_relative_to(test_root),
        )
        assert not offenders, (
            "the Worker adapter must never import the local runtime variants "
            "under event-server/src/ - they share `core`, nothing else:\n"
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
        """Default-deny for new files under src/: it holds exactly the local
        runtime variants. A Worker-adapter module appearing here means code
        written against the Cloudflare runtime has landed in the Node server,
        which fails at runtime rather than at build."""
        modules = {_src_module(ts) for ts in _ts_sources(EVENT_SERVER_SRC)}
        assert modules == PUBLIC_LOCAL_MODULES, (
            "unexpected module under event-server/src/ - the Worker adapter "
            "belongs in event-server/worker/src/; a genuinely local-runtime "
            f"module must be added to PUBLIC_LOCAL_MODULES (found: {sorted(modules)})"
        )

    def test_worker_modules_fully_classified(self):
        """The same default-deny on the other leaf. Without this, the Worker
        package is an unscanned hole: every other assertion in this file is
        scoped to src/ or core/, so a module dropped into worker/src/ would be
        invisible to all of them."""
        modules = {_worker_module(ts) for ts in _ts_sources(EVENT_SERVER_WORKER_SRC)}
        assert modules == WORKER_ADAPTER_MODULES, (
            "unexpected module under event-server/worker/src/ - a genuinely "
            "Cloudflare-runtime module must be added to WORKER_ADAPTER_MODULES "
            f"in this test (found: {sorted(modules)})"
        )


class TestEventsCorePackageBoundary:
    """The events-core package boundary is real in both directions: consumers
    reach it only by package name, and it reaches nothing outside itself.

    This is the property that let the Worker adapter live in a separate repo
    consuming a published events-core, and it does not relax now that the
    Worker is a sibling workspace. Sharing a repo makes a relative path into
    ``core/`` resolve where it previously could not - which is exactly why the
    Worker's sources are scanned here alongside the local variants. Core stays
    independently publishable or it is not a package."""

    def test_no_relative_import_into_core(self):
        core = EVENT_SERVER_CORE.resolve()
        offenders = _import_offenders(
            _ts_sources(
                EVENT_SERVER_SRC,
                EVENT_SERVER_TEST,
                EVENT_SERVER_WORKER_SRC,
                EVENT_SERVER_WORKER_TEST,
            ),
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
        or stale subpath resolves nowhere at the next npm install. The
        workspace is forgiving enough to hide this until an install runs, so
        it is asserted rather than waited for."""
        manifest = _core_manifest()
        name, exports = manifest["name"], manifest["exports"]
        offenders, core_imports = [], 0
        for ts in _ts_sources(
            EVENT_SERVER_SRC,
            EVENT_SERVER_TEST,
            EVENT_SERVER_WORKER_SRC,
            EVENT_SERVER_WORKER_TEST,
        ):
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


# ---------------------------------------------------------------------------
# Non-vacuity: the guards above must FAIL on code that genuinely violates them.
#
# A boundary guard that passes because it scans the wrong tree is worse than no
# guard, because it reads as proof. That is not hypothetical here: this file's
# previous incarnation stayed green when the Worker package landed, purely
# because every scan was scoped to src/ and core/ and never saw worker/.
#
# Each test plants a real violation in the real tree, asserts the specific
# guard rejects it, and removes it. The fixture's teardown is what guarantees
# removal even when an assertion fails mid-test.
# ---------------------------------------------------------------------------


@pytest.fixture
def planted():
    """Write files into the repo for one test, then remove them."""
    written: list[Path] = []

    def _plant(rel: str, body: str) -> Path:
        path = REPO_ROOT / rel
        assert not path.exists(), f"{rel} already exists - pick another mutant path"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        written.append(path)
        return path

    yield _plant
    for path in written:
        path.unlink(missing_ok=True)


def _rejects(check, *args):
    """The guard raised AssertionError - i.e. it caught the mutant."""
    with pytest.raises(AssertionError):
        check(*args)


class TestBoundaryGuardsAreNonVacuous:
    def test_unclassified_worker_module_is_rejected(self, planted):
        planted("event-server/worker/src/_mutant.ts", "export const x = 1;\n")
        _rejects(TestLeafPackagesNeverImportEachOther().test_worker_modules_fully_classified)

    def test_unclassified_local_module_is_rejected(self, planted):
        planted("event-server/src/_mutant.ts", "export const x = 1;\n")
        _rejects(TestLeafPackagesNeverImportEachOther().test_src_modules_fully_classified)

    def test_worker_reaching_into_core_by_relative_path_is_rejected(self, planted):
        planted(
            "event-server/worker/src/_mutant.ts",
            'import { x } from "../../core/src/core";\nexport const y = x;\n',
        )
        _rejects(TestEventsCorePackageBoundary().test_no_relative_import_into_core)

    def test_local_variant_importing_the_worker_is_rejected(self, planted):
        planted(
            "event-server/src/_mutant.ts",
            'import { f } from "../worker/src/fleet";\nexport const y = f;\n',
        )
        _rejects(TestLeafPackagesNeverImportEachOther().test_local_variants_never_import_the_worker)

    def test_worker_importing_a_local_variant_is_rejected(self, planted):
        planted(
            "event-server/worker/src/_mutant.ts",
            'import { s } from "../../src/socket-driver-common";\nexport const y = s;\n',
        )
        _rejects(TestLeafPackagesNeverImportEachOther().test_worker_never_imports_the_local_variants)

    def test_tsconfig_path_alias_is_rejected(self, planted):
        planted(
            "event-server/worker/tsconfig.mutant.json",
            '{"compilerOptions": {"baseUrl": "."}}\n',
        )
        _rejects(TestLeafPackagesNeverImportEachOther().test_no_path_aliases)


class TestContainerGuardIsNonVacuous:
    """The container guard's two halves, proven in BOTH directions: it catches
    real build/invoke code, and it does NOT catch merely naming a runtime -
    which is what the supervisor legitimately does (identity.py returns the
    literal "docker" as a platform label)."""

    @pytest.mark.parametrize("body", [
        'import subprocess\ndef b(c):\n    subprocess.run(["docker", "build", "-t", "x", c])\n',
        'import subprocess\ndef b(c):\n    subprocess.run(args=["docker", "build", c])\n',
        'import subprocess\ndef b():\n    subprocess.run("docker run x", shell=True)\n',
        'import shutil\ndef h():\n    return shutil.which("flyctl") is not None\n',
    ], ids=["argv-list", "args-keyword", "shell-string", "which"])
    def test_invoking_a_container_cli_is_rejected(self, planted, body):
        planted("bobi/_mutant_probe.py", body)
        _rejects(TestNoContainerBuildInPublicPackage().test_no_container_cli_invocation)

    def test_assembling_a_build_context_is_rejected(self, planted):
        planted("bobi/_mutant_probe.py", 'def s(c):\n    (c / "Dockerfile").write_text("FROM scratch\\n")\n')
        _rejects(TestNoContainerBuildInPublicPackage().test_no_build_context_assembly)

    def test_a_non_python_file_cannot_dodge_the_scan(self, planted):
        planted("bobi/_mutant_probe.sh", '#!/bin/sh\nflyctl deploy --app "$1"\n')
        _rejects(TestNoContainerBuildInPublicPackage().test_no_build_context_assembly)

    def test_merely_naming_a_runtime_is_ALLOWED(self, planted):
        """The other direction, and the reason the guard was restated rather
        than exception-listed: this is the supervisor's own pattern and it must
        stay legal, or the guard is just a word ban with a longer allowlist."""
        planted(
            "bobi/_mutant_probe.py",
            'def platform_name():\n'
            '    # A container without either signal: plain `docker run` under an\n'
            '    # unknown runtime. See docker-entrypoint.sh for the root choice.\n'
            '    return "docker"\n',
        )
        TestNoContainerBuildInPublicPackage().test_no_build_context_assembly()
        TestNoContainerBuildInPublicPackage().test_no_container_cli_invocation()
