"""Guard the Hatch distribution boundary and custom artifact hook.

`python -m build` builds the wheel FROM the sdist. So every path the wheel
force-includes must also live in the sdist, or the wheel build dies with
"Forced include not found" (this happened for 0.22.0 when bundled-template
dirs under agents/ were force-included into the wheel but agents/ was missing
from the sdist include list).

The deploy-asset guards (Dockerfile, docker/, deploy scripts) live in
bobi_deploy/tests/test_packaging.py - those assets ship in the deploy plugin's
wheel, not this one (#707).
"""
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

import hatch_build
from bobi.events import artifact as event_server_artifact
from hatchling.builders.sdist import SdistBuilder
from hatchling.builders.wheel import WheelBuilder

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
PROJECT_ROOT = PYPROJECT.parent


def _config():
    return tomllib.loads(PYPROJECT.read_text())


def test_dev_extra_provisions_distribution_test_toolchain():
    dev_dependencies = set(_config()["project"]["optional-dependencies"]["dev"])

    assert {"build", "hatchling"} <= dev_dependencies


def test_artifact_contract_load_does_not_write_bytecode(tmp_path, monkeypatch):
    module_name = "_bobi_event_server_artifact"
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    module_path = tmp_path / "bobi" / "events" / "artifact.py"
    module_path.parent.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "bobi" / "events" / "artifact.py", module_path)

    loaded = hatch_build._load_artifact_module(tmp_path)

    assert loaded.BUNDLE_NAME == event_server_artifact.BUNDLE_NAME
    assert module_name not in sys.modules
    assert not list(tmp_path.rglob("__pycache__"))
    assert not list(tmp_path.rglob("*.pyc"))


def test_artifact_contract_load_restores_previous_module_on_base_exception(
    tmp_path, monkeypatch,
):
    module_name = "_bobi_event_server_artifact"
    previous = ModuleType(module_name)
    monkeypatch.setitem(sys.modules, module_name, previous)
    module_path = tmp_path / "bobi" / "events" / "artifact.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text("raise KeyboardInterrupt('stop')\n")

    with pytest.raises(KeyboardInterrupt, match="stop"):
        hatch_build._load_artifact_module(tmp_path)

    assert sys.modules[module_name] is previous


def test_sdist_excludes_install_state_recursively(tmp_path):
    shutil.copy2(PYPROJECT, tmp_path / "pyproject.toml")
    sentinels = [
        tmp_path / "event-server" / ".npm-cache" / "_logs" / "root.log",
        tmp_path / "event-server" / "core" / ".npm-cache" / "_logs" / "core.log",
        (
            tmp_path
            / "event-server"
            / "core"
            / "src"
            / ".npm-cache"
            / "_logs"
            / "nested.log"
        ),
        tmp_path / "event-server" / "node_modules" / "root.js",
        tmp_path / "event-server" / "core" / "node_modules" / "core.js",
        (
            tmp_path
            / "event-server"
            / "core"
            / "src"
            / "node_modules"
            / "nested.js"
        ),
    ]
    for sentinel in sentinels:
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("legacy runtime cache")

    selected = {
        included.relative_path
        for included in SdistBuilder(str(tmp_path)).recurse_selected_project_files()
    }

    expected_excluded = {
        sentinel.relative_to(tmp_path).as_posix()
        for sentinel in sentinels
    }
    assert selected.isdisjoint(expected_excluded)


def test_direct_wheel_static_force_includes_do_not_recurse_install_state(
    tmp_path,
):
    shutil.copy2(PYPROJECT, tmp_path / "pyproject.toml")
    (tmp_path / "skills").mkdir()
    event_server_dir = tmp_path / "event-server"
    for relative in (
        "package.json",
        "package-lock.json",
        "tsconfig.json",
        "core/package.json",
        "src/local.ts",
        "core/src/core.ts",
        "src/.npm-cache/_logs/cache.log",
        "src/.npm-cache/poison.ts",
        "core/src/node_modules/poison.ts",
    ):
        path = event_server_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)

    builder = WheelBuilder(str(tmp_path))
    selected = {
        included.distribution_path
        for included in builder.recurse_forced_files(builder.config.force_include)
    }

    assert not any(
        "/.npm-cache/" in path or "/node_modules/" in path
        for path in selected
    )


