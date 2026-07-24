"""Build-time and runtime contract for the embedded local event server."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
BUNDLE_NAME = "local.js"
MANIFEST_NAME = "local.inputs.json"
METAFILE_NAME = "local.meta.json"
NOTICE_NAME = "THIRD_PARTY_NOTICES.txt"

_STATIC_INPUTS = (
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "core/package.json",
    "core/tsconfig.json",
)
_SOURCE_GLOBS = ("src/**/*.ts", "core/src/**/*.ts")
_INSTALL_STATE_DIRECTORY_NAMES = frozenset(
    {".npm-cache", "__pycache__", "node_modules"}
)

# New built-in imports are harmless but still require an explicit audit. This
# keeps an accidental third-party external from turning into a runtime module
# dependency.
_ALLOWED_BUILTIN_EXTERNAL_IMPORTS = frozenset(
    {
        "buffer",
        "crypto",
        "events",
        "http",
        "https",
        "net",
        "node:http",
        "stream",
        "tls",
        "url",
        "zlib",
    }
)
_ALLOWED_OPTIONAL_EXTERNAL_IMPORTERS = frozenset(
    {
        ("node_modules/ws/lib/buffer-util.js", "bufferutil"),
        ("node_modules/ws/lib/validation.js", "utf-8-validate"),
    }
)
_SAFE_NODE_ENVIRONMENT_KEYS = frozenset(
    {
        "ALL_PROXY",
        "CI",
        "FORCE_COLOR",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NODE_EXTRA_CA_CERTS",
        "NO_COLOR",
        "NO_PROXY",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "TZ",
        "WINDIR",
    }
)


@dataclass(frozen=True)
class AuditedDependency:
    name: str
    version: str
    license: str
    license_text: str

    def manifest_entry(self) -> dict[str, str]:
        return {
            "license": self.license,
            "license_sha256": _sha256(self.license_text.encode()),
            "name": self.name,
            "version": self.version,
        }


AUDITED_DEPENDENCIES = (
    AuditedDependency(
        name="@chat-adapter/slack",
        version="4.30.0",
        license="MIT",
        license_text="""MIT License

Copyright (c) 2026 Vercel, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the “Software”), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
""",
    ),
    AuditedDependency(
        name="ws",
        version="8.18.0",
        license="MIT",
        license_text="""Copyright (c) 2011 Einar Otto Stangvik <einaros@gmail.com>
Copyright (c) 2013 Arnout Kazemier and contributors
Copyright (c) 2016 Luigi Pinca and contributors

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
""",
    ),
)


class ArtifactValidationError(RuntimeError):
    """The local event-server artifact does not satisfy its build contract."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    try:
        return _sha256(path.read_bytes())
    except OSError as exc:
        raise ArtifactValidationError(f"cannot hash {path}: {exc}") from exc


