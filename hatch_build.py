"""Hatch build hook for the immutable embedded local event-server artifact."""

from __future__ import annotations

import atexit
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_COMMAND_OUTPUT_LIMIT = 2_000
_COMMAND_TIMEOUT_SECONDS = 300


class EventServerBuildError(RuntimeError):
    """The embedded event-server distribution artifact could not be built."""


def _remove_import_bytecode() -> None:
    cached = globals().get("__cached__")
    if not isinstance(cached, str) or not cached:
        return
    cache_path = Path(cached)
    try:
        cache_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise EventServerBuildError(
            f"failed to remove build-hook bytecode cache {cache_path}: {exc}"
        ) from exc
    try:
        cache_path.parent.rmdir()
    except OSError:
        pass


_remove_import_bytecode()


def _load_artifact_module(root: Path) -> ModuleType:
    module_path = root / "bobi" / "events" / "artifact.py"
    module_name = "_bobi_event_server_artifact"
    module = ModuleType(module_name)
    module.__file__ = str(module_path)
    module.__package__ = ""
    missing = object()
    previous = sys.modules.get(module_name, missing)
    sys.modules[module_name] = module
    try:
        code = compile(module_path.read_bytes(), str(module_path), "exec")
        exec(code, module.__dict__)
    finally:
        if previous is missing:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
    return module


def _bounded_output(result: subprocess.CompletedProcess[str]) -> str:
    output = (result.stderr or result.stdout or "").strip()
    return output[-_COMMAND_OUTPUT_LIMIT:] or "no output"