def test_every_wheel_force_include_is_in_sdist():
    cfg = _config()
    wheel = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]
    sdist_include = cfg["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]

    force_include = wheel.get("force-include", {})
    # Top-level path segment each sdist include entry covers.
    sdist_roots = {entry.split("/", 1)[0] for entry in sdist_include}

    missing = []
    for src in force_include:
        root = src.split("/", 1)[0]
        if root not in sdist_roots:
            missing.append(src)

    assert not missing, (
        f"wheel force-include source(s) not covered by sdist include "
        f"(build-from-sdist will fail): {missing}; sdist roots={sorted(sdist_roots)}"
    )


def test_force_included_template_paths_exist_on_disk():
    """The bundled-template source dirs must actually exist in the repo."""
    cfg = _config()
    force_include = cfg["tool"]["hatch"]["build"]["targets"]["wheel"].get("force-include", {})
    repo = PYPROJECT.parent
    missing = [src for src in force_include if not (repo / src).exists()]
    assert not missing, f"force-include sources missing on disk: {missing}"


def test_no_deploy_assets_in_public_wheel():
    """The public wheel ships no container-build assets (#707): no bobi/_deploy
    force-includes, no root Dockerfile/docker/scripts in the sdist. Their home
    is the bobi-deploy wheel (bobi_deploy/tests/test_packaging.py guards it)."""
    cfg = _config()
    targets = cfg["tool"]["hatch"]["build"]["targets"]
    offenders = [
        f"{src} -> {dest}"
        for src, dest in targets["wheel"].get("force-include", {}).items()
        if "_deploy" in dest or src.split("/", 1)[0] in ("Dockerfile", "docker", "scripts")
    ]
    offenders += [
        entry for entry in targets["sdist"]["include"]
        if entry.split("/", 1)[0] in ("Dockerfile", "docker", "scripts")
    ]
    assert not offenders, (
        "container-build assets crept back into the public distribution "
        "(they belong in the bobi-deploy wheel, #707): " + ", ".join(offenders)
    )


class _FakeArtifact:
    BUNDLE_NAME = "local.js"
    MANIFEST_NAME = "local.inputs.json"
    NOTICE_NAME = "THIRD_PARTY_NOTICES.txt"

    def __init__(self, *, validation_error: Exception | None = None):
        self.validation_error = validation_error
        self.validated = []
        self.generated = []

    def validate_artifact(self, event_server_dir, *, verify_inputs):
        self.validated.append((event_server_dir, verify_inputs))
        if self.validation_error:
            raise self.validation_error

    def source_input_paths(self, source):
        return []

    def sanitized_node_environment(self):
        return event_server_artifact.sanitized_node_environment()

    def generate_artifact_metadata(
        self,
        event_server_dir,
        *,
        node_version,
        npm_version,
    ):
        self.generated.append((event_server_dir, node_version, npm_version))


def _hook(root: Path, target_name: str = "wheel"):
    return hatch_build.CustomBuildHook(
        root=str(root),
        config={},
        build_config=Mock(),
        metadata=Mock(),
        directory=str(root / "build"),
        target_name=target_name,
    )


def _write_fake_artifacts(event_server_dir: Path, fake: _FakeArtifact) -> None:
    dist = event_server_dir / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    for name in (fake.BUNDLE_NAME, fake.MANIFEST_NAME, fake.NOTICE_NAME):
        (dist / name).write_text(name)


def test_editable_wheel_skips_packaged_artifact_work(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hatch_build,
        "_load_artifact_module",
        lambda root: (_ for _ in ()).throw(
            AssertionError("editable build must skip artifact work")
        ),
    )

    build_data = {}
    _hook(tmp_path).initialize("editable", build_data)

    assert build_data == {}


@pytest.mark.parametrize(
    ("target_name", "destination"),
    [
        ("wheel", "bobi/event-server/dist"),
        ("sdist", "event-server/dist"),
    ],
)
def test_source_archive_reuses_verified_artifact_without_node(
    tmp_path, monkeypatch, target_name, destination,
):
    fake = _FakeArtifact()
    event_server_dir = tmp_path / "event-server"
    _write_fake_artifacts(event_server_dir, fake)
    monkeypatch.setattr(hatch_build, "_load_artifact_module", lambda root: fake)
    monkeypatch.setattr(
        hatch_build,
        "_build_fresh_artifact",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("verified source archive must not rebuild")
        ),
    )

    build_data = {}
    hook = _hook(tmp_path, target_name)
    hook.initialize("standard", build_data)

    assert fake.validated == [(event_server_dir, True)]
    assert hook._staging_root is None
    assert set(build_data["force_include"].values()) == {
        f"{destination}/{fake.BUNDLE_NAME}",
        f"{destination}/{fake.MANIFEST_NAME}",
        f"{destination}/{fake.NOTICE_NAME}",
    }