def sanitized_node_environment(
    source: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a minimal environment for Node/npm build-time commands.

    Bobi may start a source rebuild while provider credentials are present in
    its process environment. npm lifecycle scripts do not need those values,
    inherited Node preload/module paths, or caller-controlled npm settings.
    """
    source = dict(os.environ) if source is None else source
    environment = {
        key: value
        for key, value in source.items()
        if key.upper() in _SAFE_NODE_ENVIRONMENT_KEYS
        or key.upper().startswith("LC_")
    }
    environment["NPM_CONFIG_USERCONFIG"] = os.devnull
    return environment


def _require_non_empty(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise ArtifactValidationError(f"required artifact is missing: {path}") from exc
    except OSError as exc:
        raise ArtifactValidationError(f"cannot read required artifact {path}: {exc}") from exc
    if not data:
        raise ArtifactValidationError(f"required artifact is empty: {path}")
    return data


def source_input_paths(event_server_dir: Path) -> list[Path]:
    paths = [event_server_dir / relative for relative in _STATIC_INPUTS]
    for pattern in _SOURCE_GLOBS:
        matched = sorted(
            path
            for path in event_server_dir.glob(pattern)
            if not _INSTALL_STATE_DIRECTORY_NAMES.intersection(
                path.relative_to(event_server_dir).parts
            )
        )
        if not matched:
            raise ArtifactValidationError(
                f"bundle input pattern has no files under {event_server_dir}: {pattern}"
            )
        paths.extend(matched)
    missing = [path for path in paths if not path.is_file()]
    if missing:
        shown = ", ".join(str(path) for path in missing)
        raise ArtifactValidationError(f"bundle input is missing: {shown}")
    return sorted(set(paths), key=lambda path: path.relative_to(event_server_dir).as_posix())


def source_input_hashes(event_server_dir: Path) -> dict[str, str]:
    return {
        f"event-server/{path.relative_to(event_server_dir).as_posix()}": file_sha256(path)
        for path in source_input_paths(event_server_dir)
    }


def locked_esbuild_version(event_server_dir: Path) -> str:
    lock_path = event_server_dir / "package-lock.json"
    try:
        lock = json.loads(lock_path.read_text())
        version = lock["packages"]["node_modules/esbuild"]["version"]
    except (
        FileNotFoundError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
    ) as exc:
        raise ArtifactValidationError(
            f"cannot resolve locked esbuild version from {lock_path}: {exc}"
        ) from exc
    if not isinstance(version, str) or not version:
        raise ArtifactValidationError(f"invalid locked esbuild version in {lock_path}")
    return version


def _dependency_name(input_path: str) -> str | None:
    marker = "node_modules/"
    normalized = input_path.replace("\\", "/")
    if marker not in normalized:
        return None
    remainder = normalized.rsplit(marker, 1)[1]
    parts = remainder.split("/")
    if not parts or not parts[0]:
        return None
    if parts[0].startswith("@") and len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0]


def _load_metafile(event_server_dir: Path) -> dict[str, Any]:
    meta_path = event_server_dir / "dist" / METAFILE_NAME
    data = _require_non_empty(meta_path)
    try:
        metafile = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(f"malformed esbuild metafile {meta_path}: {exc}") from exc
    if not isinstance(metafile, dict):
        raise ArtifactValidationError(f"esbuild metafile must be an object: {meta_path}")
    return metafile


def _audit_metafile(event_server_dir: Path) -> list[dict[str, str]]:
    metafile = _load_metafile(event_server_dir)
    inputs = metafile.get("inputs")
    outputs = metafile.get("outputs")
    if not isinstance(inputs, dict) or not inputs:
        raise ArtifactValidationError("esbuild metafile has no inputs")
    if not isinstance(outputs, dict) or not outputs:
        raise ArtifactValidationError("esbuild metafile has no outputs")

    normalized_outputs = {
        key.replace("\\", "/"): value
        for key, value in outputs.items()
        if isinstance(key, str)
    }
    if set(normalized_outputs) != {"dist/local.js"} or len(outputs) != 1:
        raise ArtifactValidationError(
            "esbuild must emit exactly one self-contained dist/local.js output"
        )
    local_output = normalized_outputs["dist/local.js"]
    if not isinstance(local_output, dict):
        raise ArtifactValidationError("esbuild metafile output entry is malformed")
    output_inputs = local_output.get("inputs")
    if not isinstance(output_inputs, dict) or not output_inputs:
        raise ArtifactValidationError("esbuild local.js output has no input inventory")
    included_inputs = {
        path
        for path in output_inputs
        if isinstance(path, str)
    }
    if len(included_inputs) != len(output_inputs) or not included_inputs <= set(inputs):
        raise ArtifactValidationError(
            "esbuild local.js output input inventory is malformed"
        )

    external_imports: set[str] = set()
    output_imports = local_output.get("imports", [])
    if not isinstance(output_imports, list):
        raise ArtifactValidationError("esbuild metafile output imports are malformed")
    for imported in output_imports:
        if not isinstance(imported, dict):
            raise ArtifactValidationError("esbuild metafile output import is malformed")
        path = imported.get("path")
        if not isinstance(path, str):
            raise ArtifactValidationError("esbuild metafile output import path is malformed")
        if not imported.get("external"):
            raise ArtifactValidationError(
                f"local.js is not self-contained; it imports unshipped output {path}"
            )
        external_imports.add(path)
    allowed_external_imports = _ALLOWED_BUILTIN_EXTERNAL_IMPORTS | {
        module for _, module in _ALLOWED_OPTIONAL_EXTERNAL_IMPORTERS
    }
    unexpected = sorted(external_imports - allowed_external_imports)
    if unexpected:
        raise ArtifactValidationError(
            "bundle has unexpected external import(s): " + ", ".join(unexpected)
        )

    attributed_external_imports: set[str] = set()
    for input_path in included_inputs:
        input_entry = inputs[input_path]
        if not isinstance(input_path, str) or not isinstance(input_entry, dict):
            raise ArtifactValidationError("esbuild metafile input entry is malformed")
        normalized_input = input_path.replace("\\", "/")
        imports = input_entry.get("imports", [])
        if not isinstance(imports, list):
            raise ArtifactValidationError("esbuild metafile input imports are malformed")
        for imported in imports:
            if not isinstance(imported, dict) or not imported.get("external"):
                continue
            module = imported.get("path")
            if not isinstance(module, str):
                raise ArtifactValidationError(
                    "esbuild metafile external input import is malformed"
                )
            attributed_external_imports.add(module)
            if module in _ALLOWED_BUILTIN_EXTERNAL_IMPORTS:
                continue
            if (normalized_input, module) not in _ALLOWED_OPTIONAL_EXTERNAL_IMPORTERS:
                raise ArtifactValidationError(
                    "optional external import has an unexpected importer: "
                    f"{normalized_input} -> {module}"
                )
    unattributed = sorted(external_imports - attributed_external_imports)
    if unattributed:
        raise ArtifactValidationError(
            "bundle external import(s) lack audited importer provenance: "
            + ", ".join(unattributed)
        )

    bundled = set()
    for input_path in included_inputs:
        normalized_input = input_path.replace("\\", "/")
        if normalized_input.count("node_modules/") > 1:
            raise ArtifactValidationError(
                "nested bundled dependency instances are not allowed: "
                f"{normalized_input}"
            )
        dependency_name = _dependency_name(normalized_input)
        if dependency_name is not None:
            canonical_prefix = f"node_modules/{dependency_name}/"
            if not normalized_input.startswith(canonical_prefix):
                raise ArtifactValidationError(
                    "non-root bundled dependency instance is not allowed: "
                    f"{normalized_input}"
                )
            bundled.add(dependency_name)
    expected = {dependency.name for dependency in AUDITED_DEPENDENCIES}
    if bundled != expected:
        raise ArtifactValidationError(
            "bundled dependency inventory drifted: "
            f"expected {sorted(expected)}, found {sorted(bundled)}"
        )

    inventory = []
    for dependency in AUDITED_DEPENDENCIES:
        package_dir = event_server_dir / "node_modules" / dependency.name
        package_path = package_dir / "package.json"
        license_path = package_dir / "LICENSE"
        try:
            package = json.loads(package_path.read_text())
        except (
            FileNotFoundError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise ArtifactValidationError(
                f"cannot audit bundled dependency metadata {package_path}: {exc}"
            ) from exc
        if not isinstance(package, dict):
            raise ArtifactValidationError(
                f"bundled dependency metadata must be an object: {package_path}"
            )
        if package.get("name") != dependency.name:
            raise ArtifactValidationError(
                f"bundled dependency name drifted in {package_path}: {package.get('name')!r}"
            )
        if package.get("version") != dependency.version:
            raise ArtifactValidationError(
                f"bundled dependency version drifted for {dependency.name}: "
                f"expected {dependency.version}, found {package.get('version')!r}"
            )
        if package.get("license") != dependency.license:
            raise ArtifactValidationError(
                f"bundled dependency license drifted for {dependency.name}: "
                f"expected {dependency.license}, found {package.get('license')!r}"
            )
        license_data = _require_non_empty(license_path)
        expected_license = dependency.license_text.encode()
        if license_data != expected_license:
            raise ArtifactValidationError(
                f"bundled dependency license text drifted for {dependency.name}: "
                f"{license_path}"
            )
        inventory.append(dependency.manifest_entry())
    return inventory


def notice_bytes() -> bytes:
    sections = [
        "Bobi embedded local event server\n"
        "Third-party notices for JavaScript bundled in local.js\n"
    ]
    for dependency in AUDITED_DEPENDENCIES:
        sections.append(
            f"\n{'=' * 72}\n"
            f"{dependency.name} {dependency.version}\n"
            f"License: {dependency.license}\n"
            f"{'=' * 72}\n\n"
            f"{dependency.license_text}"
        )
    return "".join(sections).encode()


def _output_entry(data: bytes) -> dict[str, int | str]:
    return {"sha256": _sha256(data), "size": len(data)}


def _expected_inventory() -> list[dict[str, str]]:
    return [dependency.manifest_entry() for dependency in AUDITED_DEPENDENCIES]


def generate_artifact_metadata(
    event_server_dir: Path,
    *,
    node_version: str,
    npm_version: str,
) -> dict[str, Any]:
    """Audit a fresh bundle and write its fixed notice and input manifest."""
    dist_dir = event_server_dir / "dist"
    bundle_data = _require_non_empty(dist_dir / BUNDLE_NAME)
    inventory = _audit_metafile(event_server_dir)

    installed_esbuild_path = event_server_dir / "node_modules" / "esbuild" / "package.json"
    try:
        installed_esbuild_package = json.loads(installed_esbuild_path.read_text())
    except (
        FileNotFoundError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise ArtifactValidationError(
            f"cannot inspect installed esbuild metadata {installed_esbuild_path}: {exc}"
        ) from exc
    if not isinstance(installed_esbuild_package, dict):
        raise ArtifactValidationError(
            f"installed esbuild metadata must be an object: {installed_esbuild_path}"
        )
    installed_esbuild = installed_esbuild_package.get("version")
    expected_esbuild = locked_esbuild_version(event_server_dir)
    if installed_esbuild != expected_esbuild:
        raise ArtifactValidationError(
            "installed esbuild does not match package-lock.json: "
            f"expected {expected_esbuild}, found {installed_esbuild!r}"
        )

    fixed_notice = notice_bytes()
    notice_path = dist_dir / NOTICE_NAME
    notice_path.write_bytes(fixed_notice)

    manifest: dict[str, Any] = {
        "bundled_dependencies": inventory,
        "inputs": source_input_hashes(event_server_dir),
        "outputs": {
            BUNDLE_NAME: _output_entry(bundle_data),
            NOTICE_NAME: _output_entry(fixed_notice),
        },
        "schema_version": SCHEMA_VERSION,
        "tools": {
            "esbuild": expected_esbuild,
            "node": node_version,
            "npm": npm_version,
        },
    }
    manifest_path = dist_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    validate_artifact(event_server_dir, verify_inputs=True)
    return manifest


def _load_manifest(path: Path) -> dict[str, Any]:
    data = _require_non_empty(path)
    try:
        manifest = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(f"malformed artifact manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ArtifactValidationError(f"artifact manifest must be an object: {path}")
    return manifest


def validate_artifact(
    event_server_dir: Path,
    *,
    verify_inputs: bool,
) -> dict[str, Any]:
    """Validate the complete carried or installed artifact set."""
    dist_dir = event_server_dir / "dist"
    bundle_data = _require_non_empty(dist_dir / BUNDLE_NAME)
    notice_data = _require_non_empty(dist_dir / NOTICE_NAME)
    manifest = _load_manifest(dist_dir / MANIFEST_NAME)

    schema_version = manifest.get("schema_version")
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        raise ArtifactValidationError(
            "unsupported artifact manifest schema: "
            f"{schema_version!r}"
        )
    if manifest.get("bundled_dependencies") != _expected_inventory():
        raise ArtifactValidationError("artifact bundled-dependency inventory is invalid")
    if notice_data != notice_bytes():
        raise ArtifactValidationError("artifact third-party notice bytes are invalid")

    tools = manifest.get("tools")
    if not isinstance(tools, dict) or any(
        not isinstance(tools.get(name), str) or not tools[name]
        for name in ("node", "npm", "esbuild")
    ):
        raise ArtifactValidationError("artifact build-tool versions are missing or invalid")

    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        raise ArtifactValidationError("artifact output manifest is missing or invalid")
    for name, data in ((BUNDLE_NAME, bundle_data), (NOTICE_NAME, notice_data)):
        expected = outputs.get(name)
        actual = _output_entry(data)
        if expected != actual:
            raise ArtifactValidationError(
                f"artifact digest or size mismatch for {dist_dir / name}"
            )

    if verify_inputs:
        inputs = manifest.get("inputs")
        actual_inputs = source_input_hashes(event_server_dir)
        if inputs != actual_inputs:
            raise ArtifactValidationError("artifact bundle input hashes do not match source")
        if tools["esbuild"] != locked_esbuild_version(event_server_dir):
            raise ArtifactValidationError(
                "artifact esbuild version does not match package-lock.json"
            )
    return manifest


def is_artifact_current(event_server_dir: Path) -> bool:
    try:
        validate_artifact(event_server_dir, verify_inputs=True)
    except ArtifactValidationError:
        return False
    return True


def canonical_json_digest(value: Any) -> str:
    data = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return _sha256(data)