def _run_command(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise EventServerBuildError(
            f"{args[0]} was not found while running {' '.join(args)!r} in {cwd}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise EventServerBuildError(
            f"{' '.join(args)} timed out after {_COMMAND_TIMEOUT_SECONDS}s in {cwd}"
        ) from exc
    except OSError as exc:
        raise EventServerBuildError(
            f"could not run {' '.join(args)!r} in {cwd}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise EventServerBuildError(
            f"{' '.join(args)} failed (exit {result.returncode}) in {cwd}: "
            f"{_bounded_output(result)}"
        )
    return result.stdout.strip()


def _require_build_node(
    event_server_dir: Path,
    *,
    env: dict[str, str],
) -> tuple[str, str]:
    node = shutil.which("node")
    if node is None:
        raise EventServerBuildError(
            "Node.js 20 is required to build the embedded event server, "
            f"but `node` was not found on PATH while building {event_server_dir}"
        )
    npm = shutil.which("npm")
    if npm is None:
        raise EventServerBuildError(
            "npm is required to build the embedded event server, "
            f"but `npm` was not found on PATH while building {event_server_dir}"
        )
    node_version = _run_command(
        [node, "--version"],
        cwd=event_server_dir,
        env=env,
    )
    try:
        node_major = int(node_version.removeprefix("v").split(".", 1)[0])
    except (ValueError, IndexError) as exc:
        raise EventServerBuildError(
            f"could not parse Node.js version {node_version!r} from {node}"
        ) from exc
    if node_major != 20:
        raise EventServerBuildError(
            "Node.js 20 is required to build the embedded event server; "
            f"found {node_version!r} at {node}"
        )
    npm_version = _run_command(
        [npm, "--version"],
        cwd=event_server_dir,
        env=env,
    )
    if not npm_version:
        raise EventServerBuildError(f"npm at {npm} returned an empty version")
    return node_version, npm_version


def _copy_build_inputs(source: Path, destination: Path, artifact: ModuleType) -> None:
    try:
        for input_path in artifact.source_input_paths(source):
            relative = input_path.relative_to(source)
            output_path = destination / relative
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(input_path, output_path)
    except Exception as exc:
        raise EventServerBuildError(
            f"failed to stage event-server inputs from {source} to {destination}: {exc}"
        ) from exc


def _build_fresh_artifact(
    source_event_server: Path,
    staging_root: Path,
    artifact: ModuleType,
) -> Path:
    staged_event_server = staging_root / "event-server"
    staged_event_server.mkdir(parents=True)
    _copy_build_inputs(source_event_server, staged_event_server, artifact)
    cache_dir = staging_root / "npm-cache"
    cache_dir.mkdir()
    command_env = artifact.sanitized_node_environment()
    command_env["NPM_CONFIG_CACHE"] = str(cache_dir)
    node_version, npm_version = _require_build_node(
        staged_event_server,
        env=command_env,
    )

    npm = shutil.which("npm")
    if npm is None:  # `_require_build_node` already checked; protects type narrowing.
        raise EventServerBuildError("npm disappeared from PATH during artifact staging")
    _run_command(
        [npm, "ci", "--no-audit", "--no-fund"],
        cwd=staged_event_server,
        env=command_env,
    )
    _run_command(
        [npm, "run", "build:local"],
        cwd=staged_event_server,
        env=command_env,
    )
    try:
        artifact.generate_artifact_metadata(
            staged_event_server,
            node_version=node_version,
            npm_version=npm_version,
        )
    except Exception as exc:
        raise EventServerBuildError(
            f"embedded event-server artifact audit failed in {staged_event_server}: {exc}"
        ) from exc
    return staged_event_server


class CustomBuildHook(BuildHookInterface):
    """Generate or validate the artifact before Hatch consumes target inputs."""

    PLUGIN_NAME = "custom"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._staging_root: Path | None = None
        self._exit_cleanup_registered = False

    def _cleanup_staging(self, *, fail_on_error: bool = True) -> None:
        staging_root = self._staging_root
        if staging_root is None:
            return
        try:
            shutil.rmtree(staging_root)
        except FileNotFoundError:
            self._staging_root = None
        except OSError as exc:
            if fail_on_error:
                raise EventServerBuildError(
                    f"failed to remove event-server build staging {staging_root}: {exc}"
                ) from exc
        else:
            self._staging_root = None

    def _cleanup_staging_at_exit(self) -> None:
        self._cleanup_staging(fail_on_error=False)

    def _register_exit_cleanup(self) -> None:
        if not self._exit_cleanup_registered:
            atexit.register(self._cleanup_staging_at_exit)
            self._exit_cleanup_registered = True

    def _unregister_exit_cleanup(self) -> None:
        if self._exit_cleanup_registered:
            atexit.unregister(self._cleanup_staging_at_exit)
            self._exit_cleanup_registered = False

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name == "wheel" and version == "editable":
            return
        if self.target_name not in {"sdist", "wheel"}:
            raise EventServerBuildError(
                f"unsupported Hatch target for event-server artifact: {self.target_name}"
            )

        root = Path(self.root)
        source_event_server = root / "event-server"
        artifact = _load_artifact_module(root)
        wheel_source_includes = {}
        if self.target_name == "wheel":
            for source_path in artifact.source_input_paths(source_event_server):
                relative = source_path.relative_to(source_event_server).as_posix()
                wheel_source_includes[str(source_path.resolve())] = (
                    f"bobi/event-server/{relative}"
                )
        artifact_event_server: Path | None = None
        carried_error: Exception | None = None

        is_vcs_checkout = (root / ".git").exists()
        if not is_vcs_checkout:
            try:
                artifact.validate_artifact(source_event_server, verify_inputs=True)
                artifact_event_server = source_event_server
            except Exception as exc:
                carried_error = exc

        try:
            if artifact_event_server is None:
                self._staging_root = Path(
                    tempfile.mkdtemp(prefix="bobi-event-server-build-")
                )
                self._register_exit_cleanup()
                artifact_event_server = _build_fresh_artifact(
                    source_event_server,
                    self._staging_root,
                    artifact,
                )

            destination_root = (
                "event-server/dist"
                if self.target_name == "sdist"
                else "bobi/event-server/dist"
            )
            artifact_includes = {}
            for name in (
                artifact.BUNDLE_NAME,
                artifact.MANIFEST_NAME,
                artifact.NOTICE_NAME,
            ):
                source_path = artifact_event_server / "dist" / name
                if not source_path.is_file() or source_path.stat().st_size == 0:
                    raise EventServerBuildError(
                        f"required built artifact is missing or empty: {source_path}"
                    )
                artifact_includes[str(source_path.resolve())] = (
                    f"{destination_root}/{name}"
                )

            force_include = build_data.setdefault("force_include", {})
            if not isinstance(force_include, dict):
                raise EventServerBuildError(
                    f"Hatch force_include data is not a mapping for {self.target_name}"
                )
            force_include.update(wheel_source_includes)
            force_include.update(artifact_includes)

        except Exception as exc:
            cleanup_error: EventServerBuildError | None = None
            try:
                self._cleanup_staging()
            except EventServerBuildError as error:
                cleanup_error = error
            else:
                self._unregister_exit_cleanup()

            reported_error: Exception = exc
            if carried_error is not None and isinstance(exc, EventServerBuildError):
                reported_error = EventServerBuildError(
                    f"carried source-archive artifact is invalid ({carried_error}); "
                    f"rebuild failed: {exc}"
                )
            if cleanup_error is not None:
                raise EventServerBuildError(
                    f"{reported_error}; staging cleanup also failed: {cleanup_error}"
                ) from exc
            if reported_error is not exc:
                raise reported_error from exc
            raise

    def finalize(
        self,
        version: str,
        build_data: dict[str, Any],
        artifact_path: str,
    ) -> None:
        self._cleanup_staging()
        self._unregister_exit_cleanup()