def test_vcs_artifact_stays_staged_until_finalize(tmp_path, monkeypatch):
    (tmp_path / ".git").write_text("gitdir: elsewhere\n")
    (tmp_path / "event-server").mkdir()
    fake = _FakeArtifact()
    monkeypatch.setattr(hatch_build, "_load_artifact_module", lambda root: fake)

    def fake_build(source, staging_root, artifact):
        event_server_dir = staging_root / "event-server"
        _write_fake_artifacts(event_server_dir, fake)
        return event_server_dir

    monkeypatch.setattr(hatch_build, "_build_fresh_artifact", fake_build)
    hook = _hook(tmp_path)
    build_data = {}

    hook.initialize("standard", build_data)
    staging_root = hook._staging_root

    assert staging_root is not None and staging_root.is_dir()
    assert all(Path(path).is_file() for path in build_data["force_include"])

    hook.finalize("standard", build_data, str(tmp_path / "result.whl"))

    assert not staging_root.exists()
    assert hook._staging_root is None


def test_wheel_hook_force_includes_declared_source_files_individually(
    tmp_path, monkeypatch,
):
    (tmp_path / ".git").write_text("gitdir: elsewhere\n")
    event_server_dir = tmp_path / "event-server"
    declared_sources = [
        event_server_dir / "package.json",
        event_server_dir / "src" / "local.ts",
    ]
    for source in declared_sources:
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(source.name)
    fake = _FakeArtifact()
    fake.source_input_paths = lambda root: declared_sources
    monkeypatch.setattr(hatch_build, "_load_artifact_module", lambda root: fake)

    def fake_build(source, staging_root, artifact):
        staged_event_server = staging_root / "event-server"
        _write_fake_artifacts(staged_event_server, fake)
        return staged_event_server

    monkeypatch.setattr(hatch_build, "_build_fresh_artifact", fake_build)
    hook = _hook(tmp_path)
    build_data = {}

    hook.initialize("standard", build_data)

    for source in declared_sources:
        relative = source.relative_to(event_server_dir).as_posix()
        assert build_data["force_include"][str(source.resolve())] == (
            f"bobi/event-server/{relative}"
        )
    assert all(Path(source).is_file() for source in build_data["force_include"])

    hook.finalize("standard", build_data, str(tmp_path / "result.whl"))


def test_finalize_cleanup_failure_is_reported_and_retains_exit_retry(
    tmp_path, monkeypatch,
):
    (tmp_path / ".git").write_text("gitdir: elsewhere\n")
    (tmp_path / "event-server").mkdir()
    fake = _FakeArtifact()
    monkeypatch.setattr(hatch_build, "_load_artifact_module", lambda root: fake)

    def fake_build(source, staging_root, artifact):
        event_server_dir = staging_root / "event-server"
        _write_fake_artifacts(event_server_dir, fake)
        return event_server_dir

    monkeypatch.setattr(hatch_build, "_build_fresh_artifact", fake_build)
    hook = _hook(tmp_path)
    build_data = {}
    hook.initialize("standard", build_data)
    staging_root = hook._staging_root
    assert staging_root is not None

    real_rmtree = hatch_build.shutil.rmtree
    monkeypatch.setattr(
        hatch_build.shutil,
        "rmtree",
        Mock(side_effect=OSError("cleanup denied")),
    )

    with pytest.raises(
        hatch_build.EventServerBuildError,
        match=r"failed to remove.*cleanup denied",
    ):
        hook.finalize("standard", build_data, str(tmp_path / "result.whl"))

    assert hook._staging_root == staging_root
    assert staging_root.exists()
    assert hook._exit_cleanup_registered

    monkeypatch.setattr(hatch_build.shutil, "rmtree", real_rmtree)
    hook._cleanup_staging()
    hook._unregister_exit_cleanup()
    assert not staging_root.exists()


