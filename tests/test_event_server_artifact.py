"""Content-addressed contract for the embedded event-server artifact."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bobi.events import artifact


def _write_inputs(event_server_dir: Path) -> None:
    files = {
        "package.json": "{}\n",
        "package-lock.json": json.dumps(
            {"packages": {"node_modules/esbuild": {"version": "0.25.12"}}}
        ),
        "tsconfig.json": "{}\n",
        "src/local.ts": "console.log('local')\n",
        "src/slack-socket-local.ts": "console.log('slack')\n",
        "src/discord-gateway-local.ts": "console.log('discord')\n",
        "core/package.json": "{}\n",
        "core/tsconfig.json": "{}\n",
        "core/src/core.ts": "export const core = true\n",
    }
    for relative, content in files.items():
        path = event_server_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _write_audit_dependencies(event_server_dir: Path) -> None:
    for dependency in artifact.AUDITED_DEPENDENCIES:
        package_dir = event_server_dir / "node_modules" / dependency.name
        package_dir.mkdir(parents=True)
        (package_dir / "package.json").write_text(
            json.dumps(
                {
                    "license": dependency.license,
                    "name": dependency.name,
                    "version": dependency.version,
                }
            )
        )
        (package_dir / "LICENSE").write_text(dependency.license_text)
    esbuild_dir = event_server_dir / "node_modules" / "esbuild"
    esbuild_dir.mkdir(parents=True)
    (esbuild_dir / "package.json").write_text('{"version":"0.25.12"}\n')


def _write_bundle_and_metafile(
    event_server_dir: Path,
    *,
    external: str | None = None,
    external_importer: str | None = None,
    extra_input: str | None = None,
) -> None:
    dist = event_server_dir / "dist"
    dist.mkdir()
    (dist / artifact.BUNDLE_NAME).write_text("console.log('bundle')\n")
    inputs = {
        "src/local.ts": {"bytes": 1, "imports": []},
        "node_modules/@chat-adapter/slack/dist/api.js": {
            "bytes": 1,
            "imports": [],
        },
        "node_modules/ws/lib/websocket.js": {"bytes": 1, "imports": []},
    }
    if extra_input:
        inputs[extra_input] = {"bytes": 1, "imports": []}
    imports = []
    if external:
        imports.append({"external": True, "kind": "require-call", "path": external})
        if external_importer:
            inputs.setdefault(external_importer, {"bytes": 1, "imports": []})
            inputs[external_importer]["imports"].append(imports[0])
    metafile = {
        "inputs": inputs,
        "outputs": {
            "dist/local.js": {
                "bytes": 1,
                "imports": imports,
                "inputs": {name: {"bytesInOutput": 1} for name in inputs},
            }
        },
    }
    (dist / artifact.METAFILE_NAME).write_text(json.dumps(metafile))


def _fresh_artifact(tmp_path: Path) -> Path:
    event_server_dir = tmp_path / "event-server"
    _write_inputs(event_server_dir)
    _write_audit_dependencies(event_server_dir)
    _write_bundle_and_metafile(event_server_dir)
    artifact.generate_artifact_metadata(
        event_server_dir,
        node_version="v20.19.2",
        npm_version="10.8.2",
    )
    return event_server_dir


def test_manifest_covers_every_bundle_input_and_output(tmp_path):
    event_server_dir = _fresh_artifact(tmp_path)

    manifest = artifact.validate_artifact(event_server_dir, verify_inputs=True)

    assert list(manifest) == [
        "bundled_dependencies",
        "inputs",
        "outputs",
        "schema_version",
        "tools",
    ]
    assert "event-server/src/slack-socket-local.ts" in manifest["inputs"]
    assert "event-server/src/discord-gateway-local.ts" in manifest["inputs"]
    assert set(manifest["outputs"]) == {
        artifact.BUNDLE_NAME,
        artifact.NOTICE_NAME,
    }
    assert (event_server_dir / "dist" / artifact.NOTICE_NAME).read_bytes() == (
        artifact.notice_bytes()
    )


def test_source_inputs_exclude_nested_install_state(tmp_path):
    event_server_dir = tmp_path / "event-server"
    _write_inputs(event_server_dir)
    sentinels = [
        event_server_dir / "src" / ".npm-cache" / "poison.ts",
        event_server_dir / "core" / "src" / "node_modules" / "poison.ts",
    ]
    for sentinel in sentinels:
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("throw new Error('not source')\n")

    source_inputs = set(artifact.source_input_paths(event_server_dir))

    assert source_inputs.isdisjoint(sentinels)


@pytest.mark.parametrize("target", ["input", "bundle", "notice", "manifest"])
def test_artifact_tampering_is_rejected(tmp_path, target):
    event_server_dir = _fresh_artifact(tmp_path)
    if target == "input":
        (event_server_dir / "src" / "local.ts").write_text("changed\n")
    elif target == "bundle":
        (event_server_dir / "dist" / artifact.BUNDLE_NAME).write_text("changed\n")
    elif target == "notice":
        (event_server_dir / "dist" / artifact.NOTICE_NAME).write_text("changed\n")
    else:
        (event_server_dir / "dist" / artifact.MANIFEST_NAME).write_text("{")

    with pytest.raises(artifact.ArtifactValidationError):
        artifact.validate_artifact(event_server_dir, verify_inputs=True)
    assert artifact.is_artifact_current(event_server_dir) is False


def test_installed_validation_checks_outputs_without_source_freshness(tmp_path):
    event_server_dir = _fresh_artifact(tmp_path)
    (event_server_dir / "src" / "local.ts").write_text("archive timestamp is irrelevant\n")

    artifact.validate_artifact(event_server_dir, verify_inputs=False)


def test_non_utf8_manifest_is_a_named_validation_error(tmp_path):
    event_server_dir = _fresh_artifact(tmp_path)
    (event_server_dir / "dist" / artifact.MANIFEST_NAME).write_bytes(b"\x80")

    with pytest.raises(
        artifact.ArtifactValidationError,
        match="malformed artifact manifest",
    ):
        artifact.validate_artifact(event_server_dir, verify_inputs=False)


@pytest.mark.parametrize("schema_version", [True, 1.0, "1"])
def test_manifest_schema_requires_an_exact_integer(tmp_path, schema_version):
    event_server_dir = _fresh_artifact(tmp_path)
    manifest_path = event_server_dir / "dist" / artifact.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = schema_version
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(
        artifact.ArtifactValidationError,
        match="unsupported artifact manifest schema",
    ):
        artifact.validate_artifact(event_server_dir, verify_inputs=False)


def test_node_command_environment_excludes_preloads_npm_config_and_secrets():
    environment = artifact.sanitized_node_environment(
        {
            "HOME": "/safe-home",
            "HTTPS_PROXY": "https://proxy.test",
            "NODE_OPTIONS": "--require=/tmp/hostile.cjs",
            "NODE_PATH": "/tmp/hostile-modules",
            "PATH": "/safe-bin",
            "SLACK_BOT_TOKEN": "provider-secret",
            "npm_config_node_options": "--require=/tmp/other.cjs",
            "npm_config_script_shell": "/tmp/hostile-shell",
        }
    )

    assert environment == {
        "HOME": "/safe-home",
        "HTTPS_PROXY": "https://proxy.test",
        "NPM_CONFIG_USERCONFIG": os.devnull,
        "PATH": "/safe-bin",
    }


def test_unexpected_external_import_fails_the_bundle_audit(tmp_path):
    event_server_dir = tmp_path / "event-server"
    _write_inputs(event_server_dir)
    _write_audit_dependencies(event_server_dir)
    _write_bundle_and_metafile(event_server_dir, external="left-pad")

    with pytest.raises(
        artifact.ArtifactValidationError,
        match=r"unexpected external import.*left-pad",
    ):
        artifact.generate_artifact_metadata(
            event_server_dir,
            node_version="v20.19.2",
            npm_version="10.8.2",
        )


@pytest.mark.parametrize(
    ("module", "importer"),
    [
        ("bufferutil", "node_modules/ws/lib/buffer-util.js"),
        ("utf-8-validate", "node_modules/ws/lib/validation.js"),
    ],
)
def test_ws_optional_external_imports_require_exact_audited_importers(
    tmp_path, module, importer,
):
    event_server_dir = tmp_path / "event-server"
    _write_inputs(event_server_dir)
    _write_audit_dependencies(event_server_dir)
    _write_bundle_and_metafile(
        event_server_dir,
        external=module,
        external_importer=importer,
    )

    artifact.generate_artifact_metadata(
        event_server_dir,
        node_version="v20.19.2",
        npm_version="10.8.2",
    )


@pytest.mark.parametrize("module", ["bufferutil", "utf-8-validate"])
def test_optional_external_import_from_application_code_fails_audit(
    tmp_path, module,
):
    event_server_dir = tmp_path / "event-server"
    _write_inputs(event_server_dir)
    _write_audit_dependencies(event_server_dir)
    _write_bundle_and_metafile(
        event_server_dir,
        external=module,
        external_importer="src/local.ts",
    )

    with pytest.raises(
        artifact.ArtifactValidationError,
        match=rf"unexpected importer.*src/local\.ts -> {module}",
    ):
        artifact.generate_artifact_metadata(
            event_server_dir,
            node_version="v20.19.2",
            npm_version="10.8.2",
        )


@pytest.mark.parametrize("nested_dependency", ["left-pad", "ws"])
def test_nested_node_module_instances_fail_the_root_inventory_contract(
    tmp_path, nested_dependency,
):
    event_server_dir = tmp_path / "event-server"
    _write_inputs(event_server_dir)
    _write_audit_dependencies(event_server_dir)
    _write_bundle_and_metafile(
        event_server_dir,
        extra_input=(
            "node_modules/@chat-adapter/slack/node_modules/"
            f"{nested_dependency}/index.js"
        ),
    )

    with pytest.raises(
        artifact.ArtifactValidationError,
        match=rf"nested bundled dependency.*{nested_dependency}",
    ):
        artifact.generate_artifact_metadata(
            event_server_dir,
            node_version="v20.19.2",
            npm_version="10.8.2",
        )


@pytest.mark.parametrize(
    "non_root_input",
    [
        "core/node_modules/ws/index.js",
        "vendor/node_modules/@chat-adapter/slack/index.js",
    ],
)
def test_non_root_dependency_instances_fail_the_bundle_audit(
    tmp_path, non_root_input,
):
    event_server_dir = tmp_path / "event-server"
    _write_inputs(event_server_dir)
    _write_audit_dependencies(event_server_dir)
    _write_bundle_and_metafile(
        event_server_dir,
        extra_input=non_root_input,
    )

    with pytest.raises(
        artifact.ArtifactValidationError,
        match=rf"non-root bundled dependency instance.*{non_root_input}",
    ):
        artifact.generate_artifact_metadata(
            event_server_dir,
            node_version="v20.19.2",
            npm_version="10.8.2",
        )


@pytest.mark.parametrize("mutation", ["sidecar-output", "chunk-import"])
def test_bundle_audit_rejects_unshipped_outputs(tmp_path, mutation):
    event_server_dir = tmp_path / "event-server"
    _write_inputs(event_server_dir)
    _write_audit_dependencies(event_server_dir)
    _write_bundle_and_metafile(event_server_dir)
    metafile_path = event_server_dir / "dist" / artifact.METAFILE_NAME
    metafile = json.loads(metafile_path.read_text())
    if mutation == "sidecar-output":
        metafile["outputs"]["dist/chunk.js"] = {
            "bytes": 1,
            "imports": [],
            "inputs": {},
        }
        match = r"exactly one self-contained dist/local\.js"
    else:
        metafile["outputs"]["dist/local.js"]["imports"].append(
            {"external": False, "kind": "import-statement", "path": "./chunk.js"}
        )
        match = r"not self-contained.*chunk\.js"
    metafile_path.write_text(json.dumps(metafile))

    with pytest.raises(artifact.ArtifactValidationError, match=match):
        artifact.generate_artifact_metadata(
            event_server_dir,
            node_version="v20.19.2",
            npm_version="10.8.2",
        )


def test_bundled_dependency_inventory_drift_fails_the_audit(tmp_path):
    event_server_dir = tmp_path / "event-server"
    _write_inputs(event_server_dir)
    _write_audit_dependencies(event_server_dir)
    _write_bundle_and_metafile(
        event_server_dir,
        extra_input="node_modules/left-pad/index.js",
    )

    with pytest.raises(
        artifact.ArtifactValidationError,
        match="bundled dependency inventory drifted",
    ):
        artifact.generate_artifact_metadata(
            event_server_dir,
            node_version="v20.19.2",
            npm_version="10.8.2",
        )


def test_bundled_dependency_metadata_must_be_an_object(tmp_path):
    event_server_dir = tmp_path / "event-server"
    _write_inputs(event_server_dir)
    _write_audit_dependencies(event_server_dir)
    _write_bundle_and_metafile(event_server_dir)
    package_path = event_server_dir / "node_modules" / "ws" / "package.json"
    package_path.write_text("[]\n")

    with pytest.raises(
        artifact.ArtifactValidationError,
        match=rf"bundled dependency metadata must be an object: {package_path}",
    ):
        artifact.generate_artifact_metadata(
            event_server_dir,
            node_version="v20.19.2",
            npm_version="10.8.2",
        )


def test_esbuild_metadata_must_be_an_object(tmp_path):
    event_server_dir = tmp_path / "event-server"
    _write_inputs(event_server_dir)
    _write_audit_dependencies(event_server_dir)
    _write_bundle_and_metafile(event_server_dir)
    package_path = event_server_dir / "node_modules" / "esbuild" / "package.json"
    package_path.write_text("[]\n")

    with pytest.raises(
        artifact.ArtifactValidationError,
        match=rf"installed esbuild metadata must be an object: {package_path}",
    ):
        artifact.generate_artifact_metadata(
            event_server_dir,
            node_version="v20.19.2",
            npm_version="10.8.2",
        )


def test_bundled_license_drift_fails_the_audit(tmp_path):
    event_server_dir = tmp_path / "event-server"
    _write_inputs(event_server_dir)
    _write_audit_dependencies(event_server_dir)
    _write_bundle_and_metafile(event_server_dir)
    (event_server_dir / "node_modules" / "ws" / "LICENSE").write_text("changed\n")

    with pytest.raises(
        artifact.ArtifactValidationError,
        match="license text drifted for ws",
    ):
        artifact.generate_artifact_metadata(
            event_server_dir,
            node_version="v20.19.2",
            npm_version="10.8.2",
        )
