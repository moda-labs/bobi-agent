"""Tests for auto-setup / project inspection."""

import json
from pathlib import Path

from modastack.setup import (
    detect_test_command,
    detect_package_manager,
    detect_skills,
    generate_dispatch_yaml,
    setup_project,
)


def test_detect_python_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'foo'\n")
    assert detect_test_command(tmp_path) == "pytest"


def test_detect_node_npm(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    assert detect_test_command(tmp_path) == "npm test"


def test_detect_node_bun(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "bun test"}}))
    (tmp_path / "bun.lockb").write_text("")
    assert detect_test_command(tmp_path) == "bun test"


def test_detect_node_pnpm(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert detect_test_command(tmp_path) == "pnpm test"


def test_detect_go(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/foo\n")
    assert detect_test_command(tmp_path) == "go test ./..."


def test_detect_rust(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'foo'\n")
    assert detect_test_command(tmp_path) == "cargo test"


def test_detect_makefile(tmp_path):
    (tmp_path / "Makefile").write_text("test:\n\techo running tests\n")
    assert detect_test_command(tmp_path) == "make test"


def test_detect_nothing(tmp_path):
    assert detect_test_command(tmp_path) == ""


def test_detect_package_manager_defaults(tmp_path):
    assert detect_package_manager(tmp_path) == "npm"


def test_detect_skills_basic(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    skills = detect_skills(tmp_path)
    assert "review" in skills
    assert "ship" in skills


def test_detect_skills_with_frontend(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.tsx").write_text("")
    skills = detect_skills(tmp_path)
    assert "qa" in skills


def test_detect_skills_with_deploy(tmp_path):
    (tmp_path / "fly.toml").write_text("")
    skills = detect_skills(tmp_path)
    assert "land-and-deploy" in skills


def test_generate_dispatch_yaml(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'foo'\n")
    config = generate_dispatch_yaml(tmp_path)

    assert config["verify"]["test_command"] == "pytest"
    assert config["agent"]["tool"] == "claude"
    assert "review" in config["agent"]["skills"]


def test_setup_project_writes_file(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    output = setup_project(tmp_path)

    assert output.exists()
    assert output.name == "config.yaml"
    assert output.parent.name == ".modastack"
    content = output.read_text()
    assert "npm test" in content