def test_initialize_failure_cleans_staging_immediately(tmp_path, monkeypatch):
    (tmp_path / ".git").write_text("gitdir: elsewhere\n")
    (tmp_path / "event-server").mkdir()
    fake = _FakeArtifact()
    staging_root = tmp_path / "external-staging"
    monkeypatch.setattr(hatch_build, "_load_artifact_module", lambda root: fake)

    def fake_mkdtemp(*args, **kwargs):
        staging_root.mkdir()
        return str(staging_root)

    def fail_build(source, staged, artifact):
        (staged / "partial").write_text("partial")
        raise hatch_build.EventServerBuildError("npm ci failed")

    monkeypatch.setattr(hatch_build.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(hatch_build, "_build_fresh_artifact", fail_build)
    hook = _hook(tmp_path)

    with pytest.raises(hatch_build.EventServerBuildError, match="npm ci failed"):
        hook.initialize("standard", {})

    assert not staging_root.exists()
    assert hook._staging_root is None


def test_invalid_carried_artifact_reports_rebuild_failure(tmp_path, monkeypatch):
    (tmp_path / "event-server").mkdir()
    fake = _FakeArtifact(validation_error=RuntimeError("bundle digest mismatch"))
    monkeypatch.setattr(hatch_build, "_load_artifact_module", lambda root: fake)
    monkeypatch.setattr(
        hatch_build,
        "_build_fresh_artifact",
        lambda *args: (_ for _ in ()).throw(
            hatch_build.EventServerBuildError("Node.js 20 was not found")
        ),
    )

    with pytest.raises(
        hatch_build.EventServerBuildError,
        match=r"bundle digest mismatch.*Node\.js 20 was not found",
    ):
        _hook(tmp_path).initialize("standard", {})


def test_build_node_must_be_exact_major_20(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hatch_build.shutil,
        "which",
        lambda name: f"/bin/{name}",
    )
    monkeypatch.setattr(
        hatch_build,
        "_run_command",
        lambda args, **kwargs: "v21.1.0" if args[-1] == "--version" else "",
    )

    with pytest.raises(
        hatch_build.EventServerBuildError,
        match=r"Node\.js 20 is required.*v21\.1\.0",
    ):
        hatch_build._require_build_node(tmp_path, env={"PATH": "/bin"})


def _fresh_hook(
    tmp_path: Path,
    monkeypatch,
    fake: _FakeArtifact | None = None,
) -> tuple[hatch_build.CustomBuildHook, _FakeArtifact, Path]:
    (tmp_path / ".git").write_text("gitdir: elsewhere\n")
    (tmp_path / "event-server").mkdir()
    staging_root = tmp_path / "external-staging"
    fake = fake or _FakeArtifact()

    def fake_mkdtemp(*args, **kwargs):
        staging_root.mkdir()
        return str(staging_root)

    monkeypatch.setattr(hatch_build.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(hatch_build, "_load_artifact_module", lambda root: fake)
    return _hook(tmp_path), fake, staging_root


@pytest.mark.parametrize(
    ("missing", "match"),
    [
        ("node", r"Node\.js 20.*node.*not found"),
        ("npm", r"npm.*not found"),
    ],
)
def test_initialize_fails_cleanly_when_build_prerequisite_is_missing(
    tmp_path, monkeypatch, missing, match,
):
    hook, _, staging_root = _fresh_hook(tmp_path, monkeypatch)
    monkeypatch.setattr(
        hatch_build.shutil,
        "which",
        lambda name: None if name == missing else f"/safe/{name}",
    )
    monkeypatch.setattr(
        hatch_build,
        "_run_command",
        lambda args, **kwargs: "v20.19.2",
    )
    build_data = {}

    with pytest.raises(hatch_build.EventServerBuildError, match=match):
        hook.initialize("standard", build_data)

    assert build_data == {}
    assert not staging_root.exists()
    assert hook._staging_root is None


@pytest.mark.parametrize(
    ("failed_command", "expected_calls"),
    [
        ("ci", [["/safe/npm", "ci", "--no-audit", "--no-fund"]]),
        (
            "build:local",
            [
                ["/safe/npm", "ci", "--no-audit", "--no-fund"],
                ["/safe/npm", "run", "build:local"],
            ],
        ),
    ],
)
def test_initialize_surfaces_exact_fresh_build_command_failure_and_cleans(
    tmp_path, monkeypatch, failed_command, expected_calls,
):
    hook, fake, staging_root = _fresh_hook(tmp_path, monkeypatch)
    monkeypatch.setattr(
        hatch_build,
        "_require_build_node",
        lambda *args, **kwargs: ("v20.19.2", "9.2.0"),
    )
    monkeypatch.setattr(hatch_build.shutil, "which", lambda name: f"/safe/{name}")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if failed_command in args:
            raise hatch_build.EventServerBuildError(
                f"{' '.join(args)} failed (exit 19) in {kwargs['cwd']}: useful-tail"
            )
        return ""

    monkeypatch.setattr(hatch_build, "_run_command", fake_run)
    build_data = {}

    with pytest.raises(
        hatch_build.EventServerBuildError,
        match=rf"npm.*{failed_command}.*exit 19.*useful-tail",
    ):
        hook.initialize("standard", build_data)

    assert calls == expected_calls
    assert fake.generated == []
    assert build_data == {}
    assert not staging_root.exists()


@pytest.mark.parametrize("output_state", ["missing", "empty"])
def test_initialize_rejects_missing_or_empty_fresh_bundle_and_cleans(
    tmp_path, monkeypatch, output_state,
):
    hook, _, staging_root = _fresh_hook(tmp_path, monkeypatch)
    monkeypatch.setattr(
        hatch_build,
        "_require_build_node",
        lambda *args, **kwargs: ("v20.19.2", "9.2.0"),
    )
    monkeypatch.setattr(hatch_build.shutil, "which", lambda name: f"/safe/{name}")

    def fake_run(args, *, cwd, **kwargs):
        if args[-1] == "build:local":
            dist = cwd / "dist"
            dist.mkdir()
            if output_state == "empty":
                (dist / "local.js").write_bytes(b"")
            (dist / "local.inputs.json").write_text("{}")
            (dist / "THIRD_PARTY_NOTICES.txt").write_text("notice")
        return ""

    monkeypatch.setattr(hatch_build, "_run_command", fake_run)
    build_data = {}

    with pytest.raises(
        hatch_build.EventServerBuildError,
        match=r"required built artifact is missing or empty.*local\.js",
    ):
        hook.initialize("standard", build_data)

    assert build_data == {}
    assert not staging_root.exists()


def test_initialize_surfaces_artifact_audit_failure_and_cleans(
    tmp_path, monkeypatch,
):
    fake = _FakeArtifact()
    fake.generate_artifact_metadata = Mock(
        side_effect=RuntimeError("license inventory drift")
    )
    hook, _, staging_root = _fresh_hook(tmp_path, monkeypatch, fake)
    monkeypatch.setattr(
        hatch_build,
        "_require_build_node",
        lambda *args, **kwargs: ("v20.19.2", "9.2.0"),
    )
    monkeypatch.setattr(hatch_build.shutil, "which", lambda name: f"/safe/{name}")

    def fake_run(args, *, cwd, **kwargs):
        if args[-1] == "build:local":
            dist = cwd / "dist"
            dist.mkdir()
            (dist / "local.js").write_text("bundle")
        return ""

    monkeypatch.setattr(hatch_build, "_run_command", fake_run)
    build_data = {}

    with pytest.raises(
        hatch_build.EventServerBuildError,
        match=r"artifact audit failed.*license inventory drift",
    ):
        hook.initialize("standard", build_data)

    assert build_data == {}
    assert not staging_root.exists()


def test_staging_input_copy_failure_names_both_paths(tmp_path, monkeypatch):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    source_input = source / "package.json"
    source_input.write_text("{}")
    fake = _FakeArtifact()
    fake.source_input_paths = lambda root: [source_input]
    monkeypatch.setattr(
        hatch_build.shutil,
        "copy2",
        Mock(side_effect=OSError("read failed")),
    )

    with pytest.raises(hatch_build.EventServerBuildError) as raised:
        hatch_build._copy_build_inputs(source, destination, fake)

    diagnostic = str(raised.value)
    assert str(source) in diagnostic
    assert str(destination) in diagnostic
    assert "read failed" in diagnostic


def test_fresh_build_sanitizes_every_node_and_npm_command(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    fake = _FakeArtifact()
    monkeypatch.setenv("NODE_OPTIONS", "--require=/tmp/hostile.cjs")
    monkeypatch.setenv("NODE_PATH", "/tmp/hostile-modules")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "provider-secret")
    monkeypatch.setenv("npm_config_node_options", "--require=/tmp/other.cjs")
    monkeypatch.setenv("npm_config_script_shell", "/tmp/hostile-shell")
    monkeypatch.setattr(hatch_build.shutil, "which", lambda name: f"/safe/{name}")
    calls = []

    def fake_run(args, *, cwd, env):
        calls.append((args, dict(env)))
        if args == ["/safe/node", "--version"]:
            return "v20.19.2"
        if args == ["/safe/npm", "--version"]:
            return "9.2.0"
        if args == ["/safe/npm", "run", "build:local"]:
            dist = cwd / "dist"
            dist.mkdir()
            (dist / "local.js").write_text("bundle")
            (dist / "local.inputs.json").write_text("{}")
            (dist / "THIRD_PARTY_NOTICES.txt").write_text("notice")
        return ""

    monkeypatch.setattr(hatch_build, "_run_command", fake_run)

    hatch_build._build_fresh_artifact(source, staging, fake)

    assert [args for args, _ in calls] == [
        ["/safe/node", "--version"],
        ["/safe/npm", "--version"],
        ["/safe/npm", "ci", "--no-audit", "--no-fund"],
        ["/safe/npm", "run", "build:local"],
    ]
    for _, environment in calls:
        assert environment["PATH"]
        assert environment["NPM_CONFIG_CACHE"] == str(staging / "npm-cache")
        assert "NODE_OPTIONS" not in environment
        assert "NODE_PATH" not in environment
        assert "SLACK_BOT_TOKEN" not in environment
        assert "npm_config_node_options" not in environment
        assert "npm_config_script_shell" not in environment


def test_build_command_failure_has_exit_path_and_bounded_output(
    tmp_path, monkeypatch,
):
    stderr = "discard-me-" * 500 + "useful-tail"
    monkeypatch.setattr(
        hatch_build.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], returncode=19, stdout="", stderr=stderr
        ),
    )

    with pytest.raises(hatch_build.EventServerBuildError) as raised:
        hatch_build._run_command(["npm", "ci"], cwd=tmp_path)

    message = str(raised.value)
    assert "npm ci failed (exit 19)" in message
    assert str(tmp_path) in message
    assert message.endswith("useful-tail")
    assert len(message) < 2_200


