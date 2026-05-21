"""Auto-generate .modastack.yaml by inspecting a repo."""

import json
import re
from pathlib import Path

import yaml


def detect_test_command(repo_path: Path) -> str:
    """Infer the test command from project files."""
    # Python
    if (repo_path / "pyproject.toml").exists():
        return "pytest"
    if (repo_path / "setup.py").exists():
        return "pytest"
    if (repo_path / "tox.ini").exists():
        return "tox"

    # JavaScript/TypeScript
    pkg_json = repo_path / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            scripts = pkg.get("scripts", {})
            if "test" in scripts:
                # Use the package manager that matches the lockfile
                runner = detect_package_manager(repo_path)
                return f"{runner} test"
        except (json.JSONDecodeError, KeyError):
            pass

    # Go
    if (repo_path / "go.mod").exists():
        return "go test ./..."

    # Rust
    if (repo_path / "Cargo.toml").exists():
        return "cargo test"

    # Ruby
    if (repo_path / "Gemfile").exists():
        return "bundle exec rspec"

    # Makefile
    makefile = repo_path / "Makefile"
    if makefile.exists():
        content = makefile.read_text()
        if "test:" in content:
            return "make test"

    return ""


def detect_package_manager(repo_path: Path) -> str:
    """Detect JS package manager from lockfile."""
    if (repo_path / "bun.lockb").exists() or (repo_path / "bun.lock").exists():
        return "bun"
    if (repo_path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (repo_path / "yarn.lock").exists():
        return "yarn"
    return "npm"


def detect_linear_project(repo_path: Path) -> str:
    """Try to infer Linear project key from repo name or branch conventions."""
    # Check for existing Linear references in CLAUDE.md or README
    for candidate in ["CLAUDE.md", "README.md", ".github/ISSUE_TEMPLATE"]:
        f = repo_path / candidate
        if f.exists() and f.is_file():
            content = f.read_text()
            # Look for Linear project key patterns like "PROJ-123"
            match = re.search(r"\b([A-Z]{2,10})-\d+", content)
            if match:
                return match.group(1)

    # Fall back to repo directory name uppercased as a guess
    return repo_path.name.upper().replace("-", "")[:6]


def detect_skills(repo_path: Path) -> list[str]:
    """Suggest gstack skills based on repo contents."""
    skills = ["review", "ship"]

    # If there's a web frontend, suggest QA
    if any((repo_path / f).exists() for f in [
        "index.html", "src/App.tsx", "src/App.vue",
        "src/App.svelte", "app/page.tsx", "templates/",
    ]):
        skills.append("qa")

    # If there's deployment config, suggest land-and-deploy
    if any((repo_path / f).exists() for f in [
        "fly.toml", "render.yaml", "vercel.json",
        "netlify.toml", "Procfile", "Dockerfile",
    ]):
        skills.append("land-and-deploy")

    return skills


def generate_dispatch_yaml(repo_path: Path) -> dict:
    """Generate a .modastack.yaml config for the repo."""
    test_cmd = detect_test_command(repo_path)
    linear_project = detect_linear_project(repo_path)
    skills = detect_skills(repo_path)

    return {
        "linear": {
            "project": linear_project,
            "trigger_labels": ["agent"],
            "skip_labels": ["blocked", "human-only"],
        },
        "complexity": {
            "trivial": "label:typo OR label:docs OR label:config",
            "medium": "default",
            "heavy": "label:feature OR label:refactor OR estimate>3",
        },
        "agent": {
            "tool": "claude",
            "skills": skills,
            "max_parallel": 2,
        },
        "verify": {
            "test_command": test_cmd,
            "review_required": True,
            "auto_merge": False,
        },
    }


def setup_repo(repo_path: Path) -> Path:
    """Generate .modastack.yaml and return the path."""
    config = generate_dispatch_yaml(repo_path)
    output_path = repo_path / ".modastack.yaml"
    output_path.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))
    return output_path