def _write_failed_target_probe(project: Path, marker: Path) -> Path:
    """Create a tiny VCS checkout whose hook succeeds before the wheel fails."""
    (project / ".git").write_text("gitdir: unavailable\n")
    shutil.copy2(PROJECT_ROOT / "hatch_build.py", project / "hatch_build.py")
    for relative in (
        Path("bobi/__init__.py"),
        Path("bobi/events/__init__.py"),
        Path("bobi/events/artifact.py"),
    ):
        destination = project / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative, destination)

    event_server_dir = project / "event-server"
    for source in event_server_artifact.source_input_paths(
        PROJECT_ROOT / "event-server"
    ):
        relative = source.relative_to(PROJECT_ROOT / "event-server")
        destination = event_server_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    (project / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [project]
            name = "bobi-build-cleanup-probe"
            version = "0.0.0"

            [tool.hatch.build.targets.wheel]
            packages = ["bobi"]

            [tool.hatch.build.targets.wheel.force-include]
            "missing-after-hook.txt" = "cleanup_probe/missing.txt"

            [tool.hatch.build.hooks.custom]
            path = "hatch_build.py"

            [build-system]
            requires = ["hatchling"]
            build-backend = "hatchling.build"
            """
        ).strip()
        + "\n"
    )

    fake_bin = project / "fake-bin"
    fake_bin.mkdir()
    node = fake_bin / "node"
    node.write_text("#!/bin/sh\nprintf 'v20.19.2\\n'\n")
    node.chmod(0o755)

    dependency_data = {
        dependency.name: {
            "license": dependency.license,
            "license_text": dependency.license_text,
            "version": dependency.version,
        }
        for dependency in event_server_artifact.AUDITED_DEPENDENCIES
    }
    npm = fake_bin / "npm"
    npm.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            f"""
            import json
            import os
            import sys
            from pathlib import Path

            root = Path.cwd()
            args = sys.argv[1:]
            dependencies = {dependency_data!r}
            if args == ["--version"]:
                print("9.2.0")
                raise SystemExit(0)
            if args == ["ci", "--no-audit", "--no-fund"]:
                for name, metadata in dependencies.items():
                    package = root / "node_modules" / name
                    package.mkdir(parents=True, exist_ok=True)
                    (package / "package.json").write_text(json.dumps({{
                        "license": metadata["license"],
                        "name": name,
                        "version": metadata["version"],
                    }}))
                    (package / "LICENSE").write_text(metadata["license_text"])
                esbuild = root / "node_modules" / "esbuild"
                esbuild.mkdir(parents=True, exist_ok=True)
                (esbuild / "package.json").write_text(json.dumps({{
                    "name": "esbuild",
                    "version": "{event_server_artifact.locked_esbuild_version(PROJECT_ROOT / "event-server")}",
                }}))
                raise SystemExit(0)
            if args == ["run", "build:local"]:
                dist = root / "dist"
                dist.mkdir(parents=True, exist_ok=True)
                (dist / "local.js").write_text("console.log('probe')\\n")
                (dist / "local.meta.json").write_text(json.dumps({{
                    "inputs": {{
                        "node_modules/@chat-adapter/slack/index.js": {{}},
                        "node_modules/ws/index.js": {{}},
                    }},
                    "outputs": {{"dist/local.js": {{
                        "imports": [],
                        "inputs": {{
                            "node_modules/@chat-adapter/slack/index.js": {{}},
                            "node_modules/ws/index.js": {{}},
                        }},
                    }}}},
                }}))
                Path({str(marker)!r}).write_text("artifact-built")
                raise SystemExit(0)
            print(f"unexpected fake npm command: {{args}}", file=sys.stderr)
            raise SystemExit(91)
            """
        ).lstrip()
    )
    npm.chmod(0o755)
    return fake_bin


def test_pep517_target_failure_cleans_staging_on_backend_exit(tmp_path):
    project = tmp_path / "probe"
    project.mkdir()
    marker = tmp_path / "artifact-built"
    fake_bin = _write_failed_target_probe(project, marker)
    backend_tmp = tmp_path / "backend-tmp"
    backend_tmp.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PATH": str(fake_bin),
            "TMPDIR": str(backend_tmp),
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            str(project),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert marker.read_text() == "artifact-built"
    assert result.returncode != 0
    diagnostic = result.stdout + result.stderr
    assert "missing-after-hook.txt" in diagnostic
    assert not list(backend_tmp.glob("bobi-event-server-build-*"))
    assert not list(project.rglob("__pycache__"))
    assert not list(project.rglob("*.pyc"))
